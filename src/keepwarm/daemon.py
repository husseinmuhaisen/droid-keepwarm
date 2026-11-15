"""The keepwarm daemon: one process per user, scanning armed sessions.

Scheduling rule: the cache expires ``ttl_seconds`` after the last model
request that touched this session's prefix. Both real turns (tracked via the
Stop hook) and our own pings count, so the next ping is due at
``max(last_model_activity, last_ping_at) + ttl - margin``. All decisions are
made by the pure :func:`decide` function; side effects live in ``_act``.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Literal

from keepwarm import droid, pinger
from keepwarm.config import Config
from keepwarm.log import get_logger
from keepwarm.state import SessionState, StateError, StateStore, ensure_dir

DecisionKind = Literal["ping", "refork", "disarm", "none"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str


def decide(state: SessionState, now: float, cfg: Config) -> Decision:
    """Pure scheduling decision for one session. Unit-tested exhaustively."""
    if state.armed_until is None:
        return Decision("none", "not armed")
    if now >= state.armed_until:
        return Decision("disarm", "warming window expired")
    if state.stopped_reason is not None:
        return Decision("none", f"stopped: {state.stopped_reason}")
    if (
        state.credits_used >= cfg.max_credits_per_window
        or state.ping_count >= cfg.max_pings_per_window
    ):
        return Decision("disarm", "budget exhausted")
    anchor = max(state.last_model_activity, state.last_ping_at or 0.0)
    if anchor <= 0.0:
        return Decision("none", "no model activity yet")
    due_at = anchor + (cfg.ttl_seconds - cfg.margin_seconds)
    if now < due_at:
        return Decision("none", "cache still fresh")
    if now < state.next_ping_after:
        return Decision("none", "backing off after a failed ping")
    if state.fork_id is None or state.needs_refork:
        return Decision("refork", "fork missing or stale")
    return Decision("ping", "cache expiring")


def daemon_running(state_dir: Path) -> bool:
    """True when another process holds the daemon pid lock."""
    pidfile = state_dir / "daemon.pid"
    try:
        fd = os.open(pidfile, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def spawn_detached(cfg: Config) -> bool:
    """Start the daemon detached, from a short-lived hook process.

    Uses the current interpreter so the package resolves exactly as it does
    for the hook itself. The daemon's own singleton lock makes races harmless.
    """
    if daemon_running(cfg.state_dir):
        return False
    argv = [sys.executable, "-m", "keepwarm", "daemon"]
    try:
        subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return False
    return True


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._store = StateStore(cfg.state_dir)
        self._log = get_logger("daemon", cfg.state_dir)
        self._stop_requested = False
        self._droid_bin: str | None = None

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        self._log.info("received signal %d, shutting down", signum)
        self._stop_requested = True

    def _resolve_droid(self) -> str:
        if self._droid_bin is None:
            self._droid_bin = droid.find_droid(self._cfg.droid_bin)
        return self._droid_bin

    def run(self, *, once: bool = False) -> int:
        ensure_dir(self._cfg.state_dir)
        pidfile = self._cfg.state_dir / "daemon.pid"
        fd = os.open(pidfile, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            print("droid-keepwarm daemon is already running", file=sys.stderr)
            return 0
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)
            self._log.info(
                "daemon started (pid=%d ttl=%ds margin=%ds scan=%ds)",
                os.getpid(),
                self._cfg.ttl_seconds,
                self._cfg.margin_seconds,
                self._cfg.scan_interval_seconds,
            )
            if once:
                self.scan_once(time.time())
                return 0
            while not self._stop_requested:
                started = time.time()
                try:
                    self.scan_once(started)
                except Exception:
                    # The daemon must not die because one scan tick failed.
                    self._log.exception("scan failed")
                elapsed = time.time() - started
                self._sleep(max(1.0, self._cfg.scan_interval_seconds - elapsed))
            return 0
        finally:
            os.close(fd)  # releases the singleton lock
            self._log.info("daemon stopped")

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    def scan_once(self, now: float) -> list[tuple[str, Decision]]:
        """One pass over all known sessions. Exposed for testing."""
        outcomes: list[tuple[str, Decision]] = []
        for summary in self._store.load_all():
            session_id = summary.session_id
            try:
                with self._store.edit(session_id) as state:
                    decision = decide(state, now, self._cfg)
                    outcomes.append((session_id, decision))
                    if decision.kind == "disarm":
                        state.disarm(decision.reason)
                        self._log.info("session %s disarmed: %s", session_id, decision.reason)
                    elif decision.kind in ("ping", "refork"):
                        self._act(state, decision.kind, now)
            except (StateError, OSError) as exc:
                self._log.warning("session %s skipped: %s", session_id, exc)
        return outcomes

    def _act(self, state: SessionState, kind: Literal["ping", "refork"], now: float) -> None:
        try:
            droid_bin = self._resolve_droid()
        except droid.DroidError as exc:
            state.ping_failures += 1
            state.next_ping_after = now + self._backoff_for(state.ping_failures)
            state.stopped_reason = str(exc)
            self._write_receipt(state, kind, now, None, str(exc))
            return
        if kind == "refork":
            outcome = pinger.refork(state, self._cfg, droid_bin, self._log)
        else:
            outcome = pinger.ping(state, self._cfg, droid_bin, self._log)

        if not outcome.ok:
            state.ping_failures += 1
            state.next_ping_after = now + self._backoff_for(state.ping_failures)
            if state.ping_failures >= self._cfg.max_ping_failures:
                state.stopped_reason = (
                    f"ping failed {state.ping_failures} times in a row "
                    f"(last: {outcome.error}); giving up"
                )
                self._log.warning("session %s stopped: %s", state.session_id, state.stopped_reason)
            self._write_receipt(state, kind, now, outcome.usage, outcome.error)
            return

        state.ping_failures = 0
        state.next_ping_after = 0.0
        state.last_ping_at = now
        state.ping_count += 1
        state.pings_this_fork += 1
        if outcome.new_fork_id is not None:
            state.fork_id = outcome.new_fork_id
            state.needs_refork = False
            state.pings_this_fork = 1
        usage = outcome.usage
        if usage is not None:
            state.credits_used += usage.factory_credits
            state.cache_read_tokens = usage.cache_read_input_tokens
            state.cache_creation_tokens = usage.cache_creation_input_tokens
            # Stop rule: from the second ping on a fork,
            # reads must be nonzero and writes must stay under 10% of reads.
            # The first ping on a fresh fork legitimately creates the cache.
            if state.pings_this_fork >= 2:
                reads = usage.cache_read_input_tokens
                writes = usage.cache_creation_input_tokens
                if reads == 0 or writes >= 0.1 * reads:
                    state.stopped_reason = (
                        "pings are missing the cache "
                        f"(read={reads} wrote={writes}); stopping to avoid spend"
                    )
                    self._log.warning(
                        "session %s stopped: %s", state.session_id, state.stopped_reason
                    )
        self._write_receipt(state, kind, now, usage, None)

    def _backoff_for(self, failures: int) -> int:
        index = min(max(failures - 1, 0), len(self._cfg.backoff_seconds) - 1)
        return self._cfg.backoff_seconds[index]

    def _write_receipt(
        self,
        state: SessionState,
        kind: str,
        now: float,
        usage: droid.Usage | None,
        error: str | None,
    ) -> None:
        record: dict[str, object] = {
            "ts": round(now, 3),
            "session_id": state.session_id,
            "fork_id": state.fork_id,
            "kind": kind,
            "ok": error is None,
            "error": error,
            "credits_used_total": state.credits_used,
            "ping_count": state.ping_count,
        }
        if usage is not None:
            record["usage"] = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "factory_credits": usage.factory_credits,
            }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        try:
            ensure_dir(self._cfg.state_dir)
            fd = os.open(
                self._cfg.state_dir / "receipts.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            self._log.warning("could not write receipt: %s", exc)
