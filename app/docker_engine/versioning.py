"""Engine API version parsing and the supported range."""

type ApiVersion = tuple[int, int]

API_VERSION_FLOOR: ApiVersion = (1, 44)
CLIENT_MAX_API_VERSION: ApiVersion = (1, 51)


def parse_api_version(text: str) -> ApiVersion:
    """Parse "MAJOR.MINOR"; raise ValueError on anything else."""
    major, dot, minor = text.strip().partition(".")
    if not dot or not all(p.isascii() and p.isdigit() for p in (major, minor)):
        raise ValueError("malformed API version")
    return int(major), int(minor)


def format_api_version(version: ApiVersion) -> str:
    """Render a version as "MAJOR.MINOR"."""
    return f"{version[0]}.{version[1]}"
