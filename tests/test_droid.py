from __future__ import annotations

import json
from pathlib import Path

from keepwarm.droid import (
    parse_exec_result,
    read_session_info,
    sessions_dir_for,
)


def test_sessions_dir_slug() -> None:
    home = Path("/home/user/.factory")
    assert sessions_dir_for("/home/user/project", home) == home / "sessions" / "-home-user-project"
    assert sessions_dir_for("/tmp", home) == home / "sessions" / "-tmp"
    assert sessions_dir_for("", home) == home / "sessions" / "-"


def test_parse_result_object() -> None:
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 2730,
            "num_turns": 1,
            "result": "ok",
            "session_id": "abc",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 5,
                "cache_read_input_tokens": 90,
                "cache_creation_input_tokens": 0,
                "factory_credits": 42,
            },
        }
    )
    result = parse_exec_result(raw)
    assert result.ok
    assert result.session_id == "abc"
    assert result.usage is not None
    assert result.usage.cache_read_input_tokens == 90
    assert result.usage.factory_credits == 42


def test_parse_prefers_last_result_line_over_noise() -> None:
    noise = 'some warning\n{"not": "a result"}\n'
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
        }
    )
    result = parse_exec_result(noise + result_line + "\ntrailing noise")
    assert result.ok
    assert result.session_id == "s1"


def test_parse_error_result() -> None:
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "529 overloaded",
        }
    )
    result = parse_exec_result(raw)
    assert not result.ok
    assert result.error == "529 overloaded"


def test_parse_garbage() -> None:
    result = parse_exec_result("no json here")
    assert not result.ok
    assert result.error is not None


def test_usage_tolerates_missing_and_weird_values() -> None:
    result = parse_exec_result(
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "ok",
                "usage": {"input_tokens": "lots", "output_tokens": None, "factory_credits": -5},
            }
        )
    )
    assert result.ok
    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.factory_credits == 0


def test_read_session_info(tmp_path: Path) -> None:
    home = tmp_path / ".factory"
    session_dir = sessions_dir_for("/work/proj", home)
    session_dir.mkdir(parents=True)
    (session_dir / "s-123.settings.json").write_text(
        json.dumps(
            {
                "model": "claude-fable-5.2",
                "reasoningEffort": "high",
                "tokenUsage": {
                    "inputTokens": 66235,
                    "outputTokens": 10584,
                    "cacheCreationTokens": 0,
                    "cacheReadTokens": 230912,
                },
            }
        ),
        encoding="utf-8",
    )
    info = read_session_info("/work/proj", "s-123", home)
    assert info is not None
    assert info.model == "claude-fable-5.2"
    assert info.reasoning_effort == "high"
    assert info.context_tokens == 66235 + 230912


def test_read_session_info_missing(tmp_path: Path) -> None:
    assert read_session_info("/nowhere", "s-999", tmp_path) is None
