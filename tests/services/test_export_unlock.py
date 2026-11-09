"""The unlock gate: one derivation at a time and a budget of attempts per session."""

import asyncio

import pytest

from app.services.export_unlock import (
    UNLOCK_ATTEMPT_LIMIT,
    UnlockAttemptsExhausted,
    UnlockGate,
    shared_unlock_gate,
)

SESSION = 7
OTHER_SESSION = 8


async def test_the_slot_admits_one_holder_at_a_time() -> None:
    gate = UnlockGate()
    inside = 0
    peak = 0

    async def derive() -> None:
        nonlocal inside, peak
        async with gate.slot():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            inside -= 1

    await asyncio.gather(*(derive() for _ in range(5)))

    assert peak == 1


def test_the_shared_gate_is_one_object() -> None:
    assert shared_unlock_gate() is shared_unlock_gate()


def test_each_reservation_takes_an_attempt_and_reports_what_is_left() -> None:
    gate = UnlockGate()

    first = gate.reserve(SESSION)
    second = gate.reserve(SESSION)

    assert (first.remaining, second.remaining) == (
        UNLOCK_ATTEMPT_LIMIT - 1,
        UNLOCK_ATTEMPT_LIMIT - 2,
    )
    assert gate.remaining(SESSION) == UNLOCK_ATTEMPT_LIMIT - 2
    assert gate.remaining(OTHER_SESSION) == UNLOCK_ATTEMPT_LIMIT


def test_reservations_made_together_cannot_exceed_the_limit() -> None:
    gate = UnlockGate()
    reserved = [gate.reserve(SESSION) for _ in range(UNLOCK_ATTEMPT_LIMIT)]

    with pytest.raises(UnlockAttemptsExhausted):
        gate.reserve(SESSION)

    assert reserved[-1].remaining == 0
    assert gate.reserve(OTHER_SESSION).remaining == UNLOCK_ATTEMPT_LIMIT - 1


def test_a_refund_returns_the_attempt_once() -> None:
    gate = UnlockGate()
    kept = gate.reserve(SESSION)
    refunded = gate.reserve(SESSION)

    refunded.refund()
    refunded.refund()

    assert gate.remaining(SESSION) == UNLOCK_ATTEMPT_LIMIT - 1
    assert kept.remaining == UNLOCK_ATTEMPT_LIMIT - 1


async def test_concurrent_unlocks_of_one_session_are_counted_before_any_derivation_ends() -> None:
    gate = UnlockGate(attempt_limit=2)
    started = asyncio.Event()
    release = asyncio.Event()

    async def unlock() -> str:
        try:
            gate.reserve(SESSION)
        except UnlockAttemptsExhausted:
            return "limited"
        async with gate.slot():
            started.set()
            await release.wait()
        return "ran"

    tasks = [asyncio.create_task(unlock()) for _ in range(4)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()

    assert sorted(await asyncio.gather(*tasks)) == ["limited", "limited", "ran", "ran"]
