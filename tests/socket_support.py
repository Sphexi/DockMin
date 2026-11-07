"""Builds a Docker socket path in each state the first-run check tells apart."""

import os
import socket
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

SOCKET_NAME = "docker.sock"
STATES = ("absent", "not_socket", "no_access", "present")
RUNS_AS_ROOT = os.geteuid() == 0


def _nothing(path: Path) -> list[socket.socket]:
    return []


def _regular_file(path: Path) -> list[socket.socket]:
    path.write_text("not a socket", encoding="utf-8")
    return []


def _bound_socket(path: Path) -> list[socket.socket]:
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound.bind(str(path))
    return [bound]


def _unreachable_socket(path: Path) -> list[socket.socket]:
    if RUNS_AS_ROOT:
        pytest.skip("root can open any socket")
    bound = _bound_socket(path)
    path.chmod(stat.S_IMODE(0))
    return bound


BUILDERS: dict[str, Callable[[Path], list[socket.socket]]] = {
    "absent": _nothing,
    "not_socket": _regular_file,
    "present": _bound_socket,
    "no_access": _unreachable_socket,
}


@contextmanager
def socket_in_state(directory: Path, state: str) -> Iterator[str]:
    """Yield the path of a Docker socket in the given state; a bound socket is closed after."""
    path = directory / SOCKET_NAME
    held = BUILDERS[state](path)
    try:
        yield str(path)
    finally:
        for bound in held:
            bound.close()
