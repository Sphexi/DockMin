"""Adopting everything on a host: the review, the confirmed plan and the apply."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.db.repositories.resource_control import (
    ResourceControl,
    ResourceControlRepository,
    ResourceType,
)
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.registry_client.manifests import ManifestCheckKind
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from app.self_identity import NOT_CONTAINERISED, SelfIdentity
from app.services.activity import ActivityRecorder
from app.services.assembly import build_access_services, build_host_clients
from app.services.object_inspector import EngineObjectInspector, EnvironmentUnavailable
from app.services.onboarding import OnboardingChanged
from app.services.onboarding_assembly import OnboardingServices, build_onboarding_services
from app.services.onboarding_ownership import Ownership
from app.services.onboarding_plan import (
    ObjectTarget,
    OnboardingReview,
    PlannedControl,
    Selection,
)
from app.services.pull_credentials import CredentialChoice
from app.web.onboarding_pages import image_rows
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.auth_support import STANDARD_ID, STANDARD_USERNAME
from tests.docker_engine.container_engine import ContainerEngine
from tests.host_read_support import (
    PROJECT,
    ReaderEngine,
    add_project_container,
    compose_labels,
)
from tests.image_support import ScriptedManifests, binding_rows, digest, full_id, register, rows_for
from tests.registry_support import ADDRESS, make_team
from tests.stack_files_support import use_files_compose, use_files_compose_for_swarm
from tests.stack_support import PROJECT_LABEL, SERVICE_LABEL, Rig, SimRun
from tests.swarm_stack_support import NAME, StackDaemon, create_stack, deploy_stack
from tests.swarm_support import NAMESPACE_LABEL

COMPOSE = "services:\n  web:\n    image: nginx:1\n"
GHOST = "ghost"
REFERENCE = f"{ADDRESS}/app:1"
LOCAL = digest(2)
ALPHA_USER = "user-alpha"
VOLUME = "data"
NETWORK = "backend"
LOOSE = 0xC1
OWN_CONTAINER = f"{0xD0:064x}"
OWN_VOLUME = "dockmin-data"
OWN_PROJECT = "dockmin"
TEAM = "platform"
LEVEL = "read"
DISMISSED_PROJECTS = 201
DISMISSED_NETWORK_BASE = 0x1000
OLD = "Old"


class ByLogin(ScriptedManifests):
    """Answers by the login's username; anonymous and any other login is refused."""

    def __init__(self, answers: dict[str | None, str]) -> None:
        super().__init__()
        self.by_login: dict[str | None, str | ManifestCheckKind] = dict(answers)

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        self.default = self.by_login.get(
            None if login is None else login.username, ManifestCheckKind.AUTHENTICATION
        )
        return await super().head_digest(target, reference, login)


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[ContainerEngine]:
    daemon = ReaderEngine()
    yield daemon
    daemon.release_gates()


def onboarding_for(rig: Rig, identity: SelfIdentity = NOT_CONTAINERISED) -> OnboardingServices:
    """Build the services as a new request would."""
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    return build_onboarding_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine,
        rig.registry.registries,
        access,
        inspector,
        recorder,
        lambda: identity,
    )


async def stock(rig: Rig, tmp_path: Path) -> ReaderEngine:
    """Put a readable project, a project with no file, an image and loose objects on the host."""
    host = rig.host
    assert isinstance(host, ReaderEngine)
    await use_files_compose(rig)
    host.serve("compose.yaml", f"x-fake:\n  record: {tmp_path / 'record'}\n{COMPOSE}")
    add_project_container(host, 1, compose_labels(), state="running")
    host.add_network("ghost_default", 0x99, Labels={PROJECT_LABEL: GHOST})
    host.add_image(REFERENCE, full_id(1), digests=(f"{ADDRESS}/app@{LOCAL}",))
    host.add(f"{LOOSE:064x}", "loose", image=REFERENCE)
    host.add_volume(VOLUME)
    host.add_network(NETWORK, 0x77)
    return host


async def review_of(rig: Rig, identity: SelfIdentity = NOT_CONTAINERISED) -> OnboardingReview:
    return await onboarding_for(rig, identity).onboarding.review(ADMIN, ENV_B)


