"""Configuration loading and validation for droid-keepwarm.

Configuration comes from ``<state_dir>/config.json`` (every key optional)
with ``DROID_KEEPWARM_*`` environment overrides for the most operational
knobs. Everything is validated once at load time; invalid values raise
:class:`ConfigError` with messages that name the offending key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ENV_PREFIX = "DROID_KEEPWARM_"
CONFIG_FILENAME = "config.json"


class ConfigError(ValueError):
    """Configuration file exists but contains invalid values."""


def default_factory_home() -> Path:
    env = os.environ.get("FACTORY_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".factory"


def default_state_dir() -> Path:
    env = os.environ.get(f"{ENV_PREFIX}STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return default_factory_home() / "keepwarm"


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration. All durations are seconds."""

    state_dir: Path = field(default_factory=default_state_dir)
    factory_home: Path = field(default_factory=default_factory_home)
    # Cache lifetime of the served model. Fable-family on Droid defaults to 5 minutes.
    ttl_seconds: int = 300
    # Ping this long before the TTL would lapse.
    margin_seconds: int = 45
    # Daemon scan cadence.
    scan_interval_seconds: int = 15
    default_window_seconds: int = 3 * 3600
    max_window_seconds: int = 24 * 3600
    always_window_seconds: int = 6 * 3600
    ping_timeout_seconds: int = 90
    ping_max_output_bytes: int = 1 << 20
    ping_prompt: str = "Reply with exactly: ok"
    # None means: use the session's own model (required for a cache hit).
    ping_model: str | None = None
    # None means: droid's per-model default.
    ping_reasoning_effort: str | None = None
    # Stop rules: credits (factory_credits as reported by droid exec) and ping count.
    max_credits_per_window: int = 20_000_000
    max_pings_per_window: int = 500
    # Retry delays after a failed ping: 1, 2, 4 minutes.
    backoff_seconds: tuple[int, ...] = (60, 120, 240)
    max_ping_failures: int = 8
    # Guard: what to do when a human sends a prompt into a cold cache.
    guard_mode: str = "refuse"  # "off" | "warn" | "refuse"
    guard_min_usd: float = 0.0
    guard_min_context_tokens: int = 20_000
    guard_resend_grace_seconds: int = 600
    # Auto-arm a warming window after a large cold cache write is detected.
    auto_arm_on_cold_write: bool = True
    auto_arm_window_seconds: int = 3 * 3600
    auto_arm_min_write_tokens: int = 50_000
    # Allow the hook to spawn the daemon detached when it is not running.
    autostart_daemon: bool = True
    # Explicit path to the droid binary; None means resolve "droid" from PATH.
    droid_bin: str | None = None
    hook_stdin_max_bytes: int = 1 << 20
    # Extra price rows keyed by model-id prefix, e.g. {"fable-5": {"write": 20.0, "read": 0.25}}.
    price_overrides: dict[str, dict[str, float]] = field(default_factory=dict)


_INT_RANGES: dict[str, tuple[int, int]] = {
    "ttl_seconds": (60, 24 * 3600),
    "margin_seconds": (5, 3600),
    "scan_interval_seconds": (5, 600),
    "default_window_seconds": (600, 7 * 24 * 3600),
    "max_window_seconds": (600, 7 * 24 * 3600),
    "always_window_seconds": (3600, 7 * 24 * 3600),
    "ping_timeout_seconds": (10, 600),
    "ping_max_output_bytes": (4096, 64 << 20),
    "max_credits_per_window": (1, 10**12),
    "max_pings_per_window": (1, 100_000),
    "max_ping_failures": (1, 100),
    "guard_min_context_tokens": (0, 10**9),
    "guard_resend_grace_seconds": (30, 3600),
    "auto_arm_window_seconds": (600, 7 * 24 * 3600),
    "auto_arm_min_write_tokens": (1000, 10**9),
    "hook_stdin_max_bytes": (4096, 64 << 20),
}

_BOOL_KEYS = {"auto_arm_on_cold_write", "autostart_daemon"}
_STR_KEYS = {"ping_prompt"}
_OPT_STR_KEYS = {"ping_model", "ping_reasoning_effort", "droid_bin"}
_GUARD_MODES = {"off", "warn", "refuse"}


def _validate_bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: expected a boolean, got {type(value).__name__}")
    return value


