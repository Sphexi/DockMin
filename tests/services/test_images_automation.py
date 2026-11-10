"""Pulls made on behalf of an automation rule, and the stored update status in the image views."""

from typing import Any

import pytest

from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.image_update_status import (
    ImageUpdateStatusRepository,
    StoredUpdateStatus,
)
from app.db.repositories.registry_access import RegistryAccessRepository
from app.services.acting import AutomationAuthority
from app.services.assembly import build_registry_resolver
from app.services.image_bindings import BindingPullCredentials, SetCandidates
from app.services.image_queries import automation_plan
from app.services.images import ImageActionService
from app.services.object_locks import ObjectKey
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import authority_context, event_rows
from tests.image_support import (
    OTHER_REFERENCE,
    REFERENCE,
    REFERENCE_KEY,
    binding_rows,
    digest,
    full_id,
    mark_missing,
    register,
)
from tests.registry_support import ADDRESS, bind_image
from tests.services.conftest import ImageRig

UPDATER = "container-updater"
CONTAINER = ObjectKey(ENV_A, "container", "a" * 64)
REMOTE_NOTE = {"trigger": "update", "remote_digest": digest(9)}
NO_CALLS: list[Any] = []
CHECKED_AT = "2026-01-02T03:04:05Z"
FAR_PAST = "2000-01-01T00:00:00Z"


class SpyCandidates(SetCandidates):
    """Counts every read of the sets a user could choose from."""

    reads = 0

    async def at(self, *arguments: Any, **keywords: Any) -> Any:
        type(self).reads += 1
        return await super().at(*arguments, **keywords)

    async def eligible(self, *arguments: Any, **keywords: Any) -> Any:
        type(self).reads += 1
        return await super().eligible(*arguments, **keywords)

    async def get(self, *arguments: Any, **keywords: Any) -> Any:
        type(self).reads += 1
        return await super().get(*arguments, **keywords)


