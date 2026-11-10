"""Bulk binding of unbound images to credential sets: the review, the applies and their bounds."""

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.registry_client.manifests import ManifestCheckKind, check_message
from app.registry_client.references import parse_reference
from app.services.assembly import build_host_clients, build_registry_resolver
from app.services.binding_candidates import ScanLimits
from app.services.bulk_binding_plan import BulkBindingReview, SetOption, SetRef
from app.services.bulk_binding_store import BulkBindingReviewStore
from app.services.bulk_bindings import (
    CHOICE_NOT_OFFERED,
    FAILED_MESSAGE,
    MISMATCH_MESSAGE,
    NEEDS_SET_MESSAGE,
    WORKS_MESSAGE,
    BulkBindingService,
    BulkReviewBusy,
    BulkReviewExpired,
)
from app.services.image_bindings import ImageBindingService
from app.services.image_updates import ImageUpdateService, UpdateCheckState
from app.services.object_inspector import EnvironmentUnavailable
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    binding_rows,
    digest,
    full_id,
    register,
    rows_for,
)
from tests.registry_support import (
    ADDRESS,
    OTHER_ADDRESS,
    add_anonymous,
    bind_image,
    grant_environment,
)
from tests.services.conftest import ImageRig
from tests.services.test_verified_bindings import (
    ANONYMOUS,
    ByLogin,
    Tripwire,
    scanner_over,
    writer_over,
)

LOCAL = digest(1)
OTHER_LOCAL = digest(3)
STALE = digest(9)
SECOND_REFERENCE = f"{ADDRESS}/app:2"
THIRD_REFERENCE = f"{ADDRESS}/app:3"
ELSEWHERE_ADDRESS = "elsewhere.example"
ELSEWHERE = f"{ELSEWHERE_ADDRESS}/app:1"
BUILT_LOCALLY = f"{ADDRESS}/built:1"
OTHER_REGISTRY_REFERENCE = f"{OTHER_ADDRESS}/app:1"
THIRD_ADDRESS = "third.example"
FOURTH_ADDRESS = "fourth.example"
HUB_REFERENCE = "docker.io/library/busybox:1"
CAP = 200
BUDGET_SECONDS = 120.0
STEP_SECONDS = 60.0
ALL_ANSWER_LOCAL = {
    "user-alpha": LOCAL,
    "user-bravo": LOCAL,
    "user-charlie": STALE,
    "user-mirror": LOCAL,
}


def with_image(rig: ImageRig, reference: str, number: int, local: str | None = LOCAL) -> None:
    """Put an image on the host under one tag, with the registry digest it was pulled by."""
    repository = reference.rpartition(":")[0]
    digests = () if local is None else (f"{repository}@{local}",)
    rig.host.add_image(reference, full_id(number), digests=digests)


def service_over(
    rig: ImageRig,
    manifests: ByLogin,
    *,
    writer: Any = None,
    limits: ScanLimits | None = None,
    monotonic: Callable[[], float] | None = None,
) -> BulkBindingService:
    return BulkBindingService(
        authoriser=rig.access.authoriser,
        hosts=build_host_clients(rig.db, rig.runtime, rig.engine),  # type: ignore[arg-type]
        scanner=scanner_over(rig, manifests, limits=limits, monotonic=monotonic),
        bindings=writer or writer_over(rig),
        binding_rows=ImageBindingRepository(rig.db),
        access=RegistryAccessRepository(rig.db),
        reviews=BulkBindingReviewStore(),
    )


async def reviewed(
    service: BulkBindingService, environment_id: int = ENV_A
) -> tuple[BulkBindingReview, str]:
    review = await service.review(ADMIN, environment_id)
    token = service._reviews.put(ADMIN.user_id, environment_id, review)
    return review, token


def plans_of(review: BulkBindingReview, *addresses: str) -> list[str]:
    return [group.digest for address in addresses if (group := review.group(address))]


def offered(review: BulkBindingReview) -> list[str]:
    return [item.reference for group in review.groups for item in group.bindable]


