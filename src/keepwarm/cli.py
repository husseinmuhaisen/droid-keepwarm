"""Command line interface: arm/off/status/receipts/guard/daemon/install/doctor."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from keepwarm import __version__, installer
from keepwarm import daemon as daemon_mod
from keepwarm.config import Config, ConfigError, load_config, update_config
from keepwarm.droid import DroidError, find_droid
from keepwarm.hook import run_hook
from keepwarm.state import SessionState, StateError, StateStore

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")
_MIN_ALWAYS_WINDOW = 3600


class CliError(RuntimeError):
    """User-facing failure with a clean message (exit code 2)."""


def parse_duration(text: str) -> int:
    """Parse durations like ``90m``, ``2h30m``, ``6h`` -> seconds."""
    match = _DURATION_RE.match(text.strip())
    if not match or not (match.group(1) or match.group(2)):
        raise CliError(f"invalid duration {text!r} (examples: 90m, 2h30m, 6h)")
    return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="droid-keepwarm",
        description=(
            "Keep Droid's prompt cache warm during breaks and warn before a cold send. "
            "Pings cost tokens; arm windows you expect to return within."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hook", help="handle a Droid lifecycle hook (reads event JSON on stdin)")

    daemon_p = sub.add_parser("daemon", help="run the warming daemon (foreground)")
    daemon_p.add_argument(
        "--once",
        action="store_true",
        help="run one scan and exit (for timers and testing)",
    )

    arm_p = sub.add_parser("arm", help="arm a warming window for a session")
    arm_p.add_argument(
        "duration",
        nargs="?",
        help="window like 90m, 2h30m, 6h (default from config), or 'always'",
    )
    arm_p.add_argument(
        "always_window",
        nargs="?",
        help="with 'always': window kept after each turn (1h to 168h)",
    )
    arm_p.add_argument("--session", help="session id (default: most recently active)")

    off_p = sub.add_parser("off", help="disarm, forget the window, and turn always off")
    off_p.add_argument("--session", help="session id (default: most recently active)")

    guard_p = sub.add_parser("guard", help="configure the cold-send guard")
    guard_p.add_argument("mode", choices=["off", "warn", "refuse"])
    guard_p.add_argument(
        "min_usd",
        nargs="?",
        type=float,
        help="refuse only at or above this estimated cost (warn below it)",
    )

    sub.add_parser("status", help="show warming state for every known session")

    receipts_p = sub.add_parser("receipts", help="show recent ping receipts")
    receipts_p.add_argument("--last", type=int, default=10, help="number of receipts (default 10)")

    sub.add_parser("install", help="register Droid hooks (user scope, ~/.factory/hooks.json)")
    sub.add_parser("uninstall", help="remove our Droid hook registrations")
    sub.add_parser("doctor", help="check installation health")
    return parser


def _resolve_session(store: StateStore, session_arg: str | None, out: TextIO) -> SessionState:
    if session_arg is not None:
        state = store.load(session_arg)
        if state is None:
            raise CliError(
                f"no keepwarm state for session {session_arg!r}; "
                "start or resume that Droid session first"
            )
        return state
    states = store.load_all()
    if not states:
        raise CliError(
            "no known sessions yet; start a Droid session first (hooks register it), "
            "or pass --session"
        )
    states.sort(key=lambda s: s.last_activity, reverse=True)
    chosen = states[0]
    if len(states) > 1:
        print(
            f"note: {len(states)} sessions known; using most recently active {chosen.session_id}",
            file=out,
        )
    return chosen


def _ensure_daemon(cfg: Config, out: TextIO) -> None:
    if not cfg.autostart_daemon:
        return
    if daemon_mod.spawn_detached(cfg):
        print("keepwarm daemon started", file=out)


def cmd_arm(args: argparse.Namespace, cfg: Config, out: TextIO) -> int:
    store = StateStore(cfg.state_dir)
    target = _resolve_session(store, args.session, out)
    now = time.time()
    with store.edit(target.session_id) as state:
        if args.duration == "always":
            window = (
                parse_duration(args.always_window)
                if args.always_window
                else cfg.always_window_seconds
            )
            if not _MIN_ALWAYS_WINDOW <= window <= 7 * 24 * 3600:
                raise CliError("the always window must be between 1h and 168h")
            state.always = True
            state.always_window_seconds = window
            state.arm(now, window)
            message = (
                f"keepwarm always-on for session {state.session_id}: every turn keeps "
                f"the cache warm for {format_duration(window)} after its last activity. "
                "Pings cost tokens; 'droid-keepwarm off' stops every window."
            )
        else:
            window = parse_duration(args.duration) if args.duration else cfg.default_window_seconds
            if window > cfg.max_window_seconds:
                raise CliError(
                    f"window {format_duration(window)} exceeds the configured maximum "
                    f"{format_duration(cfg.max_window_seconds)}"
                )
            state.arm(now, window)
            message = (
                f"keepwarm armed for session {state.session_id}: warm for "
                f"{format_duration(window)} (until "
                f"{time.strftime('%H:%M:%S', time.localtime(state.armed_until or now))}). "
                "Pings cost tokens; choose a window you expect to return within."
            )
    print(message, file=out)
    _ensure_daemon(cfg, out)
    return 0


def cmd_off(args: argparse.Namespace, cfg: Config, out: TextIO) -> int:
    store = StateStore(cfg.state_dir)
    target = _resolve_session(store, args.session, out)
    with store.edit(target.session_id) as state:
        state.always = False
        state.disarm("stopped with 'droid-keepwarm off'")
        state.next_ping_after = 0.0
        state.ping_failures = 0
    print(
        f"keepwarm off for session {target.session_id}: window cleared, always off. "
        "Armed windows in other sessions are unaffected; use --session to target them.",
        file=out,
    )
    return 0


def cmd_guard(args: argparse.Namespace, cfg: Config, out: TextIO) -> int:
    updates: dict[str, Any] = {"guard_mode": args.mode}
    if args.min_usd is not None:
        if args.min_usd < 0:
            raise CliError("min_usd must be >= 0")
        updates["guard_min_usd"] = args.min_usd
    new_cfg = update_config(cfg.state_dir, updates)
    floor = f" (refusing only at >= ${new_cfg.guard_min_usd:.2f})" if args.mode == "refuse" else ""
    print(f"guard mode: {new_cfg.guard_mode}{floor}", file=out)
    return 0


def _status_line(state: SessionState, now: float, cfg: Config) -> str:
    sid = state.session_id[:8]
    if state.armed_until is None:
        window = "not armed"
    else:
        left = state.armed_until - now
        window = f"armed, {format_duration(left)} left" if left > 0 else "window expired"
    anchor = max(state.last_model_activity, state.last_ping_at or 0.0)
    if state.armed and anchor > 0:
        due = anchor + (cfg.ttl_seconds - cfg.margin_seconds) - now
        ping = f"next ping in {format_duration(due)}" if due > 0 else "ping due now"
    else:
        ping = "-"
    cache = f"read {state.cache_read_tokens:,} / wrote {state.cache_creation_tokens:,}"
    stopped = f" [stopped: {state.stopped_reason}]" if state.stopped_reason else ""
    always = " always" if state.always else ""
    return (
        f"{sid}  {window}{always}  {ping}  pings {state.ping_count}  "
        f"last {cache}  credits {state.credits_used:,}{stopped}"
    )


def cmd_status(cfg: Config, out: TextIO) -> int:
    now = time.time()
    running = daemon_mod.daemon_running(cfg.state_dir)
    print(
        f"droid-keepwarm {__version__}  ttl {cfg.ttl_seconds}s  margin {cfg.margin_seconds}s  "
        f"daemon {'running' if running else 'not running'}  guard {cfg.guard_mode}",
        file=out,
    )
    states = StateStore(cfg.state_dir).load_all()
    if not states:
        print("no known sessions", file=out)
        return 0
    states.sort(key=lambda s: s.last_activity, reverse=True)
    for state in states:
        print(_status_line(state, now, cfg), file=out)
    return 0


def cmd_receipts(args: argparse.Namespace, cfg: Config, out: TextIO) -> int:
    path = cfg.state_dir / "receipts.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        print("no receipts yet", file=out)
        return 0
    for line in lines[-args.last :]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(record.get("ts", 0))))
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        read = usage.get("cache_read_input_tokens", 0)
        wrote = usage.get("cache_creation_input_tokens", 0)
        credits = usage.get("factory_credits", 0)
        status = "ok" if record.get("ok") else f"FAILED ({record.get('error')})"
        print(
            f"{ts}  {record.get('kind'):6s}  {str(record.get('session_id'))[:8]}  "
            f"read {read:>9,}  wrote {wrote:>9,}  credits {credits:>8,}  {status}",
            file=out,
        )
    return 0


def _install_bin_path() -> str:
    script = Path(sys.argv[0]).resolve()
    if script.name == "droid-keepwarm" and os.access(script, os.X_OK):
        return str(script)
    on_path = shutil.which("droid-keepwarm")
    if on_path:
        return on_path
    raise CliError(
        "cannot locate the droid-keepwarm executable; install with 'uv tool install .' and re-run"
    )


def cmd_install(cfg: Config, out: TextIO) -> int:
    bin_path = _install_bin_path()
    path = installer.install(cfg.factory_home, bin_path)
    print(f"hooks registered in {path}", file=out)
    print(f"hook command: {bin_path} hook", file=out)
    return 0


def cmd_uninstall(cfg: Config, out: TextIO) -> int:
    path = installer.uninstall(cfg.factory_home)
    print(f"keepwarm hooks removed from {path}", file=out)
    return 0


def cmd_doctor(cfg: Config, out: TextIO) -> int:
    failures = 0

    def ok(msg: str) -> None:
        print(f"  ok    {msg}", file=out)

    def fail(msg: str) -> None:
        nonlocal failures
        failures += 1
        print(f"  FAIL  {msg}", file=out)

    print("droid CLI:", file=out)
    try:
        binary = find_droid(cfg.droid_bin)
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
            text=True,
        )
        version = proc.stdout.strip() or proc.stderr.strip() or "unknown"
        ok(f"{binary} (version {version})")
    except (DroidError, OSError, subprocess.TimeoutExpired) as exc:
        fail(str(exc))

    print("hooks:", file=out)
    if installer.installed(cfg.factory_home):
        ok(f"registered in {installer.hooks_path(cfg.factory_home)}")
    else:
        fail("not registered; run 'droid-keepwarm install'")

    print("config:", file=out)
    try:
        load_config(cfg.state_dir)
        ok(f"{cfg.state_dir / 'config.json'} valid (or defaults)")
    except ConfigError as exc:
        fail(str(exc))

    print("daemon:", file=out)
    if daemon_mod.daemon_running(cfg.state_dir):
        ok("running")
    else:
        ok("not running (starts automatically when a window is armed)")

    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            return run_hook(sys.stdin, sys.stdout, sys.stderr)
        if args.command == "daemon":
            cfg = load_config()
            return daemon_mod.Daemon(cfg).run(once=args.once)

        cfg = load_config()
        if args.command == "arm":
            return cmd_arm(args, cfg, sys.stdout)
        if args.command == "off":
            return cmd_off(args, cfg, sys.stdout)
        if args.command == "guard":
            return cmd_guard(args, cfg, sys.stdout)
        if args.command == "status":
            return cmd_status(cfg, sys.stdout)
        if args.command == "receipts":
            return cmd_receipts(args, cfg, sys.stdout)
        if args.command == "install":
            return cmd_install(cfg, sys.stdout)
        if args.command == "uninstall":
            return cmd_uninstall(cfg, sys.stdout)
        if args.command == "doctor":
            return cmd_doctor(cfg, sys.stdout)
    except BrokenPipeError:
        # stdout went away mid-print (e.g. piping into head). Exit quietly,
        # and redirect stdout so the interpreter's exit flush cannot fail too.
        with suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        return 0
    except (CliError, ConfigError, StateError, DroidError, installer.InstallError) as exc:
        print(f"droid-keepwarm: {exc}", file=sys.stderr)
        return 2
    raise CliError(f"unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
