"""Decoded streams over an Engine connection, and the reconnecting events watcher."""

import asyncio
import json
import math
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass

from app.docker_engine.engine_http import AddressRefusedUnreachable, EngineHttp, Params
from app.docker_engine.errors import EngineError, EnvironmentUnreachable
from app.docker_engine.frames import Decoder, FrameDecodeError, JsonLinesDecoder, TruncatedStream
from app.docker_engine.models import EngineEvent
from app.docker_engine.timeouts import Timeouts
from app.logging import get_logger

_log = get_logger(__name__)

# An event stamped further ahead than this is treated as carrying no usable time.
_MAX_EVENT_CLOCK_SKEW_SECONDS = 60
_NANOS_PER_SECOND = 1_000_000_000
EVENT_LINE_BYTES = 256 * 1024
# Past the backoff's exponent cap, so the delay is the policy's maximum.
_SATURATED_ATTEMPT = 1_000_000


async def decoded_stream[T](
    http: EngineHttp,
    decoder: Decoder[T],
    method: str,
    path: str,
    *,
    params: Params | None = None,
    headers: Mapping[str, str] | None = None,
    timeouts: Timeouts | None = None,
) -> AsyncGenerator[T, None]:
    """Yield complete objects; a stream cut mid-object raises."""
    async with aclosing(
        http.stream_bytes(method, path, params=params, headers=headers, timeouts=timeouts)
    ) as chunks:
        try:
            async for chunk in chunks:
                for item in decoder.feed(chunk):
                    yield item
            decoder.finish()
        except TruncatedStream as exc:
            raise EnvironmentUnreachable(http.environment_id, str(exc)) from None
        except FrameDecodeError as exc:
            raise EngineError(http.environment_id, str(exc)) from None


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential reconnect delay, capped and jittered so hosts do not retry in step."""

    initial: float = 1.0
    factor: float = 2.0
    maximum: float = 30.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.initial <= 0:
            raise ValueError("backoff initial delay must be positive")
        if self.factor < 1:
            raise ValueError("backoff factor must be at least 1")
        if self.maximum < self.initial:
            raise ValueError("backoff maximum must not be below the initial delay")
        if not 0 <= self.jitter < 1:
            raise ValueError("backoff jitter must be at least 0 and below 1")

    def delay(self, attempt: int) -> float:
        """Return the seconds to wait before reconnect number `attempt` (from zero)."""
        # The exponent is capped at the first power that reaches `maximum`; beyond it the float
        # power would overflow long before the cap could apply.
        if self.factor > 1:
            steps = math.ceil(
                (math.log(self.maximum) - math.log(self.initial)) / math.log(self.factor)
            )
        else:
            steps = 0
        base = min(self.maximum, self.initial * self.factor ** min(max(attempt, 0), steps))
        return max(0.0, base * (1 + random.uniform(-self.jitter, self.jitter)))


def _plausible_times(event: EngineEvent) -> tuple[int, int]:
    """Return the event's (nanosecond, second) stamps, with any implausibly future one as zero."""
    now_ns = time.time_ns()
    nano, seconds = event.time_nano, event.time
    if nano > now_ns + _MAX_EVENT_CLOCK_SKEW_SECONDS * _NANOS_PER_SECOND:
        nano = 0
    if seconds > now_ns // _NANOS_PER_SECOND + _MAX_EVENT_CLOCK_SKEW_SECONDS:
        seconds = 0
    return nano, seconds


async def watch_events(
    http: EngineHttp,
    *,
    since: int | None = None,
    filters: Mapping[str, list[str]] | None = None,
    backoff: BackoffPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncGenerator[EngineEvent, None]:
    """Yield events until the client closes, reconnecting after each unreachable-host failure.

    Reconnects resume from the last event's time and drop the replayed duplicates. Errors other
    than EnvironmentUnreachable propagate. A refused endpoint address is retried at the maximum
    delay, since it cannot succeed until the endpoint or the policy changes. An event line over
    `EVENT_LINE_BYTES` raises EngineError. Wrap in `contextlib.aclosing` to release the stream.
    """
    policy = backoff or BackoffPolicy()
    attempt = 0
    last_nano = 0
    last_seconds = since
    reconnecting = False
    while not http.closed:
        refused = False
        try:
            if reconnecting:
                await http.negotiate()
            params: dict[str, str | int | bool] = {}
            if last_seconds is not None:
                params["since"] = last_seconds
            if filters:
                params["filters"] = json.dumps(filters)
            async with aclosing(
                decoded_stream(
                    http, JsonLinesDecoder(EVENT_LINE_BYTES), "GET", "/events", params=params
                )
            ) as raw:
                async for payload in raw:
                    event = http.parse(EngineEvent, payload)
                    # A stamp from the future must not become the high-water mark: every genuine
                    # event after it would then look like a replay.
                    plausible_nano, plausible_seconds = _plausible_times(event)
                    if plausible_nano and plausible_nano <= last_nano:
                        continue
                    attempt = 0
                    last_nano = max(last_nano, plausible_nano)
                    last_seconds = plausible_seconds or last_seconds
                    yield event
        except EnvironmentUnreachable as exc:
            refused = isinstance(exc, AddressRefusedUnreachable)
            _log.warning(
                "events stream refused" if refused else "events stream lost",
                extra={"environment_id": http.environment_id, "reason": exc.reason},
            )
        if http.closed:
            return
        await sleep(policy.delay(_SATURATED_ATTEMPT if refused else attempt))
        attempt += 1
        reconnecting = True
