"""Per-session state files with locking, atomic writes and symlink defense.

State lives under ``<state_dir>/sessions/<session-id>.json`` and is only ever
mutated under an ``flock``-protected read-modify-write cycle. Files are
created ``0600`` inside a ``0700`` directory and opened with ``O_NOFOLLOW``:
the daemon runs unattended and must never follow a planted symlink.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,127}$")


class StateError(RuntimeError):
    """State files are missing, corrupt, or unsafe to touch."""


def validate_session_id(session_id: str) -> str:
    """Reject anything that is not a plain session id (path traversal defense)."""
    if not _SESSION_ID_RE.match(session_id):
        raise StateError(f"invalid session id: {session_id!r}")
    return session_id


def ensure_dir(path: Path) -> None:
    """Create ``path`` (and parents) with 0700 permissions if missing."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        # Best effort: a restrictive umask already gives us 0700 on creation.
        path.chmod(0o700)


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise StateError(f"{path}: not a regular file")
    return fd


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to ``path`` atomically (tmp file + fsync + rename), mode 0600."""
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    except BaseException:
        with suppress(OSError):
            Path(tmp_name).unlink()
        raise


@dataclass
class SessionState:
    """Everything the daemon and hooks know about one Droid session."""

    session_id: str
    cwd: str = ""
    transcript_path: str = ""
    model: str | None = None
    reasoning_effort: str | None = None
    # Fork we ping on this session's behalf. None until the first refork.
    fork_id: str | None = None
    # Set when the live transcript moved past the fork (new turn, resume, compaction).
    needs_refork: bool = True
    # Warming window: absolute epoch deadline, None when disarmed.
    armed_until: float | None = None
    # "always" mode: every turn re-arms always_window_seconds from last activity.
    always: bool = False
    always_window_seconds: int = 6 * 3600
    # Last user-visible activity (prompt submit, turn stop, session start).
    last_activity: float = 0.0
    # Last completed model turn; the anchor for cache-expiry math.
    last_model_activity: float = 0.0
    last_ping_at: float | None = None
    # Backoff: do not ping before this epoch (0 means no backoff).
    next_ping_after: float = 0.0
    ping_failures: int = 0
    # Pings sent through the current fork; the first one creates cache, so the
    # cache-hit stop rule only applies from the second ping onward.
    pings_this_fork: int = 0
    ping_count: int = 0
    credits_used: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Last context size observed for the live session (for guard estimates).
    last_context_tokens: int = 0
    # Bookkeeping for cold-write detection in the Stop hook.
    last_cache_creation_seen: int = 0
    # Set when the loop stopped (budget, cache misses, repeated failures).
    stopped_reason: str | None = None
    # Guard refuse-once bookkeeping: hash of the blocked prompt + when.
    guard_consumed_hash: str | None = None
    guard_consumed_at: float = 0.0
    created_at: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> SessionState:
        known = {f.name for f in fields(cls)}
        session_id = data.get("session_id")
        if not isinstance(session_id, str):
            raise StateError("state file missing session_id")
        validate_session_id(session_id)
        kwargs: dict[str, Any] = {"session_id": session_id}
        for key, value in data.items():
            if key not in known or key == "session_id":
                continue
            kwargs[key] = value
        state = cls(**kwargs)
        # Type-smoke-test the fields the daemon arithmetic relies on.
        for numeric in (
            "last_activity",
            "last_model_activity",
            "next_ping_after",
            "ping_count",
            "credits_used",
            "pings_this_fork",
            "ping_failures",
        ):
            if not isinstance(getattr(state, numeric), (int, float)):
                raise StateError(f"state field {numeric} has wrong type")
        return state

    @property
    def armed(self) -> bool:
        return self.armed_until is not None

    def arm(self, now: float, window_seconds: float) -> None:
        """Arm (or extend) the warming window and clear any stopped state."""
        candidate = now + window_seconds
        if self.armed_until is None or self.armed_until < candidate:
            self.armed_until = candidate
        self.stopped_reason = None

    def disarm(self, reason: str | None) -> None:
        self.armed_until = None
        self.stopped_reason = reason


class StateStore:
    """Locked, atomic access to per-session state files."""

    def __init__(self, state_dir: Path) -> None:
        self._root = state_dir
        self._sessions_dir = state_dir / "sessions"

    @property
    def sessions_dir(self) -> Path:
        return self._sessions_dir

    def path_for(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self._sessions_dir / f"{session_id}.json"

    def _lock_path_for(self, session_id: str) -> Path:
        return self._sessions_dir / f"{session_id}.lock"

    @contextmanager
    def edit(self, session_id: str) -> Iterator[SessionState]:
        """Exclusive read-modify-write cycle. Saves on clean exit."""
        ensure_dir(self._sessions_dir)
        validate_session_id(session_id)
        lock_path = self._lock_path_for(session_id)
        fd = _open_nofollow(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self._load_unlocked(session_id)
            yield state
            atomic_write_json(self.path_for(session_id), state.to_json_dict())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_unlocked(self, session_id: str) -> SessionState:
        path = self.path_for(session_id)
        try:
            fd = _open_nofollow(path, os.O_RDONLY)
        except FileNotFoundError:
            return SessionState(session_id=session_id, created_at=time.time())
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise StateError(f"{path}: corrupt state file: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"{path}: expected a JSON object")
        return SessionState.from_json_dict(data)

    def load(self, session_id: str) -> SessionState | None:
        """Read state under a shared lock; None when no file exists."""
        ensure_dir(self._sessions_dir)
        validate_session_id(session_id)
        path = self.path_for(session_id)
        if not path.exists():
            return None
        lock_path = self._lock_path_for(session_id)
        fd = _open_nofollow(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return self._load_unlocked(session_id)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load_all(self) -> list[SessionState]:
        """Best-effort load of every state file; corrupt ones are skipped."""
        ensure_dir(self._sessions_dir)
        states: list[SessionState] = []
        for path in sorted(self._sessions_dir.glob("*.json")):
            session_id = path.stem
            try:
                state = self.load(session_id)
            except (StateError, OSError):
                continue
            if state is not None:
                states.append(state)
        return states

    def delete(self, session_id: str) -> None:
        validate_session_id(session_id)
        for path in (self.path_for(session_id), self._lock_path_for(session_id)):
            with suppress(FileNotFoundError):
                path.unlink()
