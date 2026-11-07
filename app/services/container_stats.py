"""Container stats: one sample for pages without scripting, and the bounded live stream."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

import anyio

from app.auth.errors import ProductError, ResourceNotFound
from app.auth.principal import Principal
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import ContainerStats
from app.services.access import ResourceKey
from app.services.authorisation import Authoriser
from app.services.containers import parse_engine_time
from app.services.object_inspector import EngineObjectInspector, unavailable_from
from app.services.object_locks import StreamSlot, StreamSlots

MIN_INTERVAL_SECONDS = 2.0
KEEPALIVE_SECONDS = 15.0
MAX_STREAM_SECONDS = 300.0
RETRY_MILLISECONDS = 3000
INLINE_DECODE_BYTES = 16 * 1024

type StreamEnd = Literal["lifetime", "finished", "unavailable", "closed"]


class ContainerNotRunning(ProductError):
    """Stats were asked of a container that is not running."""

    def __init__(self) -> None:
        super().__init__("The container is not running.")


@dataclass(frozen=True, slots=True)
class StatsLimits:
    """Timing bounds of a stream; the defaults are the product's."""

    interval: float = MIN_INTERVAL_SECONDS
    keepalive: float = KEEPALIVE_SECONDS
    lifetime: float = MAX_STREAM_SECONDS


@dataclass(frozen=True, slots=True)
class StatsSample:
    """One reading of a container's resource use; cpu_percent is None without a baseline frame."""

    read_at: datetime | None
    cpu_percent: float | None
    memory_used: int
    memory_limit: int
    memory_percent: float | None
    network_received: int
    network_sent: int
    block_read: int
    block_written: int
    processes: int


class KeepAlive:
    """Marks a quiet interval of a stream, so the response can send a comment line."""

    __slots__ = ()


KEEP_ALIVE = KeepAlive()


def sample_of(frame: ContainerStats) -> StatsSample:
    """Reduce one Engine frame to the figures the panel shows."""
    cpu, baseline = frame.cpu_stats, frame.precpu_stats
    cpu_delta = cpu.cpu_usage.total_usage - baseline.cpu_usage.total_usage
    system_delta = cpu.system_cpu_usage - baseline.system_cpu_usage
    percent: float | None = None
    if baseline.system_cpu_usage > 0 and system_delta > 0 and cpu_delta >= 0:
        percent = cpu_delta / system_delta * max(cpu.online_cpus, 1) * 100
    memory = frame.memory_stats
    cache = memory.stats.get("inactive_file", memory.stats.get("total_inactive_file", 0))
    used = memory.usage - cache if 0 <= cache <= memory.usage else memory.usage
    transfers = {"read": 0, "write": 0}
    for entry in frame.blkio_stats.io_service_bytes_recursive:
        operation = entry.op.casefold()
        if operation in transfers:
            transfers[operation] += max(entry.value, 0)
    return StatsSample(
        read_at=parse_engine_time(frame.read),
        cpu_percent=percent,
        memory_used=max(used, 0),
        memory_limit=max(memory.limit, 0),
        memory_percent=used / memory.limit * 100 if memory.limit > 0 and used >= 0 else None,
        network_received=sum(max(n.rx_bytes, 0) for n in frame.networks.values()),
        network_sent=sum(max(n.tx_bytes, 0) for n in frame.networks.values()),
        block_read=transfers["read"],
        block_written=transfers["write"],
        processes=max(frame.pids_stats.current, 0),
    )


class StatsStream:
    """Live samples of one container, throttled and bounded in time.

    Holds no database session. Iterate it and call `aclose` on every path: the stream slot is
    given back and the Engine response closed there. `end_reason` says why iteration ended:
    "lifetime" means the caller should invite a reconnect.
    """

    def __init__(
        self,
        client: EngineClient,
        container_id: str,
        slot: StreamSlot,
        limits: StatsLimits,
    ) -> None:
        self._client = client
        self._container_id = container_id
        self._slot = slot
        self._limits = limits
        self._events = self._generate()
        self.end_reason: StreamEnd | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StatsSample | KeepAlive:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        """End the stream: close the Engine response and give the slot back; safe to repeat."""
        try:
            with anyio.CancelScope(shield=True):
                await self._events.aclose()
        finally:
            self._slot.close()
            if self.end_reason is None:
                self.end_reason = "closed"

    async def _decode(self, line: bytes) -> ContainerStats:
        if len(line) <= INLINE_DECODE_BYTES:
            return self._client.containers.parse_stats(line)
        return await asyncio.to_thread(self._client.containers.parse_stats, line)

    async def _generate(self) -> AsyncGenerator[StatsSample | KeepAlive, None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._limits.lifetime
        frames = self._client.containers.stats_lines(self._container_id)

        async def next_line() -> bytes | None:
            try:
                return await anext(frames)
            except StopAsyncIteration:
                return None

        pending: asyncio.Future[bytes | None] | None = None
        last_sent: float | None = None
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.end_reason = "lifetime"
                    return
                if pending is None:
                    pending = asyncio.ensure_future(next_line())
                done, _ = await asyncio.wait(
                    {pending}, timeout=min(self._limits.keepalive, remaining)
                )
                if not done:
                    if deadline - loop.time() > 0:
                        yield KEEP_ALIVE
                    continue
                finished, pending = pending, None
                try:
                    line = finished.result()
                except EngineError:
                    self.end_reason = "unavailable"
                    return
                if line is None:
                    self.end_reason = "finished"
                    return
                now = loop.time()
                if last_sent is not None and now - last_sent < self._limits.interval:
                    continue
                last_sent = now
                try:
                    frame = await self._decode(line)
                except EngineError:
                    self.end_reason = "unavailable"
                    return
                yield sample_of(frame)
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    if pending is not None:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    await frames.aclose()
            finally:
                self._slot.close()


class ContainerStatsService:
    """Authorises once, then hands out samples; the stream itself touches no session."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        slots: StreamSlots,
        limits: StatsLimits | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._slots = slots
        self._limits = limits or StatsLimits()

    async def sample(self, actor: Principal, environment_id: int, container_id: str) -> StatsSample:
        """Read one sample at read access; ContainerNotRunning when the container is not running.

        ResourceNotFound when the container is removed before the sample is read.
        """
        await self._require_running(actor, environment_id, container_id)
        client = await self._hosts.client(environment_id)
        try:
            frame = await client.containers.stats_once(container_id)
        except NotFound:
            raise ResourceNotFound("not found") from None
        except EngineError as error:
            raise unavailable_from(error) from None
        return sample_of(frame)

    async def open(self, actor: Principal, environment_id: int, container_id: str) -> StatsStream:
        """Authorise at read access and claim a stream slot; the caller must close the stream.

        TooManyStreams when the user or the process has no free slot. The slot is claimed last,
        so a refused caller never takes one.
        """
        await self._require_running(actor, environment_id, container_id)
        client = await self._hosts.client(environment_id)
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        return StatsStream(client, container_id, slot, self._limits)

    async def _require_running(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> None:
        key = ResourceKey(environment_id, "container", container_id)
        await self._authoriser.require_access(actor, key, "read")
        inspected = await self._inspector.inspect(environment_id, container_id)
        if inspected.state.status != "running":
            raise ContainerNotRunning
