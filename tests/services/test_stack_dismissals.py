"""Dismissing and restoring unmanaged stack entries, and what the lists and the adopt do after."""

from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_dismissal import StackDismissalRepository, StoredDismissal
from app.services.activity import ActivityRecorder
from app.services.assembly import build_access_services, build_host_clients
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from app.services.stack_dismissal_assembly import build_stack_dismissal_service
from app.services.stack_dismissal_views import HostMembers, members_digest
from app.services.stack_dismissals import (
    MAX_DISMISSED_OBJECTS,
    NotLeftoversOnly,
    StackDismissalService,
    TooManyObjects,
)
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.docker_engine.container_engine import VOLUME_CREATED, swarm_network_id
from tests.docker_engine.swarm_engine import plain_spec
from tests.stack_support import PROJECT_LABEL, Rig, compose_doc, project_box, seed_stack_row
from tests.swarm_stack_support import (
    StackDaemon,
    shop_file,
    shop_variables,
    swarm_services,
)
from tests.swarm_support import NAMESPACE_LABEL

PROJECT = "Gone"
KEY = "gone"
NAMESPACE = "Old"
NAMESPACE_KEY = "old"
NETWORK_NUMBER = 0x51
STALE_DIGEST = "0" * 64
DISMISSED = "stack_dismissed"
CLEARED = "stack_dismissal_cleared"
FIRST_CONTAINER = 1
INVALID_NAME = "has space"


def service_for(rig: Rig) -> StackDismissalService:
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    return build_stack_dismissal_service(
        rig.db, rig.runtime, rig.engine, access, inspector, recorder
    )


def stored_in(rig: Rig) -> StackDismissalRepository:
    return StackDismissalRepository(rig.db, rig.clock)


async def stored(rig: Rig, kind: Any, key: str) -> StoredDismissal | None:
    return await stored_in(rig).dismissal(ENV_A, kind, key)


async def compose_digest(rig: Rig, name: str = KEY) -> str:
    projects = await rig.fresh().queries.unmanaged(ADMIN, ENV_A)
    return next(p.objects_digest for p in projects if p.name == name)


async def swarm_digest(rig: Rig, name: str = NAMESPACE) -> str:
    page = await swarm_services(rig).stacks.page(ADMIN, ENV_A)
    return next(r.objects_digest for r in page.rows if r.name == name)


def leave_project(rig: Rig) -> tuple[str, str]:
    """Leave a volume and a network of the project `Gone`; return their tokens."""
    labels = {PROJECT_LABEL: PROJECT}
    rig.host.add_volume("gone_data", labels=labels)
    network_id = rig.host.add_network("gone_default", NETWORK_NUMBER, Labels=labels)
    return f"volume:gone_data:{VOLUME_CREATED}", f"network:{network_id}"


def leave_namespace(daemon: StackDaemon) -> tuple[str, str, str]:
    """Leave a config, a secret and a network of the namespace `Old`; return their tokens."""
    labels = {NAMESPACE_LABEL: NAMESPACE}
    config_id = daemon.add_config("old-conf", labels=labels)
    secret_id = daemon.add_secret("old-sec", labels=labels)
    daemon.add_volume("old_data", labels=labels)
    network_id = swarm_network_id(NETWORK_NUMBER)
    daemon.add_network("old_net", 0, "overlay", network_id=network_id, Scope="swarm", Labels=labels)
    return f"config:{config_id}", f"secret:{secret_id}", f"network:{network_id}"


async def dismiss_project(rig: Rig, digest: str | None = None) -> None:
    await service_for(rig).dismiss(
        ADMIN, ENV_A, "compose", PROJECT, digest or await compose_digest(rig)
    )


async def dismiss_namespace(rig: Rig, digest: str | None = None) -> None:
    await service_for(rig).dismiss(
        ADMIN, ENV_A, "swarm", NAMESPACE, digest or await swarm_digest(rig)
    )


async def test_dismissing_a_leftover_project_stores_its_tokens_and_records_the_dismissal(
    stack_rig: Rig,
) -> None:
    tokens = leave_project(stack_rig)

    await dismiss_project(stack_rig)

    held = await stored(stack_rig, "compose", KEY)
    assert held is not None
    assert held.objects == tuple(sorted(tokens))
    assert held.dismissed_by_user_id == ADMIN.user_id
    [row] = await stack_rig.rows(DISMISSED)
    assert (row["result"], row["target_type"], row["target_id"]) == ("success", "stack", KEY)
    assert row["environment_id"] == ENV_A and row["actor_user_id"] == ADMIN.user_id
    assert row["detail"] == {"name": KEY, "stack_kind": "compose", "objects": len(tokens)}


