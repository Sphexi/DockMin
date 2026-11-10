"""container-updater on standalone containers: detection, update, refusal, rollback and holds."""

from datetime import timedelta
from functools import reduce
from typing import Any

import pytest

import app.subsystems.container_updater as updater
from app.db.repositories.job_run import MAX_RUN_DETAIL_KEYS
from app.docker_engine.errors import EngineError
from app.registry_client.manifests import ManifestCheckKind
from app.scheduler.model import RunResult
from app.scheduler.policy import failures_after
from app.services.activity import ActivityRecorder, SubsystemActor, UserActor
from app.services.image_bindings import BindingPullCredentials, SetCandidates
from app.services.image_updates import ImageUpdateService
from app.services.object_locks import LockHolder, ObjectKey
from app.services.registry_resolution import CredentialSetUnavailable
from app.subsystems.container_updater import ContainerUpdater
from tests.access_support import ADMIN, ENV_A, PEER_ID, STANDARD
from tests.automation_support import Subject, counts
from tests.docker_engine.container_engine import created_id
from tests.docker_engine.fake_engine import Refuse
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    digest,
    full_id,
    mark_missing,
    move_sets_elsewhere,
    register,
)
from tests.provisioning_support import OLD, old_body
from tests.registry_support import ADDRESS, bind_image, grant_user
from tests.services.conftest import ImageRig
from tests.support import present
from tests.updater_support import (
    ACTION,
    CALLER_ADDRESS,
    ISSUED_AT,
    PENDING_CALLS,
    RECEIVER,
    UPDATER,
    World,
    arm_webhook,
    as_webhook,
    container,
    labelled,
)

SWARM_LABEL = "com.docker.swarm.service.id"
PROJECT_LABEL = "com.docker.compose.project"
REPORT = {"action": "report"}
STANDARD_ID = STANDARD.user_id
NEW = created_id(1)
OTHER = f"{0xB2:064x}"
THIRD = f"{0xB3:064x}"
SHORT_IMAGE_ID = "625a12e1e4ee"
FULL_IMAGE_ID = "sha256:" + SHORT_IMAGE_ID + "0" * 52
PINNED_REFERENCE = f"{ADDRESS}/app@{digest(5)}"
RECREATE_VERBS = ["pull", "stop", "rename", "create", "start", "remove"]
BUDGET_RUNNING_OUT = timedelta(minutes=31)
COMPOSE_PROJECT = "shop"
MANAGED_NOTE = f"composed is managed by the Compose project {COMPOSE_PROJECT}"


def crash_new_container(world: World) -> None:
    """Make the replacement container exit as soon as the updater looks at it."""

    def crash() -> None:
        box = world.host.boxes.get(NEW)
        if box is not None and box.state == "running":
            box.state = "exited"
            box.exit_code = 1

    world.host.on("inspect", crash)


def compose_managed_web(world: World) -> None:
    """Add a running container named `composed` that belongs to a Compose project."""
    network_id = next(iter(world.host.networks))
    body = old_body(
        network_id, config={"Image": REFERENCE, "Labels": {PROJECT_LABEL: COMPOSE_PROJECT}}
    )
    world.host.add_configured(THIRD, "composed", body, "running")


async def hand_refusal(world: World, rule_id: int, signature: str) -> None:
    """Write the refusal row a container carried before its reason stopped refusing."""
    await ActivityRecorder(world.db, world.rig.runtime.clock).record_failure(
        SubsystemActor(UPDATER, rule_id, None, "Heal web"),
        action="image_update_refused",
        target_type="container",
        target_id=OLD,
        environment_id=ENV_A,
        reason="credential_missing",
        detail={"name": "web", "reference": REFERENCE, "refused": signature},
        message="x",
    )


async def failed_update(world: World) -> tuple[int, int]:
    """Run an update whose replacement exits, and return the rule and job ids."""
    world.new_image(3)
    crash_new_container(world)
    rule_id, job = await world.rule(container("web"))
    result = await world.run(job)
    assert result.outcome == "failure"
    return rule_id, job


async def test_a_report_run_names_the_reference_and_changes_nothing(world: World) -> None:
    _, job = await world.rule(container("web"), REPORT)

    result = await world.run(job)

    assert (result.outcome, result.attempted) == ("success", 0)
    detail = counts(result)
    assert (detail["mode"], detail["update_available"]) == ("report", 1)
    assert detail["update_available_refs"] == REFERENCE
    assert world.rows() == [] and world.verbs() == []


async def test_a_dry_run_forecasts_the_update_and_writes_and_changes_nothing(
    world: World,
) -> None:
    _, job = await world.rule(container("web"), REPORT)

    result = await world.run(job, dry=True)

    detail = counts(result)
    assert result.outcome == "success"
    assert (detail["would_update"], detail["would_update_count"]) == ("web", 1)
    assert detail["would_pull"] == REFERENCE
    assert world.rows() == [] and world.verbs() == []