def app_reference(number: int) -> str:
    return f"{ADDRESS}/app:{number}"


async def test_the_review_offers_a_reference_one_set_read_and_lists_what_it_could_not_bind(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    open_entry = await add_anonymous(image_rig.registry, "open", url=ELSEWHERE_ADDRESS)
    await grant_environment(image_rig.db, open_entry.id, ENV_A)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2, local=OTHER_LOCAL)
    with_image(image_rig, ELSEWHERE, 3)
    with_image(image_rig, BUILT_LOCALLY, 4, local=None)
    with_image(image_rig, THIRD_REFERENCE, 5)
    await bind_image(image_rig.db, alpha, ENV_A, THIRD_REFERENCE)
    manifests = Tripwire({"user-alpha": LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert (group.address, group.sets) == (ADDRESS, (SetRef(alpha, "alpha"),))
    [image] = group.bindable
    assert (image.reference, image.pick) == (REFERENCE_KEY, False)
    assert image.options == (SetOption(alpha, "alpha", LOCAL),)
    [other] = group.others
    assert (other.reference, other.kind, other.message) == (
        SECOND_REFERENCE,
        "needs_set",
        NEEDS_SET_MESSAGE,
    )
    assert other.details == (f"alpha: {MISMATCH_MESSAGE}",)
    assert {(item.reference, item.reason) for item in review.left_out} == {
        (ELSEWHERE, "no_credential_set"),
        (BUILT_LOCALLY, "no_local_digest"),
    }
    assert manifests.addresses() == {ADDRESS}
    assert image_rig.host.mutations() == []
    assert image_rig.host.verbs() == []
    assert [row["registry_credential_id"] for row in await binding_rows(image_rig.db)] == [alpha]


async def test_sets_are_tried_before_anonymous_which_is_sent_only_when_no_set_matched(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    await register(image_rig, "bravo", address=OTHER_ADDRESS, username="user-charlie")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, OTHER_REGISTRY_REFERENCE, 2)
    manifests = Tripwire({"user-alpha": LOCAL, "user-charlie": STALE, ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    by_address: dict[str, list[bool]] = {}
    for target, _, login in manifests.calls:
        by_address.setdefault(target.address, []).append(login is None)
    assert by_address[ADDRESS] == [False]
    assert sorted(by_address[OTHER_ADDRESS]) == [False, True]
    assert offered(review) == [REFERENCE_KEY]
    [mirror] = [group for group in review.groups if group.address == OTHER_ADDRESS]
    [works] = mirror.others
    assert (works.reference, works.kind, works.message) == (
        OTHER_REGISTRY_REFERENCE,
        "works",
        WORKS_MESSAGE,
    )
    assert mirror.bindable == ()
    assert review.offering == (review.group(ADDRESS),)


async def test_an_address_with_no_credential_set_is_never_contacted_and_its_references_are_listed(
    image_rig: ImageRig,
) -> None:
    hub = await add_anonymous(image_rig.registry, "open", url=ADDRESS)
    await grant_environment(image_rig.db, hub.id, ENV_A)
    with_image(image_rig, REFERENCE, 1)
    manifests = Tripwire({ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    assert review.groups == ()
    assert [(item.reference, item.reason) for item in review.left_out] == [
        (REFERENCE_KEY, "no_credential_set")
    ]
    assert manifests.calls == []


async def test_an_entry_with_its_own_certificate_settings_is_a_set_of_its_own(
    image_rig: ImageRig,
) -> None:
    private = await add_anonymous(image_rig.registry, "own-settings", verify_tls=False)
    mirror = await add_anonymous(
        image_rig.registry, "own-ca", url=OTHER_ADDRESS, ca_bundle="-----BEGIN CERTIFICATE-----"
    )
    for entry in (private, mirror):
        await grant_environment(image_rig.db, entry.id, ENV_A)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, OTHER_REGISTRY_REFERENCE, 2)
    manifests = Tripwire({ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    assert [
        (group.address, [(o.set_id, o.name) for i in group.bindable for o in i.options])
        for group in review.groups
    ] == [
        (OTHER_ADDRESS, [(mirror.id, "own-ca")]),
        (ADDRESS, [(private.id, "own-settings")]),
    ]
    assert len(manifests.calls) == 2


async def test_a_rate_limited_reference_is_listed_with_the_sets_message_and_never_offered(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    manifests = Tripwire({"user-alpha": ManifestCheckKind.RATE_LIMITED})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    [other] = group.others
    assert group.bindable == ()
    assert other.details == (f"alpha: {check_message(ManifestCheckKind.RATE_LIMITED)}",)


async def test_references_past_the_cap_are_not_checked_and_never_contacted(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    for number in range(1, CAP + 2):
        with_image(image_rig, f"{ADDRESS}/app:{number:04d}", number)
    manifests = Tripwire({"user-alpha": LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert len(group.bindable) == CAP
    assert group.others == ()
    assert review.not_checked_total == 1
    assert len(manifests.calls) == CAP


async def test_the_time_budget_leaves_the_remaining_references_not_checked(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    for number in range(1, 4):
        with_image(image_rig, app_reference(number), number)
    now = [0.0]
    manifests = Tripwire({"user-alpha": LOCAL})
    manifests.on_call = lambda: now.__setitem__(0, now[0] + STEP_SECONDS)
    limits = ScanLimits(in_flight=1, total_seconds=BUDGET_SECONDS)

    review = await service_over(
        image_rig, manifests, limits=limits, monotonic=lambda: now[0]
    ).review(ADMIN, ENV_A)

    [group] = review.groups
    assert [i.reference for i in group.bindable] == [app_reference(1), app_reference(2)]
    assert [(o.reference, o.kind) for o in group.others] == [(app_reference(3), "not_checked")]
    assert review.not_checked_total == 1


async def test_apply_binds_each_offered_reference_to_its_set_with_one_audited_row_each(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    manifests = Tripwire({"user-alpha": LOCAL})
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    heads = len(manifests.calls)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))

    assert [(item.reference, item.status) for item in result.items] == [
        (REFERENCE_KEY, "bound"),
        (SECOND_REFERENCE, "bound"),
    ]
    rows = await binding_rows(image_rig.db)
    assert [(r["image_reference"], r["registry_credential_id"], r["state"]) for r in rows] == [
        (REFERENCE_KEY, alpha, "bound"),
        (SECOND_REFERENCE, alpha, "bound"),
    ]
    assert {r["observed_digest"] for r in rows} == {LOCAL}
    events = await rows_for(image_rig.db, "image_bound")
    assert [(e["result"], e["actor_user_id"], e["target_id"]) for e in events] == [
        ("success", ADMIN.user_id, REFERENCE_KEY),
        ("success", ADMIN.user_id, SECOND_REFERENCE),
    ]
    assert all(
        e["detail"]
        == {
            "credential_set_id": alpha,
            "anonymous": False,
            "digest": LOCAL,
            "verified": True,
            "pulled": False,
            "bulk": True,
        }
        for e in events
    )
    assert len(manifests.calls) == heads
    assert image_rig.host.mutations() == []
    left = service._reviews.get(token, ADMIN.user_id, ENV_A)
    assert left is not None and left.groups == ()
    assert service._reviews.summary(token, ADMIN.user_id, ENV_A) == result


async def test_a_reference_two_sets_read_is_a_pick_and_only_an_offered_set_is_bound(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    bravo = await register(image_rig, "bravo")
    charlie = await register(image_rig, "charlie")
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire(ALL_ANSWER_LOCAL))
    review, token = await reviewed(service)
    plans = plans_of(review, ADDRESS)
    [image] = review.groups[0].bindable
    assert image.pick and [o.name for o in image.options] == ["alpha", "bravo"]

    with pytest.raises(ValidationFailed) as refused:
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans, {REFERENCE_KEY: charlie})
    with pytest.raises(ValidationFailed):
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans, {SECOND_REFERENCE: bravo})
    unchosen = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans)
    chosen = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans, {REFERENCE_KEY: bravo})

    assert refused.value.fields == {"choices": CHOICE_NOT_OFFERED}
    assert [(i.reference, i.status) for i in unchosen.items] == [(REFERENCE_KEY, "left")]
    assert [(i.reference, i.status) for i in chosen.items] == [(REFERENCE_KEY, "bound")]
    [row] = await binding_rows(image_rig.db)
    assert row["registry_credential_id"] == bravo
    [event] = await rows_for(image_rig.db, "image_bound")
    assert event["detail"]["credential_set_id"] == bravo
    assert event["detail"]["bulk"] is True


async def test_each_registry_stands_on_its_own_and_a_changed_one_stays_in_the_review(
    image_rig: ImageRig,
) -> None:
    replaced = await register(image_rig, "alpha")
    await register(image_rig, "mirror", address=OTHER_ADDRESS)
    await register(image_rig, "third", address=THIRD_ADDRESS)
    await register(image_rig, "fourth", address=FOURTH_ADDRESS)
    references = {
        ADDRESS: REFERENCE,
        OTHER_ADDRESS: OTHER_REGISTRY_REFERENCE,
        THIRD_ADDRESS: f"{THIRD_ADDRESS}/app:1",
        FOURTH_ADDRESS: f"{FOURTH_ADDRESS}/app:1",
    }
    for number, reference in enumerate(references.values(), start=1):
        with_image(image_rig, reference, number)
    manifests = Tripwire(
        {"user-alpha": LOCAL, "user-mirror": LOCAL, "user-third": LOCAL, "user-fourth": LOCAL}
    )
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    addresses = list(references)
    image_rig.host.image_store[full_id(1)]["RepoDigests"] = [f"{ADDRESS}/app@{OTHER_LOCAL}"]
    await bind_image(image_rig.db, replaced, ENV_A, OTHER_REGISTRY_REFERENCE)
    await register(image_rig, "second-third", address=THIRD_ADDRESS)

    result = await service.apply(ADMIN, ENV_A, token, addresses, plans_of(review, *addresses))

    assert set(result.changed) == {ADDRESS, THIRD_ADDRESS}
    assert [(item.reference, item.status) for item in result.items] == [
        (OTHER_REGISTRY_REFERENCE, "skipped"),
        (references[FOURTH_ADDRESS], "bound"),
    ]
    rows = {r["image_reference"] for r in await binding_rows(image_rig.db)}
    assert rows == {OTHER_REGISTRY_REFERENCE, references[FOURTH_ADDRESS]}
    kept = service._reviews.get(token, ADMIN.user_id, ENV_A)
    assert kept is not None
    assert {group.address for group in kept.groups} == {ADDRESS, THIRD_ADDRESS}
    assert [e["target_id"] for e in await rows_for(image_rig.db, "image_bound")] == [
        references[FOURTH_ADDRESS]
    ]


async def test_a_reference_bound_since_the_review_is_skipped_and_the_rest_of_its_registry_applies(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL}))
    review, token = await reviewed(service)
    await bind_image(image_rig.db, alpha, ENV_A, SECOND_REFERENCE)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))

    assert result.changed == ()
    assert {(item.reference, item.status) for item in result.items} == {
        (REFERENCE_KEY, "bound"),
        (SECOND_REFERENCE, "skipped"),
    }
    assert [e["target_id"] for e in await rows_for(image_rig.db, "image_bound")] == [REFERENCE_KEY]
    kept = service._reviews.get(token, ADMIN.user_id, ENV_A)
    assert kept is not None and kept.groups == ()


async def test_a_registry_whose_plan_is_stale_is_reported_changed_and_the_others_apply(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    await register(image_rig, "mirror", address=OTHER_ADDRESS)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, OTHER_REGISTRY_REFERENCE, 2)
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL, "user-mirror": LOCAL}))
    review, token = await reviewed(service)
    [_, other_plan] = plans_of(review, ADDRESS, OTHER_ADDRESS)

    result = await service.apply(
        ADMIN, ENV_A, token, [ADDRESS, OTHER_ADDRESS], ["0" * 64, other_plan]
    )

    assert result.changed == (ADDRESS,)
    assert [(item.reference, item.status) for item in result.items] == [
        (OTHER_REGISTRY_REFERENCE, "bound")
    ]
    kept = service._reviews.get(token, ADMIN.user_id, ENV_A)
    assert kept is not None and [group.address for group in kept.groups] == [ADDRESS]