def indexes(review: OnboardingReview) -> Selection:
    """Tick everything the review offers, with no ownership."""
    return Selection(
        stacks=frozenset(i for i, s in enumerate(review.stacks) if s.offered),
        images={i: None for i, t in enumerate(review.images) if t.outcome.outcome == "binds"},
        objects=frozenset(range(len(review.objects))),
    )


def with_ownership(selection: Selection, ownership: Ownership) -> Selection:
    return Selection(selection.stacks, selection.images, selection.objects, ownership)


async def ownership_of(rig: Rig) -> Ownership:
    team = await make_team(rig.db, TEAM, STANDARD_ID)
    return Ownership(STANDARD_ID, STANDARD_USERNAME, team, TEAM, LEVEL)


async def control_of(rig: Rig, kind: ResourceType, object_id: str) -> ResourceControl | None:
    return await ResourceControlRepository(rig.db, rig.clock).get_for(ENV_B, kind, object_id)


def names(review: OnboardingReview) -> set[tuple[str, str]]:
    return {(item.type, item.name) for item in review.objects}


async def test_the_review_lists_each_project_image_and_object_and_never_offers_dockmins_own(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    await register(rig, "alpha", environments=(ENV_B,))
    rig.registry.registries.manifests = ByLogin({ALPHA_USER: LOCAL})  # type: ignore[assignment]
    host.add(OWN_CONTAINER, "dockmin-app", image="nginx:1")
    host.add_volume(OWN_VOLUME)
    add_project_container(
        host, 5, {PROJECT_LABEL: OWN_PROJECT, SERVICE_LABEL: "app"}, state="running"
    )
    identity = SelfIdentity(
        container_id=OWN_CONTAINER,
        volumes={ENV_B: frozenset({OWN_VOLUME})},
        compose_project={ENV_B: OWN_PROJECT},
    )

    review = await review_of(rig, identity)

    by_name = {item.name: item for item in review.stacks}
    assert set(by_name) == {PROJECT, GHOST}
    assert (by_name[PROJECT].result or None) is not None
    assert by_name[PROJECT].result.file_source == "read"  # type: ignore[union-attr]
    assert by_name[GHOST].offered is False and by_name[GHOST].needs is not None
    [binding] = [t for t in review.images if t.reference == f"{ADDRESS}/app:1"]
    assert binding.outcome.outcome == "binds" and binding.offered
    assert ("container", "loose") in names(review)
    assert ("volume", VOLUME) in names(review) and ("network", NETWORK) in names(review)
    offered = names(review)
    for own in (("container", "dockmin-app"), ("volume", OWN_VOLUME)):
        assert own not in offered
    assert {(item.type, item.name) for item in review.protected} >= {
        ("container", "dockmin-app"),
        ("volume", OWN_VOLUME),
        ("stack", OWN_PROJECT),
    }
    assert not any("dockmin" in name for _, name in offered)
    assert not any(item.name == OWN_PROJECT for item in review.stacks)
    reads = await rig.rows("stack_files_read")
    assert sorted(row["target_id"] for row in reads) == [GHOST, PROJECT]


async def test_the_plan_names_each_ticked_target_and_binds_to_the_digest_of_its_lines(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    alpha = await register(rig, "alpha", environments=(ENV_B,))
    rig.registry.registries.manifests = ByLogin({ALPHA_USER: LOCAL})  # type: ignore[assignment]
    review = await review_of(rig)
    ownership = await ownership_of(rig)
    selection = with_ownership(indexes(review), ownership)

    plan = await onboarding_for(rig).onboarding.plan(ADMIN, ENV_B, review, selection)

    lines = plan.lines
    assert list(lines) == sorted(lines)
    assert any(line.startswith(f"stack compose {PROJECT} ") for line in lines)
    assert f"image {REFERENCE} {alpha} {LOCAL}" in lines
    assert f"control volume {VOLUME} {STANDARD_ID} {ownership.team_id} {LEVEL}" in lines
    assert {(item.target.type, item.target.name) for item in plan.controls} == names(review)
    assert (
        plan.digest
        == (await onboarding_for(rig).onboarding.plan(ADMIN, ENV_B, review, selection)).digest
    )


async def test_an_owner_and_a_team_give_every_object_that_owner_and_a_team_grant_never_public(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    alpha = await register(rig, "alpha", environments=(ENV_B,))
    rig.registry.registries.manifests = ByLogin({ALPHA_USER: LOCAL})  # type: ignore[assignment]
    review = await review_of(rig)
    ownership = await ownership_of(rig)
    selection = with_ownership(indexes(review), ownership)
    services = onboarding_for(rig)
    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    requests_before = len(host.requests)

    result = await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    assert [i.status for i in result.items] == ["done"] * len(result.items)
    assert result.count("stack", "done") == 1 and result.count("image", "done") == 1
    repository = ResourceControlRepository(rig.db, rig.clock)
    objects: list[tuple[ResourceType, str]] = [
        ("volume", VOLUME),
        ("network", f"{0x77:064x}"),
        ("container", f"{LOOSE:064x}"),
    ]
    for kind, object_id in objects:
        control = await repository.get_for(ENV_B, kind, object_id)
        assert control is not None
        assert (control.owner_user_id, control.is_public, control.access_level) == (
            STANDARD_ID,
            False,
            LEVEL,
        )
        assert (await repository.list_grants(control.id)).team_ids == (ownership.team_id,)
    stack_control = await control_of(rig, "stack", PROJECT)
    assert stack_control is not None and stack_control.owner_user_id == STANDARD_ID
    assert (await repository.list_grants(stack_control.id)).team_ids == (ownership.team_id,)
    created = await rows_for(rig.db, "resource_control_created")
    assert len(created) == len(plan.controls)
    assert all(row["detail"]["onboarding"] is True for row in created)
    assert len({row["target_id"] for row in created}) == len(created)
    [adopted] = await rows_for(rig.db, "stack_adopted")
    assert adopted["detail"]["onboarding"] is True and adopted["detail"]["as_is"] is True
    [bound] = await rows_for(rig.db, "image_bound")
    assert (bound["detail"]["verified"], bound["detail"]["pulled"]) == (True, False)
    assert [row["registry_credential_id"] for row in await binding_rows(rig.db)] == [alpha]
    writes = [r for r in host.requests[requests_before:] if r.method in ("POST", "DELETE")]
    assert writes == []


async def test_with_neither_an_owner_nor_a_team_objects_and_stacks_get_no_control(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    review = await review_of(rig)
    selection = indexes(review)
    services = onboarding_for(rig)

    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    result = await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    assert plan.controls == () and plan.left == len(review.objects)
    assert result.count("stack", "done") == 1 and result.of("object") == ()
    assert await rows_for(rig.db, "resource_control_created") == []
    assert await control_of(rig, "stack", PROJECT) is None
    assert await control_of(rig, "volume", VOLUME) is None


async def test_a_volume_with_a_candidate_takes_that_owner_when_none_is_chosen(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    mounting = f"{0xE1:064x}"
    host.add(
        mounting,
        "mounting",
        image=REFERENCE,
        mounts=[{"Type": "volume", "Name": VOLUME, "Destination": "/data"}],
    )
    await ResourceControlRepository(rig.db, rig.clock).create(
        ENV_B,
        "container",
        mounting,
        is_public=False,
        is_administrators_only=False,
        access_level="write",
        owner_user_id=STANDARD_ID,
    )
    review = await review_of(rig)
    [volume] = [t for t in review.objects if t.type == "volume"]
    assert volume.owner is not None and volume.owner.user_id == STANDARD_ID
    selection = Selection(objects=frozenset(range(len(review.objects))))
    services = onboarding_for(rig)

    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    control = await control_of(rig, "volume", VOLUME)
    assert control is not None and control.owner_user_id == STANDARD_ID
    [row] = [
        r for r in await rows_for(rig.db, "resource_control_created") if r["target_id"] == VOLUME
    ]
    assert row["detail"]["candidate_source"] == "container_control"
    assert await control_of(rig, "network", f"{0x77:064x}") is None


async def test_a_host_that_changed_since_the_review_refuses_the_whole_request_before_any_write(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    await register(rig, "alpha", environments=(ENV_B,))
    rig.registry.registries.manifests = ByLogin({ALPHA_USER: LOCAL})  # type: ignore[assignment]
    review = await review_of(rig)
    ownership = await ownership_of(rig)
    selection = with_ownership(indexes(review), ownership)
    services = onboarding_for(rig)
    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    await ResourceControlRepository(rig.db, rig.clock).create(
        ENV_B,
        "volume",
        VOLUME,
        is_public=False,
        is_administrators_only=False,
        access_level="write",
        owner_user_id=STANDARD_ID,
    )

    with pytest.raises(OnboardingChanged) as refused:
        await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    assert refused.value.reason == "changed_since_review"
    assert await rows_for(rig.db, "stack_adopted") == []
    assert await rows_for(rig.db, "image_bound") == []
    assert await rows_for(rig.db, "resource_control_created") == []
    assert await binding_rows(rig.db) == []
    assert await control_of(rig, "stack", PROJECT) is None


async def test_an_object_that_changed_after_the_recomputation_is_skipped_and_the_rest_proceed(
    stack_rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    review = await review_of(rig)
    ownership = await ownership_of(rig)
    selection = with_ownership(indexes(review), ownership)
    services = onboarding_for(rig)
    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    original = services.onboarding.plan

    async def plan_then_change(*args: Any) -> Any:
        recomputed = await original(*args)
        await ResourceControlRepository(rig.db, rig.clock).create(
            ENV_B,
            "volume",
            VOLUME,
            is_public=False,
            is_administrators_only=False,
            access_level="write",
            owner_user_id=STANDARD_ID,
        )
        return recomputed

    monkeypatch.setattr(services.onboarding, "plan", plan_then_change)

    result = await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    by_label = {item.label: item for item in result.of("object")}
    assert by_label[f"volume {VOLUME}"].status == "skipped"
    assert {item.status for label, item in by_label.items() if label != f"volume {VOLUME}"} == {
        "done"
    }
    assert result.count("stack", "done") == 1
    raced = await control_of(rig, "volume", VOLUME)
    assert raced is not None and raced.owner_user_id == STANDARD_ID
    assert len(await rows_for(rig.db, "resource_control_created")) == len(plan.controls) - 1


async def test_a_failed_control_phase_scan_fails_each_control_and_still_returns_the_result(
    stack_rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    review = await review_of(rig)
    selection = with_ownership(indexes(review), await ownership_of(rig))
    services = onboarding_for(rig)
    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    scanned = services.onboarding._current_objects
    calls = 0

    async def scan_then_lose_the_host(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise EnvironmentUnavailable("unreachable")
        return await scanned(*args)

    monkeypatch.setattr(services.onboarding, "_current_objects", scan_then_lose_the_host)

    result = await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    assert result.count("stack", "done") == 1
    assert [item.status for item in result.of("object")] == ["failed"] * len(plan.controls)
    assert {item.message for item in result.of("object")} == {
        str(EnvironmentUnavailable("unreachable"))
    }
    assert await rows_for(rig.db, "resource_control_created") == []


async def test_a_project_redeployed_between_the_review_and_the_apply_is_skipped_not_adopted(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    review = await review_of(rig)
    selection = Selection(stacks=frozenset(i for i, s in enumerate(review.stacks) if s.offered))
    services = onboarding_for(rig)
    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    del host.boxes[f"{0xB0001:064x}"]
    add_project_container(host, 2, compose_labels(), state="running")

    result = await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    [item] = result.of("stack")
    assert (item.status, item.message) == ("skipped", "It changed on the host since the review.")
    [attempt] = await rows_for(rig.db, "stack_adopted")
    assert (attempt["result"], attempt["detail"]["reason"]) == (
        "failure",
        "changed_since_review",
    )
    assert await control_of(rig, "stack", PROJECT) is None


async def test_claiming_an_owner_stores_the_access_level_the_plan_carries(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    planned = PlannedControl(
        ObjectTarget("volume", VOLUME, VOLUME),
        STANDARD_ID,
        STANDARD_USERNAME,
        None,
        "",
        "read",
        None,
    )

    claimed = await onboarding_for(rig).onboarding.claim_owner(ADMIN, ENV_B, planned)

    control = await control_of(rig, "volume", VOLUME)
    assert claimed is True and control is not None
    assert (control.owner_user_id, control.access_level) == (STANDARD_ID, "read")
    [row] = await rows_for(rig.db, "resource_control_created")
    assert row["detail"]["access_level"] == "read"


async def test_apply_runs_no_compose_up_and_no_engine_mutation(
    stack_rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    await register(rig, "alpha", environments=(ENV_B,))
    rig.registry.registries.manifests = ByLogin({ALPHA_USER: LOCAL})  # type: ignore[assignment]
    review = await review_of(rig)
    selection = with_ownership(indexes(review), await ownership_of(rig))
    services = onboarding_for(rig)
    plan = await services.onboarding.plan(ADMIN, ENV_B, review, selection)
    compose_calls: list[str] = []

    def forbidden(name: str) -> Any:
        async def refuse(self: Any, *args: Any, **kwargs: Any) -> Any:
            compose_calls.append(name)
            raise AssertionError(f"compose {name} ran")

        return refuse

    for verb in ("compose_up", "compose_down", "compose_start", "compose_stop"):
        monkeypatch.setattr(SimRun, verb, forbidden(verb))
    mark = len(host.requests)

    await services.onboarding.apply(ADMIN, ENV_B, review, selection, plan.digest)

    mutating = [f"{r.method} {r.path}" for r in host.requests[mark:] if r.method != "GET"]
    assert (compose_calls, mutating) == ([], [])


async def test_a_standard_user_cannot_review_plan_or_resolve_ownership(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    services = onboarding_for(rig)
    review = await review_of(rig)

    with pytest.raises(PermissionDenied):
        await services.onboarding.review(STANDARD, ENV_B)
    with pytest.raises(PermissionDenied):
        await services.onboarding.plan(STANDARD, ENV_B, review, Selection())
    with pytest.raises(PermissionDenied):
        await services.onboarding.resolve_ownership(STANDARD, "", "", "write")


async def test_an_unknown_owner_or_team_and_a_bad_level_are_refused_by_field(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    services = onboarding_for(rig)

    with pytest.raises(ValidationFailed) as refused:
        await services.onboarding.resolve_ownership(ADMIN, "nobody-here", "no-such-team", "admin")

    assert set(refused.value.fields) == {"owner", "team", "level"}


async def test_an_image_only_the_registry_serves_without_credentials_needs_no_binding(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    await register(rig, "alpha", environments=(ENV_B,))
    manifests = ByLogin({ALPHA_USER: digest(9), None: LOCAL})
    rig.registry.registries.manifests = manifests  # type: ignore[assignment]

    review = await review_of(rig)

    [index] = [i for i, t in enumerate(review.images) if t.reference == REFERENCE]
    target = review.images[index]
    [row] = [r for r in image_rows(review) if r.index == index]
    assert (target.outcome.outcome, target.offered) == ("anonymous_only", False)
    assert (row.offered, row.ticked, row.unneeded) == (False, False, True)
    assert "needs no binding" in row.outcome
    assert [login is None for _, _, login in manifests.calls] == [False, True]
    with pytest.raises(ValidationFailed):
        await onboarding_for(rig).onboarding.plan(
            ADMIN, ENV_B, review, Selection(images={index: CredentialChoice(None)})
        )
    assert await binding_rows(rig.db) == []


async def test_a_ticked_index_the_review_does_not_hold_is_refused(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    await stock(rig, tmp_path)
    review = await review_of(rig)

    with pytest.raises(ValidationFailed):
        await onboarding_for(rig).onboarding.plan(
            ADMIN, ENV_B, review, Selection(objects=frozenset({len(review.objects)}))
        )
    with pytest.raises(ValidationFailed):
        await onboarding_for(rig).onboarding.plan(
            ADMIN, ENV_B, review, Selection(images={0: CredentialChoice(None)})
        )


async def test_a_swarm_namespace_is_reviewed_and_adopted_with_the_chosen_owner_and_team(
    defaulting_stack_rig: Rig, tmp_path: Path
) -> None:
    rig = defaulting_stack_rig
    daemon = rig.host
    assert isinstance(daemon, StackDaemon)
    await use_files_compose_for_swarm(rig, tmp_path / "files-compose")
    await create_stack(rig)
    await deploy_stack(rig)
    deployed = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME)
    await rig.fresh().swarm_actions.forget(ADMIN, ENV_A, NAME, stack_id=deployed.id)
    await ResourceControlRepository(rig.db, rig.clock).delete_for(ENV_A, "stack", NAME)
    ownership = await ownership_of(rig)
    services = onboarding_for(rig)
    writes = len(daemon.mutating())

    review = await services.onboarding.review(ADMIN, ENV_A)

    [target] = [item for item in review.stacks if item.kind == "swarm"]
    assert target.name == NAME and target.offered
    members = ("service", "config", "secret")
    assert [item.type for item in review.objects if item.type in members] == []
    selection = Selection(stacks=frozenset({review.stacks.index(target)}), ownership=ownership)
    plan = await services.onboarding.plan(ADMIN, ENV_A, review, selection)
    result = await services.onboarding.apply(ADMIN, ENV_A, review, selection, plan.digest)

    assert result.count("stack", "done") == 1
    repository = ResourceControlRepository(rig.db, rig.clock)
    control = await repository.get_for(ENV_A, "stack", NAME)
    assert control is not None and control.owner_user_id == STANDARD_ID
    assert (await repository.list_grants(control.id)).team_ids == (ownership.team_id,)
    [adopted] = await rig.rows("stack_adopted")
    assert adopted["detail"]["onboarding"] is True and adopted["detail"]["kind"] == "swarm"
    assert len(daemon.mutating()) == writes


async def test_the_review_leaves_dismissed_projects_out_counts_them_and_they_do_not_use_the_cap(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    dismissals = StackDismissalRepository(rig.db, rig.clock)
    for number in range(DISMISSED_PROJECTS):
        project = f"gone{number:03d}"
        network_id = host.add_network(
            f"{project}_default", DISMISSED_NETWORK_BASE + number, Labels={PROJECT_LABEL: project}
        )
        await dismissals.put_dismissal(ENV_B, "compose", project, [f"network:{network_id}"], None)

    review = await review_of(rig)

    assert {item.name for item in review.stacks} == {PROJECT, GHOST}
    assert review.dismissed == DISMISSED_PROJECTS


async def test_a_dismissed_project_that_changed_since_is_reviewed_again_and_not_counted(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = await stock(rig, tmp_path)
    await StackDismissalRepository(rig.db, rig.clock).put_dismissal(
        ENV_B, "compose", GHOST, [f"network:{0x99:064x}"], None
    )
    before = await review_of(rig)
    host.add_network("ghost_extra", 0x9A, Labels={PROJECT_LABEL: GHOST})

    after = await review_of(rig)

    assert {item.name for item in before.stacks} == {PROJECT}
    assert before.dismissed == 1
    assert {item.name for item in after.stacks} == {PROJECT, GHOST}
    assert after.dismissed == 0


async def test_the_review_leaves_a_dismissed_swarm_namespace_out_and_counts_it(
    defaulting_stack_rig: Rig,
) -> None:
    rig = defaulting_stack_rig
    daemon = rig.host
    assert isinstance(daemon, StackDaemon)
    config_id = daemon.add_config("old-conf", labels={NAMESPACE_LABEL: OLD})
    before = await onboarding_for(rig).onboarding.review(ADMIN, ENV_A)
    await StackDismissalRepository(rig.db, rig.clock).put_dismissal(
        ENV_A, "swarm", OLD, [f"config:{config_id}"], None
    )

    after = await onboarding_for(rig).onboarding.review(ADMIN, ENV_A)

    assert [(item.kind, item.name) for item in before.stacks] == [("swarm", OLD)]
    assert before.dismissed == 0
    assert after.stacks == () and after.dismissed == 1
