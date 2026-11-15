from __future__ import annotations

from keepwarm.config import Config
from keepwarm.droid import SessionInfo
from keepwarm.guard import evaluate_guard, prompt_hash
from keepwarm.state import SessionState

NOW = 50_000.0


def make_info(model: str | None = "claude-fable-5.2", context: int = 100_000) -> SessionInfo:
    return SessionInfo(
        model=model,
        reasoning_effort="high",
        input_tokens=context,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
    )


def cold_state(context_tokens: int = 100_000) -> SessionState:
    state = SessionState(session_id="guard-1")
    state.last_model_activity = NOW - 3600  # 1h ago, well past the 5-minute TTL
    state.last_context_tokens = context_tokens
    return state


def test_allows_when_guard_off() -> None:
    cfg = Config(guard_mode="off")
    decision = evaluate_guard(cold_state(), make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.action == "allow"


def test_allows_when_cache_fresh() -> None:
    cfg = Config(guard_mode="refuse")
    state = cold_state()
    state.last_model_activity = NOW - 60
    decision = evaluate_guard(state, make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.action == "allow"


def test_allows_below_min_context() -> None:
    cfg = Config(guard_mode="refuse", guard_min_context_tokens=200_000)
    decision = evaluate_guard(
        cold_state(), make_info(context=100_000), now=NOW, prompt="hi", config=cfg
    )
    assert decision.action == "allow"


def test_allows_when_no_model_activity() -> None:
    cfg = Config(guard_mode="refuse")
    state = SessionState(session_id="guard-2")
    decision = evaluate_guard(state, make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.action == "allow"


def test_refuse_blocks_with_estimate() -> None:
    cfg = Config(guard_mode="refuse")
    decision = evaluate_guard(cold_state(), make_info(), now=NOW, prompt="summarize", config=cfg)
    assert decision.action == "block"
    assert decision.message is not None
    assert "100,000 tokens" in decision.message
    # fable-5 row: 100k tokens at $20/MTok = $2.00
    assert "$2.00" in decision.message
    assert "Send it again" in decision.message


def test_identical_resend_within_grace_is_allowed() -> None:
    cfg = Config(guard_mode="refuse")
    state = cold_state()
    first = evaluate_guard(state, make_info(), now=NOW, prompt="summarize", config=cfg)
    assert first.action == "block"
    state.guard_consumed_hash = prompt_hash("summarize")
    state.guard_consumed_at = NOW
    second = evaluate_guard(state, make_info(), now=NOW + 30, prompt="summarize", config=cfg)
    assert second.action == "allow"


def test_different_prompt_after_block_still_blocks() -> None:
    cfg = Config(guard_mode="refuse")
    state = cold_state()
    state.guard_consumed_hash = prompt_hash("summarize")
    state.guard_consumed_at = NOW
    decision = evaluate_guard(
        state, make_info(), now=NOW + 30, prompt="a different prompt", config=cfg
    )
    assert decision.action == "block"


def test_warn_mode_never_blocks() -> None:
    cfg = Config(guard_mode="warn")
    decision = evaluate_guard(cold_state(), make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.action == "warn"
    assert decision.message is not None


def test_unknown_model_blocks_without_price() -> None:
    cfg = Config(guard_mode="refuse")
    decision = evaluate_guard(
        cold_state(), make_info(model="mystery-9-turbo"), now=NOW, prompt="hi", config=cfg
    )
    assert decision.action == "block"
    assert decision.message is not None
    assert "no price row" in decision.message


def test_below_floor_warns_and_sends() -> None:
    cfg = Config(guard_mode="refuse", guard_min_usd=5.00)
    decision = evaluate_guard(cold_state(), make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.action == "warn"
    assert decision.message is not None
    assert "below your $5.00 guard floor" in decision.message


def test_idle_formatting_in_message() -> None:
    cfg = Config(guard_mode="warn")
    state = cold_state()
    state.last_model_activity = NOW - 7320  # 2h02m
    decision = evaluate_guard(state, make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.message is not None
    assert "2h02m ago" in decision.message


def test_idle_seconds_only_format() -> None:
    cfg = Config(guard_mode="warn")
    state = cold_state()
    state.last_model_activity = NOW - 320  # past the 300s TTL by 20s
    decision = evaluate_guard(state, make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.message is not None
    assert "5m20s ago" in decision.message


def test_idle_minutes_format() -> None:
    cfg = Config(guard_mode="warn")
    state = cold_state()
    state.last_model_activity = NOW - 600
    decision = evaluate_guard(state, make_info(), now=NOW, prompt="hi", config=cfg)
    assert decision.message is not None
    assert "10m ago" in decision.message
