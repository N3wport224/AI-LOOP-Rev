"""Convenience entry point: `python cli.py <command>` is equivalent to `automonetize <command>`."""

import sys

from dashboard.cli import main

if __name__ == "__main__":
    sys.exit(main())