async def test_a_stale_digest_a_standard_user_and_a_project_with_a_container_are_refused(
    stack_rig: Rig,
) -> None:
    leave_project(stack_rig)
    digest = await compose_digest(stack_rig)

    with pytest.raises(ChangedSinceReview):
        await dismiss_project(stack_rig, STALE_DIGEST)
    with pytest.raises(PermissionDenied):
        await service_for(stack_rig).dismiss(STANDARD, ENV_A, "compose", PROJECT, digest)
    project_box(stack_rig.host, FIRST_CONTAINER, PROJECT, state="exited")
    with pytest.raises(NotLeftoversOnly) as with_container:
        await dismiss_project(stack_rig, digest)

    assert with_container.value.reason == "not_leftovers_only"
    assert await stored(stack_rig, "compose", KEY) is None
    failures = await stack_rig.rows(DISMISSED)
    assert [(row["result"], row["detail"]["reason"]) for row in failures] == [
        ("failure", "changed_since_review"),
        ("failure", "not_leftovers_only"),
    ]


async def test_an_unknown_a_managed_or_an_unusable_name_cannot_be_dismissed(
    stack_rig: Rig,
) -> None:
    leave_project(stack_rig)
    await seed_stack_row(stack_rig, ENV_A, KEY, compose_doc())
    service = service_for(stack_rig)

    with pytest.raises(ResourceNotFound):
        await service.dismiss(ADMIN, ENV_A, "compose", "nothere", STALE_DIGEST)
    with pytest.raises(ResourceNotFound):
        await service.dismiss(ADMIN, ENV_A, "compose", PROJECT, STALE_DIGEST)
    with pytest.raises(StackRejected) as unusable:
        await service.dismiss(ADMIN, ENV_A, "compose", INVALID_NAME, STALE_DIGEST)

    assert list(unusable.value.fields) == ["name"]
    assert await stored(stack_rig, "compose", KEY) is None


async def test_exactly_the_most_objects_a_dismissal_can_name_is_stored_and_one_more_is_refused(
    stack_rig: Rig,
) -> None:
    recorder = ActivityRecorder(stack_rig.db, stack_rig.clock)
    inspector = EngineObjectInspector(
        build_host_clients(stack_rig.db, stack_rig.runtime, stack_rig.engine)
    )
    access = build_access_services(stack_rig.db, stack_rig.runtime, inspector, recorder)

    def reader(count: int) -> Any:
        async def read(environment_id: int, key: str) -> HostMembers:
            return HostMembers(0, tuple(f"network:{n:04d}" for n in range(count)))

        return read

    def service_with(count: int) -> StackDismissalService:
        return StackDismissalService(
            db=stack_rig.db,
            authoriser=access.authoriser,
            hosts=inspector.hosts,
            stacks=StackRepository(stack_rig.db, stack_rig.registry.vault, stack_rig.clock),
            dismissals=stored_in(stack_rig),
            activity=recorder,
            compose=reader(count),
            swarm=reader(count),
        )

    too_many = tuple(f"network:{n:04d}" for n in range(MAX_DISMISSED_OBJECTS + 1))
    with pytest.raises(TooManyObjects) as refused:
        await service_with(MAX_DISMISSED_OBJECTS + 1).dismiss(
            ADMIN, ENV_A, "compose", PROJECT, members_digest(too_many)
        )
    exact = too_many[:MAX_DISMISSED_OBJECTS]
    await service_with(MAX_DISMISSED_OBJECTS).dismiss(
        ADMIN, ENV_A, "compose", PROJECT, members_digest(exact)
    )

    assert refused.value.reason == "too_many_objects"
    held = await stored(stack_rig, "compose", KEY)
    assert held is not None and len(held.objects) == MAX_DISMISSED_OBJECTS


async def test_restoring_removes_the_dismissal_and_records_it_and_restoring_again_records_nothing(
    stack_rig: Rig,
) -> None:
    leave_project(stack_rig)
    await dismiss_project(stack_rig)

    await service_for(stack_rig).clear(ADMIN, ENV_A, "compose", PROJECT)
    await service_for(stack_rig).clear(ADMIN, ENV_A, "compose", PROJECT)
    with pytest.raises(PermissionDenied):
        await service_for(stack_rig).clear(STANDARD, ENV_A, "compose", PROJECT)

    assert await stored(stack_rig, "compose", KEY) is None
    [row] = await stack_rig.rows(CLEARED)
    assert (row["result"], row["target_id"]) == ("success", KEY)
    assert row["detail"] == {"name": KEY, "stack_kind": "compose"}