async def test_a_dry_run_counts_a_refusal_without_writing_it(
    world: World, image_rig: ImageRig
) -> None:
    await move_sets_elsewhere(image_rig.db)
    _, job = await world.rule(container("web"))

    result = await world.run(job, dry=True)

    assert counts(result)["refused"] == 1
    assert world.rows() == [] and world.verbs() == []


async def test_an_update_pulls_then_recreates_and_both_rows_name_the_rule_and_run(
    world: World,
) -> None:
    rule_id, job = await world.rule(container("web"), name="Keep web fresh")

    result = await world.run(job)
    again = await world.run(job)

    assert result.outcome == "success" and result.attempted == 1
    detail = counts(result)
    assert (detail["updated"], detail["pulled"]) == (1, 1)
    assert world.verbs()[:6] == RECREATE_VERBS
    [pulled] = world.rows("image_pulled")
    [recreated] = world.rows("container_recreated")
    for row in (pulled, recreated):
        assert (row["actor_subsystem"], row["rule_id"]) == (UPDATER, rule_id)
        assert row["detail"]["rule_name"] == "Keep web fresh"
        assert row["detail"]["trigger"] == "update"
    assert pulled["detail"]["remote_digest"] == digest(2)
    assert recreated["detail"]["update_reason"] == "update_available"
    assert recreated["detail"]["verified"] is True
    assert {row["actor_subsystem"] for row in world.rows()} == {UPDATER}
    assert counts(again)["up_to_date"] == 1


