"""Droid lifecycle hook handler.

This code runs inside Droid's event path, so the rules are strict: be fast
(no network, one small local file read at most), never crash, and never
block a session unless the guard deliberately refuses a cold send. Any
unexpected failure degrades to a silent no-op with a log line.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, TextIO

from keepwarm import daemon as daemon_mod
from keepwarm import droid as droid_mod
from keepwarm.config import Config, ConfigError, load_config
from keepwarm.guard import evaluate_guard, prompt_hash
from keepwarm.log import get_logger
from keepwarm.state import SessionState, StateError, StateStore


@dataclass(frozen=True)
class HookReply:
    exit_code: int = 0
    stdout_json: dict[str, Any] | None = None
    stderr: str | None = None


def _refresh_info(state: SessionState, cfg: Config) -> droid_mod.SessionInfo | None:
    info = droid_mod.read_session_info(state.cwd, state.session_id, cfg.factory_home)
    if info is not None:
        state.model = info.model or state.model
        state.reasoning_effort = info.reasoning_effort or state.reasoning_effort
        state.last_context_tokens = info.context_tokens
    return info


def _apply_basics(state: SessionState, payload: dict[str, Any], now: float) -> None:
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path")
    if isinstance(cwd, str) and cwd:
        state.cwd = cwd
    if isinstance(transcript, str) and transcript:
        state.transcript_path = transcript
    state.last_activity = now


def _on_session_start(state: SessionState, now: float, cfg: Config) -> HookReply:
    _refresh_info(state, cfg)
    # The transcript may have changed while we were away (resume, clear).
    state.needs_refork = True
    if state.always:
        state.arm(now, state.always_window_seconds)
    return HookReply()


def _on_prompt(state: SessionState, payload: dict[str, Any], now: float, cfg: Config) -> HookReply:
    info = _refresh_info(state, cfg)
    raw_prompt = payload.get("prompt")
    prompt = raw_prompt if isinstance(raw_prompt, str) else ""
    decision = evaluate_guard(state, info, now=now, prompt=prompt, config=cfg)
    if decision.action == "block":
        # Refuse once: remember the hash so an identical resend goes through.
        state.guard_consumed_hash = prompt_hash(prompt)
        state.guard_consumed_at = now
        return HookReply(exit_code=2, stderr=decision.message)
    if decision.action == "warn":
        return HookReply(stdout_json={"systemMessage": decision.message})
    return HookReply()


def _on_stop(state: SessionState, now: float, cfg: Config) -> HookReply:
    info = _refresh_info(state, cfg)
    state.last_model_activity = now
    if state.fork_id is not None:
        # The live transcript just grew past the fork's prefix.
        state.needs_refork = True
    if info is not None:
        delta = info.cache_creation_tokens - state.last_cache_creation_seen
        state.last_cache_creation_seen = info.cache_creation_tokens
        if cfg.auto_arm_on_cold_write and delta >= cfg.auto_arm_min_write_tokens:
            # We just paid a cold write; hold the cache warm from here.
            state.arm(now, cfg.auto_arm_window_seconds)
    if state.always:
        state.arm(now, state.always_window_seconds)
    return HookReply()


def handle_event(
    payload: dict[str, Any],
    *,
    now: float,
    cfg: Config,
    store: StateStore,
    log: logging.Logger,
) -> HookReply:
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(event, str) or not isinstance(session_id, str):
        return HookReply()
    try:
        with store.edit(session_id) as state:
            _apply_basics(state, payload, now)
            if event == "SessionStart":
                return _on_session_start(state, now, cfg)
            if event == "UserPromptSubmit":
                return _on_prompt(state, payload, now, cfg)
            if event == "Stop":
                return _on_stop(state, now, cfg)
            if event == "PreCompact":
                state.needs_refork = True
                return HookReply()
            if event == "SessionEnd":
                state.disarm("session ended")
                return HookReply()
            return HookReply()
    except StateError as exc:
        log.warning("hook %s for session %s: %s", event, session_id, exc)
        return HookReply()


def _read_stdin_capped(stdin: TextIO, max_bytes: int) -> str:
    return stdin.read(max_bytes + 1)[:max_bytes]


def run_hook(stdin: TextIO, stdout: TextIO, stderr: TextIO, *, now: float | None = None) -> int:
    effective_now = time.time() if now is None else now
    try:
        cfg = load_config()
    except ConfigError as exc:
        # A broken config must not break the user's session. Exit clean.
        print(f"droid-keepwarm: config error: {exc}", file=stderr)
        return 0
    log = get_logger("hook", cfg.state_dir)
    try:
        raw = _read_stdin_capped(stdin, cfg.hook_stdin_max_bytes)
        payload: dict[str, Any] = {}
        if raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
    except json.JSONDecodeError:
        log.warning("hook received non-JSON stdin; ignoring")
        return 0

    try:
        reply = handle_event(
            payload, now=effective_now, cfg=cfg, store=StateStore(cfg.state_dir), log=log
        )
    except Exception:
        # A hook must never crash Droid.
        log.exception("hook crashed")
        return 0

    if cfg.autostart_daemon:
        try:
            store = StateStore(cfg.state_dir)
            if any(s.armed for s in store.load_all()):
                daemon_mod.spawn_detached(cfg)
        except (StateError, OSError) as exc:
            log.warning("daemon autostart check failed: %s", exc)

    if reply.stdout_json is not None:
        stdout.write(json.dumps(reply.stdout_json) + "\n")
    if reply.stderr is not None:
        print(reply.stderr, file=stderr)
    return reply.exit_code


def main() -> int:
    return run_hook(sys.stdin, sys.stdout, sys.stderr)