async def test_a_dismissed_project_leaves_the_list_and_shows_again_when_something_new_appears(
    stack_rig: Rig,
) -> None:
    leave_project(stack_rig)
    await dismiss_project(stack_rig)

    hidden = await stack_rig.fresh().queries.page(ADMIN, ENV_A)
    shown = await stack_rig.fresh().queries.page(ADMIN, ENV_A, show_dismissed=True)
    unmanaged, covered = await stack_rig.fresh().queries.undismissed_unmanaged(ADMIN, ENV_A)
    stack_rig.host.add_network("gone_extra", NETWORK_NUMBER + 1, Labels={PROJECT_LABEL: PROJECT})
    with_object = await stack_rig.fresh().queries.page(ADMIN, ENV_A)
    project_box(stack_rig.host, FIRST_CONTAINER, PROJECT, state="exited")
    with_container = await stack_rig.fresh().queries.page(ADMIN, ENV_A)
    with pytest.raises(PermissionDenied):
        await stack_rig.fresh().queries.undismissed_unmanaged(STANDARD, ENV_A)

    assert hidden.unmanaged == () and hidden.dismissed_hidden == 1
    assert [p.name for p in shown.unmanaged] == [KEY] and shown.dismissed_hidden == 1
    assert shown.unmanaged[0].dismissal is not None and shown.unmanaged[0].dismissal.applies
    assert shown.unmanaged[0].dismissal.dismissed_by == ADMIN.username
    assert (unmanaged, covered) == ((), 1)
    for changed in (with_object, with_container):
        [entry] = changed.unmanaged
        assert entry.dismissal is not None and entry.dismissal.changed_since
        assert changed.dismissed_hidden == 0
    assert with_object.unmanaged[0].leftovers_only is True
    assert with_container.unmanaged[0].leftovers_only is False


async def test_adopting_or_creating_a_stack_deletes_the_compose_dismissal_in_the_same_transaction(
    stack_rig: Rig,
) -> None:
    leave_project(stack_rig)
    await dismiss_project(stack_rig)
    await stored_in(stack_rig).put_dismissal(ENV_A, "compose", "fresh", ["network:x"], None)

    await stack_rig.fresh().actions.adopt(ADMIN, ENV_A, name=KEY, compose_text=compose_doc())
    await stack_rig.fresh().actions.create(
        ADMIN, ENV_A, name="fresh", compose_text=compose_doc(), resolve_pulls=True
    )

    assert await stored(stack_rig, "compose", KEY) is None
    assert await stored(stack_rig, "compose", "fresh") is None
    [adopted] = await stack_rig.rows("stack_adopted")
    [created] = await stack_rig.rows("stack_created")
    assert adopted["detail"]["dismissal_cleared"] is True
    assert created["detail"]["dismissal_cleared"] is True


async def test_a_dismissal_of_another_name_or_kind_survives_an_adopt(stack_rig: Rig) -> None:
    leave_project(stack_rig)
    await stored_in(stack_rig).put_dismissal(ENV_A, "compose", "other", ["network:x"], None)
    await stored_in(stack_rig).put_dismissal(ENV_A, "swarm", KEY, ["config:x"], None)

    await stack_rig.fresh().actions.adopt(ADMIN, ENV_A, name=KEY, compose_text=compose_doc())

    assert await stored(stack_rig, "compose", "other") is not None
    assert await stored(stack_rig, "swarm", KEY) is not None
    [adopted] = await stack_rig.rows("stack_adopted")
    assert "dismissal_cleared" not in adopted["detail"]