async def test_the_pull_resolves_only_the_bound_set_with_no_user_and_no_choice(
    world: World, image_rig: ImageRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = await register(image_rig, "other")
    asked: list[tuple[Any, Any, int | None]] = []
    candidate_reads: list[str] = []
    original = BindingPullCredentials.resolve

    async def spy(
        self: Any, environment_id: int, reference: str, *, principal: Any, choice: Any = None
    ) -> Any:
        resolved = await original(
            self, environment_id, reference, principal=principal, choice=choice
        )
        asked.append((principal, choice, resolved.credential_set_id))
        return resolved

    def counting(name: str) -> Any:
        original_read = getattr(SetCandidates, name)

        async def read(self: Any, *arguments: Any, **keywords: Any) -> Any:
            candidate_reads.append(name)
            return await original_read(self, *arguments, **keywords)

        return read

    monkeypatch.setattr(BindingPullCredentials, "resolve", spy)
    for name in ("at", "eligible", "get"):
        monkeypatch.setattr(SetCandidates, name, counting(name))
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    assert result.outcome == "success"
    [(principal, choice, chosen)] = asked
    assert (principal, choice) == (None, None)
    assert chosen is not None and chosen != other
    assert candidate_reads == []


async def test_a_binding_that_does_not_fit_the_image_is_refused_once_then_held_without_a_pull(
    world: World, image_rig: ImageRig
) -> None:
    await move_sets_elsewhere(image_rig.db)
    rule_id, job = await world.rule(container("web"))

    result = await world.run(job)
    world.advance(10)
    held = await world.run(job)

    assert (result.outcome, result.reason, result.attempted) == (
        "failure",
        "credential_mismatch",
        1,
    )
    assert counts(result)["refused"] == 1
    [row] = world.rows("image_update_refused")
    assert (row["result"], row["target_id"]) == ("failure", OLD)
    assert row["detail"]["reason"] == "credential_mismatch"
    assert (row["actor_subsystem"], row["rule_id"]) == (UPDATER, rule_id)
    assert world.verbs() == []
    assert (held.outcome, counts(held)["held"]) == ("success", 1)
    assert len(world.rows("image_update_refused")) == 1


async def test_a_credential_that_vanishes_between_the_check_and_the_pull_is_refused_with_a_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(self: Any, *arguments: Any, **keywords: Any) -> Any:
        raise CredentialSetUnavailable("missing")

    monkeypatch.setattr(BindingPullCredentials, "resolve", refuse)
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    assert result.reason == "credential_unavailable"
    [row] = world.rows("image_update_refused")
    assert row["detail"]["reason"] == "credential_unavailable"
    assert world.verbs() == []


async def test_an_image_whose_bound_set_was_deleted_is_updated_anonymously_and_counted(
    world: World, image_rig: ImageRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_id = (present(await image_rig.db.fetch_one("SELECT id FROM registry_credential")))["id"]
    await mark_missing(image_rig.db, set_id)
    chosen: list[int | None] = []
    original = BindingPullCredentials.resolve

    async def spy(self: Any, *arguments: Any, **keywords: Any) -> Any:
        resolved = await original(self, *arguments, **keywords)
        chosen.append(resolved.credential_set_id)
        return resolved

    monkeypatch.setattr(BindingPullCredentials, "resolve", spy)
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    detail = counts(result)
    assert result.outcome == "success"
    assert (detail["updated"], detail["anonymous_fallback"]) == (1, 1)
    assert chosen == [None]
    assert world.rows("image_update_refused") == []
    assert world.verbs()[:6] == RECREATE_VERBS


async def test_an_unbound_image_is_updated_with_no_binding_written(
    world: World, image_rig: ImageRig
) -> None:
    await image_rig.db.execute("DELETE FROM image_credential_binding")
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    assert "anonymous_fallback" not in counts(result)
    assert (await image_rig.db.fetch_one("SELECT 1 FROM image_credential_binding")) is None


async def test_an_image_that_needs_authentication_is_refused_once_held_and_released_by_a_binding(
    world: World, image_rig: ImageRig
) -> None:
    await image_rig.db.execute("DELETE FROM image_credential_binding")
    world.manifests.default = ManifestCheckKind.AUTHENTICATION
    _, job = await world.rule(container("web"))

    first = await world.run(job)
    world.advance(10)
    held = await world.run(job)

    assert (first.outcome, first.reason, first.attempted) == (
        "failure",
        "authentication_required",
        1,
    )
    [row] = world.rows("image_update_refused")
    assert (row["detail"]["reason"], row["target_id"]) == ("authentication_required", OLD)
    assert (held.outcome, counts(held)["held"]) == ("success", 1)
    assert len(world.rows("image_update_refused")) == 1
    assert reduce(failures_after, [first.outcome, held.outcome], 0) == 0
    assert world.verbs() == []

    set_id = (present(await image_rig.db.fetch_one("SELECT id FROM registry_credential")))["id"]
    await bind_image(image_rig.db, set_id, ENV_A, REFERENCE_KEY)
    world.new_image(2)
    world.advance(10)
    released = await world.run(job)

    assert (released.outcome, counts(released)["updated"]) == ("success", 1)
    assert world.verbs()[:6] == RECREATE_VERBS


async def test_a_report_run_over_an_image_that_needs_authentication_fails_each_time(
    world: World, image_rig: ImageRig
) -> None:
    await image_rig.db.execute("DELETE FROM image_credential_binding")
    world.manifests.default = ManifestCheckKind.AUTHENTICATION
    _, job = await world.rule(container("web"), REPORT)

    results = [await world.run(job), await world.run(job)]

    for result in results:
        assert (result.outcome, result.reason) == ("failure", "authentication_required")
    assert world.rows() == [] and world.verbs() == []


async def test_a_transient_check_failure_fails_every_run_and_is_never_held(
    world: World,
) -> None:
    world.manifests.default = ManifestCheckKind.TIMEOUT
    _, job = await world.rule(container("web"))

    first = await world.run(job)
    world.advance(10)
    second = await world.run(job)

    for result in (first, second):
        assert (result.outcome, result.reason) == ("failure", "timeout")
    assert world.rows() == [] and world.verbs() == []


async def test_a_stored_refusal_for_a_retired_reason_is_ignored_and_the_image_updates(
    world: World,
) -> None:
    rule_id, job = await world.rule(container("web"))
    await hand_refusal(world, rule_id, f"{REFERENCE}=not_bound")

    result = await world.run(job)

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    assert "held" not in counts(result)
    assert world.verbs()[:6] == RECREATE_VERBS


async def test_a_failed_start_rolls_back_records_both_rows_and_then_holds_until_a_rule_save(
    world: World,
) -> None:
    rule_id, job = await failed_update(world)

    [failure] = world.rows("container_recreated")
    [rolled] = world.rows("container_update_rolled_back")
    restored = world.host.boxes[OLD].state
    held_before = len(world.rows())
    world.advance(10)
    held = await world.run(job)
    await world.save_rule(rule_id)
    released = await world.run(job)

    assert (failure["result"], failure["detail"]["rolled_back"]) == ("failure", True)
    assert (rolled["target_id"], rolled["detail"]["digest_after"]) == (OLD, digest(3))
    assert rolled["detail"]["verify_failure"] == "exited"
    assert (rolled["actor_subsystem"], rolled["rule_id"]) == (UPDATER, rule_id)
    assert restored == "running"
    assert (held.outcome, counts(held)["held"]) == ("success", 1)
    assert "held" not in counts(released)
    assert held_before == 3


async def test_a_rolled_back_row_records_image_ids_with_the_engine_prefix(world: World) -> None:
    await failed_update(world)

    [rolled] = world.rows("container_update_rolled_back")

    detail = rolled["detail"]
    assert detail["image_after"] == full_id(3)
    assert detail["image_before"].startswith("sha256:")
    assert detail["image_before"] != detail["image_after"]


async def test_a_rollback_hold_on_a_local_image_compares_the_image_id_not_the_digest(
    world: World,
) -> None:
    world.host.add_image(
        REFERENCE, full_id(2), config={"Cmd": ["serve"]}, digests=(f"{ADDRESS}/app@{digest(2)}",)
    )
    world.manifests.default = digest(2)
    crash_new_container(world)
    _, job = await world.rule(container("web"))

    await world.run(job)
    [rolled] = world.rows("container_update_rolled_back")
    world.host.add_image(
        REFERENCE,
        full_id(2),
        config={"Cmd": ["serve"]},
        digests=(f"{ADDRESS}/app@{digest(7)}", f"{ADDRESS}/app@{digest(2)}"),
    )
    world.advance(10)
    second = await world.run(job)

    assert rolled["detail"]["image_after"] == full_id(2)
    assert counts(second)["held"] == 1
    assert len(world.rows("container_recreated")) == 1


async def test_a_permanent_recreate_refusal_is_held_until_the_rule_is_saved(world: World) -> None:
    network_id = next(iter(world.host.networks))
    world.host.boxes.pop(OLD)
    world.host.add_configured(
        OLD,
        "web",
        old_body(network_id, config={"Image": REFERENCE}, host={"AutoRemove": True}),
        "running",
    )
    rule_id, job = await world.rule(container("web"))

    first = await world.run(job)
    world.advance(10)
    second = await world.run(job)
    await world.save_rule(rule_id)
    third = await world.run(job)

    [failed, *_] = world.rows("container_recreated")
    assert failed["detail"]["reason"] == "not_recreatable"
    assert first.outcome == "failure"
    assert (second.outcome, counts(second)["held"]) == ("success", 1)
    assert "held" not in counts(third)
    assert len(world.rows("container_recreated")) == 2


async def test_a_registry_outage_fails_the_run_with_its_kind_and_writes_nothing(
    world: World,
) -> None:
    world.manifests.default = ManifestCheckKind.TCP
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    assert (result.outcome, result.reason, counts(result)["failed"]) == ("failure", "tcp", 1)
    assert world.rows() == [] and world.verbs() == []


async def test_a_registry_that_no_credential_set_names_is_checked_anonymously(
    world: World,
) -> None:
    unconfigured = "elsewhere.example/app:1"
    network_id = next(iter(world.host.networks))
    world.host.add_image(unconfigured, full_id(4), digests=(f"elsewhere.example/app@{digest(4)}",))
    world.host.add_configured(
        OTHER, "other", old_body(network_id, config={"Image": unconfigured}), "running"
    )
    _, job = await world.rule(container("other"), REPORT)

    result = await world.run(job)

    [(target, _, login)] = world.manifests.calls
    assert counts(result)["update_available"] == 1
    assert (target.address, login) == ("elsewhere.example", None)


async def test_a_failed_pull_fails_the_container_with_one_pull_row_and_no_recreate(
    world: World,
) -> None:
    world.host.pull_status = 500
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    assert result.outcome == "failure"
    assert len(world.rows("image_pulled")) == 1
    assert world.rows("container_recreated") == []


async def test_a_local_image_newer_than_the_container_recreates_without_a_pull(
    world: World,
) -> None:
    world.host.add_image(
        REFERENCE, full_id(2), config={"Cmd": ["serve"]}, digests=(f"{ADDRESS}/app@{digest(2)}",)
    )
    world.manifests.default = digest(2)
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    detail = counts(result)
    assert (result.outcome, detail["local_newer"]) == ("success", 1)
    assert "pulled" not in detail and world.rows("image_pulled") == []
    [recreated] = world.rows("container_recreated")
    assert recreated["detail"]["update_reason"] == "local_newer"


async def test_two_containers_on_one_reference_share_one_pull(world: World) -> None:
    world.container_like_web("web2", 0xB2)
    _, job = await world.rule(labelled())

    result = await world.run(job)

    assert (counts(result)["updated"], counts(result)["pulled"]) == (2, 1)
    assert world.verbs().count("pull") == 1
    assert len(world.rows("image_pulled")) == 1 and len(world.rows("container_recreated")) == 2


async def test_two_containers_on_one_reference_share_one_failed_pull(world: World) -> None:
    world.container_like_web("web2", 0xB2)
    world.host.pull_status = 500
    _, job = await world.rule(labelled())

    result = await world.run(job)

    assert (result.outcome, counts(result)["failed"]) == ("failure", 2)
    assert world.verbs().count("pull") == 1
    assert len(world.rows("image_pulled")) == 1


async def test_every_reason_a_container_is_left_alone_is_counted_and_nothing_is_written(
    world: World,
) -> None:
    network_id = next(iter(world.host.networks))
    host = world.host
    host.add_image(None, FULL_IMAGE_ID)
    host.add_image(None, full_id(5), digests=(PINNED_REFERENCE,))

    def labelled_box(identifier: str, name: str, state: str = "running", **config: Any) -> None:
        labels = {"tier": "front", **config.pop("labels", {})}
        body = old_body(network_id, config={"Labels": labels, **config})
        host.add_configured(identifier, name, body, state)

    host.boxes.pop(OLD)
    labelled_box(OLD, "stopped", state="exited", Image=REFERENCE)
    labelled_box(OTHER, "swarmed", Image=REFERENCE, labels={SWARM_LABEL: "x"})
    labelled_box(THIRD, "composed", Image=REFERENCE, labels={PROJECT_LABEL: "p"})
    labelled_box(f"{0xB4:064x}", "by-id", Image=FULL_IMAGE_ID)
    labelled_box(f"{0xB5:064x}", "pinned", Image=PINNED_REFERENCE)
    _, job = await world.rule(labelled())

    result = await world.run(job)

    detail = counts(result)
    assert result.outcome == "success"
    expected = {
        "not_running": 1,
        "swarm_managed": 1,
        "compose_managed": 1,
        "image_id_reference": 1,
        "pinned": 1,
    }
    assert {key: detail[key] for key in expected} == expected
    assert len(detail) <= MAX_RUN_DETAIL_KEYS
    assert world.rows() == [] and world.verbs() == []


async def test_a_run_over_the_redeploy_cap_defers_the_rest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.container_like_web("web2", 0xB2)
    monkeypatch.setattr(updater, "MAX_REDEPLOYS", 1)
    _, job = await world.rule(labelled())

    result = await world.run(job)

    assert (counts(result)["updated"], counts(result)["deferred"]) == (1, 1)


async def test_a_run_past_its_start_budget_defers_what_it_has_not_begun(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "START_BUDGET", timedelta(0))
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    assert counts(result)["deferred"] == 1
    assert world.rows() == []


async def test_the_examined_cap_and_the_reference_cap_defer_the_overflow(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.manifests.default = digest(1)
    world.container_like_web("web2", 0xB2)
    other = f"{ADDRESS}/other:1"
    world.host.add_image(other, full_id(9), digests=(f"{ADDRESS}/other@{digest(9)}",))
    network_id = next(iter(world.host.networks))
    world.host.add_configured(
        THIRD, "web3", old_body(network_id, config={"Image": other}), "running"
    )
    _, job = await world.rule(labelled(), REPORT)

    monkeypatch.setattr(updater, "MAX_EXAMINED", 2)
    examined = await world.run(job)
    monkeypatch.setattr(updater, "MAX_EXAMINED", 200)
    monkeypatch.setattr(updater, "MAX_REFERENCES", 1)
    world.host.boxes.pop(OTHER)
    referenced = await world.run(job)

    assert (counts(examined)["matched"], counts(examined)["deferred"]) == (3, 1)
    assert (counts(referenced)["references"], counts(referenced)["deferred"]) == (1, 1)


async def test_a_busy_container_skips_the_run_after_its_pull_and_a_suppressed_one_before_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ContainerUpdater, "lock_wait", 0.2)
    key = ObjectKey(ENV_A, "container", OLD)
    _, job = await world.rule(container("web"))
    holder = LockHolder(UserActor(1), "someone", "stop")

    async with world.subject.deps.engine.locks.hold(key, holder):
        busy = await world.run(job)
    world.subject.suppressions.note(key)
    suppressed = await world.run(job)

    assert (busy.outcome, busy.reason) == ("skipped", "object_busy")
    assert (suppressed.outcome, suppressed.reason) == ("skipped", "suppressed")
    assert [row["action"] for row in world.rows()] == ["image_pulled"]
    assert world.verbs() == ["pull"]


async def test_a_named_container_that_is_gone_fails_the_run_as_target_missing(
    world: World,
) -> None:
    _, job = await world.rule(container("nothing-here"))

    result = await world.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")


async def test_several_named_containers_are_each_updated_and_a_missing_name_is_reported(
    world: World,
) -> None:
    world.container_like_web("web2", 0xB2)
    _, job = await world.rule(container("web2", "ghost", "web"))

    result = await world.run(job)

    assert (result.outcome, result.attempted) == ("success", 2)
    detail = counts(result)
    assert (detail["updated"], detail["pulled"]) == (2, 1)
    assert detail["targets"] == "web, web2"
    assert detail["target_outcomes"] == "web: done, web2: done"
    assert (detail["not_found"], detail["not_found_names"]) == (1, "ghost")
    assert {row["target_id"] for row in world.rows("container_recreated")} == {OLD, OTHER}


async def test_one_named_container_failing_does_not_stop_the_others(world: World) -> None:
    world.container_like_web("web2", 0xB2)
    world.host.fail("stop", 500, target=OLD)
    _, job = await world.rule(container("web", "web2"))

    result = await world.run(job)

    assert result.outcome == "partial"
    detail = counts(result)
    assert (detail["updated"], detail["failed"]) == (1, 1)
    assert detail["target_outcomes"].startswith("web: failed (")
    assert detail["target_outcomes"].endswith(", web2: done")


async def test_the_reference_budget_is_one_per_run_across_the_names(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = f"{ADDRESS}/other:1"
    world.host.add_image(other, full_id(9), digests=(f"{ADDRESS}/other@{digest(9)}",))
    network_id = next(iter(world.host.networks))
    world.host.add_configured(
        THIRD, "web3", old_body(network_id, config={"Image": other}), "running"
    )
    monkeypatch.setattr(updater, "MAX_REFERENCES", 1)
    _, job = await world.rule(container("web", "web3"), REPORT)

    result = await world.run(job)

    detail = counts(result)
    assert (detail["references"], detail["deferred"]) == (1, 1)
    assert detail["targets"] == "web, web3"


async def test_a_compose_managed_container_acts_on_nothing_and_the_run_says_so(
    world: World,
) -> None:
    compose_managed_web(world)
    _, job = await world.rule(container("composed"))

    result = await world.run(job)
    dry = await world.run(job, dry=True)

    for ran in (result, dry):
        detail = counts(ran)
        assert ran.outcome == "success"
        assert (detail["compose_managed"], detail["acts_on_nothing"]) == (1, MANAGED_NOTE)
        assert detail["targets"] == "composed"
    assert world.rows() == [] and world.verbs() == []


async def test_a_compose_managed_name_among_several_is_noted_while_the_others_are_updated(
    world: World,
) -> None:
    compose_managed_web(world)
    _, job = await world.rule(container("composed", "web"))

    result = await world.run(job)

    detail = counts(result)
    assert (detail["updated"], detail["compose_managed"]) == (1, 1)
    assert detail["acts_on_nothing"] == MANAGED_NOTE
    assert detail["target_outcomes"] == "composed: skipped (compose_managed), web: done"


async def test_a_standard_owner_with_write_access_is_told_a_compose_managed_target_acts_on_nothing(
    world: World,
) -> None:
    compose_managed_web(world)
    await world.subject.grant(THIRD, STANDARD_ID)
    _, job = await world.rule(container("composed"), REPORT, owner=STANDARD_ID)

    result = await world.run(job)

    assert counts(result)["acts_on_nothing"] == MANAGED_NOTE


async def test_a_standard_owner_acts_only_where_they_may_write_and_learns_nothing_of_the_rest(
    world: World, image_rig: ImageRig
) -> None:
    set_id = (present(await image_rig.db.fetch_one("SELECT id FROM registry_credential")))["id"]
    await grant_user(image_rig.db, set_id, STANDARD_ID)
    world.container_like_web("mine", 0xB2)
    world.container_like_web("hidden", 0xB3)
    await world.subject.grant(OTHER, STANDARD_ID)
    await world.subject.grant(OLD, PEER_ID, is_public=True, access_level="read")
    await world.subject.grant(THIRD, PEER_ID)
    _, named = await world.rule(container("web"), REPORT, owner=STANDARD_ID, name="named")
    _, hidden = await world.rule(container("hidden"), REPORT, owner=STANDARD_ID, name="hidden")
    _, labels = await world.rule(labelled(), REPORT, owner=STANDARD_ID, name="labels")

    by_name = await world.run(named)
    by_hidden_name = await world.run(hidden)
    by_label = await world.run(labels)

    assert (by_name.outcome, by_name.reason) == ("failure", "permission_denied")
    assert (by_hidden_name.outcome, by_hidden_name.reason) == ("failure", "target_missing")
    detail = counts(by_label)
    assert by_label.outcome == "success"
    assert (detail["matched"], detail["not_permitted"], detail["update_available"]) == (2, 1, 1)
    assert world.subject.subsystem_events() == [] and world.verbs() == []


async def test_a_standard_owners_names_are_each_settled_by_what_the_owner_may_see_and_write(
    world: World, image_rig: ImageRig
) -> None:
    set_id = (present(await image_rig.db.fetch_one("SELECT id FROM registry_credential")))["id"]
    await grant_user(image_rig.db, set_id, STANDARD_ID)
    world.container_like_web("mine", 0xB2)
    world.container_like_web("hidden", 0xB3)
    await world.subject.grant(OTHER, STANDARD_ID)
    await world.subject.grant(OLD, PEER_ID, is_public=True, access_level="read")
    await world.subject.grant(THIRD, PEER_ID)
    _, job = await world.rule(
        container("hidden", "mine", "web"), REPORT, owner=STANDARD_ID, name="several"
    )

    result = await world.run(job)

    detail = counts(result)
    assert (result.outcome, result.reason) == ("failure", "permission_denied")
    assert detail["targets"] == "mine, web"
    assert detail["target_outcomes"] == "mine: unchanged, web: failed (permission_denied)"
    assert (detail["not_found"], detail["not_found_names"]) == (1, "hidden")
    assert detail["update_available"] == 1
    assert world.subject.subsystem_events() == [] and world.verbs() == []


async def test_a_container_that_stops_after_planning_is_not_recreated(world: World) -> None:
    def pause() -> None:
        world.host.boxes[OLD].state = "paused"

    world.host.on("pull", pause)
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    detail = counts(result)
    assert detail["not_running"] == 1 and "failed" not in detail
    assert world.rows("container_recreated") == []
    assert world.host.boxes[OLD].state == "paused" and "stop" not in world.verbs()


async def test_an_unreachable_host_skips_the_run_at_the_list_and_at_the_inspect(
    world: World,
) -> None:
    _, job = await world.rule(container("web"), REPORT)
    world.host.route("GET", "/containers/json", Refuse())
    listing = await world.run(job)
    world.host.clear_faults()
    world.host.route("GET", f"/containers/{OLD}/json", Refuse())

    inspecting = await world.run(job)

    for result in (listing, inspecting):
        assert (result.outcome, result.reason) == ("skipped", "environment_unreachable")


async def test_an_updater_built_without_a_registry_runtime_fails_its_runs(
    image_rig: ImageRig, settings: Any
) -> None:
    subject = Subject(image_rig, settings, ContainerUpdater)
    _, job = await subject.rule(container("web"), ACTION)

    result: RunResult = await subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "registry_runtime_missing")


class TestStandardOwnerCredentialGrant:
    """A standard owner's rule may use only a bound set the owner holds a grant on."""

    async def test_without_a_grant_the_update_is_refused_held_and_not_stored(
        self, world: World, image_rig: ImageRig
    ) -> None:
        await world.subject.grant(OLD, STANDARD_ID)
        _, job = await world.rule(container("web"), owner=STANDARD_ID)

        result = await world.run(job)
        world.advance(10)
        held = await world.run(job)

        assert (result.outcome, result.reason) == ("failure", "credential_unavailable")
        [row] = world.rows("image_update_refused")
        assert row["detail"]["reason"] == "credential_unavailable"
        assert world.verbs() == []
        stored = await image_rig.db.fetch_limited(
            "SELECT 1 FROM image_update_status WHERE image_reference = ?", (REFERENCE_KEY,), limit=1
        )
        assert stored == []
        assert (held.outcome, counts(held)["held"]) == ("success", 1)
        assert len(world.rows("image_update_refused")) == 1

    async def test_with_a_grant_the_update_goes_ahead(
        self, world: World, image_rig: ImageRig
    ) -> None:
        await world.subject.grant(OLD, STANDARD_ID)
        set_id = (present(await image_rig.db.fetch_one("SELECT id FROM registry_credential")))["id"]
        await grant_user(image_rig.db, set_id, STANDARD_ID)
        _, job = await world.rule(container("web"), owner=STANDARD_ID)

        result = await world.run(job)

        assert (result.outcome, counts(result)["updated"]) == ("success", 1)

    async def test_the_pull_refuses_even_when_the_check_does_not(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = ImageUpdateService.check

        async def unguarded(
            self: Any,
            principal: Any,
            environment_id: int,
            references: Any,
            *,
            authorise_set: Any = None,
        ) -> Any:
            return await original(self, principal, environment_id, references)

        monkeypatch.setattr(ImageUpdateService, "check", unguarded)
        await world.subject.grant(OLD, STANDARD_ID)
        _, job = await world.rule(container("web"), owner=STANDARD_ID)

        result = await world.run(job)

        assert result.reason == "credential_unavailable"
        [refusal] = world.rows("image_update_refused")
        assert refusal["detail"]["reason"] == "credential_unavailable"
        assert "pull" not in world.verbs()
        [pulled] = world.rows("image_pulled")
        assert (pulled["result"], pulled["detail"]["reason"]) == (
            "failure",
            "credential_unavailable",
        )


class TestPreview:
    """What the confirmation pages list: only what the viewer can read."""

    selector = '{"kind":"labels","labels":[["tier","front"]]}'

    async def test_a_viewer_without_access_sees_nothing_and_one_with_read_sees_the_name(
        self, world: World
    ) -> None:
        hidden = await world.subject.subsystem.preview_targets(STANDARD, ENV_A, self.selector)
        await world.subject.grant(OLD, STANDARD_ID, access_level="read")
        readable = await world.subject.subsystem.preview_targets(STANDARD, ENV_A, self.selector)

        assert hidden is not None and (hidden.names, hidden.others) == ((), 0)
        assert readable is not None and (readable.names, readable.others) == (("web",), 0)

    async def test_others_counts_only_readable_matches_beyond_the_name_cap(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.access_support import ADMIN

        for number in range(2, 5):
            world.container_like_web(f"web{number}", 0xB0 + number)
        monkeypatch.setattr(updater, "MAX_PREVIEW_NAMES", 2)

        seen = await world.subject.subsystem.preview_targets(ADMIN, ENV_A, self.selector)

        assert seen is not None
        assert (len(seen.names), seen.others, seen.note) == (2, 2, None)

    async def test_containers_that_are_not_actionable_are_not_listed(self, world: World) -> None:
        from tests.access_support import ADMIN

        world.host.boxes[OLD].state = "exited"

        seen = await world.subject.subsystem.preview_targets(ADMIN, ENV_A, self.selector)

        assert seen is not None and seen.names == ()

    async def test_an_unreachable_host_raises_so_the_rule_service_can_say_so(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.object_inspector import EnvironmentUnavailable
        from tests.access_support import ADMIN

        async def broken(client: Any, selector: Any) -> Any:
            raise EngineError(ENV_A, "down")

        monkeypatch.setattr(updater, "_containers_of", broken)

        with pytest.raises(EnvironmentUnavailable):
            await world.subject.subsystem.preview_targets(
                ADMIN, ENV_A, '{"kind":"container","names":["web"]}'
            )


WEBHOOK_UPDATE = {"action": "update", "verify_seconds": 0}
WEBHOOK_REDEPLOY = {"action": "redeploy", "verify_seconds": 0}


async def test_a_webhook_update_pulls_with_the_bound_set_and_attributes_its_rows_to_the_receiver(
    world: World,
) -> None:
    as_webhook(world)
    rule_id, job = await world.rule(container("web"), WEBHOOK_UPDATE)
    await arm_webhook(world, rule_id)

    result = await world.subject.run(job, trigger="webhook")

    detail = counts(result)
    assert (result.outcome, detail["mode"], detail["updated"]) == ("success", "update", 1)
    assert (detail["calls"], detail["caller_address"]) == (PENDING_CALLS, CALLER_ADDRESS)
    assert world.verbs() == RECREATE_VERBS
    assert world.manifests.calls
    [recreated] = world.rows("container_recreated")
    assert (recreated["actor_subsystem"], recreated["rule_id"]) == (RECEIVER, rule_id)
    assert recreated["detail"]["trigger"] == "webhook"
    assert recreated["detail"]["webhook_action"] == "update"
    pending = await world.db.fetch_one("SELECT pending_calls FROM webhook")
    assert present(pending)["pending_calls"] == 0


async def test_a_webhook_update_with_nothing_new_checks_the_registry_and_writes_nothing(
    world: World,
) -> None:
    as_webhook(world)
    world.new_image(1)
    rule_id, job = await world.rule(container("web"), WEBHOOK_UPDATE)
    await arm_webhook(world, rule_id)

    result = await world.subject.run(job, trigger="webhook")

    assert (result.outcome, result.attempted) == ("success", 0)
    assert world.manifests.calls
    assert world.rows() == [] and world.verbs() == []


async def test_a_webhook_redeploy_recreates_the_container_without_asking_the_registry(
    world: World,
) -> None:
    as_webhook(world)
    rule_id, job = await world.rule(container("web"), WEBHOOK_REDEPLOY)
    await arm_webhook(world, rule_id)

    called = await world.subject.run(job, trigger="webhook")
    manual = await world.subject.run(job, trigger="manual")

    detail = counts(called)
    assert (called.outcome, detail["mode"], detail["redeployed"]) == ("success", "redeploy", 1)
    assert (detail["calls"], detail["caller_address"]) == (PENDING_CALLS, CALLER_ADDRESS)
    assert "calls" not in counts(manual) and "caller_address" not in counts(manual)
    assert world.manifests.calls == []
    assert "pull" not in world.verbs()
    [recreated, _] = world.rows("container_recreated")
    assert recreated["actor_subsystem"] == RECEIVER
    assert recreated["detail"]["trigger"] == "webhook"
    assert recreated["detail"]["webhook_action"] == "redeploy"


async def test_a_webhook_redeploy_dry_run_lists_the_names_it_would_redeploy(world: World) -> None:
    as_webhook(world)
    _, job = await world.rule(container("web"), WEBHOOK_REDEPLOY)

    result = await world.run(job, dry=True)

    detail = counts(result)
    assert (detail["would_redeploy"], detail["would_redeploy_count"]) == ("web", 1)
    assert world.rows() == [] and world.verbs() == [] and world.manifests.calls == []


async def test_a_webhook_run_whose_token_is_revoked_is_skipped_and_acts_on_nothing(
    world: World,
) -> None:
    as_webhook(world)
    rule_id, job = await world.rule(container("web"), WEBHOOK_UPDATE)
    await arm_webhook(world, rule_id)
    await world.db.execute(
        "UPDATE webhook SET token_hash = NULL, revoked_at = ?, revoked_reason = 'manual' "
        "WHERE rule_id = ?",
        (ISSUED_AT, rule_id),
    )

    result = await world.subject.run(job, trigger="webhook")

    assert (result.outcome, result.reason) == ("skipped", "token_revoked")
    assert world.rows() == [] and world.verbs() == [] and world.manifests.calls == []


async def test_a_webhook_rule_refuses_a_label_selector(world: World) -> None:
    as_webhook(world)
    rule_id, job = await world.rule(labelled(), WEBHOOK_UPDATE)
    await arm_webhook(world, rule_id)

    result = await world.subject.run(job, trigger="webhook")

    assert (result.outcome, result.reason) == ("failure", "selector_invalid")
    assert world.verbs() == []


async def test_a_webhook_run_fails_owner_access_lost_once_the_owner_is_disabled(
    world: World,
) -> None:
    as_webhook(world)
    rule_id, job = await world.rule(container("web"), WEBHOOK_REDEPLOY)
    await arm_webhook(world, rule_id)
    await world.db.execute("UPDATE user SET is_enabled = 0 WHERE id = ?", (ADMIN.user_id,))

    result = await world.subject.run(job, trigger="webhook")

    assert (result.outcome, result.reason) == ("failure", "owner_access_lost")
    assert world.rows() == [] and world.verbs() == []
