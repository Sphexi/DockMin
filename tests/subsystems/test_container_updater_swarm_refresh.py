"""container-updater renewing the stored registry credential of services bound to Amazon ECR."""

from datetime import timedelta
from typing import Any

import pytest

import app.subsystems.container_updater_swarm as swarm_path
from app.services.activity import UserActor
from app.services.image_bindings import BindingPullCredentials
from app.services.object_locks import LockHolder, ObjectKey
from app.services.registry_resolution import CredentialSetUnavailable
from app.subsystems.base import RunContext
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import counts
from tests.docker_engine.swarm_engine import plain_spec
from tests.image_support import digest
from tests.registry_support import add_ecr, bind_image, grant_environment
from tests.swarm_updater_support import (
    ECR_DB_REFERENCE,
    ECR_REFERENCE,
    REPORT,
    SERVICE,
    STACK,
    UPDATE,
    after_update,
    deployed_shop,
    ecr_service,
    image_of,
    inject_sleeper,
    paused,
    rows,
    shop_text,
)
from tests.updater_support import UPDATER

SIX_HOURS = 6 * 3600
TWO_DAYS = 2 * 24 * 3600
JUST_UNDER = SIX_HOURS - 1
SECOND_PASS_SECONDS = 61
REFRESHED = "service_credentials_refreshed"
MANY = 1
UPDATE_AND_REFRESH = 2


def ecr_shop_text() -> str:
    return shop_text(ECR_REFERENCE, ECR_DB_REFERENCE)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    inject_sleeper(monkeypatch)


async def test_a_dry_run_names_the_refresh_and_a_real_run_renews_the_credential_in_place(
    swarm: Any, settings: Any
) -> None:
    subject, _, sid = await ecr_service(swarm, settings)
    tasks = set(swarm.host.service_tasks(sid))
    _, job = await subject.rule(SERVICE, UPDATE)

    dry = await subject.run(job, dry=True)
    wrote = rows(subject)
    result = await subject.run(job)

    assert counts(dry)["would_refresh"] == "service web" and wrote == []
    assert (result.outcome, counts(result)["refreshed"]) == ("success", 1)
    assert "updated" not in counts(result)
    [row] = rows(subject, REFRESHED)
    assert (row["actor_subsystem"], row["detail"]["trigger"]) == (UPDATER, "refresh")
    assert row["detail"]["provider"] == "ecr"
    assert image_of(swarm.host, sid).startswith(f"{ECR_REFERENCE}@")
    service = swarm.host.services[sid]
    assert service["PreviousSpec"] == service["Spec"]
    assert swarm.host.auth_headers[-1][0] == "update"
    assert set(swarm.host.service_tasks(sid)) == tasks


async def test_a_renewal_re_derives_a_token_that_would_expire_before_the_next_renewal(
    swarm: Any, settings: Any
) -> None:
    subject, _, _ = await ecr_service(swarm, settings)
    _, job = await subject.rule(SERVICE, UPDATE)
    await subject.run(job)
    derived_after_first = swarm.registry.deriver.issued
    subject.advance(SIX_HOURS + 1)

    second = await subject.run(job)

    assert counts(second)["refreshed"] == 1
    assert swarm.registry.deriver.issued == derived_after_first + 1


@pytest.mark.parametrize(
    ("expression", "horizon_seconds"),
    [("3600", SIX_HOURS), (str(TWO_DAYS), TWO_DAYS)],
    ids=["period-under-six-hours", "period-over-six-hours"],
)
async def test_a_renewal_asks_for_a_token_that_lasts_the_longer_of_the_rule_period_and_six_hours(
    swarm: Any,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    expression: str,
    horizon_seconds: int,
) -> None:
    asked: list[timedelta | None] = []
    resolve = BindingPullCredentials.resolve

    async def watching(self: BindingPullCredentials, *args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs.get("min_remaining"))
        return await resolve(self, *args, **kwargs)

    monkeypatch.setattr(BindingPullCredentials, "resolve", watching)
    subject, _, _ = await ecr_service(swarm, settings)
    _, job = await subject.rule(SERVICE, UPDATE, expression=expression)

    await subject.run(job)

    assert asked
    assert set(asked) == {timedelta(seconds=horizon_seconds)}


