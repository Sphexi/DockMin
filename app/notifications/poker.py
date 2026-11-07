"""Brings the notification sender's next run forward when deliveries are queued."""

import asyncio
from collections.abc import Awaitable, Callable

from app.logging import get_logger

logger = get_logger(__name__)

type Expedite = Callable[[], Awaitable[object]]

RETRY_SECONDS = 1.0


class NotificationPoker:
    """Asks the scheduler to run the sender now, with at most one request in flight.

    A poke while a request is in flight is folded into one more request. A poke that arrives while
    the sender is already running cannot start another run; `take` lets the running pass see it,
    and a poke still untaken after a refused request is requested once more.
    """

    def __init__(self, *, retry_seconds: float = RETRY_SECONDS) -> None:
        self._retry_seconds = retry_seconds
        self._expedite: Expedite | None = None
        self._task: asyncio.Task[None] | None = None
        self._wanted = False
        self._arrived = False
        self._closed = False

    def bind(self, expedite: Expedite) -> None:
        """Set how a run is requested; pokes before this wait for the sender's first run."""
        self._expedite = expedite

    def poke(self) -> None:
        """Note that deliveries were queued; runs on the event loop and never raises."""
        if self._closed:
            return
        self._arrived = True
        self._wanted = True
        if self._task is None and self._expedite is not None:
            self._task = asyncio.get_running_loop().create_task(self._drain())

    def take(self) -> bool:
        """Report whether a poke arrived since the last call, and forget it."""
        arrived, self._arrived = self._arrived, False
        return arrived

    async def aclose(self) -> None:
        """Stop requesting runs and wait for a request in flight to end."""
        self._closed = True
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _drain(self) -> None:
        try:
            while self._wanted and not self._closed:
                self._wanted = False
                expedite = self._expedite
                if expedite is None:
                    return
                try:
                    moved = await expedite()
                    if moved is False and self._arrived:
                        await asyncio.sleep(self._retry_seconds)
                        if self._arrived and not self._closed:
                            await expedite()
                except Exception as error:
                    logger.warning(
                        "notification_poke_failed", extra={"error": type(error).__name__}
                    )
        finally:
            self._task = None
