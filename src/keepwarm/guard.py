"""The cold-send guard.

Runs inside the UserPromptSubmit hook. When the cache has likely expired and
the session carries enough context to make a rewrite hurt, it either warns
(``systemMessage``) or refuses once (exit 2), letting an explicit resend pay
the cost deliberately. The cost figure is an upper
bound ("up to N tokens"), not a quote.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from keepwarm.config import Config
from keepwarm.droid import SessionInfo
from keepwarm.pricing import (
    estimate_read_cost_usd,
    estimate_write_cost_usd,
    price_for,
)
from keepwarm.state import SessionState

GuardAction = Literal["allow", "warn", "block"]


@dataclass(frozen=True)
class GuardDecision:
    action: GuardAction
    message: str | None = None


def prompt_hash(prompt: str) -> str:
    """Only a hash of the prompt is ever stored; never the prompt itself."""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def _format_idle(idle_seconds: float) -> str:
    total = int(idle_seconds)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m{seconds:02d}s"
    return f"{total}s"


def evaluate_guard(
    state: SessionState,
    info: SessionInfo | None,
    *,
    now: float,
    prompt: str,
    config: Config,
) -> GuardDecision:
    """Decide what a UserPromptSubmit hook should do. Pure and fast."""
    if config.guard_mode == "off":
        return GuardDecision("allow")
    if state.last_model_activity <= 0:
        return GuardDecision("allow")  # nothing cached yet this session
    if info is None or info.context_tokens < config.guard_min_context_tokens:
        return GuardDecision("allow")
    idle = now - state.last_model_activity
    if idle < config.ttl_seconds:
        return GuardDecision("allow")

    # Refuse-once: an identical resend inside the grace window goes through.
    if (
        state.guard_consumed_hash is not None
        and state.guard_consumed_hash == prompt_hash(prompt)
        and now - state.guard_consumed_at <= config.guard_resend_grace_seconds
    ):
        return GuardDecision("allow")

    context = info.context_tokens
    row = price_for(info.model, config.price_overrides)
    idle_text = _format_idle(idle)
    if row is None:
        message = (
            f"keepwarm: the prompt cache went cold {idle_text} ago. "
            f"Sending this re-writes up to {context:,} tokens and I have no price row "
            f"for model {info.model!r}, so I cannot estimate the cost. "
            "Send it again to pay it, or /clear and start from a note."
        )
        # A model with no price row is always asked about in refuse mode.
        return GuardDecision("block" if config.guard_mode == "refuse" else "warn", message)

    write_cost = estimate_write_cost_usd(context, row)
    warm_cost = estimate_read_cost_usd(context, row)
    message = (
        f"keepwarm: the prompt cache went cold {idle_text} ago. "
        f"Sending this re-writes up to {context:,} tokens at "
        f"${row.write_usd_per_mtok:.2f}/MTok = ${write_cost:.2f} "
        f"(a warm turn would have cost ${warm_cost:.2f}). "
        "Send it again to pay it, or /clear and start from a note."
    )
    if config.guard_mode == "warn":
        return GuardDecision("warn", message)
    if write_cost < config.guard_min_usd:
        return GuardDecision(
            "warn",
            f"keepwarm: cache cold; rewrite estimate ${write_cost:.2f} is below your "
            f"${config.guard_min_usd:.2f} guard floor, sending.",
        )
    return GuardDecision("block", message)
