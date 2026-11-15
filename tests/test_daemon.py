from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from keepwarm.config import Config
from keepwarm.daemon import Daemon
from keepwarm.state import StateStore


def make_cfg(state_dir: Path, tmp_path: Path, fake_droid: Path, **overrides: object) -> Config:
    base: dict[str, object] = {
        "state_dir": state_dir,
        "factory_home": tmp_path / "factory",
        "ttl_seconds": 300,
        "margin_seconds": 45,
        "backoff_seconds": (60, 120, 240),
        "autostart_daemon": False,
        "droid_bin": str(fake_droid),
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def arm_due(store: StateStore, session_id: str, now: float) -> None:
    with store.edit(session_id) as state:
        state.cwd = "/work/proj"
        state.arm(now - 400, 3600)
        state.last_activity = now - 400
        state.last_model_activity = now - 400  # past due: 400 > 300 - 45
        state.needs_refork = True
        state.fork_id = None


def read_receipts(cfg: Config) -> list[dict[str, object]]:
    path = cfg.state_dir / "receipts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_refork_then_ping_success(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DROID_HOME", str(tmp_path / "fake-droid-home"))
    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    store = StateStore(cfg.state_dir)
    now = time.time()
    arm_due(store, "daemon-1", now)

    daemon = Daemon(cfg)
    decisions = daemon.scan_once(now)
    assert decisions[0][1].kind == "refork"
    state = store.load("daemon-1")
    assert state is not None
    assert state.fork_id is not None
    assert not state.needs_refork
    assert state.ping_count == 1
    assert state.pings_this_fork == 1
    assert state.credits_used == 1000
    assert state.last_ping_at == now

    # Second scan while fresh: nothing to do.
    decisions = daemon.scan_once(now + 10)
    assert decisions[0][1].kind == "none"

    # Once due again: a real ping through the fork.
    later = now + 300
    decisions = daemon.scan_once(later)
    assert decisions[0][1].kind == "ping"
    state = store.load("daemon-1")
    assert state is not None
    assert state.ping_count == 2
    assert state.pings_this_fork == 2
    assert state.credits_used == 2000
    assert state.stopped_reason is None  # fake reports healthy cache reads

    receipts = read_receipts(cfg)
    assert [r["kind"] for r in receipts] == ["refork", "ping"]
    assert all(r["ok"] for r in receipts)


