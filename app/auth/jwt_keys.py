"""Verification keys: a fixed set, or a set kept fresh from a fetcher."""

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from app.auth.jwt_key_documents import VerificationKey
from app.logging import get_logger

REFRESH_AFTER: Final = 3600.0
MIN_REFRESH_INTERVAL: Final = 60.0
MAX_KEY_AGE: Final = 24 * 3600.0

_log = get_logger(__name__)


class KeyFetchError(Exception):
    """A key fetch that failed; `reason` is a short code and the text carries no URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeyRefused(Exception):
    """No key can verify the token; `reason` is the internal code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeyFetcher(Protocol):
    """Something that can fetch the current key set."""

    async def fetch(self) -> list[VerificationKey]:
        """Return the usable keys, or raise KeyFetchError."""
        ...


class KeyStore:
    """The current verification keys, kept fresh from a fetcher when there is one."""

    def __init__(
        self,
        *,
        fixed: Sequence[VerificationKey] = (),
        fetcher: KeyFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._monotonic = monotonic
        self._keys: tuple[VerificationKey, ...] | None = tuple(fixed) if fetcher is None else None
        self._fetched_at = 0.0
        self._last_attempt: float | None = None
        self._attempts = 0
        self._lock = asyncio.Lock()

    async def select(self, kid: str | None) -> VerificationKey:
        """Return the key a token naming `kid` is checked with, or raise KeyRefused."""
        keys = await self._current()
        chosen = _choose(keys, kid)
        if chosen is None and kid is not None and self._fetcher is not None:
            await self._refresh()
            keys = await self._current(refresh=False)
            chosen = _choose(keys, kid)
        if chosen is not None:
            return chosen
        if not keys:
            raise KeyRefused("keys_unavailable")
        raise KeyRefused("unknown_kid" if kid is not None else "kid_required")

    async def _current(self, *, refresh: bool = True) -> tuple[VerificationKey, ...]:
        if self._fetcher is None:
            return self._keys or ()
        if refresh and (self._keys is None or self._monotonic() - self._fetched_at > REFRESH_AFTER):
            await self._refresh()
        if self._keys is None or self._monotonic() - self._fetched_at > MAX_KEY_AGE:
            return ()
        return self._keys

    async def _refresh(self) -> None:
        assert self._fetcher is not None
        seen = self._attempts
        async with self._lock:
            if self._attempts != seen:
                return
            now = self._monotonic()
            if self._last_attempt is not None and now - self._last_attempt < MIN_REFRESH_INTERVAL:
                return
            self._attempts += 1
            self._last_attempt = now
            try:
                keys = await self._fetcher.fetch()
            except KeyFetchError as error:
                _log.warning("jwks_fetch_failed", extra={"reason": error.reason})
                return
            self._keys = tuple(keys)
            self._fetched_at = self._monotonic()


def _choose(keys: tuple[VerificationKey, ...], kid: str | None) -> VerificationKey | None:
    if kid is None:
        return keys[0] if len(keys) == 1 else None
    named = [key for key in keys if key.kid == kid]
    if len(named) == 1:
        return named[0]
    if not named and len(keys) == 1 and keys[0].kid is None:
        return keys[0]
    return None
