"""Ping execution: (re)fork a session and send minimal warming turns."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from keepwarm import droid
from keepwarm.config import Config
from keepwarm.state import SessionState


@dataclass(frozen=True)
class PingOutcome:
    kind: Literal["ping", "refork"]
    ok: bool
    usage: droid.Usage | None
    new_fork_id: str | None = None
    error: str | None = None


def _model_for(state: SessionState, cfg: Config) -> str | None:
    # The prompt cache is per-model: default to the session's own model.
    return cfg.ping_model or state.model


def _effort_for(state: SessionState, cfg: Config) -> str | None:
    return cfg.ping_reasoning_effort or state.reasoning_effort


def refork(state: SessionState, cfg: Config, droid_bin: str, log: logging.Logger) -> PingOutcome:
    """Create a fresh fork of the live session and run one turn inside it.

    The fork's request shares the live session's transcript prefix, so the
    cache entry it creates is the same entry the live session will read on
    its next turn. The live transcript is never touched.
    """
    result = droid.fork_session(
        droid_bin,
        state.session_id,
        cwd=state.cwd,
        prompt=cfg.ping_prompt,
        model=_model_for(state, cfg),
        reasoning_effort=_effort_for(state, cfg),
        timeout_seconds=cfg.ping_timeout_seconds,
        max_output_bytes=cfg.ping_max_output_bytes,
    )
    if not result.ok:
        log.warning("refork of %s failed: %s", state.session_id, result.error)
        return PingOutcome("refork", False, result.usage, None, result.error)
    if not result.session_id:
        log.warning("refork of %s returned no session id", state.session_id)
        return PingOutcome("refork", False, result.usage, None, "fork returned no session id")
    log.info(
        "reforked %s -> %s (read=%d wrote=%d credits=%d)",
        state.session_id,
        result.session_id,
        result.usage.cache_read_input_tokens if result.usage else 0,
        result.usage.cache_creation_input_tokens if result.usage else 0,
        result.usage.factory_credits if result.usage else 0,
    )
    return PingOutcome("refork", True, result.usage, result.session_id)


def ping(state: SessionState, cfg: Config, droid_bin: str, log: logging.Logger) -> PingOutcome:
    """Send one minimal turn to the existing fork."""
    if state.fork_id is None:
        return PingOutcome("ping", False, None, None, "no fork to ping")
    result = droid.ping_session(
        droid_bin,
        state.fork_id,
        cwd=state.cwd,
        prompt=cfg.ping_prompt,
        model=_model_for(state, cfg),
        reasoning_effort=_effort_for(state, cfg),
        timeout_seconds=cfg.ping_timeout_seconds,
        max_output_bytes=cfg.ping_max_output_bytes,
    )
    if not result.ok:
        log.warning(
            "ping of %s (fork %s) failed: %s", state.session_id, state.fork_id, result.error
        )
        return PingOutcome("ping", False, result.usage, None, result.error)
    if result.usage:
        log.info(
            "pinged %s via fork %s (read=%d wrote=%d credits=%d)",
            state.session_id,
            state.fork_id,
            result.usage.cache_read_input_tokens,
            result.usage.cache_creation_input_tokens,
            result.usage.factory_credits,
        )
    return PingOutcome("ping", True, result.usage)
