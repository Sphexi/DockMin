"""Update availability: states, reasons, where the server may connect, caching and limits."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.image_update_status import (
    ImageUpdateStatusRepository,
    StoredUpdateStatus,
)
from app.registry_client.manifests import ManifestCheckKind
from app.services.assembly import build_host_clients, build_registry_resolver
from app.services.image_updates import (
    BUSY_REASON,
    MAX_CHECK_REFERENCES,
    STATUS_RETENTION,
    ImageUpdateService,
    TooManyUpdateChecks,
    UpdateCheckState,
    UpdateStatus,
    is_image_id_reference,
)
from app.services.object_inspector import EnvironmentUnavailable
from app.services.registry_resolution import CredentialSetUnavailable
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import STANDARD_ID
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    ScriptedManifests,
    digest,
    full_id,
    mark_missing,
    register,
)
from tests.registry_support import ADDRESS, OTHER_ADDRESS, SECRET, USERNAME, bind_image
from tests.services.conftest import ImageRig

LOCAL = digest(1)
REMOTE = digest(2)
PINNED = f"{ADDRESS}/app@{digest(5)}"
SECOND = f"{ADDRESS}/app:2"
UNREACHABLE_NAME = "elsewhere.example"
NOT_CONFIGURED = f"{UNREACHABLE_NAME}/app:1"
CACHE_SECONDS = 30
IMAGE = 1


type Build = Callable[..., ImageUpdateService]


@pytest.fixture
def manifests() -> ScriptedManifests:
    return ScriptedManifests()


@pytest.fixture
def build(image_rig: ImageRig, manifests: ScriptedManifests) -> Build:
    def make(
        state: UpdateCheckState | None = None,
        statuses: ImageUpdateStatusRepository | None = None,
    ) -> ImageUpdateService:
        hosts = build_host_clients(image_rig.db, image_rig.runtime, image_rig.engine)  # type: ignore[arg-type]
        return ImageUpdateService(
            bindings=ImageBindingRepository(image_rig.db),
            resolver=build_registry_resolver(
                image_rig.db, image_rig.runtime, image_rig.registry.registries
            ),
            hosts=hosts,
            authoriser=image_rig.access.authoriser,
            manifests=manifests,  # type: ignore[arg-type]
            state=state or UpdateCheckState(image_rig.clock),
            clock=image_rig.clock,
            statuses=statuses,
        )

    return make


@pytest.fixture
def service(build: Build) -> ImageUpdateService:
    return build()


def pulled(rig: ImageRig, reference: str = REFERENCE, local: str | None = LOCAL) -> None:
    repository = reference.rpartition(":")[0]
    digests = () if local is None else (f"{repository}@{local}",)
    rig.host.add_image(reference, full_id(IMAGE), digests=digests)


async def check(
    service: ImageUpdateService,
    references: list[str] | None = None,
    who: Principal | None = ADMIN,
) -> list[UpdateStatus]:
    return list(await service.check(who, ENV_A, references or [REFERENCE]))


async def test_a_bound_reference_whose_registry_digest_is_the_local_one_is_up_to_date(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    manifests.default = LOCAL
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    [status] = await check(service)

    assert (status.state, status.reason) == ("up_to_date", None)
    assert (status.local_digest, status.remote_digest) == (LOCAL, LOCAL)
    assert (status.reference, status.display) == (REFERENCE_KEY, REFERENCE)


async def test_a_different_registry_digest_is_an_update_available(
    image_rig: ImageRig, service: ImageUpdateService
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    [status] = await check(service)

    assert (status.state, status.local_digest, status.remote_digest) == (
        "update_available",
        LOCAL,
        REMOTE,
    )


async def test_any_local_digest_of_the_repository_counts_as_current(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    manifests.default = REMOTE
    await bind_image(image_rig.db, None, ENV_A)
    await register(image_rig, "granted")
    repository = REFERENCE.rpartition(":")[0]
    image_rig.host.add_image(
        REFERENCE, full_id(IMAGE), digests=(f"{repository}@{LOCAL}", f"{repository}@{REMOTE}")
    )

    [status] = await check(service)

    assert status.state == "up_to_date"


@pytest.mark.parametrize("kind", list(ManifestCheckKind))
async def test_a_registry_that_cannot_be_read_is_unknown_with_its_kind_never_up_to_date(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests, kind: Any
) -> None:
    manifests.default = kind
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    [status] = await check(service)

    assert (status.state, status.reason, status.remote_digest) == ("unknown", kind.value, None)


async def test_an_image_that_was_never_pulled_or_pushed_is_unknown_for_lack_of_a_local_digest(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig, local=None)

    [status] = await check(service)

    assert (status.state, status.reason) == ("unknown", "no_local_digest")
    assert manifests.calls == []


async def test_a_reference_that_is_not_on_any_image_has_no_local_digest(
    image_rig: ImageRig, service: ImageUpdateService
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)

    [status] = await check(service)

    assert (status.state, status.reason) == ("unknown", "no_local_digest")


async def test_a_reference_pinned_by_digest_is_pinned_and_never_checked(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await bind_image(image_rig.db, None, ENV_A, PINNED)

    [status] = await check(service, [PINNED])

    assert status.state == "pinned"
    assert manifests.calls == []


async def test_a_reference_with_no_binding_is_not_bound_and_never_checked(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    pulled(image_rig)

    [status] = await check(service)

    assert status.state == "not_bound"
    assert manifests.calls == []


async def test_a_credential_missing_binding_is_unknown_and_never_checked(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    gone = await register(image_rig, "gone")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)
    pulled(image_rig)

    [status] = await check(service)

    assert (status.state, status.reason) == ("unknown", "credential_missing")
    assert manifests.calls == []


async def test_a_bound_set_is_the_only_one_asked_and_its_login_and_address_are_used(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await register(image_rig, "default-but-unbound", default=True)
    bound = await register(image_rig, "bound", username=USERNAME, secret=SECRET)
    await bind_image(image_rig.db, bound, ENV_A)
    pulled(image_rig)

    await check(service)

    [(target, reference, login)] = manifests.calls
    assert target.address == ADDRESS
    assert (reference.key, login is not None and login.username) == (REFERENCE_KEY, USERNAME)
    assert login is not None
    assert login.secret.get_secret_value() == SECRET
    assert target.verify_tls is True


async def test_a_standard_user_without_a_grant_on_the_bound_set_gets_unknown_and_no_connection(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    bound = await register(image_rig, "bound")
    await bind_image(image_rig.db, bound, ENV_A)
    pulled(image_rig)

    [status] = await check(service, who=STANDARD)

    assert (status.state, status.reason) == ("unknown", "credential_not_available")
    assert manifests.calls == []


async def test_a_standard_user_with_a_grant_on_the_bound_set_may_check(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    bound = await register(image_rig, "bound", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)
    pulled(image_rig)

    [status] = await check(service, who=STANDARD)

    assert status.state == "update_available"
    assert len(manifests.calls) == 1


async def test_a_bound_set_whose_address_no_longer_matches_is_unknown_and_never_connected_to(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    bound = await register(image_rig, "bound")
    await bind_image(image_rig.db, bound, ENV_A)
    await image_rig.db.execute(
        "UPDATE registry_credential SET url = ? WHERE id = ?", (OTHER_ADDRESS, bound)
    )
    pulled(image_rig)

    [status] = await check(service)

    assert (status.state, status.reason) == ("unknown", "credential_mismatch")
    assert manifests.calls == []


async def test_an_anonymous_binding_to_a_registry_no_granted_set_names_never_connects(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await register(image_rig, "elsewhere", address=OTHER_ADDRESS)
    await bind_image(image_rig.db, None, ENV_A, NOT_CONFIGURED)
    pulled(image_rig, NOT_CONFIGURED)

    [status] = await check(service, [NOT_CONFIGURED])

    assert (status.state, status.reason) == ("unknown", "registry_not_configured")
    assert manifests.calls == []


async def test_a_set_at_the_address_that_is_not_granted_to_the_environment_does_not_enable_it(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await register(image_rig, "ungranted", environments=())
    await bind_image(image_rig.db, None, ENV_A)
    pulled(image_rig)

    [status] = await check(service)

    assert status.reason == "registry_not_configured"
    assert manifests.calls == []


async def test_an_anonymous_binding_at_a_granted_address_is_checked_without_a_login_and_verified(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await register(image_rig, "granted")
    await image_rig.db.execute("UPDATE registry_credential SET verify_tls = 1, ca_bundle = NULL")
    await bind_image(image_rig.db, None, ENV_A)
    pulled(image_rig)

    [status] = await check(service)

    [(target, _, login)] = manifests.calls
    assert status.state == "update_available"
    assert login is None
    assert (target.address, target.verify_tls, target.ca_bundle) == (ADDRESS, True, None)


async def test_an_unparsable_reference_is_unknown_and_the_others_are_still_checked(
    image_rig: ImageRig, service: ImageUpdateService
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    statuses = await check(service, ["Not A Reference", REFERENCE])

    assert [(s.state, s.reason) for s in statuses] == [
        ("unknown", "invalid_reference"),
        ("update_available", None),
    ]


@pytest.mark.parametrize(
    "references",
    [
        pytest.param([], id="none"),
        pytest.param([f"{ADDRESS}/app:{n}" for n in range(MAX_CHECK_REFERENCES + 1)], id="26"),
    ],
)
async def test_between_one_and_25_distinct_references_may_be_checked_at_once(
    service: ImageUpdateService, references: list[str]
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await service.check(ADMIN, ENV_A, references)

    assert set(refused.value.fields) == {"references"}


async def test_a_repeated_reference_is_checked_once_and_reported_once(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    statuses = await check(service, [REFERENCE, REFERENCE])

    assert len(statuses) == 1
    assert len(manifests.calls) == 1


async def test_a_standard_user_who_cannot_see_the_environment_gets_not_found_and_no_check(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await EnvironmentAccessRepository(image_rig.db).remove_user("environment", ENV_A, STANDARD_ID)

    with pytest.raises(ResourceNotFound):
        await check(service, who=STANDARD)

    assert manifests.calls == []


async def test_automation_checks_without_a_user_and_uses_only_the_bound_set(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    await register(image_rig, "unbound")
    bound = await register(image_rig, "bound", username="the-bound-user")
    await bind_image(image_rig.db, bound, ENV_A)
    pulled(image_rig)

    [status] = await check(service, who=None)

    [(_, _, login)] = manifests.calls
    assert status.state == "update_available"
    assert login is not None
    assert login.username == "the-bound-user"


async def test_an_unreachable_host_is_an_environment_error_not_an_answer(
    image_rig: ImageRig, service: ImageUpdateService
) -> None:
    image_rig.host.fail("image_list", 500)

    with pytest.raises(EnvironmentUnavailable):
        await check(service)


async def test_a_definite_answer_and_an_unknown_are_kept_for_the_same_thirty_seconds(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    service = build()
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    await bind_image(image_rig.db, set_id, ENV_A, SECOND)
    pulled(image_rig)
    pulled(image_rig, SECOND)
    image_rig.host.add_image(SECOND, full_id(IMAGE + 1), digests=(f"{ADDRESS}/app@{LOCAL}",))
    manifests.answers[f"{ADDRESS}/app:2"] = ManifestCheckKind.TIMEOUT

    await check(service, [REFERENCE, SECOND])
    image_rig.clock.advance(timedelta(seconds=CACHE_SECONDS - 1))
    await check(service, [REFERENCE, SECOND])
    within = len(manifests.calls)
    image_rig.clock.advance(timedelta(seconds=2))
    await check(service, [REFERENCE, SECOND])

    assert within == 2
    assert len(manifests.calls) == 4


async def test_a_changed_local_digest_is_a_new_question_and_is_not_served_from_the_cache(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)
    await check(service)

    pulled(image_rig, local=digest(7))
    await check(service)

    assert len(manifests.calls) == 2


class CountingState(UpdateCheckState):
    """Counts the questions asked and says when a given number of them are waiting."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.asked = 0
        self.both_asked = asyncio.Event()

    async def verdict(self, key: Any, work: Any) -> Any:
        self.asked += 1
        if self.asked == 2:
            self.both_asked.set()
        return await super().verdict(key, work)


