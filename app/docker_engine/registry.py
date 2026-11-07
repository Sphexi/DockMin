"""One long-lived Engine client per environment, replaced when its connection changes."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.docker_engine.client import EngineClient
from app.docker_engine.transport import ConnectionSettings, TransportBundle, create_transport
from app.logging import get_logger

_log = get_logger(__name__)


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    settings: ConnectionSettings
    client: EngineClient


class EngineClientRegistry:
    """Hands out one negotiated EngineClient per environment id."""

    def __init__(
        self, bundle_factory: Callable[[ConnectionSettings], TransportBundle] = create_transport
    ) -> None:
        self._bundle_factory = bundle_factory
        self._entries: dict[int, _Entry] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(
        self, environment_id: int, settings: ConnectionSettings, fingerprint: str
    ) -> EngineClient:
        """Return the live client, replacing it when the fingerprint or the settings changed.

        The fingerprint must cover the endpoint as well as the TLS material: `settings` is also
        compared, but its equality ignores the PEM text, so the fingerprint is the only thing that
        notices a changed certificate. Negotiation runs before a new client is returned and raises
        VersionUnsupported from here; a client that fails to negotiate is closed and not cached.
        """
        async with self._lock_for(environment_id):
            entry = self._entries.get(environment_id)
            if (
                entry is not None
                and entry.fingerprint == fingerprint
                and entry.settings == settings
                and not entry.client.closed
            ):
                return entry.client

            return await self._install(environment_id, fingerprint, settings, entry)

    async def client_for(
        self,
        environment_id: int,
        fingerprint: str,
        settings_factory: Callable[[], ConnectionSettings | Awaitable[ConnectionSettings]],
    ) -> EngineClient:
        """Return the live client for a matching fingerprint without building any settings.

        `settings_factory` runs only on a miss, so a hit never decrypts the client key. The
        fingerprint is the only comparison here and must cover the endpoint and the TLS material
        as they are stored. A factory that raises leaves the existing entry untouched.
        """
        entry = self._entries.get(environment_id)
        if entry is not None and entry.fingerprint == fingerprint and not entry.client.closed:
            return entry.client
        async with self._lock_for(environment_id):
            entry = self._entries.get(environment_id)
            if entry is not None and entry.fingerprint == fingerprint and not entry.client.closed:
                return entry.client
            built = settings_factory()
            settings = await built if inspect.isawaitable(built) else built
            return await self._install(environment_id, fingerprint, settings, entry)

    async def _install(
        self,
        environment_id: int,
        fingerprint: str,
        settings: ConnectionSettings,
        previous: _Entry | None,
    ) -> EngineClient:
        client = EngineClient(environment_id, self._bundle_factory(settings), settings)
        try:
            await client.negotiate()
        except BaseException:
            await client.aclose()
            raise

        self._entries[environment_id] = _Entry(fingerprint, settings, client)
        if previous is not None:
            _log.info("engine client replaced", extra={"environment_id": environment_id})
            await previous.client.aclose()
        return client

    async def invalidate(self, environment_id: int) -> None:
        """Close and forget an environment's client, if it has one."""
        async with self._lock_for(environment_id):
            entry = self._entries.pop(environment_id, None)
            if entry is not None:
                await entry.client.aclose()

    async def aclose(self) -> None:
        """Close every client; call once at shutdown."""
        for environment_id in list(self._entries):
            await self.invalidate(environment_id)
        # Shutdown only: dropping a lock while a caller waits on it would let two callers race.
        self._locks.clear()

    def _lock_for(self, environment_id: int) -> asyncio.Lock:
        return self._locks.setdefault(environment_id, asyncio.Lock())
