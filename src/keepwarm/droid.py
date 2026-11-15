"""Thin, safe wrapper around the ``droid`` CLI.

Every invocation is an argv array passed straight to execve: no shell, ever.
Output is captured to a bounded temp file so a runaway process cannot exhaust
memory. Only token counts and session ids cross this boundary, never prompt
or transcript content beyond the static ping prompt we send ourselves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keepwarm.state import StateError, validate_session_id


class DroidError(RuntimeError):
    """The droid CLI is missing, failed, or returned something unparseable."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    factory_credits: int = 0

    @classmethod
    def from_mapping(cls, data: object) -> Usage:
        if not isinstance(data, dict):
            return cls()

        def _int(key: str) -> int:
            value = data.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0
            return max(0, int(value))

        return cls(
            input_tokens=_int("input_tokens"),
            output_tokens=_int("output_tokens"),
            cache_read_input_tokens=_int("cache_read_input_tokens"),
            cache_creation_input_tokens=_int("cache_creation_input_tokens"),
            factory_credits=_int("factory_credits"),
        )


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    session_id: str | None
    result_text: str
    usage: Usage | None
    duration_ms: int
    error: str | None


@dataclass(frozen=True)
class SessionInfo:
    """Subset of a session's ``.settings.json`` that we actually use."""

    model: str | None
    reasoning_effort: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int

    @property
    def context_tokens(self) -> int:
        """Upper-bound context estimate (what a resumed turn would re-send)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


def find_droid(configured: str | None) -> str:
    """Resolve the droid binary. Honors an explicit path, else PATH lookup."""
    if configured:
        candidate = shutil.which(configured) or (
            configured if Path(configured).is_file() and os.access(configured, os.X_OK) else None
        )
        if candidate is None:
            raise DroidError(f"configured droid_bin is not executable: {configured!r}")
        return candidate
    path = shutil.which("droid")
    if path is None:
        raise DroidError(
            "droid CLI not found on PATH; install it (https://docs.factory.ai/cli) "
            "or set droid_bin in config.json"
        )
    return path


def sessions_dir_for(cwd: str, factory_home: Path) -> Path:
    """Map a working directory to Droid's per-project sessions directory.

    Droid stores sessions under ``~/.factory/sessions/<slug>/`` where the slug
    is the cwd with every path separator replaced by ``-`` (verified against
    droid 1.12.1: ``/home/factory-user`` -> ``-home-factory-user``).
    """
    slug = cwd.replace("/", "-") if cwd else "-"
    return factory_home / "sessions" / slug


def read_session_info(cwd: str, session_id: str, factory_home: Path) -> SessionInfo | None:
    """Read model/usage from ``<session>.settings.json``; None when unavailable."""
    try:
        validate_session_id(session_id)
    except StateError:
        return None
    path = sessions_dir_for(cwd, factory_home) / f"{session_id}.settings.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("tokenUsage")
    usage_map: dict[str, Any] = usage if isinstance(usage, dict) else {}

    def _uint(key: str) -> int:
        value = usage_map.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    model = data.get("model")
    effort = data.get("reasoningEffort")
    return SessionInfo(
        model=model if isinstance(model, str) and model else None,
        reasoning_effort=effort if isinstance(effort, str) and effort else None,
        input_tokens=_uint("inputTokens"),
        output_tokens=_uint("outputTokens"),
        cache_creation_tokens=_uint("cacheCreationTokens"),
        cache_read_tokens=_uint("cacheReadTokens"),
    )


def parse_exec_result(stdout: str) -> ExecResult:
    """Parse the result object from ``droid exec -o json`` output.

    With ``-o json`` the result is a single JSON object; with stream formats it
    is the last ``{"type": "result", ...}`` line. We scan from the end so both
    work, and so noise printed by hooks or MCP servers cannot confuse us.
    """
    candidate: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            candidate = parsed
            break
    if candidate is None:
        # ``-o json`` prints one bare object without a "type" marker in some
        # versions; fall back to the last parseable object at all.
        for line in reversed(stdout.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                candidate = parsed
                break
    if candidate is None:
        return ExecResult(
            ok=False,
            session_id=None,
            result_text="",
            usage=None,
            duration_ms=0,
            error="no JSON result in droid exec output",
        )

    is_error = bool(candidate.get("is_error", False))
    subtype = candidate.get("subtype")
    ok = not is_error and subtype in (None, "success")
    session_id = candidate.get("session_id")
    result_text = candidate.get("result")
    duration = candidate.get("duration_ms", 0)
    error: str | None = None
    if not ok:
        error = (
            result_text
            if isinstance(result_text, str) and result_text
            else str(subtype or "unknown error")
        )
    return ExecResult(
        ok=ok,
        session_id=session_id if isinstance(session_id, str) and session_id else None,
        result_text=result_text if isinstance(result_text, str) else "",
        usage=Usage.from_mapping(candidate.get("usage")),
        duration_ms=int(duration) if isinstance(duration, (int, float)) else 0,
        error=error,
    )


def run_exec(
    droid_bin: str,
    args: list[str],
    *,
    cwd: str | None,
    timeout_seconds: int,
    max_output_bytes: int,
) -> ExecResult:
    """Run ``droid exec`` with argv only. Never raises for CLI-level failures."""
    argv = [droid_bin, "exec", *args]
    workdir = cwd if cwd and Path(cwd).is_dir() else None
    try:
        with tempfile.TemporaryFile(prefix="keepwarm-exec-") as out:
            try:
                proc = subprocess.run(  # noqa: S603 - argv list, no shell
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds,
                    check=False,
                    cwd=workdir,
                )
            except subprocess.TimeoutExpired:
                return ExecResult(
                    ok=False,
                    session_id=None,
                    result_text="",
                    usage=None,
                    duration_ms=timeout_seconds * 1000,
                    error=f"timeout after {timeout_seconds}s",
                )
            out.seek(0)
            raw = out.read(max_output_bytes + 1)
    except OSError as exc:
        return ExecResult(
            ok=False,
            session_id=None,
            result_text="",
            usage=None,
            duration_ms=0,
            error=f"failed to launch droid: {exc}",
        )
    truncated = len(raw) > max_output_bytes
    text = raw[:max_output_bytes].decode("utf-8", errors="replace")
    result = parse_exec_result(text)
    if proc.returncode != 0 and result.ok:
        # Non-zero exit with a parseable success body: trust the exit code.
        stderr = (proc.stderr or b"")[:4096].decode("utf-8", errors="replace").strip()
        return ExecResult(
            ok=False,
            session_id=result.session_id,
            result_text=result.result_text,
            usage=result.usage,
            duration_ms=result.duration_ms,
            error=f"exit code {proc.returncode}: {stderr or 'no stderr'}",
        )
    if truncated and result.ok:
        # A truncated stream may hide the real result; treat as failure.
        return ExecResult(
            ok=False,
            session_id=result.session_id,
            result_text=result.result_text,
            usage=result.usage,
            duration_ms=result.duration_ms,
            error="droid output exceeded the capture limit",
        )
    return result


def fork_session(
    droid_bin: str,
    source_session_id: str,
    *,
    cwd: str,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int,
    max_output_bytes: int,
) -> ExecResult:
    """Fork ``source_session_id`` and run one minimal turn inside the fork."""
    validate_session_id(source_session_id)
    args = ["--fork", source_session_id, "-o", "json"]
    if cwd:
        args += ["--cwd", cwd]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--reasoning-effort", reasoning_effort]
    args.append(prompt)
    return run_exec(
        droid_bin,
        args,
        cwd=cwd or None,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def ping_session(
    droid_bin: str,
    fork_id: str,
    *,
    cwd: str,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int,
    max_output_bytes: int,
) -> ExecResult:
    """Send one minimal turn to an existing fork session."""
    validate_session_id(fork_id)
    args = ["--session-id", fork_id, "-o", "json"]
    if cwd:
        args += ["--cwd", cwd]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--reasoning-effort", reasoning_effort]
    args.append(prompt)
    return run_exec(
        droid_bin,
        args,
        cwd=cwd or None,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