async def test_checks_for_one_question_asked_at_once_make_one_registry_request(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    state = CountingState(image_rig.clock, per_user=5)
    first, second = build(state), build(state)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)
    manifests.gate = asyncio.Event()

    one = asyncio.ensure_future(check(first, who=ADMIN))
    two = asyncio.ensure_future(check(second, who=ADMIN))
    await state.both_asked.wait()
    manifests.gate.set()
    answers = await asyncio.gather(one, two)

    assert len(manifests.calls) == 1
    assert [a[0].state for a in answers] == ["update_available", "update_available"]


async def test_a_check_that_finds_no_free_slot_is_unknown_busy_and_is_not_kept(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    state = UpdateCheckState(image_rig.clock, concurrency=1, slot_wait=0)
    service = build(state)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    await bind_image(image_rig.db, set_id, ENV_A, SECOND)
    pulled(image_rig)
    pulled(image_rig, SECOND)
    manifests.gate = asyncio.Event()

    holder = asyncio.ensure_future(check(service, [REFERENCE]))
    await manifests.entered.wait()
    [busy] = await check(service, [SECOND])
    manifests.gate.set()
    await holder
    manifests.gate = None
    [again] = await check(service, [SECOND])

    assert (busy.state, busy.reason) == ("unknown", BUSY_REASON)
    assert again.state == "update_available"
    assert len(manifests.calls) == 2


async def test_a_user_running_the_most_checks_a_user_may_is_refused_another_and_others_are_not(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    state = UpdateCheckState(image_rig.clock, per_user=1)
    service = build(state)
    set_id = await register(image_rig, "bound", users=(STANDARD_ID,))
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)
    manifests.gate = asyncio.Event()

    first = asyncio.ensure_future(check(service, who=STANDARD))
    await manifests.entered.wait()
    with pytest.raises(TooManyUpdateChecks):
        await check(service, [SECOND], who=STANDARD)
    other = await check(service, [SECOND], who=ADMIN)
    manifests.gate.set()
    await first

    assert other[0].state == "not_bound"


async def test_a_finished_request_frees_the_users_slot(image_rig: ImageRig, build: Build) -> None:
    service = build(UpdateCheckState(image_rig.clock, per_user=1))

    await check(service, who=STANDARD)
    statuses = await check(service, who=STANDARD)

    assert statuses[0].state == "not_bound"


async def test_the_check_logs_counts_only(
    image_rig: ImageRig,
    service: ImageUpdateService,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    set_id = await register(image_rig, "bound", secret=SECRET)
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    await check(service)

    [record] = logged("image_update_checked")
    assert (record["update_available"], record["unknown"], record["environment_id"]) == (
        1,
        0,
        ENV_A,
    )
    assert SECRET not in str(record) and REFERENCE not in str(record)


SHORT_ID = "625a12e1e4ee"
FULL_ID = "sha256:" + "ab" * 32
BARE_ID = "ab" * 32
UNPARSABLE = "not a reference!"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SHORT_ID, True),
        (FULL_ID, True),
        (BARE_ID, True),
        ("625a12e1e4e", False),
        ("ABCDEF123456", False),
        ("nginx:1", False),
        (f"app@sha256:{BARE_ID}", False),
        (REFERENCE, False),
    ],
)
def test_only_text_shaped_like_an_image_id_is_an_image_id_reference(
    text: str, expected: bool
) -> None:
    assert is_image_id_reference(text) is expected


