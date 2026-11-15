from __future__ import annotations

from keepwarm.pricing import (
    DEFAULT_TABLE,
    estimate_read_cost_usd,
    estimate_write_cost_usd,
    price_for,
)


def test_prefix_matching() -> None:
    assert price_for("claude-fable-5.2") is DEFAULT_TABLE["fable-5"]
    assert price_for("Claude-Sonnet-4.5") is DEFAULT_TABLE["claude-sonnet-4"]
    assert price_for("CLAUDE-OPUS-4.8") is DEFAULT_TABLE["claude-opus-4"]


def test_unknown_model_returns_none() -> None:
    assert price_for("gpt-9-ultra") is None
    assert price_for(None) is None
    assert price_for("") is None


def test_longest_prefix_wins() -> None:
    overrides = {"claude-sonnet-4.5": {"write": 4.0, "read": 0.4}}
    row = price_for("claude-sonnet-4.5", overrides)
    assert row is not None
    assert row.write_usd_per_mtok == 4.0
    # The shorter built-in prefix still applies to other sonnet ids.
    assert price_for("claude-sonnet-4.6", overrides) is DEFAULT_TABLE["claude-sonnet-4"]


def test_estimates() -> None:
    row = DEFAULT_TABLE["fable-5"]
    assert estimate_write_cost_usd(100_000, row) == 2.00
    assert estimate_read_cost_usd(200_000, row) == 0.05
