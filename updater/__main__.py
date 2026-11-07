"""Entry point for `python -I <path to this directory>`."""

import sys
from pathlib import Path

# Directory execution puts this directory on sys.path, not its parent, and -I adds nothing else.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from updater.cli import main

if __name__ == "__main__":
    sys.exit(main())