async def test_an_image_id_is_unknown_without_a_registry_call_and_is_not_stored(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)

    statuses = await check(service, [SHORT_ID, FULL_ID, BARE_ID])

    assert [(s.state, s.reason) for s in statuses] == [("unknown", "image_id_reference")] * 3
    assert manifests.calls == []
    assert await repository.for_references(ENV_A, [SHORT_ID, FULL_ID, BARE_ID]) == {}


async def test_the_image_carrying_the_tag_supplies_the_id_and_digest_not_a_superseded_one(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    manifests.default = REMOTE
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    repository = REFERENCE.rpartition(":")[0]
    image_rig.host.add_image(REFERENCE, full_id(1), digests=(f"{repository}@{LOCAL}",))
    image_rig.host.add_image(REFERENCE, full_id(2), digests=(f"{repository}@{REMOTE}",))

    [status] = await check(service)

    assert status.local_image_id == full_id(2).removeprefix("sha256:")
    assert (status.local_digest, status.state) == (REMOTE, "up_to_date")


async def test_results_are_stored_when_a_tag_moves_and_a_newer_answer_replaces_them(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    manifests.default = LOCAL
    pulled(image_rig)

    await check(service)
    first = (await repository.for_references(ENV_A, [REFERENCE_KEY]))[REFERENCE_KEY]
    manifests.default = REMOTE
    image_rig.clock.advance(timedelta(seconds=CACHE_SECONDS + 1))
    await check(service)
    second = (await repository.for_references(ENV_A, [REFERENCE_KEY]))[REFERENCE_KEY]

    assert (first.state, first.remote_digest) == ("up_to_date", LOCAL)
    assert (second.state, second.remote_digest, second.local_digest) == (
        "update_available",
        REMOTE,
        LOCAL,
    )
    assert second.checked_at > first.checked_at


async def test_a_busy_or_unparsable_result_is_not_stored(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(UpdateCheckState(image_rig.clock, concurrency=1, slot_wait=0), repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    await bind_image(image_rig.db, set_id, ENV_A, SECOND)
    pulled(image_rig)
    pulled(image_rig, SECOND)
    manifests.gate = asyncio.Event()

    holder = asyncio.ensure_future(check(service, [REFERENCE]))
    await manifests.entered.wait()
    statuses = await check(service, [SECOND, UNPARSABLE])
    manifests.gate.set()
    await holder

    assert [(s.state, s.reason) for s in statuses] == [
        ("unknown", BUSY_REASON),
        ("unknown", "invalid_reference"),
    ]
    assert list(await repository.for_references(ENV_A, [SECOND, UNPARSABLE])) == []


class _InspectedWrite(ImageUpdateStatusRepository):
    def __init__(self, session: Any) -> None:
        super().__init__(session)
        self.writes: list[bool] = []

    async def upsert(self, *arguments: Any, **keywords: Any) -> None:
        self.writes.append(self._session.in_transaction)
        await super().upsert(*arguments, **keywords)


async def test_the_status_is_written_after_the_check_and_outside_any_transaction(
    image_rig: ImageRig, build: Build
) -> None:
    repository = _InspectedWrite(image_rig.db)
    service = build(statuses=repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    await check(service)

    assert repository.writes == [False]


class _BrokenWrite(ImageUpdateStatusRepository):
    async def upsert(self, *arguments: Any, **keywords: Any) -> None:
        raise RuntimeError(SECRET)


async def test_a_failed_status_write_is_logged_without_its_text_and_never_fails_the_check(
    image_rig: ImageRig,
    build: Build,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    service = build(statuses=_BrokenWrite(image_rig.db))
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    [status] = await check(service)

    assert status.state == "update_available"
    [record] = logged("image_update_status_write_failed")
    assert record["error"] == "RuntimeError"
    assert SECRET not in str(record)


async def test_rows_older_than_the_retention_are_pruned_by_the_next_write(
    image_rig: ImageRig, build: Build
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)
    stale = f"{ADDRESS}/stale:1"
    old = format_timestamp(image_rig.clock() - STATUS_RETENTION - timedelta(seconds=1))
    await repository.upsert(
        ENV_A,
        [StoredUpdateStatus(stale, "up_to_date", None, LOCAL, LOCAL, old)],
        prune_before=format_timestamp(image_rig.clock() - STATUS_RETENTION - timedelta(days=1)),
    )

    await check(service)

    kept = await repository.for_references(ENV_A, [stale, REFERENCE_KEY])
    assert list(kept) == [REFERENCE_KEY]


@pytest.mark.parametrize("granted", [True, False])
async def test_a_result_that_depends_on_who_asked_is_stored_only_when_nobody_was_refused(
    image_rig: ImageRig, build: Build, granted: bool
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)
    users = (STANDARD_ID,) if granted else ()
    set_id = await register(image_rig, "bound", users=users)
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)

    [status] = await check(service, who=STANDARD)

    stored = await repository.for_references(ENV_A, [REFERENCE_KEY])
    assert status.reason == (None if granted else "credential_not_available")
    assert (REFERENCE_KEY in stored) is granted


async def test_a_refusal_by_the_automation_gate_is_unknown_and_never_stored(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    pulled(image_rig)
    asked: list[int] = []

    async def refuse(bound: int) -> None:
        asked.append(bound)
        raise CredentialSetUnavailable("not_granted_user")

    [status] = await service.check(None, ENV_A, [REFERENCE], authorise_set=refuse)

    assert (status.state, status.reason) == ("unknown", "credential_not_available")
    assert asked == [set_id]
    assert manifests.calls == []
    assert await repository.for_references(ENV_A, [REFERENCE_KEY]) == {}


async def test_a_deployed_digest_is_compared_alone_and_the_result_is_not_stored(
    image_rig: ImageRig, build: Build, manifests: ScriptedManifests
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    manifests.default = REMOTE

    [status] = await service.check(ADMIN, ENV_A, [REFERENCE], deployed={REFERENCE: LOCAL})

    assert (status.state, status.local_digest, status.remote_digest) == (
        "update_available",
        LOCAL,
        REMOTE,
    )
    assert status.local_image_id is None
    assert await repository.for_references(ENV_A, [REFERENCE_KEY]) == {}


async def test_the_host_is_not_read_when_every_reference_is_deployed(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    manifests.default = REMOTE

    [status] = await service.check(ADMIN, ENV_A, [REFERENCE], deployed={REFERENCE: REMOTE})

    assert status.state == "up_to_date"
    assert not any(request.path.endswith("/images/json") for request in image_rig.host.requests)


async def test_a_mixed_check_reads_the_host_once_and_stores_only_the_hosts_reference(
    image_rig: ImageRig, build: Build
) -> None:
    repository = ImageUpdateStatusRepository(image_rig.db)
    service = build(statuses=repository)
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    await bind_image(image_rig.db, set_id, ENV_A, SECOND)
    pulled(image_rig, SECOND, REMOTE)

    deployed, hosted = await service.check(
        ADMIN, ENV_A, [REFERENCE, SECOND], deployed={REFERENCE: LOCAL}
    )

    listings = [r for r in image_rig.host.requests if r.path.endswith("/images/json")]
    assert (deployed.state, hosted.state) == ("update_available", "up_to_date")
    assert len(listings) == 1
    assert list(await repository.for_references(ENV_A, [REFERENCE_KEY, hosted.reference])) == [
        hosted.reference
    ]


async def test_one_reference_under_two_deployed_digests_is_two_registry_questions(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    manifests.default = REMOTE

    [old] = await service.check(ADMIN, ENV_A, [REFERENCE], deployed={REFERENCE: LOCAL})
    [current] = await service.check(ADMIN, ENV_A, [REFERENCE], deployed={REFERENCE: REMOTE})

    assert (old.state, current.state) == ("update_available", "up_to_date")
    assert len(manifests.calls) == 2


async def test_a_malformed_deployed_digest_is_unknown_and_asks_no_registry(
    image_rig: ImageRig, service: ImageUpdateService, manifests: ScriptedManifests
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)

    [status] = await service.check(ADMIN, ENV_A, [REFERENCE], deployed={REFERENCE: "not-a-digest"})

    assert (status.state, status.reason) == ("unknown", "no_local_digest")
    assert manifests.calls == []
