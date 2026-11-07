"""The poker: at most one request in flight, no poke lost, and nothing raised."""

import asyncio
from collections.abc import Callable
from typing import Any

from app.notifications.poker import NotificationPoker

RETRY_SECONDS = 0.01


async def test_a_poke_before_a_request_can_be_made_waits_for_the_first_run_to_take_it() -> None:
    poker = NotificationPoker()

    poker.poke()

    assert poker.take() is True
    assert poker.take() is False


async def test_pokes_during_a_request_are_folded_into_one_more_request() -> None:
    calls: list[int] = []
    started, release, folded = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def expedite() -> bool:
        calls.append(1)
        if len(calls) == 1:
            started.set()
            await release.wait()
        else:
            folded.set()
        return True

    poker = NotificationPoker()
    poker.bind(expedite)
    poker.poke()
    await started.wait()
    poker.poke()
    poker.poke()
    poker.poke()
    release.set()
    await folded.wait()
    await poker.aclose()

    assert len(calls) == 2


async def test_a_request_refused_while_a_poke_is_untaken_is_made_again() -> None:
    answers = iter([False, True])
    calls: list[int] = []
    repeated = asyncio.Event()

    async def expedite() -> bool:
        calls.append(1)
        if len(calls) == 2:
            repeated.set()
        return next(answers)

    poker = NotificationPoker(retry_seconds=RETRY_SECONDS)
    poker.bind(expedite)

    poker.poke()
    await repeated.wait()
    await poker.aclose()

    assert len(calls) == 2


async def test_a_refusal_is_not_repeated_once_the_running_pass_has_taken_the_poke() -> None:
    calls: list[int] = []
    called = asyncio.Event()
    poker = NotificationPoker(retry_seconds=RETRY_SECONDS)

    async def expedite() -> bool:
        calls.append(1)
        poker.take()
        called.set()
        return False

    poker.bind(expedite)
    poker.poke()
    await called.wait()
    await poker.aclose()

    assert len(calls) == 1


async def test_a_request_that_raises_is_logged_and_does_not_escape(
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    called = asyncio.Event()

    async def expedite() -> bool:
        called.set()
        raise RuntimeError("the scheduler is closed")

    poker = NotificationPoker()
    poker.bind(expedite)

    poker.poke()
    await called.wait()
    await poker.aclose()

    [record] = logged("notification_poke_failed")
    assert record["error"] == "RuntimeError"


async def test_a_closed_poker_makes_no_request() -> None:
    calls: list[int] = []

    async def expedite() -> bool:
        calls.append(1)
        return True

    poker = NotificationPoker()
    poker.bind(expedite)
    await poker.aclose()

    poker.poke()
    await poker.aclose()

    assert calls == [] and poker.take() is False
