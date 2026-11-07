"""Package version for the build: DOCKMIN_VERSION when set, otherwise a local-build marker."""

import os

version = os.environ.get("DOCKMIN_VERSION") or "0+local"
