"""Credentials that outlive a password change or a forced sign-out are a recovery hole."""

from collections.abc import Awaitable, Callable

import pytest

from app.auth.errors import LockedOut, TokenNotFound, ValidationFailed
from app.auth.principal import PasswordCredentials, Principal
from app.auth.services import AuthServices
from app.auth.users import CHANGED_SINCE_REVIEW
from app.db.engine import Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.scheduled_job import ScheduledJob, ScheduledJobRepository
from app.db.repositories.webhook import WebhookRepository
from tests.auth.test_api_tokens import issue
from tests.auth_support import (
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    NEW_PASSWORD,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    read_events,
)
from tests.support import MAX_LOGIN_FAILURES

WRONG_PASSWORD = "not-the-password-at-all"
OWNED_TOKENS = 2
INTERNAL = "internal"
WEBHOOK_SUBSYSTEM = "webhook-receiver"
WEBHOOK_PREFIX = "abcdefgh"
WEBHOOK_HASH = "0" * 64
REPLACEMENT_PREFIX = "ijklmnop"
ROTATED_PREFIX = "qrstuvwx"
ARMED_AT = "2030-01-01T00:00:00Z"

type Operation = Callable[[AuthServices, Principal, Principal], Awaitable[object]]


async def reset(services: AuthServices, administrator: Principal, owner: Principal) -> None:
    await services.user_admin.reset_password(
        administrator, STANDARD_ID, new_password=NEW_PASSWORD, remote_address=CLIENT_ADDRESS
    )


async def end_sessions(
    services: AuthServices, administrator: Principal, owner: Principal
) -> object:
    return await services.user_admin.revoke_sessions(
        administrator, STANDARD_ID, remote_address=CLIENT_ADDRESS
    )


async def change(services: AuthServices, administrator: Principal, owner: Principal) -> None:
    await services.user_admin.change_password(
        owner,
        current_password=STANDARD_PASSWORD,
        new_password=NEW_PASSWORD,
        new_password_confirm=NEW_PASSWORD,
        remote_address=CLIENT_ADDRESS,
    )


OPERATIONS = [
    pytest.param(reset, "password_reset", id="administrator-reset"),
    pytest.param(end_sessions, "sessions_revoked", id="end-sessions"),
    pytest.param(change, "password_change", id="own-change"),
]


@pytest.mark.parametrize(("operation", "action"), OPERATIONS)
async def test_every_token_of_the_user_stops_working_and_nobody_elses_does(
    services: AuthServices,
    db: Session,
    administrator: Principal,
    standard_user: Principal,
    operation: Operation,
    action: str,
) -> None:
    owned = [await issue(services, standard_user, f"mine-{n}") for n in range(OWNED_TOKENS)]
    others = await issue(services, administrator, "admin's own")

    await operation(services, administrator, standard_user)

    assert [await services.tokens.resolve(token.value) for token in owned] == [None] * OWNED_TOKENS
    assert await services.tokens.resolve(others.value) is not None
    event = next(e for e in await read_events(db) if e["action"] == action)
    assert event["detail"]["tokens_revoked"] == OWNED_TOKENS


async def armed_webhook(
    db: Session, owner_id: int, *, name: str = "hook", prefix: str = WEBHOOK_PREFIX
) -> tuple[AutomationRule, ScheduledJob]:
    """Insert a webhook rule with a live token, a call waiting and its job armed."""
    rule = await AutomationRuleRepository(db).insert(
        name=name,
        subsystem=WEBHOOK_SUBSYSTEM,
        environment_id=None,
        target_selector="",
        parameters="{}",
        is_enabled=False,
        created_by_user_id=owner_id,
    )
    webhooks = WebhookRepository(db)
    await webhooks.create(rule.id, prefix, WEBHOOK_HASH)
    job = await ScheduledJobRepository(db).insert(
        subsystem=WEBHOOK_SUBSYSTEM,
        rule_id=rule.id,
        schedule_kind="webhook",
        schedule_expression="",
        time_zone="UTC",
        overrun_policy="skip",
        catch_up=False,
        failure_threshold=None,
        next_run_at=ARMED_AT,
    )
    await webhooks.record_call(rule.id, "192.0.2.1", armed=True)
    return rule, job


async def assert_disarmed(db: Session, rule: AutomationRule, job: ScheduledJob) -> None:
    stored = await ScheduledJobRepository(db).get(job.id)
    webhook = await WebhookRepository(db).get(rule.id)
    assert stored is not None and webhook is not None
    assert (stored.next_run_at, webhook.pending_calls) == (None, 0)


@pytest.mark.parametrize(("operation", "action"), OPERATIONS)
async def test_the_webhook_tokens_of_the_user_are_revoked_as_recovery_and_counted(
    services: AuthServices,
    db: Session,
    administrator: Principal,
    standard_user: Principal,
    operation: Operation,
    action: str,
) -> None:
    rule, job = await armed_webhook(db, STANDARD_ID)

    await operation(services, administrator, standard_user)

    revoked = await WebhookRepository(db).get(rule.id)
    assert revoked is not None
    assert (revoked.is_revoked, revoked.revoked_reason) == (True, "account_recovery")
    await assert_disarmed(db, rule, job)
    event = next(e for e in await read_events(db) if e["action"] == action)
    assert event["detail"]["webhook_tokens_revoked"] == 1


