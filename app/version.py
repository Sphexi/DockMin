"""The installed product version."""

import re
from importlib import metadata
from pathlib import Path
from typing import Final

from app.logging import get_logger

UNKNOWN_PRODUCT_VERSION: Final = "unknown"

REVISION_FILE: Final = Path("/usr/share/dockmin/revision")

_PRODUCT_NAME = "dockmin"
_REVISION = re.compile(r"[0-9a-f]{40}")

logger = get_logger(__name__)
_version_warning_logged = False


def product_version() -> str:
    """Return the installed product version, or "unknown" when the package is not installed."""
    global _version_warning_logged
    try:
        return metadata.version(_PRODUCT_NAME)
    except metadata.PackageNotFoundError:
        if not _version_warning_logged:
            _version_warning_logged = True
            logger.warning("product_version_unknown")
        return UNKNOWN_PRODUCT_VERSION


def build_revision() -> str | None:
    """Return the commit the image was built from, or None when the build recorded none."""
    try:
        text = REVISION_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if _REVISION.fullmatch(text) else None
