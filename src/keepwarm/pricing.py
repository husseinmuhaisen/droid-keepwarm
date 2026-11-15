"""Price estimates for cache rewrites.

Prices are API-equivalent list prices (per million tokens): estimates for
decision-making, not invoices. The
5-minute cache-write price applies to Droid's default TTL. Override or extend
rows via ``price_overrides`` in config.json.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceRow:
    write_usd_per_mtok: float
    read_usd_per_mtok: float
    note: str = ""


# Keys are matched case-insensitively against the model id (substring match,
# longest match wins, so "fable-5" catches "claude-fable-5.2" while a more
# specific override still beats a shorter built-in row). Figures follow
# Anthropic's published prompt-caching prices
# (5-minute write = 1.25x input, read = 0.1x input).
DEFAULT_TABLE: dict[str, PriceRow] = {
    "claude-opus-4": PriceRow(18.75, 1.50, "Anthropic Opus 4.x list (5m write / read)"),
    "claude-sonnet-4": PriceRow(3.75, 0.30, "Anthropic Sonnet 4.x list (5m write / read)"),
    "claude-haiku-4": PriceRow(1.25, 0.10, "Anthropic Haiku 4.x list (5m write / read)"),
    "fable-5": PriceRow(20.00, 0.25, "Fable 5.x estimate"),
}


def normalize_model(model: str) -> str:
    return model.strip().lower()


def price_for(
    model: str | None, overrides: dict[str, dict[str, float]] | None = None
) -> PriceRow | None:
    """Longest-prefix price lookup; None when the model has no row."""
    if not model:
        return None
    table: dict[str, PriceRow] = DEFAULT_TABLE.copy()
    for prefix, override_row in (overrides or {}).items():
        table[prefix.lower()] = PriceRow(
            override_row["write"], override_row["read"], "config.json override"
        )
    normalized = normalize_model(model)
    best: tuple[int, PriceRow] | None = None
    for prefix, price_row in table.items():
        if prefix in normalized and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price_row)
    return best[1] if best else None


def estimate_write_cost_usd(context_tokens: int, row: PriceRow) -> float:
    return context_tokens / 1_000_000 * row.write_usd_per_mtok


def estimate_read_cost_usd(context_tokens: int, row: PriceRow) -> float:
    return context_tokens / 1_000_000 * row.read_usd_per_mtok
