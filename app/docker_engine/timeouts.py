"""Per-call timeout profiles; passed as arguments, never held as module state."""

from dataclasses import dataclass, replace

import httpx

_WAIT_MARGIN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Seconds for each phase of a request; None disables that phase's limit."""

    connect: float | None
    read: float | None
    write: float | None
    pool: float | None

    @classmethod
    def standard(cls) -> "Timeouts":
        """Fail fast on an unreachable host and on ordinary calls."""
        return cls(connect=5.0, read=30.0, write=10.0, pool=5.0)

    @classmethod
    def long(cls) -> "Timeouts":
        """Allow operations such as pulls and stop-with-grace-period to stay silent for a while."""
        return cls(connect=5.0, read=600.0, write=10.0, pool=5.0)

    @classmethod
    def long_for(cls, wait_seconds: float) -> "Timeouts":
        """Allow the long profile, or `wait_seconds` plus a margin when the daemon waits longer."""
        base = cls.long()
        read = max(base.read or 0.0, wait_seconds + _WAIT_MARGIN_SECONDS)
        return replace(base, read=read)

    @classmethod
    def stream(cls) -> "Timeouts":
        """No read limit: a quiet log or events stream is healthy, not stalled."""
        return cls(connect=5.0, read=None, write=10.0, pool=5.0)

    def to_httpx(self) -> httpx.Timeout:
        """Convert to the httpx representation."""
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.write, pool=self.pool)
