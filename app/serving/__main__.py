"""Entry point: python -m app.serving."""

import sys

from app.serving.launcher import main

if __name__ == "__main__":
    sys.exit(main())