async def test_a_registry_with_many_sets_does_not_hide_the_sets_of_another_address(
    image_rig: ImageRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.db.repositories.registry_access.MAX_OFFERED_SETS", 1)
    await register(image_rig, "alpha")
    await register(image_rig, "bravo")
    await register(image_rig, "mirror", address=OTHER_ADDRESS)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, OTHER_REGISTRY_REFERENCE, 2)
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL, "user-mirror": LOCAL}))

    review = await service.review(ADMIN, ENV_A)

    assert {group.address for group in review.groups} == {ADDRESS, OTHER_ADDRESS}
    assert review.left_out == ()


async def test_a_request_the_review_does_not_back_is_refused_before_any_write(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL}))
    review, token = await reviewed(service)
    [plan] = plans_of(review, ADDRESS)

    refusals = [
        ([], []),
        (["unknown.example"], [plan]),
        ([ADDRESS], ["0" * 64]),
        ([ADDRESS], []),
        ([ADDRESS, ADDRESS], [plan, plan]),
    ]
    for registries, plans in refusals:
        with pytest.raises(ValidationFailed):
            await service.apply(ADMIN, ENV_A, token, registries, plans)

    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []
    assert service._reviews.summary(token, ADMIN.user_id, ENV_A) is None


class RacingWriter:
    """Binds the second reference just before the first is written, as another request would."""

    def __init__(self, inner: ImageBindingService, rig: ImageRig, set_id: int) -> None:
        self._inner = inner
        self._rig = rig
        self._set_id = set_id

    async def bind_verified(
        self, actor: Any, environment_id: int, reference: str, *args: Any, **kwargs: Any
    ) -> str:
        if reference == REFERENCE_KEY:
            await bind_image(self._rig.db, self._set_id, ENV_A, SECOND_REFERENCE)
        return await self._inner.bind_verified(actor, environment_id, reference, *args, **kwargs)


