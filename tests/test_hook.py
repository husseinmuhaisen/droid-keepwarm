from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from keepwarm.config import Config
from keepwarm.hook import HookReply, handle_event, run_hook
from keepwarm.state import StateStore

NOW = 100_000.0


@pytest.fixture()
def log(tmp_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"keepwarm.test-hook-{tmp_path.name}")
    logger.addHandler(logging.NullHandler())
    return logger


def write_settings(
    cfg: Config,
    cwd: str,
    session_id: str,
    *,
    context: int,
    cache_creation: int,
    model: str = "claude-fable-5.2",
) -> None:
    from keepwarm.droid import sessions_dir_for

    session_dir = sessions_dir_for(cwd, cfg.factory_home)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{session_id}.settings.json").write_text(
        json.dumps(
            {
                "model": model,
                "reasoningEffort": "high",
                "tokenUsage": {
                    "inputTokens": context,
                    "outputTokens": 0,
                    "cacheCreationTokens": cache_creation,
                    "cacheReadTokens": 0,
                },
            }
        ),
        encoding="utf-8",
    )


def payload(event: str, session_id: str = "hook-1", **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "hook_event_name": event,
        "session_id": session_id,
        "cwd": "/work/proj",
        "transcript_path": "/tmp/transcript.jsonl",
    }
    base.update(extra)
    return base


def test_session_start_creates_state_with_model(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=80_000, cache_creation=0)
    reply = handle_event(payload("SessionStart"), now=NOW, cfg=cfg, store=store, log=log)
    assert reply.exit_code == 0
    state = store.load("hook-1")
    assert state is not None
    assert state.model == "claude-fable-5.2"
    assert state.last_activity == NOW
    assert state.needs_refork


def test_stop_marks_model_activity_and_refork(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=80_000, cache_creation=0)
    with store.edit("hook-1") as state:
        state.fork_id = "fork-9"
        state.needs_refork = False
    handle_event(payload("Stop"), now=NOW, cfg=cfg, store=store, log=log)
    state = store.load("hook-1")
    assert state is not None
    assert state.last_model_activity == NOW
    assert state.needs_refork


def test_stop_auto_arms_after_cold_write(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=80_000, cache_creation=100_000)
    handle_event(payload("Stop"), now=NOW, cfg=cfg, store=store, log=log)
    state = store.load("hook-1")
    assert state is not None
    assert state.armed_until == pytest.approx(NOW + cfg.auto_arm_window_seconds)


def test_stop_small_write_does_not_auto_arm(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=80_000, cache_creation=10_000)
    handle_event(payload("Stop"), now=NOW, cfg=cfg, store=store, log=log)
    state = store.load("hook-1")
    assert state is not None
    assert state.armed_until is None


def test_guard_blocks_cold_prompt_then_allows_resend(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=100_000, cache_creation=100_000)
    with store.edit("hook-1") as state:
        state.last_model_activity = NOW - 3600
    first = handle_event(
        payload("UserPromptSubmit", prompt="summarize"), now=NOW, cfg=cfg, store=store, log=log
    )
    assert first.exit_code == 2
    assert first.stderr is not None
    # context = input (100k) + cache creation (100k): the estimate is an upper bound
    assert "re-writes up to 200,000 tokens" in first.stderr
    second = handle_event(
        payload("UserPromptSubmit", prompt="summarize"), now=NOW + 30, cfg=cfg, store=store, log=log
    )
    assert second.exit_code == 0
    assert second.stderr is None


def test_guard_allows_fresh_prompt(cfg: Config, store: StateStore, log: logging.Logger) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=100_000, cache_creation=0)
    with store.edit("hook-1") as state:
        state.last_model_activity = NOW - 60
    reply = handle_event(
        payload("UserPromptSubmit", prompt="hi"), now=NOW, cfg=cfg, store=store, log=log
    )
    assert reply.exit_code == 0


def test_precompact_flags_refork(cfg: Config, store: StateStore, log: logging.Logger) -> None:
    with store.edit("hook-1") as state:
        state.fork_id = "fork-9"
        state.needs_refork = False
    handle_event(payload("PreCompact"), now=NOW, cfg=cfg, store=store, log=log)
    state = store.load("hook-1")
    assert state is not None
    assert state.needs_refork


