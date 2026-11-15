"""``python -m keepwarm`` entry point (used by the daemon's self-spawn)."""

from keepwarm.cli import main

raise SystemExit(main())