async def test_dismissing_a_leftover_namespace_stores_its_config_secret_and_network_tokens(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    tokens = leave_namespace(stack_daemon)

    await dismiss_namespace(swarm_stack_rig)

    held = await stored(swarm_stack_rig, "swarm", NAMESPACE_KEY)
    assert held is not None and held.objects == tuple(sorted(tokens))
    [row] = await swarm_stack_rig.rows(DISMISSED)
    assert row["target_id"] == NAMESPACE_KEY
    assert row["detail"] == {"name": NAMESPACE_KEY, "stack_kind": "swarm", "objects": 3}


async def test_a_namespace_with_a_service_or_a_standard_user_is_refused(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    leave_namespace(stack_daemon)
    digest = await swarm_digest(swarm_stack_rig)
    stack_daemon.add_service(plain_spec("old_web", Labels={NAMESPACE_LABEL: NAMESPACE}))
    service = service_for(swarm_stack_rig)

    with pytest.raises(PermissionDenied):
        await service.dismiss(STANDARD, ENV_A, "swarm", NAMESPACE, digest)
    with pytest.raises(NotLeftoversOnly) as with_service:
        await service.dismiss(ADMIN, ENV_A, "swarm", NAMESPACE, digest)

    assert with_service.value.reason == "not_leftovers_only"
    assert await stored(swarm_stack_rig, "swarm", NAMESPACE_KEY) is None
    [row] = await swarm_stack_rig.rows(DISMISSED)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "not_leftovers_only")


async def test_a_namespace_whose_objects_changed_since_the_review_is_refused_as_changed(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    leave_namespace(stack_daemon)
    digest = await swarm_digest(swarm_stack_rig)
    stack_daemon.add_config("old-new", labels={NAMESPACE_LABEL: NAMESPACE})

    with pytest.raises(ChangedSinceReview):
        await dismiss_namespace(swarm_stack_rig, digest)

    assert await stored(swarm_stack_rig, "swarm", NAMESPACE_KEY) is None


async def test_a_dismissed_namespace_leaves_the_list_and_shows_again_when_something_new_appears(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    leave_namespace(stack_daemon)
    await dismiss_namespace(swarm_stack_rig)

    hidden = await swarm_services(swarm_stack_rig).stacks.page(ADMIN, ENV_A)
    shown = await swarm_services(swarm_stack_rig).stacks.page(ADMIN, ENV_A, show_dismissed=True)
    detail = await swarm_services(swarm_stack_rig).stacks.detail(ADMIN, ENV_A, NAMESPACE)
    stack_daemon.add_config("old-new", labels={NAMESPACE_LABEL: NAMESPACE})
    with_config = await swarm_services(swarm_stack_rig).stacks.page(ADMIN, ENV_A)
    stack_daemon.add_service(plain_spec("old_web", Labels={NAMESPACE_LABEL: NAMESPACE}))
    with_service = await swarm_services(swarm_stack_rig).stacks.page(ADMIN, ENV_A)
    await service_for(swarm_stack_rig).clear(ADMIN, ENV_A, "swarm", NAMESPACE)
    await service_for(swarm_stack_rig).clear(ADMIN, ENV_A, "swarm", NAMESPACE)

    assert hidden.rows == () and hidden.dismissed_hidden == 1 and hidden.total == 0
    assert [r.name for r in shown.rows] == [NAMESPACE] and shown.dismissed_hidden == 1
    assert shown.rows[0].dismissal is not None and shown.rows[0].dismissal.applies
    assert detail.dismissal is not None and detail.dismissal.applies
    for changed in (with_config, with_service):
        [row] = changed.rows
        assert row.dismissal is not None and row.dismissal.changed_since
        assert changed.dismissed_hidden == 0
    assert with_config.rows[0].leftovers_only is True
    assert with_service.rows[0].leftovers_only is False
    assert await stored(swarm_stack_rig, "swarm", NAMESPACE_KEY) is None
    assert len(await swarm_stack_rig.rows(CLEARED)) == 1


async def test_adopting_or_creating_a_swarm_stack_deletes_the_namespace_dismissal(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    leave_namespace(stack_daemon)
    await dismiss_namespace(swarm_stack_rig)
    await stored_in(swarm_stack_rig).put_dismissal(ENV_A, "swarm", "fresh", ["config:x"], None)

    await swarm_stack_rig.fresh().swarm_adopter.adopt(
        ADMIN, ENV_A, NAMESPACE, compose_text=shop_file(api=False), variables=shop_variables()
    )
    await swarm_stack_rig.fresh().swarm_actions.create(
        ADMIN, ENV_A, name="fresh", compose_text=shop_file(), variables=shop_variables()
    )

    assert await stored(swarm_stack_rig, "swarm", NAMESPACE_KEY) is None
    assert await stored(swarm_stack_rig, "swarm", "fresh") is None
    [adopted] = await swarm_stack_rig.rows("stack_adopted")
    [created] = await swarm_stack_rig.rows("stack_created")
    assert adopted["detail"]["dismissal_cleared"] is True
    assert created["detail"]["dismissal_cleared"] is True
