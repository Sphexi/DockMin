"""Binding unbound images anonymously in bulk: the review, the confirmed apply, and their bounds."""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.registry_client.manifests import ManifestCheckKind, check_message
from app.registry_client.references import parse_reference
from app.services.anonymous_binding_plan import AnonymousBindingReview
from app.services.anonymous_binding_store import AnonymousBindingReviewStore
from app.services.anonymous_bindings import (
    FAILED_MESSAGE,
    MISMATCH_MESSAGE,
    AnonymousBindingChanged,
    AnonymousBindingService,
    AnonymousReviewBusy,
    AnonymousReviewExpired,
)
from app.services.assembly import build_host_clients, build_registry_resolver
from app.services.binding_candidates import ScanLimits
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
    seed_default_entries,
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
ELSEWHERE = "elsewhere.example/app:1"
BUILT_LOCALLY = f"{ADDRESS}/built:1"
OTHER_REGISTRY_REFERENCE = f"{OTHER_ADDRESS}/app:1"
HUB_REFERENCE = "docker.io/library/busybox:1"
CAP = 200
BUDGET_SECONDS = 120.0
STEP_SECONDS = 60.0

type Change = Callable[[ImageRig, AnonymousBindingReview], Awaitable[None]]


def with_image(rig: ImageRig, reference: str, number: int, local: str | None = LOCAL) -> None:
    """Put an image on the host under one tag, with the registry digest it was pulled by."""
    repository = reference.rpartition(":")[0]
    digests = () if local is None else (f"{repository}@{local}",)
    rig.host.add_image(reference, full_id(number), digests=digests)


async def open_entry(
    rig: ImageRig, name: str = "open", address: str = ADDRESS, **overrides: Any
) -> int:
    """Create an entry with no authentication and grant it to ENV_A."""
    created = await add_anonymous(rig.registry, name, url=address, **overrides)
    await grant_environment(rig.db, created.id, ENV_A)
    return created.id


def service_over(
    rig: ImageRig,
    manifests: ByLogin,
    *,
    writer: Any = None,
    limits: ScanLimits | None = None,
    monotonic: Callable[[], float] | None = None,
) -> AnonymousBindingService:
    return AnonymousBindingService(
        authoriser=rig.access.authoriser,
        hosts=build_host_clients(rig.db, rig.runtime, rig.engine),  # type: ignore[arg-type]
        scanner=scanner_over(rig, manifests, limits=limits, monotonic=monotonic),
        bindings=writer or writer_over(rig),
        binding_rows=ImageBindingRepository(rig.db),
        access=RegistryAccessRepository(rig.db),
        reviews=AnonymousBindingReviewStore(),
    )


async def reviewed(
    service: AnonymousBindingService, environment_id: int = ENV_A
) -> tuple[AnonymousBindingReview, str]:
    review = await service.review(ADMIN, environment_id)
    token = service._reviews.put(ADMIN.user_id, environment_id, review)
    return review, token


