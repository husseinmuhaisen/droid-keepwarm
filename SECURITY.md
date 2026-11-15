# Security Policy

## Reporting

Report vulnerabilities by opening a private security advisory on GitHub (or by
emailing the maintainer listed in `pyproject.toml`). Do not open public issues
for security reports.

## Threat model

droid-keepwarm runs unattended next to your Droid sessions:

- The **hook** executes inside Droid's event path with your credentials. It
  performs no network I/O, reads at most one small local JSON file, caps stdin
  at 1 MiB, and degrades every unexpected failure to a silent no-op (logged)
  so it can never wedge a session.
- The **daemon** shells out to the `droid` CLI using argv arrays only (never a
  shell), with a 90s timeout and a 1 MiB output cap per invocation.
- State, receipts and logs live in `~/.factory/keepwarm/` (`0700`), files
  `0600`, opened with `O_NOFOLLOW`, written atomically under `flock`. Session
  ids are validated against a strict allowlist pattern before touching the
  filesystem (path traversal defense).
- We never store or log prompt or transcript content. The guard keeps only a
  SHA-256 of a refused prompt (to let an identical resend through).
- `install`/`uninstall` back up `~/.factory/hooks.json` before every write and
  refuse to modify an unparseable file.

## Supply chain

Zero runtime dependencies by design. Development dependencies are locked in
`uv.lock`, audited in CI with `pip-audit`, and kept current with Dependabot.
GitHub Actions are audited with `zizmor`. Runtime has no third-party code to
audit beyond Python itself and the `droid` binary it invokes.