async def test_a_reference_bound_between_the_recheck_and_the_write_is_skipped(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    service = service_over(
        image_rig,
        Tripwire({"user-alpha": LOCAL}),
        writer=RacingWriter(writer_over(image_rig), image_rig, alpha),
    )
    review, token = await reviewed(service)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))

    assert [(item.reference, item.status) for item in result.items] == [
        (REFERENCE_KEY, "bound"),
        (SECOND_REFERENCE, "skipped"),
    ]
    assert [e["target_id"] for e in await rows_for(image_rig.db, "image_bound")] == [REFERENCE_KEY]


async def test_a_standard_user_can_neither_review_nor_apply_and_nothing_is_written(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha", users=(STANDARD.user_id,))
    with_image(image_rig, REFERENCE, 1)
    manifests = Tripwire({"user-alpha": LOCAL})
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    calls = len(manifests.calls)

    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await service.review(STANDARD, ENV_A)
    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await service.apply(STANDARD, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))
    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await scanner_over(image_rig, manifests).check_references(
            STANDARD, ENV_A, [(parse_reference(REFERENCE_KEY), (LOCAL,))]
        )

    assert len(manifests.calls) == calls
    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_a_review_cannot_be_applied_to_another_environment_or_after_it_expired(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL}))
    review, token = await reviewed(service)
    plans = plans_of(review, ADDRESS)

    with pytest.raises(BulkReviewExpired):
        await service.apply(ADMIN, ENV_B, token, [ADDRESS], plans)
    with pytest.raises(BulkReviewExpired):
        await service.apply(ADMIN, ENV_A, "x" * 22, [ADDRESS], plans)

    assert await binding_rows(image_rig.db) == []


