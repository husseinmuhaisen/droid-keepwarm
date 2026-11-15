from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from keepwarm import cli
from keepwarm.cli import CliError, format_duration, parse_duration
from keepwarm.config import Config, load_config
from keepwarm.state import StateStore


@pytest.fixture()
def patched(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setattr("keepwarm.cli.load_config", lambda: cfg)
    return cfg


def test_parse_duration() -> None:
    assert parse_duration("90m") == 5400
    assert parse_duration("2h30m") == 9000
    assert parse_duration("6h") == 21600
    assert parse_duration(" 45m ") == 2700


def test_parse_duration_rejects_garbage() -> None:
    for bad in ("", "abc", "10", "h", "1h30", "1.5h", "-5m"):
        with pytest.raises(CliError):
            parse_duration(bad)


def test_format_duration() -> None:
    assert format_duration(45) == "45s"
    assert format_duration(125) == "2m05s"
    assert format_duration(7320) == "2h02m"


def seed_session(cfg: Config, session_id: str = "cli-1") -> None:
    store = StateStore(cfg.state_dir)
    with store.edit(session_id) as state:
        state.cwd = "/work/proj"
        state.last_activity = time.time()
        state.last_model_activity = time.time()


def test_arm_and_off(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    seed_session(patched)
    assert cli.main(["arm", "90m", "--session", "cli-1"]) == 0
    state = StateStore(patched.state_dir).load("cli-1")
    assert state is not None
    assert state.armed_until == pytest.approx(time.time() + 5400, abs=5)
    assert not state.always

    assert cli.main(["off", "--session", "cli-1"]) == 0
    state = StateStore(patched.state_dir).load("cli-1")
    assert state is not None
    assert state.armed_until is None
    assert not state.always


def test_arm_always_with_window(patched: Config) -> None:
    seed_session(patched)
    assert cli.main(["arm", "always", "24h", "--session", "cli-1"]) == 0
    state = StateStore(patched.state_dir).load("cli-1")
    assert state is not None
    assert state.always
    assert state.always_window_seconds == 86400
    assert state.armed_until == pytest.approx(time.time() + 86400, abs=5)


def test_arm_always_rejects_silly_windows(patched: Config) -> None:
    seed_session(patched)
    assert cli.main(["arm", "always", "30m", "--session", "cli-1"]) == 2


def test_arm_window_capped_by_config(patched: Config) -> None:
    seed_session(patched)
    assert cli.main(["arm", "48h", "--session", "cli-1"]) == 2


def test_arm_unknown_session(patched: Config) -> None:
    assert cli.main(["arm", "90m", "--session", "nope-0"]) == 2


def test_arm_uses_most_recent_session(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    seed_session(patched, "cli-old")
    seed_session(patched, "cli-new")
    store = StateStore(patched.state_dir)
    with store.edit("cli-old") as state:
        state.last_activity = time.time() - 100
    assert cli.main(["arm"]) == 0  # default window
    old = store.load("cli-old")
    new = store.load("cli-new")
    assert old is not None and new is not None
    assert old.armed_until is None
    assert new.armed_until is not None


def test_status_output(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    seed_session(patched)
    assert cli.main(["arm", "90m", "--session", "cli-1"]) == 0
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "armed" in out
    assert "guard refuse" in out


def test_receipts_empty(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["receipts"]) == 0
    assert "no receipts yet" in capsys.readouterr().out


def test_receipts_prints_rows(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    receipts = patched.state_dir / "receipts.jsonl"
    patched.state_dir.mkdir(parents=True, exist_ok=True)
    receipts.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "session_id": "cli-1",
                "fork_id": "f",
                "kind": "ping",
                "ok": True,
                "error": None,
                "credits_used_total": 1000,
                "ping_count": 1,
                "usage": {
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 0,
                    "factory_credits": 1000,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert cli.main(["receipts", "--last", "5"]) == 0
    out = capsys.readouterr().out
    assert "ping" in out
    assert "9,000" in out


def test_guard_command_updates_config(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["guard", "refuse", "5"]) == 0
    cfg = load_config(patched.state_dir)
    assert cfg.guard_mode == "refuse"
    assert cfg.guard_min_usd == 5.0
    assert "guard mode: refuse" in capsys.readouterr().out


def test_guard_rejects_negative_floor(patched: Config) -> None:
    assert cli.main(["guard", "refuse", "-1"]) == 2


def test_status_with_no_sessions(patched: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 0
    assert "no known sessions" in capsys.readouterr().out


def test_install_and_uninstall(
    patched: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("keepwarm.cli._install_bin_path", lambda: "/fake/bin/droid-keepwarm")
    assert cli.main(["install"]) == 0
    assert "hooks registered" in capsys.readouterr().out
    assert cli.main(["uninstall"]) == 0
    assert "hooks removed" in capsys.readouterr().out


def test_install_errors_when_binary_unfindable(
    patched: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("keepwarm.cli.shutil.which", lambda _name: None)
    assert cli.main(["install"]) == 2


def test_doctor_healthy(
    patched: Config,
    fake_droid: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    cfg2 = dataclasses.replace(patched, droid_bin=str(fake_droid))
    monkeypatch.setattr("keepwarm.cli.load_config", lambda *_a, **_k: cfg2)
    monkeypatch.setattr("keepwarm.cli._install_bin_path", lambda: "/fake/bin/droid-keepwarm")
    assert cli.main(["install"]) == 0
    capsys.readouterr()
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "FAIL" not in out


def test_doctor_reports_missing_hooks(
    patched: Config,
    fake_droid: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    cfg2 = dataclasses.replace(patched, droid_bin=str(fake_droid))
    monkeypatch.setattr("keepwarm.cli.load_config", lambda *_a, **_k: cfg2)
    assert cli.main(["doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_daemon_dispatch(patched: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    class FakeDaemon:
        def __init__(self, cfg: Config) -> None:
            pass

        def run(self, *, once: bool = False) -> int:
            calls.append(once)
            return 0

    monkeypatch.setattr("keepwarm.cli.daemon_mod.Daemon", FakeDaemon)
    assert cli.main(["daemon", "--once"]) == 0
    assert calls == [True]


def test_hook_dispatch(patched: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keepwarm.cli.run_hook", lambda *_a, **_k: 7)
    assert cli.main(["hook"]) == 7


def test_status_broken_pipe_exits_cleanly(patched: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_session(patched)

    class ClosedPipe:
        def write(self, *_a: object, **_k: object) -> int:
            raise BrokenPipeError

        def fileno(self) -> int:
            import io

            raise io.UnsupportedOperation("not a real fd")

    monkeypatch.setattr("keepwarm.cli.sys.stdout", ClosedPipe())
    assert cli.main(["status"]) == 0


def test_arm_starts_daemon_when_autostart(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import dataclasses

    cfg2 = dataclasses.replace(cfg, autostart_daemon=True)
    monkeypatch.setattr("keepwarm.cli.load_config", lambda: cfg2)
    spawned: list[bool] = []
    monkeypatch.setattr(
        "keepwarm.cli.daemon_mod.spawn_detached", lambda _cfg: spawned.append(True) or True
    )
    seed_session(cfg2)
    assert cli.main(["arm", "90m", "--session", "cli-1"]) == 0
    assert spawned == [True]
    assert "keepwarm daemon started" in capsys.readouterr().out