@pytest.mark.parametrize(
    ("elapsed", "due"),
    [(JUST_UNDER, False), (SIX_HOURS, True), (SIX_HOURS + 1, True)],
    ids=["a-second-short", "exactly-six-hours", "a-second-over"],
)
async def test_a_credential_is_renewed_again_only_six_hours_after_the_last_renewal(
    swarm: Any, settings: Any, elapsed: int, due: bool
) -> None:
    subject, _, _ = await ecr_service(swarm, settings)
    _, job = await subject.rule(SERVICE, UPDATE)
    await subject.run(job)
    subject.advance(elapsed)

    result = await subject.run(job)

    assert ("refreshed" in counts(result)) is due
    assert ("refresh_not_due" in counts(result)) is not due
    assert len(rows(subject, REFRESHED)) == (2 if due else 1)


async def test_another_rule_shares_the_clock_and_a_report_rule_never_renews(
    swarm: Any, settings: Any
) -> None:
    subject, _, _ = await ecr_service(swarm, settings)
    _, job = await subject.rule(SERVICE, UPDATE)
    _, other = await subject.rule(SERVICE, UPDATE, name="second")
    _, report = await subject.rule(SERVICE, REPORT, name="report")
    await subject.run(job)

    second = await subject.run(other)
    subject.advance(SIX_HOURS + 1)
    reported = await subject.run(report)

    assert counts(second)["refresh_not_due"] == 1
    assert "refreshed" not in counts(reported) and len(rows(subject, REFRESHED)) == 1


async def test_a_service_updated_in_the_run_is_not_renewed_but_counts_as_renewed_afterwards(
    swarm: Any, settings: Any
) -> None:
    subject, manifests, _ = await ecr_service(swarm, settings)
    manifests.default = digest(2)
    _, job = await subject.rule(SERVICE, UPDATE)

    result = await subject.run(job)
    subject.advance(SECOND_PASS_SECONDS)
    again = await subject.run(job)

    assert counts(result)["updated"] == 1 and "refreshed" not in counts(result)
    assert rows(subject, REFRESHED) == []
    assert counts(again)["refresh_not_due"] == 1


async def test_a_held_update_does_not_stop_the_renewal_of_the_stored_credential(
    swarm: Any, settings: Any
) -> None:
    subject, manifests, sid = await ecr_service(swarm, settings)
    manifests.default = digest(2)
    after_update(swarm.host, paused)
    _, job = await subject.rule(SERVICE, UPDATE)
    first = await subject.run(job)
    swarm.host.services[sid].pop("UpdateStatus", None)
    subject.advance(SIX_HOURS + 1)

    second = await subject.run(job)

    assert counts(first)["rolled_back"] == 1
    assert counts(second)["held"] == 1 and counts(second)["refreshed"] == 1


async def test_an_unpinned_service_on_an_ecr_reference_is_renewed_but_never_updated(
    swarm: Any, settings: Any
) -> None:
    subject, _, sid = await ecr_service(swarm, settings, image=ECR_REFERENCE)
    _, job = await subject.rule(SERVICE, UPDATE)

    result = await subject.run(job)

    assert counts(result)["refreshed"] == 1 and counts(result)["unpinned"] == 1
    assert "updated" not in counts(result)
    assert image_of(swarm.host, sid) == ECR_REFERENCE


async def test_a_service_on_a_static_set_is_never_renewed(swarm_web: Any) -> None:
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert "refreshed" not in counts(result) and "refresh_not_due" not in counts(result)
    assert rows(swarm_web.subject, REFRESHED) == []


