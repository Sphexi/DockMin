"""Per-address rate limit on requests that resolve to no signed-in principal."""

import ipaddress
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection

from app.config import Settings
from app.logging import LOG_INTERVAL_SECONDS, get_logger
from app.paths import HEALTH_PATH, READY_PATH
from app.tls import served_over_tls

ANONYMOUS_BURST: Final = 60
ANONYMOUS_REFILL_PER_SECOND: Final = 1.0
MAX_TRACKED_ADDRESSES: Final = 10_000
PROBE_PATHS: Final = frozenset({HEALTH_PATH, READY_PATH})
IPV6_PREFIX_BITS: Final = 64

logger = get_logger(__name__)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


def bucket_key(address: str) -> str:
    """Return the key an address is counted under: an IPv6 address by its /64, others as given."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network((parsed, IPV6_PREFIX_BITS), strict=False))
    return str(parsed)


class AddressRateLimiter:
    """Token buckets per client address, bounded in number with the least recently used dropped."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._logged_at: float | None = None

    def take(self, address: str) -> float | None:
        """Take a token; return None when admitted, else the seconds until one is available."""
        now = self._clock()
        key = bucket_key(address)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(float(ANONYMOUS_BURST), now)
            self._buckets[key] = bucket
            if len(self._buckets) > MAX_TRACKED_ADDRESSES:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(
                float(ANONYMOUS_BURST), bucket.tokens + elapsed * ANONYMOUS_REFILL_PER_SECOND
            )
            bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return None
        return (1.0 - bucket.tokens) / ANONYMOUS_REFILL_PER_SECOND

    def log_due(self) -> bool:
        """Report whether a refusal may be logged now; at most one is logged a minute."""
        now = self._clock()
        if self._logged_at is not None and now - self._logged_at < LOG_INTERVAL_SECONDS:
            return False
        self._logged_at = now
        return True


def unenforced_listener(settings: Settings) -> str | None:
    """Name a listener that serves the application and sees no per-client address, if any."""
    if settings.trusted_proxies:
        return None
    return "http" if settings.effective_http_mode == "serve" else None


def enforcement_applies(connection: HTTPConnection) -> bool:
    """Report whether client addresses are per-client here, so a refusal is safe to apply."""
    settings: Settings = connection.app.state.settings
    return bool(settings.trusted_proxies) or served_over_tls(connection)


async def limit_anonymous(connection: HTTPConnection) -> None:
    """Spend a token for a request that resolved to no principal; refuse it when none is left."""
    if getattr(connection.state, "principal", None) is not None:
        return
    if connection.scope["type"] == "http" and connection.url.path in PROBE_PATHS:
        return
    limiter: AddressRateLimiter = connection.app.state.rate_limiter
    address = connection.client.host if connection.client else "unknown"
    wait = limiter.take(address)
    if wait is None:
        return
    enforced = enforcement_applies(connection)
    if limiter.log_due():
        logger.warning("rate_limit_exceeded", extra={"enforced": enforced})
    if not enforced:
        return
    if connection.scope["type"] == "websocket":
        raise WebSocketException(code=1008)
    raise HTTPException(status_code=429, headers={"Retry-After": str(math.ceil(wait))})