def _validate_int(key: str, value: object) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key}: expected an integer, got {type(value).__name__}")
    lo, hi = _INT_RANGES[key]
    if not lo <= value <= hi:
        raise ConfigError(f"{key}: expected between {lo} and {hi}, got {value}")
    return value


def _validate_str(key: str, value: object, *, max_len: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key}: expected a non-empty string")
    if len(value) > max_len:
        raise ConfigError(f"{key}: value longer than {max_len} characters")
    return value


def _validate_backoff(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 8
        or any(isinstance(v, bool) or not isinstance(v, int) or not 10 <= v <= 3600 for v in value)
    ):
        raise ConfigError("backoff_seconds: expected a list of 1-8 integers between 10 and 3600")
    return tuple(value)


def _validate_price_overrides(value: object) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        raise ConfigError("price_overrides: expected an object keyed by model prefix")
    out: dict[str, dict[str, float]] = {}
    for prefix, row in value.items():
        if not isinstance(prefix, str) or not prefix:
            raise ConfigError("price_overrides: model prefixes must be non-empty strings")
        if not isinstance(row, dict):
            raise ConfigError(f"price_overrides[{prefix!r}]: expected an object")
        try:
            write = float(row["write"])
            read = float(row["read"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                f"price_overrides[{prefix!r}]: expected numeric 'write' and 'read' keys"
            ) from exc
        if write <= 0 or read <= 0:
            raise ConfigError(f"price_overrides[{prefix!r}]: prices must be positive")
        out[prefix] = {"write": write, "read": read}
    return out


def _apply_mapping(cfg: Config, data: dict[str, Any]) -> Config:
    unknown = sorted(
        k
        for k in data
        if k not in _INT_RANGES
        and k not in _BOOL_KEYS
        and k not in _STR_KEYS
        and k not in _OPT_STR_KEYS
        and k not in {"guard_mode", "guard_min_usd", "backoff_seconds", "price_overrides"}
    )
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")

    updates: dict[str, Any] = {}
    for key in _INT_RANGES & data.keys():
        updates[key] = _validate_int(key, data[key])
    for key in _BOOL_KEYS & data.keys():
        updates[key] = _validate_bool(key, data[key])
    for key in _STR_KEYS & data.keys():
        updates[key] = _validate_str(key, data[key])
    for key in _OPT_STR_KEYS & data.keys():
        value = data[key]
        if value is not None:
            value = _validate_str(key, value)
        updates[key] = value
    if "guard_mode" in data:
        mode = data["guard_mode"]
        if mode not in _GUARD_MODES:
            raise ConfigError(f"guard_mode: expected one of {sorted(_GUARD_MODES)}, got {mode!r}")
        updates["guard_mode"] = mode
    if "guard_min_usd" in data:
        value = data["guard_min_usd"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 100_000
        ):
            raise ConfigError("guard_min_usd: expected a number between 0 and 100000")
        updates["guard_min_usd"] = float(value)
    if "backoff_seconds" in data:
        updates["backoff_seconds"] = _validate_backoff(data["backoff_seconds"])
    if "price_overrides" in data:
        updates["price_overrides"] = _validate_price_overrides(data["price_overrides"])

    merged = replace(cfg, **updates)
    if merged.margin_seconds >= merged.ttl_seconds:
        raise ConfigError(
            f"margin_seconds ({merged.margin_seconds}) must be smaller than "
            f"ttl_seconds ({merged.ttl_seconds})"
        )
    if merged.default_window_seconds > merged.max_window_seconds:
        raise ConfigError("default_window_seconds must not exceed max_window_seconds")
    return merged


def load_config(state_dir: Path | None = None) -> Config:
    """Load and validate configuration. Missing file means defaults."""
    base = replace(Config(), state_dir=state_dir) if state_dir is not None else Config()
    path = base.state_dir / CONFIG_FILENAME
    if not path.exists():
        return base
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object at the top level")
    return _apply_mapping(base, data)


def update_config(state_dir: Path, updates: dict[str, Any]) -> Config:
    """Merge ``updates`` into config.json atomically and return the new Config."""
    from keepwarm.state import atomic_write_json, ensure_dir  # local import: avoid cycle

    ensure_dir(state_dir)
    path = state_dir / CONFIG_FILENAME
    current: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                current = loaded
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    current.update(updates)
    atomic_write_json(path, current)
    # Re-validate everything before reporting success.
    return load_config(state_dir)