async def test_the_review_sends_one_anonymous_head_per_reference_at_an_address_with_an_open_entry(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    await register(image_rig, "account")
    bound = await register(image_rig, "bound")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2, local=OTHER_LOCAL)
    with_image(image_rig, ELSEWHERE, 3)
    with_image(image_rig, BUILT_LOCALLY, 4, local=None)
    with_image(image_rig, THIRD_REFERENCE, 5)
    await bind_image(image_rig.db, bound, ENV_A, THIRD_REFERENCE)
    manifests = Tripwire({ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert (group.address, group.entries, group.authenticated_sets) == (ADDRESS, ("open",), 2)
    assert [(m.reference, m.digest) for m in group.matched] == [(REFERENCE_KEY, LOCAL)]
    assert [(u.reference, u.reason) for u in group.unmatched] == [(SECOND_REFERENCE, "mismatch")]
    assert {(item.reference, item.reason) for item in review.left_out} == {
        (ELSEWHERE, "no_anonymous_entry"),
        (BUILT_LOCALLY, "no_local_digest"),
    }
    assert [(t.address, ref.key, login) for t, ref, login in manifests.calls] == [
        (ADDRESS, REFERENCE_KEY, None),
        (ADDRESS, SECOND_REFERENCE, None),
    ]
    assert image_rig.host.mutations() == []
    assert image_rig.host.verbs() == []
    assert [row["registry_credential_id"] for row in await binding_rows(image_rig.db)] == [bound]


async def test_a_registry_without_an_open_entry_is_never_contacted_and_its_references_are_listed(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "account")
    with_image(image_rig, REFERENCE, 1)
    manifests = Tripwire({ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    assert review.groups == ()
    assert [(item.reference, item.reason) for item in review.left_out] == [
        (REFERENCE_KEY, "no_anonymous_entry")
    ]
    assert manifests.calls == []


async def test_a_429_reads_rate_limited_and_is_sent_once_and_never_a_match(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    manifests = Tripwire({ANONYMOUS: ManifestCheckKind.RATE_LIMITED})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert group.matched == ()
    assert [(u.reference, u.reason) for u in group.unmatched] == [(REFERENCE_KEY, "rate_limited")]
    assert len(manifests.calls) == 1


async def test_references_past_the_cap_are_not_checked_and_never_contacted(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    for number in range(1, CAP + 2):
        with_image(image_rig, f"{ADDRESS}/app:{number:04d}", number)
    manifests = Tripwire({ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert len(group.matched) == CAP
    assert group.unmatched == ()
    assert review.not_checked_total == 1
    assert len(manifests.calls) == CAP


async def test_the_time_budget_leaves_the_remaining_references_not_checked(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    for number in range(1, 4):
        with_image(image_rig, f"{ADDRESS}/app:{number}", number)
    now = [0.0]
    manifests = Tripwire({ANONYMOUS: LOCAL})
    manifests.on_call = lambda: now.__setitem__(0, now[0] + STEP_SECONDS)
    limits = ScanLimits(in_flight=1, total_seconds=BUDGET_SECONDS)

    review = await service_over(
        image_rig, manifests, limits=limits, monotonic=lambda: now[0]
    ).review(ADMIN, ENV_A)

    [group] = review.groups
    assert [m.reference for m in group.matched] == [f"{ADDRESS}/app:1", f"{ADDRESS}/app:2"]
    assert [(u.reference, u.reason) for u in group.unmatched] == [
        (f"{ADDRESS}/app:3", "not_checked")
    ]


async def test_apply_binds_each_confirmed_reference_anonymously_with_one_audited_row_each(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    await register(image_rig, "account")
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    manifests = Tripwire({ANONYMOUS: LOCAL})
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    heads = len(manifests.calls)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    assert [(item.reference, item.status) for item in result.items] == [
        (REFERENCE_KEY, "bound"),
        (SECOND_REFERENCE, "bound"),
    ]
    rows = await binding_rows(image_rig.db)
    assert [(r["image_reference"], r["registry_credential_id"], r["state"]) for r in rows] == [
        (REFERENCE_KEY, None, "bound"),
        (SECOND_REFERENCE, None, "bound"),
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
            "credential_set_id": None,
            "anonymous": True,
            "digest": LOCAL,
            "verified": True,
            "pulled": False,
            "bulk": True,
        }
        for e in events
    )
    assert len(manifests.calls) == heads
    assert image_rig.host.mutations() == []


async def test_only_the_confirmed_registry_is_written_and_the_other_stays_in_the_review(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    await open_entry(image_rig, "mirror", OTHER_ADDRESS)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, OTHER_REGISTRY_REFERENCE, 2)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    review, token = await reviewed(service)

    await service.apply(ADMIN, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    assert [r["image_reference"] for r in await binding_rows(image_rig.db)] == [REFERENCE_KEY]
    left = service._reviews.get(token, ADMIN.user_id, ENV_A)
    assert left is not None
    assert [group.address for group in left.groups] == [OTHER_ADDRESS]


async def test_applying_nothing_or_a_registry_the_review_lacks_writes_nothing(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    review, token = await reviewed(service)

    with pytest.raises(ValidationFailed):
        await service.apply(ADMIN, ENV_A, token, [], review.digest([]))
    with pytest.raises(ValidationFailed):
        await service.apply(ADMIN, ENV_A, token, ["unknown.example"], review.digest([ADDRESS]))

    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []


async def bind_meanwhile(rig: ImageRig, review: AnonymousBindingReview) -> None:
    await bind_image(rig.db, None, ENV_A, REFERENCE_KEY)


async def replace_the_image(rig: ImageRig, review: AnonymousBindingReview) -> None:
    rig.host.image_store[full_id(1)]["RepoDigests"] = [f"{ADDRESS}/app@{OTHER_LOCAL}"]


async def remove_the_entry(rig: ImageRig, review: AnonymousBindingReview) -> None:
    await rig.db.execute("DELETE FROM registry_environment_access")


@pytest.mark.parametrize(
    "change",
    [bind_meanwhile, replace_the_image, remove_the_entry],
    ids=["bound-meanwhile", "image-replaced", "entry-withdrawn"],
)
async def test_a_changed_plan_refuses_the_whole_request_before_any_write(
    image_rig: ImageRig, change: Change
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    review, token = await reviewed(service)
    expected = review.digest([ADDRESS])
    await change(image_rig, review)
    before = await binding_rows(image_rig.db)

    with pytest.raises(AnonymousBindingChanged) as refused:
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], expected)

    assert refused.value.reason == "changed_since_review"
    assert await binding_rows(image_rig.db) == before
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_a_digest_that_is_not_the_reviewed_plan_refuses_before_any_write(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    _, token = await reviewed(service)

    with pytest.raises(AnonymousBindingChanged):
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], "0" * 64)

    assert await binding_rows(image_rig.db) == []


class RacingWriter:
    """Binds the second reference just before the first is written, as another request would."""

    def __init__(self, inner: ImageBindingService, rig: ImageRig) -> None:
        self._inner = inner
        self._rig = rig

    async def bind_verified(
        self, actor: Any, environment_id: int, reference: str, *args: Any, **kwargs: Any
    ) -> str:
        if reference == REFERENCE_KEY:
            await bind_image(self._rig.db, None, ENV_A, SECOND_REFERENCE)
        return await self._inner.bind_verified(actor, environment_id, reference, *args, **kwargs)


async def test_a_reference_bound_between_the_recompute_and_the_write_is_skipped(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    service = service_over(
        image_rig,
        Tripwire({ANONYMOUS: LOCAL}),
        writer=RacingWriter(writer_over(image_rig), image_rig),
    )
    review, token = await reviewed(service)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    assert [(item.reference, item.status) for item in result.items] == [
        (REFERENCE_KEY, "bound"),
        (SECOND_REFERENCE, "skipped"),
    ]
    assert [e["target_id"] for e in await rows_for(image_rig.db, "image_bound")] == [REFERENCE_KEY]


async def test_a_standard_user_can_neither_review_nor_apply_and_nothing_is_written(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    manifests = Tripwire({ANONYMOUS: LOCAL})
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    calls = len(manifests.calls)

    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await service.review(STANDARD, ENV_A)
    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await service.apply(STANDARD, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    assert len(manifests.calls) == calls
    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_a_review_cannot_be_applied_to_another_environment_or_after_it_expired(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    review, token = await reviewed(service)

    with pytest.raises(AnonymousReviewExpired):
        await service.apply(ADMIN, ENV_B, token, [ADDRESS], review.digest([ADDRESS]))
    with pytest.raises(AnonymousReviewExpired):
        await service.apply(ADMIN, ENV_A, "x" * 22, [ADDRESS], review.digest([ADDRESS]))

    assert await binding_rows(image_rig.db) == []


async def test_an_image_bound_by_the_bulk_action_gets_a_stored_update_state_from_a_check(
    image_rig: ImageRig,
) -> None:
    hub, _ = await seed_default_entries(image_rig.db)
    await RegistryAccessRepository(image_rig.db).add_environment(hub, ENV_A)
    with_image(image_rig, HUB_REFERENCE, 1)
    manifests = Tripwire({ANONYMOUS: LOCAL})
    service = service_over(image_rig, manifests)
    review, token = await reviewed(service)
    await service.apply(ADMIN, ENV_A, token, ["docker.io"], review.digest(["docker.io"]))
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

    assert status.reason != "registry_not_configured"
    assert (status.state, status.local_digest) == ("up_to_date", LOCAL)


def app_reference(number: int) -> str:
    return f"{ADDRESS}/app:{number}"


def matched_references(review: AnonymousBindingReview) -> list[str]:
    return [item.reference for group in review.groups for item in group.matched]


async def test_containers_come_first_and_each_review_resumes_after_the_last_one_checked(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    for number in range(1, 6):
        with_image(image_rig, app_reference(number), number)
    image_rig.host.add(f"{4:064x}", "web-4", image=app_reference(4))
    image_rig.host.add(f"{5:064x}", "web-5", image=app_reference(5))
    manifests = Tripwire({ANONYMOUS: LOCAL})
    manifests.answers[app_reference(4)] = STALE
    service = service_over(image_rig, manifests, limits=ScanLimits(references=2))

    first = await service.review(ADMIN, ENV_A)
    second = await service.review(ADMIN, ENV_A)
    third = await service.review(ADMIN, ENV_A)

    assert [sorted(call) for call in (manifests.keys()[0:2], manifests.keys()[2:4])] == [
        [app_reference(4), app_reference(5)],
        [app_reference(1), app_reference(2)],
    ]
    assert matched_references(first) == [app_reference(5)]
    assert [u.reference for u in first.groups[0].unmatched] == [app_reference(4)]
    assert matched_references(second) == [app_reference(1), app_reference(2)]
    assert sorted(manifests.keys()[4:6]) == [app_reference(3), app_reference(4)]
    assert matched_references(third) == [app_reference(3)]
    assert [r.not_checked_total for r in (first, second, third)] == [3, 3, 3]


async def test_a_review_that_leaves_nothing_unchecked_forgets_where_the_last_one_stopped(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    bound = await register(image_rig, "bound")
    for number in range(1, 4):
        with_image(image_rig, app_reference(number), number)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}), limits=ScanLimits(references=2))
    await service.review(ADMIN, ENV_A)
    stopped = service._reviews.resume_point(ADMIN.user_id, ENV_A)
    await bind_image(image_rig.db, bound, ENV_A, app_reference(3))

    review = await service.review(ADMIN, ENV_A)

    assert stopped == (1, app_reference(2))
    assert review.not_checked_total == 0
    assert service._reviews.resume_point(ADMIN.user_id, ENV_A) is None


async def test_the_unmatched_rows_of_a_group_are_capped_and_its_total_is_kept(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    for number in range(1, CAP + 2):
        with_image(image_rig, app_reference(number), number)
    service = service_over(
        image_rig, Tripwire({ANONYMOUS: STALE}), limits=ScanLimits(references=CAP + 5)
    )

    review = await service.review(ADMIN, ENV_A)

    [group] = review.groups
    assert (len(group.unmatched), group.unmatched_total) == (CAP, CAP + 1)
    assert review.not_checked_total == 0


async def test_every_unmatched_reference_carries_the_clients_message_for_its_reason(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    for number in range(1, 4):
        with_image(image_rig, app_reference(number), number)
    manifests = Tripwire({ANONYMOUS: LOCAL})
    manifests.answers[app_reference(1)] = ManifestCheckKind.NO_DIGEST
    manifests.answers[app_reference(2)] = ManifestCheckKind.PROTOCOL
    manifests.answers[app_reference(3)] = STALE

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert [(u.reason, u.message) for u in group.unmatched] == [
        ("no_digest", check_message(ManifestCheckKind.NO_DIGEST)),
        ("protocol", check_message(ManifestCheckKind.PROTOCOL)),
        ("mismatch", MISMATCH_MESSAGE),
    ]


async def test_the_anonymous_check_contacts_only_permitted_addresses_and_only_for_administrators(
    image_rig: ImageRig,
) -> None:
    manifests = Tripwire({ANONYMOUS: LOCAL})
    scanner = scanner_over(image_rig, manifests)
    references = [
        (parse_reference(REFERENCE_KEY), (LOCAL,)),
        (parse_reference(OTHER_REGISTRY_REFERENCE), (LOCAL,)),
    ]

    outcomes = await scanner.check_anonymously(ADMIN, ENV_A, references, [ADDRESS])
    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await scanner.check_anonymously(STANDARD, ENV_A, references, [ADDRESS])

    assert [found.reference for found in outcomes] == [REFERENCE_KEY]
    assert manifests.addresses() == {ADDRESS}
    assert len(manifests.calls) == 1


async def test_an_address_whose_only_entry_has_its_own_certificate_settings_is_left_out(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig, "own-settings", verify_tls=False)
    await open_entry(image_rig, "mirror-own", OTHER_ADDRESS, verify_tls=False)
    await open_entry(image_rig, "mirror-plain", OTHER_ADDRESS)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, OTHER_REGISTRY_REFERENCE, 2)
    manifests = Tripwire({ANONYMOUS: LOCAL})

    review = await service_over(image_rig, manifests).review(ADMIN, ENV_A)

    [group] = review.groups
    assert (group.address, group.entries) == (OTHER_ADDRESS, ("mirror-plain",))
    assert [(item.reference, item.reason) for item in review.left_out] == [
        (REFERENCE_KEY, "entry_tls_settings")
    ]
    assert manifests.addresses() == {OTHER_ADDRESS}


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
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    with_image(image_rig, SECOND_REFERENCE, 2)
    writer = FailingWriter(
        writer_over(image_rig), REFERENCE_KEY, sqlite3.OperationalError("database is locked")
    )
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}), writer=writer)
    review, token = await reviewed(service)
    caplog.set_level(logging.ERROR)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    assert [(item.reference, item.status) for item in result.items] == [
        (REFERENCE_KEY, "failed"),
        (SECOND_REFERENCE, "bound"),
    ]
    assert result.items[0].message == FAILED_MESSAGE
    assert [r["image_reference"] for r in await binding_rows(image_rig.db)] == [SECOND_REFERENCE]
    assert service._reviews.get(token, ADMIN.user_id, ENV_A) is None
    [record] = [r for r in caplog.records if r.getMessage() == "anonymous_binding_write_failed"]
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
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    service = service_over(
        image_rig,
        Tripwire({ANONYMOUS: LOCAL}),
        writer=WithdrawingWriter(writer_over(image_rig), image_rig),
    )
    review, token = await reviewed(service)

    result = await service.apply(ADMIN, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    [item] = result.items
    assert item.status == "failed"
    assert item.message not in ("", FAILED_MESSAGE)
    assert await binding_rows(image_rig.db) == []


async def test_a_credential_set_granted_at_the_address_after_the_review_refuses_before_any_write(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    review, token = await reviewed(service)
    expected = review.digest([ADDRESS])
    await register(image_rig, "account")

    with pytest.raises(AnonymousBindingChanged) as refused:
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], expected)

    assert refused.value.reason == "changed_since_review"
    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []


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
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    writer = BlockingWriter(writer_over(image_rig))
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}), writer=writer)
    review, token = await reviewed(service)
    expected = review.digest([ADDRESS])
    first = asyncio.create_task(service.apply(ADMIN, ENV_A, token, [ADDRESS], expected))
    await writer.entered.wait()

    with pytest.raises(AnonymousReviewBusy):
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], expected)
    writer.release.set()
    result = await first

    assert result.count("bound") == 1
    assert len(await rows_for(image_rig.db, "image_bound")) == 1


async def test_apply_on_an_unreachable_environment_writes_nothing_and_keeps_the_review(
    image_rig: ImageRig,
) -> None:
    await open_entry(image_rig)
    with_image(image_rig, REFERENCE, 1)
    service = service_over(image_rig, Tripwire({ANONYMOUS: LOCAL}))
    review, token = await reviewed(service)
    image_rig.host.fail("image_list", 500)

    with pytest.raises(EnvironmentUnavailable):
        await service.apply(ADMIN, ENV_A, token, [ADDRESS], review.digest([ADDRESS]))

    assert await binding_rows(image_rig.db) == []
    assert service._reviews.begin_apply(token, ADMIN.user_id, ENV_A)
