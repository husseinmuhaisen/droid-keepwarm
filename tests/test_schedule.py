from __future__ import annotations

from keepwarm.config import Config
from keepwarm.daemon import decide
from keepwarm.state import SessionState

NOW = 10_000.0


def armed_state(**overrides: object) -> SessionState:
    state = SessionState(session_id="sched-1")
    state.arm(NOW - 100, 3600)
    state.last_activity = NOW - 100
    state.last_model_activity = NOW - 100
    state.needs_refork = False
    state.fork_id = "fork-1"
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_not_armed(cfg: Config) -> None:
    state = SessionState(session_id="sched-2")
    assert decide(state, NOW, cfg).kind == "none"


def test_window_expired_disarms(cfg: Config) -> None:
    state = armed_state(armed_until=NOW - 1)
    decision = decide(state, NOW, cfg)
    assert decision.kind == "disarm"
    assert "expired" in decision.reason


def test_cache_still_fresh(cfg: Config) -> None:
    # last activity 100s ago, ttl 300, margin 45 -> due at +255s
    decision = decide(armed_state(), NOW, cfg)
    assert decision.kind == "none"


def test_due_with_fresh_fork_pings(cfg: Config) -> None:
    state = armed_state(last_model_activity=NOW - 260, last_activity=NOW - 260)
    assert decide(state, NOW, cfg).kind == "ping"


def test_due_without_fork_reforks(cfg: Config) -> None:
    state = armed_state(last_model_activity=NOW - 260, fork_id=None)
    assert decide(state, NOW, cfg).kind == "refork"


def test_due_with_stale_fork_reforks(cfg: Config) -> None:
    state = armed_state(last_model_activity=NOW - 260, needs_refork=True)
    assert decide(state, NOW, cfg).kind == "refork"


def test_successful_ping_extends_freshness(cfg: Config) -> None:
    state = armed_state(last_model_activity=NOW - 260, last_ping_at=NOW - 10)
    assert decide(state, NOW, cfg).kind == "none"


def test_backoff_blocks_until_deadline(cfg: Config) -> None:
    state = armed_state(last_model_activity=NOW - 260, next_ping_after=NOW + 60)
    assert decide(state, NOW, cfg).kind == "none"
    state.next_ping_after = NOW - 1
    assert decide(state, NOW, cfg).kind == "ping"


def test_stopped_reason_blocks(cfg: Config) -> None:
    state = armed_state(last_model_activity=NOW - 260, stopped_reason="missing cache")
    assert decide(state, NOW, cfg).kind == "none"


def test_budget_exhausted_disarms(cfg: Config) -> None:
    state = armed_state(credits_used=cfg.max_credits_per_window)
    assert decide(state, NOW, cfg).kind == "disarm"
    state = armed_state(ping_count=cfg.max_pings_per_window)
    assert decide(state, NOW, cfg).kind == "disarm"


def test_no_activity_yet(cfg: Config) -> None:
    state = SessionState(session_id="sched-9")
    state.arm(NOW - 100, 3600)
    assert decide(state, NOW, cfg).kind == "none"
