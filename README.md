# droid-keepwarm

**Keep Factory Droid's prompt cache warm during breaks — and get warned before a cold send.**

A cache-warming companion for the
[Factory Droid CLI](https://docs.factory.ai/droid-cli/overview). Droid sessions on
Anthropic-family models (Fable 5.2 included) use a **5-minute prompt-cache TTL**.
Any gap longer than that — a coffee, a meeting, a long tool call — expires the cached
prefix, and your next turn silently re-writes the whole transcript at cache-write prices.
`droid-keepwarm` pings a fork of your session just before the TTL lapses, and its guard
shows you the estimated rewrite cost before you pay it.

> Pings cost tokens. Arm windows you expect to return within.

## How it works

```
Droid hooks (SessionStart / UserPromptSubmit / Stop / PreCompact / SessionEnd)
        │  ~/.factory/hooks.json → `droid-keepwarm hook` (fast, no network)
        ▼
per-session state  (~/.factory/keepwarm/sessions/*.json, flock + atomic writes)
        ▲
droid-keepwarm daemon (one per user)
        │  due = max(last turn, last ping) + TTL(300s) − margin(45s)
        ▼
droid exec --fork <session>  →  droid exec -s <fork-id> "Reply with exactly: ok"
```

- **The live transcript is never touched.** The ping runs in a *fork* of your session
  (`droid exec --fork`, then `droid exec --session-id <fork>`). The fork's request shares
  your session's transcript prefix, so the cache entry it refreshes is the same entry your
  next real turn reads. Verified against droid 1.12.1.
- **Receipts, not vibes.** Every ping's usage (`cache_read_input_tokens`,
  `cache_creation_input_tokens`, `factory_credits`) is recorded from `droid exec -o json`
  to `~/.factory/keepwarm/receipts.jsonl`.
- **Stop rules.** From the second ping on a fork, if cache reads are
  zero or writes reach 10% of reads, the loop stops instead of spending. Credit and ping
  budgets cap every window. Failures back off 1, 2, 4 minutes.
- **The guard** runs in the `UserPromptSubmit` hook. When the cache is cold and your
  session carries real context, `refuse` mode (default) blocks the send once with the
  estimated rewrite cost; sending the same prompt again pays it deliberately. `warn` mode
  prints the estimate and sends. `droid-keepwarm guard refuse 5` only blocks at $5+.
- **Auto-arm.** After a large cold cache write is detected on turn end, a 3-hour window
  arms itself so the next break doesn't cost the same rewrite again (configurable).

## Install

```sh
git clone <this repo> && cd droid-keepwarm
uv tool install .
droid-keepwarm install     # registers hooks in ~/.factory/hooks.json (backs up first)
droid-keepwarm doctor      # verifies droid, hooks, config
```

Requires Python ≥ 3.11 and the [Droid CLI](https://docs.factory.ai/droid-cli/cli-reference).
Uninstall with `droid-keepwarm uninstall && uv tool uninstall droid-keepwarm`.

## Usage

```sh
droid-keepwarm arm 90m        # keep the current session warm for 90 minutes
droid-keepwarm arm            # default window (3h)
droid-keepwarm arm always     # every turn re-arms 6h; survives as a setting
droid-keepwarm arm always 24h # same, with a 24h floor (1h–168h)
droid-keepwarm off            # stop, forget the window, always off
droid-keepwarm status         # window, next ping, last cache read/write, credits
droid-keepwarm receipts       # per-ping receipts
droid-keepwarm guard warn|refuse|off [min_usd]
```

The daemon starts itself when a window is armed. You can also run it directly
(`droid-keepwarm daemon`) or from a systemd user unit:

```ini
[Unit]
Description=droid-keepwarm cache warmer

[Service]
ExecStart=%h/.local/bin/droid-keepwarm daemon
Restart=on-failure

[Install]
WantedBy=default.target
```

## Configuration

`~/.factory/keepwarm/config.json` — every key optional, validated at load with
actionable error messages. Highlights:

| key | default | meaning |
| --- | --- | --- |
| `ttl_seconds` | `300` | cache lifetime of the served model |
| `margin_seconds` | `45` | ping this long before expiry |
| `guard_mode` | `"refuse"` | `off` / `warn` / `refuse` |
| `guard_min_usd` | `0.0` | refuse only at or above this estimate |
| `guard_min_context_tokens` | `20000` | guard ignores smaller sessions |
| `max_credits_per_window` | `20000000` | per-window spend cap (factory credits) |
| `max_pings_per_window` | `500` | per-window ping cap |
| `backoff_seconds` | `[60, 120, 240]` | retry delays after failed pings |
| `auto_arm_on_cold_write` | `true` | arm 3h after a detected cold write |
| `price_overrides` | `{}` | extra `{"prefix": {"write": …, "read": …}}` rows |

Guard estimates are **API-equivalent list prices** (per MTok, 5-minute write vs read),
decision aids, not invoices. Built-in rows cover Claude Opus/Sonnet/
Haiku 4.x and Fable 5.x; add your own via `price_overrides`.

## Limits

- Windows are per-session. Forks accumulate under `~/.factory/sessions/…` (small JSONL
  copies; delete them like any session if you want).
- A window restored after a daemon restart waits for the next scan tick — it does not
  back-fill missed pings.
- The guard's context figure is an upper bound (last turn's input + cache read + write),
  the standard caveat for resumed sessions.
- If Droid ever offers a longer TTL (Claude Code's `promptCacheTtl` equivalent), prefer
  raising `ttl_seconds` over more pings: a 1-hour TTL cuts ping frequency ~12x.

## Security model

- Zero runtime dependencies; stdlib only. The hook path must start fast and cannot be
  allowed to break a Droid session — every unexpected failure degrades to a no-op with
  a log line, never an error into Droid.
- No `shell=True` anywhere; the droid CLI is invoked as an argv array with a 90s timeout
  and a 1 MiB output cap.
- State and receipts are `0600` inside a `0700` directory, opened with `O_NOFOLLOW`,
  written atomically (tmp + fsync + rename) under `flock`.
- Only token counts, session ids and paths are logged or stored. The guard stores a
  SHA-256 of the refused prompt, never the prompt.
- `droid-keepwarm install` backs up `hooks.json` before every write and refuses to touch
  an unparseable one.

## Development

Trail-of-Bits-style tooling: **uv** (lockfile committed), **ruff** (lint + format),
**mypy --strict**, **pytest + coverage** (gate: 85%), **hypothesis**-friendly pure
scheduling logic, CI with pip-audit and zizmor.

```sh
uv sync
uv run pytest --cov
uv run ruff check && uv run ruff format --check
uv run mypy
```

The integration suite runs the daemon against a fake `droid` binary
(`tests/conftest.py`) — no network, no tokens spent.

## License

MIT. Not affiliated with or endorsed by Factory.
