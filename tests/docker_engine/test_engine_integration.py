"""Runs the client against a real local Docker socket; skips when there is none."""

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.docker_engine import EngineClient, EnvironmentUnreachable
from app.docker_engine.transport import DEFAULT_SOCKET_PATH, UnixSocketSettings
from app.docker_engine.versioning import (
    API_VERSION_FLOOR,
    CLIENT_MAX_API_VERSION,
    parse_api_version,
)
from tests.docker_engine.helpers import ENV_ID

pytestmark = pytest.mark.integration

UNIX_SCHEME = "unix://"


def _socket_path() -> str | None:
    if sys.platform == "win32":
        return None
    host = os.environ.get("DOCKER_HOST", "")
    candidate = (
        host.removeprefix(UNIX_SCHEME) if host.startswith(UNIX_SCHEME) else DEFAULT_SOCKET_PATH
    )
    return candidate if Path(candidate).is_socket() else None


@pytest.fixture
async def live_client() -> AsyncIterator[EngineClient]:
    """Return a negotiated client on the local Docker socket, or skip the test."""
    path = _socket_path()
    if path is None:
        pytest.skip("no local Docker socket")
    client = EngineClient(ENV_ID, UnixSocketSettings(path).create_transport())
    try:
        await client.negotiate()
    except EnvironmentUnreachable as exc:
        await client.aclose()
        pytest.skip(f"Docker socket present but unreachable: {exc.reason}")
    yield client
    await client.aclose()


async def test_a_real_daemon_negotiates_a_supported_version_and_answers_typed_calls(
    live_client: EngineClient,
) -> None:
    info = await live_client.system.info()
    containers = await live_client.containers.list(all=True)

    assert live_client.daemon is not None
    pinned = parse_api_version(live_client.daemon.negotiated_api_version)
    assert API_VERSION_FLOOR <= pinned <= CLIENT_MAX_API_VERSION
    assert info.server_version == live_client.daemon.engine_version
    assert isinstance(containers, list)