async def test_a_set_the_owner_may_not_use_is_refused_once_for_the_renewal_and_then_held(
    swarm: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, _, sid = await ecr_service(swarm, settings)
    _, job = await subject.rule(SERVICE, UPDATE)

    async def refuse(self: Any, set_id: int) -> None:
        raise CredentialSetUnavailable("not_granted_user")

    monkeypatch.setattr(RunContext, "authorise_credential_set", refuse)

    first = await subject.run(job)
    again = await subject.run(job)

    [row] = rows(subject, "image_update_refused")
    assert first.outcome == "failure" and row["target_id"] == sid
    assert rows(subject, REFRESHED) == [] and counts(again)["held"] == UPDATE_AND_REFRESH
    assert len(rows(subject, "image_update_refused")) == 1


async def test_a_service_whose_refresh_cannot_be_sent_unchanged_is_refused_once_and_held(
    swarm: Any, settings: Any
) -> None:
    subject, _, sid = await ecr_service(swarm, settings)
    spec = swarm.host.services[sid]["Spec"]
    spec["Networks"] = [{"Target": "legacy"}]
    spec["TaskTemplate"].pop("Networks", None)
    _, job = await subject.rule(SERVICE, UPDATE)

    first = await subject.run(job)
    again = await subject.run(job)

    [row] = rows(subject, REFRESHED)
    assert first.outcome == "failure" and row["detail"]["reason"] == "legacy_networks"
    assert counts(again)["held"] == 1 and len(rows(subject, REFRESHED)) == 1


async def test_every_member_of_a_stack_is_renewed_once_per_six_hours(
    swarm_stack_rig: Any, settings: Any
) -> None:
    shop = await deployed_shop(swarm_stack_rig, settings, ecr_shop_text(), ecr=True)
    _, job = await shop.subject.rule(STACK, UPDATE)

    dry = await shop.subject.run(job, dry=True)
    wrote = rows(shop.subject)
    first = await shop.subject.run(job)
    second = await shop.subject.run(job)
    shop.subject.advance(SIX_HOURS + 1)
    third = await shop.subject.run(job)

    assert counts(dry)["would_refresh_count"] == 2 and wrote == []
    assert counts(first)["refreshed"] == 2 and rows(shop.subject, "stack_redeployed") == []
    refreshed = rows(shop.subject, REFRESHED)
    assert {r["detail"]["name"] for r in refreshed} == {"Shop_web", "Shop_api"}
    assert {r["detail"]["stack"] for r in refreshed} == {"Shop"}
    assert counts(second)["refresh_not_due"] == 2 and counts(third)["refreshed"] == 2


async def test_the_members_of_a_stack_just_redeployed_are_not_renewed(
    swarm_stack_rig: Any, settings: Any
) -> None:
    shop = await deployed_shop(swarm_stack_rig, settings, ecr_shop_text(), ecr=True)
    shop.move(digest(9), ECR_REFERENCE)
    _, job = await shop.subject.rule(STACK, UPDATE)

    result = await shop.subject.run(job)

    assert counts(result)["updated"] == 1 and "refreshed" not in counts(result)
    assert rows(shop.subject, REFRESHED) == []


async def test_no_more_renewals_than_the_cap_are_sent_in_one_run(
    swarm_stack_rig: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = await deployed_shop(swarm_stack_rig, settings, ecr_shop_text(), ecr=True)
    monkeypatch.setattr(swarm_path, "MAX_REFRESHES", MANY)
    _, job = await shop.subject.rule(STACK, UPDATE)

    result = await shop.subject.run(job)

    assert counts(result)["refreshed"] == 1 and counts(result)["deferred"] == 1
    assert len(rows(shop.subject, REFRESHED)) == 1


async def test_a_stack_member_a_user_just_acted_on_is_not_renewed(
    swarm_stack_rig: Any, settings: Any
) -> None:
    shop = await deployed_shop(swarm_stack_rig, settings, ecr_shop_text(), ecr=True)
    _, job = await shop.subject.rule(STACK, {**UPDATE, "suppression_seconds": 300})
    key = ObjectKey(ENV_A, "service", shop.member_id("Shop_web"))
    async with shop.rig.engine.locks.hold(key, LockHolder(UserActor.of(ADMIN), "admin", "scale")):
        pass

    result = await shop.subject.run(job)

    assert result.outcome == "skipped" and rows(shop.subject, REFRESHED) == []


async def test_a_service_on_a_second_ecr_set_is_still_renewed_through_its_own_binding(
    swarm: Any, settings: Any
) -> None:
    subject, _, sid = await ecr_service(swarm, settings)
    other = f"{ECR_REFERENCE.rsplit(':', 1)[0]}:2"
    created = await add_ecr(swarm.registry, "ecr-two")
    await grant_environment(swarm.db, created.id, ENV_A)
    await bind_image(swarm.db, created.id, ENV_A, other)
    second = swarm.host.add_service(plain_spec("api", image=f"{other}@{digest(1)}", replicas=1))
    _, job = await subject.rule({"kind": "service", "names": ["api"]}, UPDATE, name="api")

    result = await subject.run(job)

    [row] = rows(subject, REFRESHED)
    assert counts(result)["refreshed"] == 1 and row["target_id"] == second
    assert row["target_id"] != sid and row["detail"]["credential_set_id"] == created.id
