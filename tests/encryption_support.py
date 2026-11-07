"""A stand-in for the re-key pass that holds still after its first progress report."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from app.db.repositories.rekey import DEFAULT_BATCH_SIZE, RekeyPass, RekeyProgress


class Steps:
    """Stands in for the pass class; the first pass built pauses once until released."""

    def __init__(self) -> None:
        self.armed = True
        self.reached = asyncio.Event()
        self.proceed = asyncio.Event()

    def __call__(
        self,
        session: Any,
        vault: Any,
        batch_size: int = DEFAULT_BATCH_SIZE,
        *,
        verify_current: bool = False,
    ) -> RekeyPass:
        return PausingPass(self, session, vault, batch_size, verify_current=verify_current)


class PausingPass(RekeyPass):
    """The real pass, held after its first progress report until the test lets it go."""

    def __init__(self, steps: Steps, *arguments: Any, verify_current: bool) -> None:
        super().__init__(*arguments, verify_current=verify_current)
        self._steps = steps

    async def run(self) -> AsyncGenerator[RekeyProgress, None]:
        async for progress in super().run():
            yield progress
            await self._hold()

    async def verify(self) -> AsyncGenerator[RekeyProgress, None]:
        async for progress in super().verify():
            yield progress
            await self._hold()

    async def _hold(self) -> None:
        if self._steps.armed:
            self._steps.armed = False
            self._steps.reached.set()
            await self._steps.proceed.wait()