def test_session_end_disarms(cfg: Config, store: StateStore, log: logging.Logger) -> None:
    with store.edit("hook-1") as state:
        state.arm(NOW - 100, 3600)
    handle_event(payload("SessionEnd"), now=NOW, cfg=cfg, store=store, log=log)
    state = store.load("hook-1")
    assert state is not None
    assert state.armed_until is None
    assert state.stopped_reason == "session ended"


def test_unknown_event_is_noop(cfg: Config, store: StateStore, log: logging.Logger) -> None:
    reply = handle_event(payload("Notification"), now=NOW, cfg=cfg, store=store, log=log)
    assert reply == HookReply()


def test_run_hook_swallows_garbage(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keepwarm.hook.load_config", lambda: cfg)
    assert run_hook(io.StringIO("{not json"), io.StringIO(), io.StringIO()) == 0
    assert run_hook(io.StringIO(""), io.StringIO(), io.StringIO()) == 0
    assert run_hook(io.StringIO("[1,2]"), io.StringIO(), io.StringIO()) == 0


def test_run_hook_end_to_end_block(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keepwarm.hook.load_config", lambda: cfg)
    write_settings(cfg, "/work/proj", "hook-1", context=100_000, cache_creation=100_000)
    store = StateStore(cfg.state_dir)
    with store.edit("hook-1") as state:
        state.last_model_activity = 1.0  # ancient -> cold
    stdin = io.StringIO(json.dumps(payload("UserPromptSubmit", prompt="go")))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run_hook(stdin, stdout, stderr, now=NOW)
    assert code == 2
    assert "keepwarm:" in stderr.getvalue()
    assert stdout.getvalue() == ""


def test_run_hook_config_error_is_silent_success(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from keepwarm.config import ConfigError

    def boom() -> Config:
        raise ConfigError("bad config")

    monkeypatch.setattr("keepwarm.hook.load_config", boom)
    stderr = io.StringIO()
    assert run_hook(io.StringIO("{}"), io.StringIO(), stderr) == 0
    assert "config error" in stderr.getvalue()


def test_run_hook_never_crashes_droid(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keepwarm.hook.load_config", lambda: cfg)

    def boom(*_a: object, **_k: object) -> HookReply:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("keepwarm.hook.handle_event", boom)
    stdin = io.StringIO(json.dumps(payload("Stop")))
    assert run_hook(stdin, io.StringIO(), io.StringIO()) == 0


def test_autostart_spawns_daemon_when_armed(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import dataclasses

    cfg2 = dataclasses.replace(cfg, autostart_daemon=True)
    monkeypatch.setattr("keepwarm.hook.load_config", lambda: cfg2)
    spawned: list[Config] = []
    monkeypatch.setattr(
        "keepwarm.hook.daemon_mod.spawn_detached", lambda c: spawned.append(c) or True
    )
    store = StateStore(cfg2.state_dir)
    with store.edit("hook-1") as state:
        state.arm(1.0, 3600)
    stdin = io.StringIO(json.dumps(payload("SessionStart")))
    assert run_hook(stdin, io.StringIO(), io.StringIO(), now=NOW) == 0
    assert spawned == [cfg2]


def test_session_start_rearms_in_always_mode(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    write_settings(cfg, "/work/proj", "hook-1", context=1000, cache_creation=0)
    with store.edit("hook-1") as state:
        state.always = True
        state.always_window_seconds = 7200
        state.stopped_reason = "budget exhausted"
    handle_event(payload("SessionStart"), now=NOW, cfg=cfg, store=store, log=log)
    state = store.load("hook-1")
    assert state is not None
    assert state.armed_until == pytest.approx(NOW + 7200)
    assert state.stopped_reason is None  # a new turn revives a stopped loop


def test_missing_fields_payload_is_noop(
    cfg: Config, store: StateStore, log: logging.Logger
) -> None:
    assert (
        handle_event({"hook_event_name": "Stop"}, now=NOW, cfg=cfg, store=store, log=log)
        == HookReply()
    )
    assert (
        handle_event({"session_id": "x-1"}, now=NOW, cfg=cfg, store=store, log=log) == HookReply()
    )
