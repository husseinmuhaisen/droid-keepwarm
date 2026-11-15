from __future__ import annotations

import logging
from pathlib import Path

import pytest

from keepwarm import pinger
from keepwarm.config import Config
from keepwarm.droid import ExecResult
from keepwarm.state import SessionState


@pytest.fixture()
def log(tmp_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"keepwarm.test-pinger-{tmp_path.name}")
    logger.addHandler(logging.NullHandler())
    return logger


def armed(fork_id: str | None = "fork-1") -> SessionState:
    state = SessionState(session_id="pinger-1")
    state.cwd = "/work/proj"
    state.model = "claude-fable-5.2"
    state.reasoning_effort = "high"
    state.fork_id = fork_id
    return state


def test_ping_without_fork(cfg: Config, log: logging.Logger) -> None:
    outcome = pinger.ping(armed(fork_id=None), cfg, "/bin/true", log)
    assert not outcome.ok
    assert outcome.error == "no fork to ping"


def test_refork_failure(
    cfg: Config,
    fake_droid: Path,
    log: logging.Logger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "fake-droid-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_DROID_HOME", str(home))
    (home / "fail_next").touch()
    outcome = pinger.refork(armed(), cfg, str(fake_droid), log)
    assert not outcome.ok
    assert outcome.error == "simulated 529 overloaded"
    assert outcome.new_fork_id is None


def test_refork_without_session_id(
    cfg: Config, log: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pinger.droid,
        "fork_session",
        lambda *a, **k: ExecResult(
            ok=True, session_id=None, result_text="ok", usage=None, duration_ms=1, error=None
        ),
    )
    outcome = pinger.refork(armed(), cfg, "/bin/true", log)
    assert not outcome.ok
    assert outcome.error == "fork returned no session id"


def test_ping_uses_config_model_override(
    cfg: Config,
    fake_droid: Path,
    log: logging.Logger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "fake-droid-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_DROID_HOME", str(home))
    override = Config(**{**cfg.__dict__, "ping_model": "claude-opus-4.8"})
    outcome = pinger.ping(armed(), override, str(fake_droid), log)
    assert outcome.ok
    argv_lines = (home / "argv-log.jsonl").read_text("utf-8").splitlines()
    assert "claude-opus-4.8" in argv_lines[0]
    assert "claude-fable-5.2" not in argv_lines[0]


def test_ping_success_flow(
    cfg: Config,
    fake_droid: Path,
    log: logging.Logger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "fake-droid-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_DROID_HOME", str(home))
    outcome = pinger.ping(armed(), cfg, str(fake_droid), log)
    assert outcome.ok
    assert outcome.usage is not None
    assert outcome.usage.factory_credits == 1000
