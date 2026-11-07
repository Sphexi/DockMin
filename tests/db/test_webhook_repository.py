"""The webhook repository: the calls waiting on a webhook and the take that empties them."""

from dataclasses import dataclass

import pytest

from app.db.engine import Session
from app.db.repositories import UserRepository
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.webhook import (
    MAX_ADDRESS_CHARS,
    MAX_PENDING_CALLS,
    PendingCalls,
    WebhookRepository,
)
from tests.db.helpers import CREATED_AT, PASSWORD_HASH, at

SUBSYSTEM = "webhook-receiver"
PREFIX = "AbCd1234"
TOKEN_HASH = "ab" * 32
FIRST_ADDRESS = "192.0.2.1"
LAST_ADDRESS = "192.0.2.2"
CALLS = 3
NO_WEBHOOK_RULE = 4242


@dataclass(slots=True)
class Hook:
    repository: WebhookRepository
    rule_id: int


@pytest.fixture
async def hook(session: Session) -> Hook:
    owner = await UserRepository(session, clock=at(CREATED_AT)).create(
        "hook-owner", PASSWORD_HASH, "administrator"
    )
    rule = await AutomationRuleRepository(session, at(CREATED_AT)).insert(
        name="hook",
        subsystem=SUBSYSTEM,
        environment_id=None,
        target_selector="",
        parameters="{}",
        is_enabled=False,
        created_by_user_id=owner.id,
    )
    repository = WebhookRepository(session, at(CREATED_AT))
    await repository.create(rule.id, PREFIX, TOKEN_HASH)
    return Hook(repository, rule.id)


async def test_taking_the_pending_calls_returns_the_count_and_last_caller_and_empties_the_count(
    hook: Hook,
) -> None:
    await hook.repository.record_call(hook.rule_id, FIRST_ADDRESS, armed=True)
    await hook.repository.record_call(hook.rule_id, FIRST_ADDRESS, armed=False)
    await hook.repository.record_call(hook.rule_id, LAST_ADDRESS, armed=False)

    taken = await hook.repository.take_pending(hook.rule_id)
    again = await hook.repository.take_pending(hook.rule_id)

    stored = await hook.repository.get(hook.rule_id)
    assert taken == PendingCalls(CALLS, LAST_ADDRESS)
    assert again == PendingCalls(0, LAST_ADDRESS)
    assert stored is not None and stored.pending_calls == 0


async def test_taking_the_pending_calls_of_a_rule_with_no_webhook_finds_none(hook: Hook) -> None:
    assert await hook.repository.take_pending(NO_WEBHOOK_RULE) == PendingCalls(0, None)


async def test_a_call_that_arms_the_job_restarts_the_count_at_one(hook: Hook) -> None:
    await hook.repository.record_call(hook.rule_id, FIRST_ADDRESS, armed=False)
    await hook.repository.record_call(hook.rule_id, FIRST_ADDRESS, armed=False)

    await hook.repository.record_call(hook.rule_id, LAST_ADDRESS, armed=True)

    stored = await hook.repository.get(hook.rule_id)
    assert stored is not None and stored.pending_calls == 1


async def test_the_count_stops_at_the_ceiling_and_a_long_address_is_cut(
    hook: Hook, session: Session
) -> None:
    await session.execute(
        "UPDATE webhook SET pending_calls = ? WHERE rule_id = ?",
        (MAX_PENDING_CALLS, hook.rule_id),
    )

    await hook.repository.record_call(hook.rule_id, "a" * (MAX_ADDRESS_CHARS + 1), armed=False)

    stored = await hook.repository.get(hook.rule_id)
    assert stored is not None
    assert (stored.pending_calls, stored.last_call_address) == (
        MAX_PENDING_CALLS,
        "a" * MAX_ADDRESS_CHARS,
    )
