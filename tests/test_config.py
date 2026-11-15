from __future__ import annotations

import json
from pathlib import Path

import pytest

from keepwarm.config import Config, ConfigError, load_config, update_config


def write(state_dir: Path, data: object) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.ttl_seconds == 300
    assert cfg.margin_seconds < cfg.ttl_seconds
    assert cfg.guard_mode == "refuse"


def test_valid_overrides(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "ttl_seconds": 3600,
            "margin_seconds": 300,
            "guard_mode": "warn",
            "max_pings_per_window": 50,
        },
    )
    cfg = load_config(tmp_path)
    assert cfg.ttl_seconds == 3600
    assert cfg.guard_mode == "warn"
    assert cfg.max_pings_per_window == 50


def test_unknown_key_rejected(tmp_path: Path) -> None:
    write(tmp_path, {"ttl_sekonds": 300})
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(tmp_path)


def test_type_errors_named(tmp_path: Path) -> None:
    write(tmp_path, {"ttl_seconds": "300"})
    with pytest.raises(ConfigError, match="ttl_seconds"):
        load_config(tmp_path)
    write(tmp_path, {"ttl_seconds": True})
    with pytest.raises(ConfigError, match="ttl_seconds"):
        load_config(tmp_path)


def test_range_errors(tmp_path: Path) -> None:
    write(tmp_path, {"ttl_seconds": 10})
    with pytest.raises(ConfigError, match="ttl_seconds"):
        load_config(tmp_path)


def test_margin_must_be_below_ttl(tmp_path: Path) -> None:
    write(tmp_path, {"ttl_seconds": 100, "margin_seconds": 100})
    with pytest.raises(ConfigError, match="margin_seconds"):
        load_config(tmp_path)


def test_guard_mode_enum(tmp_path: Path) -> None:
    write(tmp_path, {"guard_mode": "nope"})
    with pytest.raises(ConfigError, match="guard_mode"):
        load_config(tmp_path)


def test_backoff_validation(tmp_path: Path) -> None:
    write(tmp_path, {"backoff_seconds": [1]})
    with pytest.raises(ConfigError, match="backoff_seconds"):
        load_config(tmp_path)
    write(tmp_path, {"backoff_seconds": [60, 120]})
    assert load_config(tmp_path).backoff_seconds == (60, 120)


def test_price_overrides_validation(tmp_path: Path) -> None:
    write(tmp_path, {"price_overrides": {"fable-5": {"write": 21.0, "read": 0.3}}})
    cfg = load_config(tmp_path)
    assert cfg.price_overrides["fable-5"] == {"write": 21.0, "read": 0.3}
    write(tmp_path, {"price_overrides": {"fable-5": {"write": -1, "read": 0.3}}})
    with pytest.raises(ConfigError, match="price_overrides"):
        load_config(tmp_path)


def test_invalid_json(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(tmp_path)


def test_update_config_merges_and_validates(tmp_path: Path) -> None:
    update_config(tmp_path, {"guard_mode": "warn"})
    update_config(tmp_path, {"guard_min_usd": 2.5})
    cfg = load_config(tmp_path)
    assert cfg.guard_mode == "warn"
    assert cfg.guard_min_usd == 2.5
    with pytest.raises(ConfigError):
        update_config(tmp_path, {"guard_mode": "bogus"})


def test_env_state_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DROID_KEEPWARM_STATE_DIR", str(tmp_path / "custom"))
    cfg = Config()
    assert cfg.state_dir == tmp_path / "custom"


def test_opt_str_key_wrong_type(tmp_path: Path) -> None:
    write(tmp_path, {"ping_model": 5})
    with pytest.raises(ConfigError, match="ping_model"):
        load_config(tmp_path)


def test_bool_key_wrong_type(tmp_path: Path) -> None:
    write(tmp_path, {"auto_arm_on_cold_write": "yes"})
    with pytest.raises(ConfigError, match="auto_arm_on_cold_write"):
        load_config(tmp_path)


def test_guard_min_usd_validation(tmp_path: Path) -> None:
    write(tmp_path, {"guard_min_usd": -1})
    with pytest.raises(ConfigError, match="guard_min_usd"):
        load_config(tmp_path)
    write(tmp_path, {"guard_min_usd": "5"})
    with pytest.raises(ConfigError, match="guard_min_usd"):
        load_config(tmp_path)
    write(tmp_path, {"guard_min_usd": 5})
    assert load_config(tmp_path).guard_min_usd == 5.0


def test_backoff_too_many_entries(tmp_path: Path) -> None:
    write(tmp_path, {"backoff_seconds": [60] * 9})
    with pytest.raises(ConfigError, match="backoff_seconds"):
        load_config(tmp_path)


def test_default_window_must_not_exceed_max(tmp_path: Path) -> None:
    write(tmp_path, {"default_window_seconds": 90000, "max_window_seconds": 600})
    with pytest.raises(ConfigError, match="default_window_seconds"):
        load_config(tmp_path)


def test_empty_prompt_rejected(tmp_path: Path) -> None:
    write(tmp_path, {"ping_prompt": "   "})
    with pytest.raises(ConfigError, match="ping_prompt"):
        load_config(tmp_path)


def test_price_overrides_wrong_shape(tmp_path: Path) -> None:
    write(tmp_path, {"price_overrides": [1, 2]})
    with pytest.raises(ConfigError, match="price_overrides"):
        load_config(tmp_path)
    write(tmp_path, {"price_overrides": {"fable-5": "cheap"}})
    with pytest.raises(ConfigError, match="price_overrides"):
        load_config(tmp_path)


def test_update_config_refuses_invalid_existing(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        update_config(tmp_path, {"guard_mode": "warn"})


def test_non_dict_top_level_rejected(tmp_path: Path) -> None:
    write(tmp_path, [1, 2, 3])
    with pytest.raises(ConfigError, match="JSON object"):
        load_config(tmp_path)