async def test_an_image_bound_by_the_bulk_action_gets_a_stored_update_state_from_a_check(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "hub", address="docker.io")
    with_image(image_rig, HUB_REFERENCE, 1)
    manifests = Tripwire({"user-hub": LOCAL})
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    await service.apply(ADMIN, ENV_A, token, ["docker.io"], plans_of(review, "docker.io"))
    updates = ImageUpdateService(
        bindings=ImageBindingRepository(image_rig.db),
        resolver=build_registry_resolver(
            image_rig.db, image_rig.runtime, image_rig.registry.registries
        ),
        hosts=build_host_clients(image_rig.db, image_rig.runtime, image_rig.engine),  # type: ignore[arg-type]
        authoriser=image_rig.access.authoriser,
        manifests=manifests,  # type: ignore[arg-type]
        state=UpdateCheckState(image_rig.clock),
        clock=image_rig.clock,
    )

    [status] = await updates.check(ADMIN, ENV_A, [HUB_REFERENCE])

    assert (status.state, status.local_digest) == ("up_to_date", LOCAL)


async def test_containers_come_first_and_each_review_resumes_after_the_last_one_checked(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    for number in range(1, 6):
        with_image(image_rig, app_reference(number), number)
    image_rig.host.add(f"{4:064x}", "web-4", image=app_reference(4))
    image_rig.host.add(f"{5:064x}", "web-5", image=app_reference(5))
    manifests = Tripwire({"user-alpha": LOCAL})
    manifests.answers[app_reference(4)] = STALE
    service = service_over(image_rig, manifests, limits=ScanLimits(references=2))

    first = await service.review(ADMIN, ENV_A)
    after_first = len(manifests.calls)
    second = await service.review(ADMIN, ENV_A)
    after_second = len(manifests.calls)
    third = await service.review(ADMIN, ENV_A)

    heads = manifests.keys()
    assert [
        set(heads[:after_first]),
        set(heads[after_first:after_second]),
        set(heads[after_second:]),
    ] == [
        {app_reference(4), app_reference(5)},
        {app_reference(1), app_reference(2)},
        {app_reference(3), app_reference(4)},
    ]
    assert offered(first) == [app_reference(5)]
    assert [o.reference for o in first.groups[0].others] == [app_reference(4)]
    assert offered(second) == [app_reference(1), app_reference(2)]
    assert offered(third) == [app_reference(3)]
    assert [r.not_checked_total for r in (first, second, third)] == [3, 3, 3]


async def test_a_review_that_leaves_nothing_unchecked_forgets_where_the_last_one_stopped(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    for number in range(1, 4):
        with_image(image_rig, app_reference(number), number)
    service = service_over(
        image_rig, Tripwire({"user-alpha": LOCAL}), limits=ScanLimits(references=2)
    )
    await service.review(ADMIN, ENV_A)
    stopped = service._reviews.resume_point(ADMIN.user_id, ENV_A)
    await bind_image(image_rig.db, alpha, ENV_A, app_reference(3))

    review = await service.review(ADMIN, ENV_A)

    assert stopped == (1, app_reference(2))
    assert review.not_checked_total == 0
    assert service._reviews.resume_point(ADMIN.user_id, ENV_A) is None


async def test_the_other_rows_of_a_group_are_capped_and_each_names_its_sets_failure(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    for number in range(1, CAP + 2):
        with_image(image_rig, app_reference(number), number)
    manifests = Tripwire({"user-alpha": STALE})
    manifests.answers[app_reference(1)] = ManifestCheckKind.NO_DIGEST
    service = service_over(image_rig, manifests, limits=ScanLimits(references=CAP + 5))

    review = await service.review(ADMIN, ENV_A)

    [group] = review.groups
    assert (len(group.others), group.others_total) == (CAP, CAP + 1)
    assert review.not_checked_total == 0
    assert group.others[0].details == (f"alpha: {check_message(ManifestCheckKind.NO_DIGEST)}",)
    assert group.others[1].details == (f"alpha: {MISMATCH_MESSAGE}",)


class FailingWriter:
    """Raises a failure that is not a product error for one reference and binds the others."""

    def __init__(self, inner: ImageBindingService, failing: str, error: Exception) -> None:
        self._inner = inner
        self._failing = failing
        self._error = error

    async def bind_verified(
        self, actor: Any, environment_id: int, reference: str, *args: Any, **kwargs: Any
    ) -> str:
        if reference == self._failing:
            raise self._error
        return await self._inner.bind_verified(actor, environment_id, reference, *args, **kwargs)


async def test_a_write_that_fails_outside_the_product_errors_is_reported_and_the_rest_proceed(
    image_rig: ImageRig, caplog: pytest.LogCaptureFixture
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    writer = FailingWriter(
        writer_over(image_rig), REFERENCE_KEY, sqlite3.OperationalError("database is locked")
    )
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL}), writer=writer)
    review, token = await reviewed(service)
    caplog.set_level(logging.ERROR)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))

    assert [(item.reference, item.status) for item in result.items] == [
        (REFERENCE_KEY, "failed"),
        (SECOND_REFERENCE, "bound"),
    ]
    assert result.items[0].message == FAILED_MESSAGE
    assert [r["image_reference"] for r in await binding_rows(image_rig.db)] == [SECOND_REFERENCE]
    kept = service._reviews.get(token, ADMIN.user_id, ENV_A)
    assert kept is not None and offered(kept) == [REFERENCE_KEY]
    [record] = [r for r in caplog.records if r.getMessage() == "bulk_binding_write_failed"]
    assert record.__dict__["error"] == "OperationalError"
    assert "database is locked" not in caplog.text


