"""Builds the environment of a Compose process from nothing; the server's own is never inherited."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from app.compose_cli.workdir import CONFIG_DIRECTORY, HOME_DIRECTORY, TEMP_DIRECTORY

RESERVED_PREFIXES: Final[tuple[str, ...]] = (
    "DOCKER_",
    "COMPOSE_",
    "BUILDKIT_",
    "BUILDX_",
    "DOCKMIN_",
    "LC_",
    "LD_",
    "OTEL_",
    "SSL_",
    "XDG_",
)
RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "TZ",
        "NO_COLOR",
        "PWD",
        "SHELL",
        "USER",
        "LOGNAME",
        "GODEBUG",
        "GOGC",
        "GOMAXPROCS",
        "GOMEMLIMIT",
        "GOTRACEBACK",
    }
)
TARGET_VARIABLES: Final[frozenset[str]] = frozenset(
    {"DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
)
SOURCE_PREFIX: Final = "DOCKMIN_SOURCE_"
DISCLOSING_VARIABLES: Final[frozenset[str]] = TARGET_VARIABLES | {
    "DOCKER_CONFIG",
    "HOME",
    "TMPDIR",
}

_PROXY_SUFFIX: Final = "_PROXY"
_VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SOURCE_NAME: Final = re.compile(rf"{SOURCE_PREFIX}[0-9]{{1,4}}")
_SEARCH_PATH: Final = "/usr/local/bin:/usr/bin:/bin"


def is_reserved(name: str) -> bool:
    """Report whether a stack variable name would steer Compose, Go or the process."""
    folded = name.upper()
    return (
        folded in RESERVED_NAMES
        or folded.startswith(RESERVED_PREFIXES)
        or folded.endswith(_PROXY_SUFFIX)
    )


def build_environment(
    workdir: Path,
    target_env: Mapping[str, str],
    variables: Mapping[str, str],
    sources: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the complete environment for one Compose process.

    Only the connection variables in TARGET_VARIABLES are taken from target_env. `sources`
    may name only generated source variables. Raises ValueError for a variable whose name is
    reserved or malformed or whose value holds NUL.
    """
    environment = {
        "PATH": _SEARCH_PATH,
        "HOME": str(workdir / HOME_DIRECTORY),
        "DOCKER_CONFIG": str(workdir / CONFIG_DIRECTORY),
        "TMPDIR": str(workdir / TEMP_DIRECTORY),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "NO_COLOR": "1",
        "DOCKER_CLI_HINTS": "false",
        "COMPOSE_MENU": "false",
    }
    environment.update((k, v) for k, v in target_env.items() if k in TARGET_VARIABLES)
    for name, value in variables.items():
        if _VARIABLE_NAME.fullmatch(name) is None or is_reserved(name):
            raise ValueError("A stack variable name is reserved or malformed.")
        if "\0" in value:
            raise ValueError("A stack variable value holds a NUL character.")
        environment[name] = value
    for name, value in (sources or {}).items():
        if _SOURCE_NAME.fullmatch(name) is None:
            raise ValueError("A source variable name is outside the generated names.")
        if "\0" in value:
            raise ValueError("A source value holds a NUL character.")
        environment[name] = value
    return environment