async def test_revoking_or_rotating_a_webhook_disarms_its_job_and_clears_the_waiting_calls(
    db: Session, standard_user: Principal
) -> None:
    webhooks = WebhookRepository(db)
    revoked_rule, revoked_job = await armed_webhook(db, STANDARD_ID)
    rotated_rule, rotated_job = await armed_webhook(
        db, STANDARD_ID, name="other", prefix=REPLACEMENT_PREFIX
    )

    await webhooks.revoke(revoked_rule.id, expected_prefix=WEBHOOK_PREFIX, reason="manual")
    await webhooks.rotate(
        rotated_rule.id,
        expected_prefix=REPLACEMENT_PREFIX,
        prefix=ROTATED_PREFIX,
        token_hash=WEBHOOK_HASH,
    )

    await assert_disarmed(db, revoked_rule, revoked_job)
    await assert_disarmed(db, rotated_rule, rotated_job)


@pytest.mark.parametrize(("operation", "action"), OPERATIONS)
async def test_an_operation_with_no_tokens_to_revoke_records_a_zero_count(
    services: AuthServices,
    db: Session,
    administrator: Principal,
    standard_user: Principal,
    operation: Operation,
    action: str,
) -> None:
    await operation(services, administrator, standard_user)

    event = next(e for e in await read_events(db) if e["action"] == action)
    assert event["detail"]["tokens_revoked"] == 0
    assert event["detail"]["webhook_tokens_revoked"] == 0


async def test_a_refused_password_change_leaves_the_tokens_working(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    with pytest.raises(ValidationFailed):
        await services.user_admin.change_password(
            standard_user,
            current_password=WRONG_PASSWORD,
            new_password=NEW_PASSWORD,
            new_password_confirm=NEW_PASSWORD,
            remote_address=CLIENT_ADDRESS,
        )

    assert await services.tokens.resolve(issued.value) is not None


async def test_wrong_guesses_at_the_current_password_lock_the_change_out(
    services: AuthServices, db: Session, standard_user: Principal
) -> None:
    guesses = [
        await _guess(services, standard_user, WRONG_PASSWORD) for _ in range(MAX_LOGIN_FAILURES)
    ]

    locked = await _guess(services, standard_user, STANDARD_PASSWORD)

    assert [g.fields for g in guesses] == [
        {"current_password": "The current password is incorrect."}
    ] * MAX_LOGIN_FAILURES
    assert locked.fields == {"current_password": "Too many failed attempts. Try again later."}
    changes = [e for e in await read_events(db) if e["action"] == "password_change"]
    assert changes[-1]["detail"]["reason"] == "locked_out"
    assert all(e["result"] == "failure" for e in changes)


async def test_the_lock_a_password_change_causes_also_stops_signing_in(
    services: AuthServices, standard_user: Principal
) -> None:
    for _ in range(MAX_LOGIN_FAILURES):
        await _guess(services, standard_user, WRONG_PASSWORD)

    with pytest.raises(LockedOut):
        await services.login.login(
            INTERNAL,
            PasswordCredentials(STANDARD_USERNAME, STANDARD_PASSWORD),
            remote_address=CLIENT_ADDRESS,
            user_agent="test",
        )


async def _guess(services: AuthServices, owner: Principal, password: str) -> ValidationFailed:
    with pytest.raises(ValidationFailed) as raised:
        await services.user_admin.change_password(
            owner,
            current_password=password,
            new_password=NEW_PASSWORD,
            new_password_confirm=NEW_PASSWORD,
            remote_address=CLIENT_ADDRESS,
        )
    return raised.value


async def test_deleting_an_account_that_was_renamed_since_the_review_is_refused_and_deletes_nothing(
    services: AuthServices, administrator: Principal, standard_user: Principal
) -> None:
    await services.user_admin.update_profile(
        standard_user, username="renamed-after-review", remote_address=CLIENT_ADDRESS
    )

    with pytest.raises(ValidationFailed) as refused:
        await services.user_admin.delete(
            administrator,
            STANDARD_ID,
            expected_name=STANDARD_USERNAME,
            remote_address=CLIENT_ADDRESS,
        )

    assert refused.value.fields == {"name": CHANGED_SINCE_REVIEW}
    assert await services.users.get(STANDARD_ID) is not None


async def test_a_token_revoked_through_another_users_page_is_not_found(
    services: AuthServices, administrator: Principal, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    with pytest.raises(TokenNotFound):
        await services.tokens.revoke(
            administrator,
            issued.record.id,
            remote_address=CLIENT_ADDRESS,
            owner_id=administrator.user_id,
        )

    assert await services.tokens.resolve(issued.value) is not None
    _ = ADMIN_USERNAME