class WithdrawingWriter:
    """Withdraws every grant just before the first write, as an administrator could."""

    def __init__(self, inner: ImageBindingService, rig: ImageRig) -> None:
        self._inner = inner
        self._rig = rig

    async def bind_verified(self, *args: Any, **kwargs: Any) -> str:
        await self._rig.db.execute("DELETE FROM registry_environment_access")
        return await self._inner.bind_verified(*args, **kwargs)


async def test_a_binding_the_write_refuses_is_reported_as_failed_with_its_message(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    service = service_over(
        image_rig,
        Tripwire({"user-alpha": LOCAL}),
        writer=WithdrawingWriter(writer_over(image_rig), image_rig),
    )
    review, token = await reviewed(service)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))

    [item] = result.items
    assert item.status == "failed"
    assert item.message not in ("", FAILED_MESSAGE)
    assert await binding_rows(image_rig.db) == []


class BlockingWriter:
    """Holds the first write until released, so a second apply can arrive meanwhile."""

    def __init__(self, inner: ImageBindingService) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def bind_verified(self, *args: Any, **kwargs: Any) -> str:
        self.entered.set()
        await self.release.wait()
        return await self._inner.bind_verified(*args, **kwargs)


async def test_a_review_that_is_being_applied_refuses_a_second_apply(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    writer = BlockingWriter(writer_over(image_rig))
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL}), writer=writer)
    review, token = await reviewed(service)
    plans = plans_of(review, ADDRESS)
    first = asyncio.create_task(service.apply(ADMIN, ENV_A, token, [ADDRESS], plans))
    await writer.entered.wait()

    with pytest.raises(BulkReviewBusy):
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans)
    writer.release.set()
    result = await first

    assert result.count("bound") == 1
    assert len(await rows_for(image_rig.db, "image_bound")) == 1


async def test_apply_on_an_unreachable_environment_writes_nothing_and_keeps_the_review(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({"user-alpha": LOCAL}))
    review, token = await reviewed(service)
    image_rig.host.fail("image_list", 500)

    with pytest.raises(EnvironmentUnavailable):
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], plans_of(review, ADDRESS))

    assert await binding_rows(image_rig.db) == []
    assert service._reviews.begin_apply(token, ADMIN.user_id, ENV_A)
