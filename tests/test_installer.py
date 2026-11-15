from __future__ import annotations

import json
from pathlib import Path

import pytest

from keepwarm.installer import (
    HOOK_EVENTS,
    InstallError,
    install,
    installed,
    uninstall,
)

BIN = "/home/user/.local/bin/droid-keepwarm"


def read_hooks(factory_home: Path) -> dict[str, object]:
    return json.loads((factory_home / "hooks.json").read_text("utf-8"))  # type: ignore[no-any-return]


def test_install_creates_all_events(tmp_path: Path) -> None:
    install(tmp_path, BIN)
    data = read_hooks(tmp_path)
    for event in HOOK_EVENTS:
        assert event in data
        groups = data[event]
        assert isinstance(groups, list)
        assert groups[0]["hooks"][0]["command"] == f"{BIN} hook"
        assert groups[0]["hooks"][0]["timeout"] == 5
    assert installed(tmp_path)


def test_install_preserves_other_hooks(tmp_path: Path) -> None:
    (tmp_path / "hooks.json").write_text(
        json.dumps(
            {
                "PreToolUse": [
                    {
                        "matcher": "Execute",
                        "hooks": [{"type": "command", "command": "/bin/audit.sh"}],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "/bin/other.sh"}]}],
            }
        ),
        encoding="utf-8",
    )
    install(tmp_path, BIN)
    data = read_hooks(tmp_path)
    assert data["PreToolUse"][0]["hooks"][0]["command"] == "/bin/audit.sh"
    stop_commands = [h["command"] for g in data["Stop"] for h in g["hooks"]]
    assert "/bin/other.sh" in stop_commands
    assert f"{BIN} hook" in stop_commands


def test_install_is_idempotent(tmp_path: Path) -> None:
    install(tmp_path, BIN)
    install(tmp_path, BIN)
    data = read_hooks(tmp_path)
    for event in HOOK_EVENTS:
        ours = [g for g in data[event] if any("droid-keepwarm" in h["command"] for h in g["hooks"])]
        assert len(ours) == 1
    # second install created a backup of the first
    backups = list(tmp_path.glob("hooks.json.keepwarm-backup-*"))
    assert len(backups) == 1


def test_uninstall_removes_only_ours(tmp_path: Path) -> None:
    (tmp_path / "hooks.json").write_text(
        json.dumps(
            {
                "Stop": [{"hooks": [{"type": "command", "command": "/bin/other.sh"}]}],
            }
        ),
        encoding="utf-8",
    )
    install(tmp_path, BIN)
    uninstall(tmp_path)
    data = read_hooks(tmp_path)
    assert "Stop" in data  # the other hook stays
    for event in HOOK_EVENTS:
        if event not in data:
            continue
        for group in data[event]:
            for hook in group["hooks"]:
                assert "droid-keepwarm" not in hook["command"]
    assert not installed(tmp_path)


def test_install_refuses_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "hooks.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(InstallError):
        install(tmp_path, BIN)
    # and the file is untouched
    assert (tmp_path / "hooks.json").read_text("utf-8") == "{nope"


def test_hooks_file_permissions(tmp_path: Path) -> None:
    path = install(tmp_path, BIN)
    assert path.stat().st_mode & 0o777 == 0o600