class SpyResolver:
    """Records which credential sets were opened for a pull or a login."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.pulls: list[int] = []
        self.logins: list[Any] = []

    async def resolve_pull_credentials(self, set_id: int, **keywords: Any) -> Any:
        self.pulls.append(set_id)
        return await self.inner.resolve_pull_credentials(set_id, **keywords)

    async def resolve_login(self, *arguments: Any, **keywords: Any) -> Any:
        self.logins.append(arguments)
        return await self.inner.resolve_login(*arguments, **keywords)


class SpyCredentials(BindingPullCredentials):
    """Records the arguments of every resolution."""

    def __init__(self, **keywords: Any) -> None:
        super().__init__(**keywords)
        self.calls: list[tuple[int, str, dict[str, Any]]] = []

    async def resolve(self, environment_id: int, reference: str, **keywords: Any) -> Any:
        self.calls.append((environment_id, reference, keywords))
        return await super().resolve(environment_id, reference, **keywords)


def pulling(rig: ImageRig) -> tuple[ImageActionService, SpyCredentials, SpyResolver]:
    real = rig.images.actions
    resolver = SpyResolver(build_registry_resolver(rig.db, rig.runtime, rig.registry.registries))
    SpyCandidates.reads = 0
    credentials = SpyCredentials(
        bindings=ImageBindingRepository(rig.db),
        candidates=SpyCandidates(
            access=RegistryAccessRepository(rig.db),
            environments=EnvironmentAccessRepository(rig.db),
        ),
        resolver=resolver,
    )
    service = ImageActionService(
        db=rig.db,
        database=rig.database,
        authoriser=rig.access.authoriser,
        hosts=real._hosts,
        locks=rig.engine.locks,
        credentials=credentials,
        bindings=real._bindings,
        activity=real._activity,
        clock=rig.runtime.clock,
    )
    return service, credentials, resolver


async def authority_for(rig: ImageRig) -> tuple[AutomationAuthority, Any]:
    context = await authority_context(
        rig.db, rig.clock, rig.engine.locks, environment_id=ENV_A, subsystem=UPDATER
    )
    return context.authority(), context


def pull_calls(rig: ImageRig) -> list[Any]:
    return rig.host.calls("POST", "/images/create")


async def test_a_pull_for_a_rule_uses_only_the_bound_set_with_no_user_and_no_choice(
    image_rig: ImageRig,
) -> None:
    bound = await register(image_rig, "bound")
    other = await register(image_rig, "other")
    await bind_image(image_rig.db, bound, ENV_A, REFERENCE_KEY)
    image_rig.host.pull_id = full_id(5)
    service, credentials, resolver = pulling(image_rig)
    acting, context = await authority_for(image_rig)
    asked: list[ObjectKey] = []
    inner = acting.authorise

    async def spy(key: ObjectKey) -> None:
        asked.append(key)
        await inner(key)

    watched = AutomationAuthority(
        actor=acting.actor,
        authorise=spy,
        hold=acting.hold,
        lock_wait=acting.lock_wait,
        authorise_read=acting.authorise_read,
        owner_is_administrator=acting.owner_is_administrator,
        authorise_credential_set=acting.authorise_credential_set,
    )

    outcome = await service.pull(
        watched,
        ENV_A,
        automation_plan(ENV_A, REFERENCE),
        on_behalf_of=CONTAINER,
        note=REMOTE_NOTE,
    )

    assert asked == [CONTAINER]
    [(_, _, keywords)] = credentials.calls
    assert keywords == {"principal": None, "choice": None}
    assert SpyCandidates.reads == 0
    assert resolver.pulls == [bound] and other not in resolver.pulls and resolver.logins == []
    assert outcome.binding == "kept"
    [row] = await event_rows(image_rig.db, "image_pulled")
    assert (row["actor_type"], row["actor_subsystem"], row["rule_id"]) == (
        "subsystem",
        UPDATER,
        context.actor.rule_id,
    )
    assert row["detail"]["trigger"] == "update"
    assert row["detail"]["remote_digest"] == digest(9)
    assert row["detail"]["credential_set_id"] == bound


async def test_a_rule_pulls_anonymously_when_the_reference_is_unbound_or_its_set_was_deleted(
    image_rig: ImageRig,
) -> None:
    bound = await register(image_rig, "bound")
    await register(image_rig, "other")
    await bind_image(image_rig.db, bound, ENV_A, REFERENCE_KEY)
    await mark_missing(image_rig.db, bound)
    service, credentials, resolver = pulling(image_rig)
    acting, _ = await authority_for(image_rig)

    for reference in (REFERENCE, OTHER_REFERENCE):
        await service.pull(acting, ENV_A, automation_plan(ENV_A, reference), on_behalf_of=CONTAINER)

    assert [call[1] for call in credentials.calls] == [REFERENCE, OTHER_REFERENCE]
    assert SpyCandidates.reads == 0
    assert resolver.pulls == [] and resolver.logins == []
    assert [call.headers.get("x-registry-auth") for call in pull_calls(image_rig)] == [None, None]
    assert [row["state"] for row in await binding_rows(image_rig.db)] == ["credential_missing"]


async def test_a_rule_must_name_what_it_acts_for_and_a_user_must_not(image_rig: ImageRig) -> None:
    service, _, _ = pulling(image_rig)
    acting, _ = await authority_for(image_rig)

    with pytest.raises(ValueError):
        await service.pull(acting, ENV_A, automation_plan(ENV_A, REFERENCE))
    with pytest.raises(ValueError):
        await service.pull(ADMIN, ENV_A, automation_plan(ENV_A, REFERENCE), on_behalf_of=CONTAINER)

    assert pull_calls(image_rig) == NO_CALLS


async def test_the_image_list_and_detail_show_the_stored_status(image_rig: ImageRig) -> None:
    image_rig.host.add_image("nginx:1", full_id(1))
    key = "docker.io/library/nginx:1"
    await ImageUpdateStatusRepository(image_rig.db).upsert(
        ENV_A,
        [StoredUpdateStatus(key, "update_available", None, None, digest(2), CHECKED_AT)],
        prune_before=FAR_PAST,
    )
    queries = image_rig.images.queries

    page = await queries.page(ADMIN, ENV_A)
    [row] = page.page.items
    detail = await queries.detail(ADMIN, ENV_A, row.id)

    [reference] = row.references
    assert (reference.update_state, reference.update_checked_at) == ("update_available", CHECKED_AT)
    assert detail.references[0].update_state == "update_available"


async def test_a_stored_status_is_shown_only_while_the_local_image_is_the_one_it_was_taken_for(
    image_rig: ImageRig,
) -> None:
    host = image_rig.host
    taken_for, moved_away, moved_to_remote = (
        "nginx:1",
        "ghcr.io/o/app:2",
        f"{ADDRESS}/app:3",
    )
    local, remote = digest(1), digest(2)
    host.add_image(taken_for, full_id(1), digests=(f"docker.io/library/nginx@{local}",))
    host.add_image(moved_away, full_id(2), digests=(f"ghcr.io/o/app@{digest(7)}",))
    host.add_image(moved_to_remote, full_id(3), digests=(f"{ADDRESS}/app@{remote}",))
    statuses = [
        StoredUpdateStatus(key, "update_available", None, local, remote, CHECKED_AT)
        for key in (
            "docker.io/library/nginx:1",
            "ghcr.io/o/app:2",
            f"{ADDRESS}/app:3",
        )
    ]
    await ImageUpdateStatusRepository(image_rig.db).upsert(ENV_A, statuses, prune_before=FAR_PAST)

    page = await image_rig.images.queries.page(ADMIN, ENV_A)

    shown = {r.key: r.update_state for item in page.page.items for r in item.references}
    assert shown == {
        "docker.io/library/nginx:1": "update_available",
        "ghcr.io/o/app:2": None,
        f"{ADDRESS}/app:3": "up_to_date",
    }
