from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from keepwarm.state import (
    SessionState,
    StateError,
    StateStore,
    atomic_write_json,
    validate_session_id,
)


def test_validate_session_id_rejects_traversal() -> None:
    for bad in ("../etc", "a/b", "..", "", "x" * 200, "has space", ".hidden-ok?"):
        with pytest.raises(StateError):
            validate_session_id(bad)


def test_validate_session_id_accepts_uuids_and_slugs() -> None:
    validate_session_id("f887c503-70a8-41aa-b748-96ef9b7087b1")
    validate_session_id("abc123")


def test_roundtrip(store: StateStore) -> None:
    with store.edit("session-1") as state:
        state.model = "claude-fable-5.2"
        state.arm(1000.0, 3600)
        state.credits_used = 42
    loaded = store.load("session-1")
    assert loaded is not None
    assert loaded.model == "claude-fable-5.2"
    assert loaded.armed_until == 4600.0
    assert loaded.credits_used == 42
    assert loaded.stopped_reason is None


def test_missing_session_returns_none(store: StateStore) -> None:
    assert store.load("nope-123") is None


def test_corrupt_state_raises(store: StateStore) -> None:
    path = store.path_for("corrupt-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError):
        store.load("corrupt-1")


def test_load_all_skips_corrupt(store: StateStore) -> None:
    with store.edit("good-1") as state:
        state.credits_used = 7
    bad = store.path_for("bad-1")
    bad.write_text("[]{", encoding="utf-8")
    states = store.load_all()
    assert [s.session_id for s in states] == ["good-1"]


def test_permissions(store: StateStore) -> None:
    with store.edit("perms-1") as state:
        state.credits_used = 1
    path = store.path_for("perms-1")
    assert stat_perm(path) == 0o600
    assert stat_perm(path.parent) == 0o700


def stat_perm(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_symlink_state_file_rejected(store: StateStore, tmp_path: Path) -> None:
    with store.edit("victim-1") as state:
        state.credits_used = 5
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path = store.path_for("victim-1")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises((StateError, OSError)):
        store.load("victim-1")


def test_concurrent_editors_keep_all_increments(store: StateStore) -> None:
    threads = 8
    increments = 25

    def worker() -> None:
        for _ in range(increments):
            with store.edit("race-1") as state:
                state.credits_used += 1

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    loaded = store.load("race-1")
    assert loaded is not None
    assert loaded.credits_used == threads * increments


def test_arm_extends_but_never_shrinks() -> None:
    state = SessionState(session_id="arm-1")
    state.arm(1000.0, 100)
    assert state.armed_until == 1100.0
    state.arm(1050.0, 10)  # candidate 1060 < 1100: keep the longer window
    assert state.armed_until == 1100.0
    state.stopped_reason = "x"
    state.arm(1050.0, 200)
    assert state.armed_until == 1250.0
    assert state.stopped_reason is None


def test_disarm() -> None:
    state = SessionState(session_id="arm-2")
    state.arm(1000.0, 100)
    state.disarm("done")
    assert state.armed_until is None
    assert state.stopped_reason == "done"


def test_atomic_write_json_mode(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "file.json"
    atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text("utf-8")) == {"a": 1}
    assert stat_perm(path) == 0o600


def test_delete_removes_state_and_lock(store: StateStore) -> None:
    with store.edit("del-1") as state:
        state.credits_used = 3
    assert store.load("del-1") is not None
    store.delete("del-1")
    assert store.load("del-1") is None
    store.delete("del-1")  # idempotent


def test_from_json_dict_rejects_wrong_numeric_types() -> None:
    with pytest.raises(StateError):
        SessionState.from_json_dict({"session_id": "num-1", "credits_used": "lots"})


def test_from_json_dict_ignores_unknown_keys() -> None:
    state = SessionState.from_json_dict({"session_id": "num-2", "future_field": [1, 2]})
    assert state.session_id == "num-2"


def test_directory_at_state_path_rejected(store: StateStore) -> None:
    path = store.path_for("dir-1")
    path.mkdir(parents=True)
    with pytest.raises((StateError, OSError)):
        store.load("dir-1")


def test_load_all_returns_sorted(store: StateStore) -> None:
    for sid in ("zzz-1", "aaa-1"):
        with store.edit(sid) as state:
            state.credits_used = 1
    assert [s.session_id for s in store.load_all()] == ["aaa-1", "zzz-1"]
