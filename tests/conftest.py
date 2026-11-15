from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from keepwarm.config import Config
from keepwarm.state import StateStore


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "keepwarm"


@pytest.fixture()
def cfg(state_dir: Path, tmp_path: Path) -> Config:
    return Config(
        state_dir=state_dir,
        factory_home=tmp_path / "factory",
        ttl_seconds=300,
        margin_seconds=45,
        backoff_seconds=(60, 120, 240),
        autostart_daemon=False,
    )


@pytest.fixture()
def store(cfg: Config) -> StateStore:
    return StateStore(cfg.state_dir)


FAKE_DROID = (
    r"""#!/usr/bin/env python3
"""
    + '"""'
    + r"""Fake droid CLI for integration tests.

Behavior is driven by files/env in FAKE_DROID_HOME:
  fail_next          - if present: print an error result and exit 1 (consumed)
  usage.json         - optional usage overrides
  FAKE_DROID_SLEEP   - sleep N seconds before answering (timeout testing)
  FAKE_DROID_EXIT    - exit with this code after printing a success result
Every exec invocation appends its argv to argv-log.jsonl.
"""
    + '"""'
    + r"""
import json
import os
import sys
import time
import uuid

home = os.environ.get("FAKE_DROID_HOME", "/tmp/fake-droid")
os.makedirs(home, exist_ok=True)

sleep_s = float(os.environ.get("FAKE_DROID_SLEEP", "0"))
if sleep_s:
    time.sleep(sleep_s)

with open(os.path.join(home, "argv-log.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")

fail_marker = os.path.join(home, "fail_next")
if os.path.exists(fail_marker):
    os.unlink(fail_marker)
    print(json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "result": "simulated 529 overloaded",
                      "session_id": "00000000-0000-0000-0000-000000000000"}))
    sys.exit(1)

usage = {"input_tokens": 10000, "output_tokens": 5,
         "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 0,
         "factory_credits": 1000}
overrides = os.path.join(home, "usage.json")
if os.path.exists(overrides):
    with open(overrides, encoding="utf-8") as handle:
        usage.update(json.load(handle))

session_id = str(uuid.uuid4())
argv = sys.argv[1:]
for flag in ("-s", "--session-id"):
    if flag in argv:
        session_id = argv[argv.index(flag) + 1]

print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 123, "num_turns": 1, "result": "ok",
    "session_id": session_id, "usage": usage,
}))
sys.exit(int(os.environ.get("FAKE_DROID_EXIT", "0")))
"""
)


@pytest.fixture()
def fake_droid(tmp_path: Path) -> Path:
    """A standalone fake `droid` binary on disk."""
    home = tmp_path / "fake-droid-home"
    home.mkdir()
    binary = tmp_path / "fake-droid"
    binary.write_text(FAKE_DROID, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary
