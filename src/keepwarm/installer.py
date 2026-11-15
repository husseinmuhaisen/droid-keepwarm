"""Idempotent installer for Droid hook registrations in ~/.factory/hooks.json.

Never clobbers: existing hook groups are preserved, only groups whose command
references ``droid-keepwarm`` are replaced, and the original file is backed up
before every write. Refuses to run over an unparseable hooks.json.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from keepwarm.state import atomic_write_json

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact")
_MARKER = "droid-keepwarm"


class InstallError(RuntimeError):
    """hooks.json is unparseable or unsafe to modify."""


def hooks_path(factory_home: Path) -> Path:
    return factory_home / "hooks.json"


def hook_command(bin_path: str) -> dict[str, Any]:
    return {"type": "command", "command": f"{bin_path} hook", "timeout": 5}


def _load(factory_home: Path) -> dict[str, Any]:
    path = hooks_path(factory_home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"{path}: invalid JSON ({exc}). Fix it or move it aside, then re-run install."
        ) from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path}: expected a JSON object keyed by event name")
    return data


def _is_ours(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and _MARKER in str(h.get("command", "")) for h in hooks)


def _save(factory_home: Path, data: dict[str, Any]) -> Path:
    path = hooks_path(factory_home)
    factory_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.keepwarm-backup-{stamp}")
        shutil.copy2(path, backup)
    atomic_write_json(path, data)
    return path


def install(factory_home: Path, bin_path: str) -> Path:
    """Register our hook on every event we care about. Idempotent."""
    data = _load(factory_home)
    for event in HOOK_EVENTS:
        existing = data.get(event)
        groups = [g for g in (existing if isinstance(existing, list) else []) if not _is_ours(g)]
        groups.append({"hooks": [hook_command(bin_path)]})
        data[event] = groups
    return _save(factory_home, data)


def uninstall(factory_home: Path) -> Path:
    """Remove only our hook groups; leave everyone else's untouched."""
    data = _load(factory_home)
    for event in HOOK_EVENTS:
        existing = data.get(event)
        if not isinstance(existing, list):
            continue
        kept = [g for g in existing if not _is_ours(g)]
        if kept:
            data[event] = kept
        else:
            data.pop(event, None)
    return _save(factory_home, data)


def installed(factory_home: Path) -> bool:
    try:
        data = _load(factory_home)
    except InstallError:
        return False
    for event in HOOK_EVENTS:
        groups = data.get(event)
        if not isinstance(groups, list) or not any(_is_ours(g) for g in groups):
            return False
    return True