def test_stop_rule_triggers_on_zero_reads(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "fake-droid-home"
    monkeypatch.setenv("FAKE_DROID_HOME", str(home))
    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    store = StateStore(cfg.state_dir)
    now = time.time()
    arm_due(store, "daemon-2", now)
    daemon = Daemon(cfg)
    daemon.scan_once(now)  # refork, first ping creates cache
    # Now the fork stops reading from cache: reads 0, some writes.
    home.mkdir(exist_ok=True)
    (home / "usage.json").write_text(
        json.dumps({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 5000}),
        encoding="utf-8",
    )
    daemon.scan_once(now + 300)
    state = store.load("daemon-2")
    assert state is not None
    assert state.stopped_reason is not None
    assert "missing the cache" in state.stopped_reason
    # Stopped: no further pings.
    decisions = daemon.scan_once(now + 600)
    assert decisions[0][1].kind == "none"


def test_failure_backoff_and_give_up(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "fake-droid-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_DROID_HOME", str(home))
    cfg = make_cfg(state_dir, tmp_path, fake_droid, max_ping_failures=3)
    store = StateStore(cfg.state_dir)
    now = time.time()
    arm_due(store, "daemon-3", now)
    daemon = Daemon(cfg)

    (home / "fail_next").touch()
    daemon.scan_once(now)
    state = store.load("daemon-3")
    assert state is not None
    assert state.ping_failures == 1
    assert state.next_ping_after == pytest.approx(now + 60)
    assert state.fork_id is None

    elapsed = 61.0
    for i, backoff in enumerate((120, 240), start=2):
        (home / "fail_next").touch()
        daemon.scan_once(now + elapsed)
        state = store.load("daemon-3")
        assert state is not None
        assert state.ping_failures == i
        assert state.next_ping_after == pytest.approx(now + elapsed + backoff)
        elapsed += backoff + 1
    assert state.stopped_reason is not None
    assert "giving up" in state.stopped_reason


def test_window_expiry_disarms(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DROID_HOME", str(tmp_path / "fake-droid-home"))
    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    store = StateStore(cfg.state_dir)
    now = time.time()
    with store.edit("daemon-4") as state:
        state.arm(now - 10, 5)  # expired 5s ago
        state.last_model_activity = now - 400
    daemon = Daemon(cfg)
    decisions = daemon.scan_once(now)
    assert decisions[0][1].kind == "disarm"
    state = store.load("daemon-4")
    assert state is not None
    assert state.armed_until is None
    assert state.stopped_reason == "warming window expired"


def test_budget_disarm(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DROID_HOME", str(tmp_path / "fake-droid-home"))
    cfg = make_cfg(state_dir, tmp_path, fake_droid, max_credits_per_window=1500)
    store = StateStore(cfg.state_dir)
    now = time.time()
    arm_due(store, "daemon-5", now)
    daemon = Daemon(cfg)
    daemon.scan_once(now)  # refork: 1000 credits
    daemon.scan_once(now + 300)  # ping: 2000 credits total
    daemon.scan_once(now + 600)  # budget exceeded -> disarm
    state = store.load("daemon-5")
    assert state is not None
    assert state.armed_until is None
    assert state.stopped_reason == "budget exhausted"


def test_run_once(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DROID_HOME", str(tmp_path / "fake-droid-home"))
    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    store = StateStore(cfg.state_dir)
    arm_due(store, "daemon-6", time.time())
    assert Daemon(cfg).run(once=True) == 0
    state = store.load("daemon-6")
    assert state is not None
    assert state.ping_count == 1


def test_daemon_running_detection(state_dir: Path, tmp_path: Path, fake_droid: Path) -> None:
    import fcntl
    import os

    from keepwarm.daemon import daemon_running

    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    assert daemon_running(cfg.state_dir) is False
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.state_dir / "daemon.pid", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert daemon_running(cfg.state_dir) is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert daemon_running(cfg.state_dir) is False


def test_spawn_detached(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from keepwarm import daemon as daemon_mod

    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    monkeypatch.setattr(daemon_mod, "daemon_running", lambda _d: False)
    spawned: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            spawned.append(argv)

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", FakePopen)
    assert daemon_mod.spawn_detached(cfg) is True
    assert (spawned and spawned[0][-2:] == ["-m", "keepwarm"]) or spawned[0][-1] == "daemon"

    monkeypatch.setattr(daemon_mod, "daemon_running", lambda _d: True)
    spawned.clear()
    assert daemon_mod.spawn_detached(cfg) is False
    assert spawned == []


def test_run_loop_stops_cleanly(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from keepwarm.daemon import daemon_running

    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    daemon = Daemon(cfg)
    scans: list[float] = []

    def fake_scan(now: float) -> list[tuple[str, object]]:
        scans.append(now)
        daemon.request_stop(0, None)
        return []

    monkeypatch.setattr(daemon, "scan_once", fake_scan)
    assert daemon.run() == 0
    assert len(scans) == 1
    assert daemon_running(cfg.state_dir) is False  # lock released on exit


def test_run_when_already_running(
    state_dir: Path, tmp_path: Path, fake_droid: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl
    import os

    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.state_dir / "daemon.pid", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert Daemon(cfg).run() == 0
        assert "already running" in capsys.readouterr().err
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_act_marks_state_when_droid_missing(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    cfg = make_cfg(state_dir, tmp_path, fake_droid, droid_bin=None)
    store = StateStore(cfg.state_dir)
    now = time.time()
    arm_due(store, "daemon-7", now)
    daemon = Daemon(cfg)
    monkeypatch.setattr(daemon, "_resolve_droid", _raise_droid_error)
    daemon.scan_once(now)
    state = store.load("daemon-7")
    assert state is not None
    assert state.stopped_reason == "boom"
    receipts = read_receipts(cfg)
    assert receipts and receipts[0]["error"] == "boom"


def _raise_droid_error() -> str:
    from keepwarm.droid import DroidError

    raise DroidError("boom")


def test_receipt_write_failure_is_logged_not_raised(
    state_dir: Path, tmp_path: Path, fake_droid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DROID_HOME", str(tmp_path / "fake-droid-home"))
    cfg = make_cfg(state_dir, tmp_path, fake_droid)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "receipts.jsonl").mkdir()  # a directory: appends fail
    store = StateStore(cfg.state_dir)
    now = time.time()
    arm_due(store, "daemon-8", now)
    Daemon(cfg).scan_once(now)  # must not raise
    state = store.load("daemon-8")
    assert state is not None
    assert state.ping_count == 1
