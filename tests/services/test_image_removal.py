"""Tag, untag, remove and prune: named targets, reviewed state, force, what the daemon is told."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.services.activity import UserActor
from app.services.container_actions import EngineRefused
from app.services.image_views import (
    ImageChangedMidway,
    ImageChangedSinceReview,
    ImageGone,
    ImageHasDependents,
    ImageInUse,
    PruneTarget,
    references_digest,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ENGINE_TEXT
from tests.docker_engine.fake_engine import json_reply
from tests.image_support import (
    binding_rows,
    digest,
    full_id,
    image_id,
    register,
    rows_for,
)
from tests.registry_support import bind_image
from tests.services.conftest import ImageRig

TWO_TAGS = 1
ONE_TAG = 2
OTHER = 3
DANGLING = 4
UNUSED_TAGGED = 5
NGINX_KEYS = ["docker.io/library/nginx:1", "docker.io/library/nginx:latest"]
REPOSITORY = "ghcr.io/o/app"
PULLED_KEYS = [f"{REPOSITORY}:1", f"{REPOSITORY}@{digest(2)}"]
OTHER_CLIENT = "another client"


@pytest.fixture
async def daemon(image_rig: ImageRig) -> ImageRig:
    """Two tags on one image, a pulled image with a tag and a digest, and a dangling one."""
    host = image_rig.host
    host.add_image("nginx:1", full_id(TWO_TAGS), size=300)
    host.add_image("nginx:latest", full_id(TWO_TAGS))
    host.add_image(
        f"{REPOSITORY}:1", full_id(ONE_TAG), size=100, digests=(f"{REPOSITORY}@{digest(2)}",)
    )
    host.add_image("other:1", full_id(OTHER), size=10)
    host.add_image(None, full_id(DANGLING), size=70)
    return image_rig


def stopped_user(rig: ImageRig) -> None:
    rig.host.add(container_id(1), "web", image="other:1", image_id=full_id(OTHER), state="exited")


def deletions(rig: ImageRig) -> list[str]:
    return [r.path for r in rig.host.calls("DELETE")]


def mutations(rig: ImageRig) -> list[str]:
    return [p for p in rig.host.mutations() if "/containers/" not in p]


async def tag(
    rig: ImageRig,
    target: str,
    *,
    image: int = ONE_TAG,
    expected: str = "none",
    who: Any = ADMIN,
) -> Any:
    return await rig.images.actions.tag(
        who, ENV_A, image_id(image), target, expected_replaced_id=expected
    )


async def remove(
    rig: ImageRig, image: int, keys: list[str], *, in_use: int = 0, **flags: Any
) -> Any:
    return await rig.images.actions.remove(
        ADMIN,
        ENV_A,
        image_id(image),
        expected_references=references_digest(keys),
        expected_in_use=in_use,
        **flags,
    )


async def test_tagging_gives_the_image_a_new_reference_and_writes_no_binding(
    daemon: ImageRig,
) -> None:
    outcome = await tag(daemon, "fresh:1")

    [row] = await rows_for(daemon.db, "image_tagged")
    assert (outcome.unchanged, outcome.replaced_image_id) == (False, None)
    assert "fresh:1" in daemon.host.tags_of(full_id(ONE_TAG))
    assert (row["target_id"], row["detail"]) == (
        image_id(ONE_TAG),
        {"reference": "docker.io/library/fresh:1"},
    )
    assert await binding_rows(daemon.db) == []
    assert daemon.host.calls("POST", f"/images/{full_id(ONE_TAG)}/tag")[0].query == {
        "repo": "docker.io/library/fresh",
        "tag": "1",
    }


async def test_tagging_over_a_reference_another_image_holds_needs_that_images_id_and_takes_it(
    daemon: ImageRig,
) -> None:
    outcome = await tag(daemon, "other:1", expected=image_id(OTHER))

    [row] = await rows_for(daemon.db, "image_tagged")
    assert outcome.replaced_image_id == image_id(OTHER)
    assert daemon.host.tags_of(full_id(OTHER)) == []
    assert row["detail"]["replaced_image_id"] == image_id(OTHER)


@pytest.mark.parametrize(
    "expected",
    [pytest.param("none", id="reviewed-as-free"), pytest.param(image_id(DANGLING), id="another")],
)
async def test_tagging_over_a_reference_held_by_a_different_image_than_reviewed_changes_nothing(
    daemon: ImageRig, expected: str
) -> None:
    with pytest.raises(ImageChangedSinceReview):
        await tag(daemon, "other:1", expected=expected)

    [row] = await rows_for(daemon.db, "image_tagged")
    assert daemon.host.tags_of(full_id(OTHER)) == ["other:1"]
    assert row["detail"]["reason"] == "replaced_changed"
    assert daemon.host.calls("POST", f"/images/{full_id(ONE_TAG)}/tag") == []


async def test_tagging_an_image_with_a_reference_it_already_has_does_nothing_and_writes_no_row(
    daemon: ImageRig,
) -> None:
    outcome = await tag(daemon, f"{REPOSITORY}:1")

    assert outcome.unchanged is True
    assert await rows_for(daemon.db, "image_tagged") == []
    assert daemon.host.calls("POST", f"/images/{full_id(ONE_TAG)}/tag") == []


@pytest.mark.parametrize(
    ("target", "expected", "field"),
    [
        pytest.param(f"app@{digest(1)}", "none", "reference", id="digest-target"),
        pytest.param("Not A Reference", "none", "reference", id="malformed-target"),
        pytest.param("fresh:1", "nonsense", "image", id="malformed-expectation"),
    ],
)
async def test_a_tag_that_is_not_well_formed_is_refused_before_the_daemon_is_asked(
    daemon: ImageRig, target: str, expected: str, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await tag(daemon, target, expected=expected)

    assert set(refused.value.fields) == {field}
    assert mutations(daemon) == []


async def test_tagging_an_image_the_daemon_does_not_have_reports_it_gone(daemon: ImageRig) -> None:
    with pytest.raises(ImageGone):
        await tag(daemon, "fresh:1", image=99)

    assert [r["detail"]["reason"] for r in await rows_for(daemon.db, "image_tagged")] == [
        "not_found"
    ]


async def test_tagging_is_refused_while_the_image_or_the_reference_is_locked(
    daemon: ImageRig,
) -> None:
    holder = LockHolder(UserActor(STANDARD_ID), "standard-user", "remove")
    lock = ObjectKey(ENV_A, "image_reference", "docker.io/library/fresh:1")

    async with daemon.engine.locks.hold(lock, holder):
        with pytest.raises(ObjectBusy):
            await tag(daemon, "fresh:1")

    [row] = await rows_for(daemon.db, "image_tagged")
    assert row["detail"]["reason"] == "object_busy"
    assert row["detail"]["holder_action"] == "remove"
    assert mutations(daemon) == []


async def test_untagging_one_of_several_references_leaves_the_image(daemon: ImageRig) -> None:
    outcome = await daemon.images.actions.untag(
        ADMIN, ENV_A, image_id(TWO_TAGS), "nginx:1", expected_last=False, expected_in_use=0
    )

    [row] = await rows_for(daemon.db, "image_untagged")
    assert (outcome.untagged, outcome.deleted) == (1, False)
    assert daemon.host.tags_of(full_id(TWO_TAGS)) == ["nginx:latest"]
    assert row["detail"] == {
        "reference": NGINX_KEYS[0],
        "force": False,
        "in_use_containers": 0,
        "deleted": False,
        "untagged": 1,
    }
    assert deletions(daemon) == ["/images/nginx:1"]


async def test_untagging_the_last_tag_of_a_pulled_image_deletes_it_with_its_digest_reference(
    daemon: ImageRig,
) -> None:
    outcome = await daemon.images.actions.untag(
        ADMIN, ENV_A, image_id(ONE_TAG), f"{REPOSITORY}:1", expected_last=True, expected_in_use=0
    )

    [row] = await rows_for(daemon.db, "image_untagged")
    assert (outcome.deleted, outcome.untagged) == (True, 2)
    assert full_id(ONE_TAG) not in daemon.host.image_store
    assert row["detail"]["deleted"] is True


async def test_untagging_the_last_tag_of_a_used_image_is_refused_unless_forced(
    daemon: ImageRig,
) -> None:
    stopped_user(daemon)

    with pytest.raises(ImageInUse) as refused:
        await daemon.images.actions.untag(
            ADMIN, ENV_A, image_id(OTHER), "other:1", expected_last=True, expected_in_use=1
        )

    [row] = await rows_for(daemon.db, "image_untagged")
    assert refused.value.containers == 1
    assert row["detail"]["reason"] == "image_in_use"
    assert deletions(daemon) == []


async def test_a_forced_untag_of_a_running_containers_image_leaves_it_untagged_but_present(
    daemon: ImageRig,
) -> None:
    daemon.host.add(container_id(1), "web", image="other:1", image_id=full_id(OTHER))

    outcome = await daemon.images.actions.untag(
        ADMIN,
        ENV_A,
        image_id(OTHER),
        "other:1",
        expected_last=True,
        expected_in_use=1,
        force=True,
    )

    assert (outcome.untagged, outcome.deleted, outcome.force) == (1, False, True)
    assert full_id(OTHER) in daemon.host.image_store
    assert daemon.host.tags_of(full_id(OTHER)) == []


@pytest.mark.parametrize(
    ("reference", "expected_last", "expected_in_use"),
    [
        pytest.param("nginx:1", True, 0, id="was-reviewed-as-the-last-tag"),
        pytest.param("other:1", False, 0, id="was-reviewed-as-not-the-last-tag"),
        pytest.param("other:1", True, 3, id="in-use-count-changed"),
        pytest.param("ghcr.io/o/app:7", False, 0, id="reference-is-no-longer-on-the-image"),
    ],
)
async def test_an_untag_whose_reviewed_state_no_longer_holds_changes_nothing(
    daemon: ImageRig, reference: str, expected_last: bool, expected_in_use: int
) -> None:
    image = TWO_TAGS if reference == "nginx:1" else OTHER

    with pytest.raises(ImageChangedSinceReview):
        await daemon.images.actions.untag(
            ADMIN,
            ENV_A,
            image_id(image),
            reference,
            expected_last=expected_last,
            expected_in_use=expected_in_use,
        )

    assert deletions(daemon) == []


async def test_an_untag_of_a_reference_removed_meanwhile_is_a_change_not_a_success(
    daemon: ImageRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = daemon.host._image_delete

    def vanish_first(reference: str, query: dict[str, str]) -> Any:
        document = daemon.host.image_store[full_id(TWO_TAGS)]
        document["RepoTags"].remove("nginx:1")
        del daemon.host.images["nginx:1"]
        return original(reference, query)

    monkeypatch.setattr(daemon.host, "_image_delete", vanish_first)

    with pytest.raises(ImageChangedSinceReview):
        await daemon.images.actions.untag(
            ADMIN, ENV_A, image_id(TWO_TAGS), "nginx:1", expected_last=False, expected_in_use=0
        )

    [row] = await rows_for(daemon.db, "image_untagged")
    assert row["detail"]["reason"] == "references_changed"


async def test_removing_an_image_removes_each_reference_by_name_then_the_image(
    daemon: ImageRig,
) -> None:
    outcome = await remove(daemon, TWO_TAGS, NGINX_KEYS)

    [row] = await rows_for(daemon.db, "image_removed")
    assert (outcome.references, outcome.untagged, outcome.deleted, outcome.force) == (
        2,
        2,
        True,
        False,
    )
    assert full_id(TWO_TAGS) not in daemon.host.image_store
    assert deletions(daemon) == ["/images/nginx:1", "/images/nginx:latest"]
    assert row["detail"] == {
        "references": 2,
        "force": False,
        "in_use_containers": 0,
        "size": 300,
        "untagged": 2,
        "deleted": True,
    }


async def test_removing_an_image_with_no_references_removes_it_by_id(daemon: ImageRig) -> None:
    outcome = await remove(daemon, DANGLING, [])

    assert (outcome.references, outcome.untagged, outcome.deleted) == (0, 0, True)
    assert deletions(daemon) == [f"/images/{full_id(DANGLING)}"]


async def test_removing_a_pulled_image_names_its_tag_and_its_digest_as_the_daemon_wrote_them(
    daemon: ImageRig,
) -> None:
    outcome = await remove(daemon, ONE_TAG, PULLED_KEYS)

    assert (outcome.untagged, outcome.deleted) == (2, True)
    assert deletions(daemon)[0] == f"/images/{REPOSITORY}:1"


async def test_a_removal_whose_reference_list_changed_since_review_removes_nothing(
    daemon: ImageRig,
) -> None:
    with pytest.raises(ImageChangedSinceReview):
        await remove(daemon, TWO_TAGS, NGINX_KEYS[:1])

    [row] = await rows_for(daemon.db, "image_removed")
    assert row["detail"]["reason"] == "references_changed"
    assert deletions(daemon) == []


async def test_a_removal_whose_container_count_changed_since_review_removes_nothing(
    daemon: ImageRig,
) -> None:
    stopped_user(daemon)

    with pytest.raises(ImageChangedSinceReview):
        await remove(daemon, OTHER, ["docker.io/library/other:1"], in_use=0)

    assert deletions(daemon) == []
    assert [r["detail"]["reason"] for r in await rows_for(daemon.db, "image_removed")] == [
        "in_use_changed"
    ]


async def test_removing_an_image_in_use_is_refused_and_names_how_many_containers_use_it(
    daemon: ImageRig,
) -> None:
    for number in (1, 2):
        daemon.host.add(
            container_id(number), f"c{number}", image="other:1", image_id=full_id(OTHER)
        )

    with pytest.raises(ImageInUse) as refused:
        await remove(daemon, OTHER, ["docker.io/library/other:1"], in_use=2)

    [row] = await rows_for(daemon.db, "image_removed")
    assert refused.value.containers == 2
    assert row["detail"]["reason"] == "image_in_use"
    assert deletions(daemon) == []


async def test_forcing_the_removal_of_a_stopped_containers_image_is_one_call_by_id(
    daemon: ImageRig,
) -> None:
    stopped_user(daemon)

    outcome = await remove(daemon, OTHER, ["docker.io/library/other:1"], in_use=1, force=True)

    [request] = daemon.host.calls("DELETE")
    [row] = await rows_for(daemon.db, "image_removed")
    assert request.path == f"/images/{full_id(OTHER)}"
    assert request.query == {"force": "true", "noprune": "true"}
    assert (outcome.deleted, outcome.force, outcome.in_use_containers) == (True, True, 1)
    assert row["detail"]["force"] is True


async def test_a_forced_removal_the_daemon_still_refuses_reports_our_own_in_use_count(
    daemon: ImageRig,
) -> None:
    daemon.host.add(container_id(1), "web", image="other:1", image_id=full_id(OTHER))

    with pytest.raises(ImageInUse) as refused:
        await remove(daemon, OTHER, ["docker.io/library/other:1"], in_use=1, force=True)

    assert refused.value.containers == 1
    assert ENGINE_TEXT not in str(refused.value)
    assert full_id(OTHER) in daemon.host.image_store


async def test_a_conflict_with_nothing_using_the_image_is_reported_as_dependents(
    daemon: ImageRig,
) -> None:
    daemon.host.fail("image_remove", 409)

    with pytest.raises(ImageHasDependents) as refused:
        await remove(daemon, TWO_TAGS, NGINX_KEYS)

    [row] = await rows_for(daemon.db, "image_removed")
    assert ENGINE_TEXT not in str(refused.value)
    assert row["detail"]["reason"] == "image_has_dependents"


async def test_an_engine_error_during_removal_is_a_product_error_without_the_engines_text(
    daemon: ImageRig,
) -> None:
    daemon.host.fail("image_remove", 500)

    with pytest.raises(EngineRefused) as refused:
        await remove(daemon, TWO_TAGS, NGINX_KEYS)

    [row] = await rows_for(daemon.db, "image_removed")
    assert ENGINE_TEXT not in str(refused.value) + str(row["detail"])
    assert row["detail"]["reason"] == "engine_error"


async def test_removing_an_image_that_is_gone_reports_it_and_writes_a_failure_row(
    daemon: ImageRig,
) -> None:
    with pytest.raises(ImageGone):
        await remove(daemon, 99, [])

    assert [r["result"] for r in await rows_for(daemon.db, "image_removed")] == ["failure"]


async def test_a_removal_never_takes_a_name_that_was_moved_to_another_image_midway(
    daemon: ImageRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = daemon.host._image_delete
    calls: list[str] = []

    def retag_second(reference: str, query: dict[str, str]) -> Any:
        calls.append(reference)
        reply = original(reference, query)
        if len(calls) == 1:
            daemon.host.add_image("nginx:latest", full_id(OTHER))
        return reply

    monkeypatch.setattr(daemon.host, "_image_delete", retag_second)

    with pytest.raises(ImageChangedMidway):
        await remove(daemon, TWO_TAGS, NGINX_KEYS)

    [row] = await rows_for(daemon.db, "image_removed")
    assert "nginx:latest" in daemon.host.tags_of(full_id(OTHER))
    assert full_id(OTHER) in daemon.host.image_store
    assert row["detail"]["reason"] == "references_changed"
    assert row["detail"]["untagged"] == 1


async def test_a_removal_that_untags_a_name_nobody_reviewed_stops_and_records_it(
    daemon: ImageRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = daemon.host._delete_reference

    def untag_one_more(*arguments: Any) -> Any:
        reply = original(*arguments)
        daemon.host.add_image("stranger:1", full_id(OTHER))
        document = daemon.host.image_store[full_id(TWO_TAGS)]
        document["RepoTags"].append("stranger:1")
        body = json.loads(reply.body)
        body.append({"Untagged": "stranger:1"})
        return json_reply(body)

    monkeypatch.setattr(daemon.host, "_delete_reference", untag_one_more)

    with pytest.raises(ImageChangedMidway):
        await remove(daemon, TWO_TAGS, NGINX_KEYS)

    [row] = await rows_for(daemon.db, "image_removed")
    assert row["detail"]["unreviewed_untagged"] == 1
    assert row["detail"]["reason"] == "references_changed"


async def test_removing_an_image_keeps_its_bindings_so_they_can_be_reaped(
    daemon: ImageRig,
) -> None:
    anywhere = await register(daemon, "anywhere", address="ghcr.io")
    await bind_image(daemon.db, anywhere, ENV_A, f"{REPOSITORY}:1")

    await remove(daemon, ONE_TAG, PULLED_KEYS)

    listed = await daemon.images.bindings.reapable(ADMIN, ENV_A)
    assert len(await binding_rows(daemon.db)) == 1
    assert [b.reference for b in listed.bindings] == [f"{REPOSITORY}:1"]


async def test_a_removal_is_busy_while_a_reference_of_the_image_is_locked(
    daemon: ImageRig,
) -> None:
    holder = LockHolder(UserActor(STANDARD_ID), "standard-user", "pull")
    lock = ObjectKey(ENV_A, "image_reference", NGINX_KEYS[1])

    async with daemon.engine.locks.hold(lock, holder):
        with pytest.raises(ObjectBusy):
            await remove(daemon, TWO_TAGS, NGINX_KEYS)

    assert deletions(daemon) == []


ACTIONS = {
    "tag": "image_tagged",
    "untag": "image_untagged",
    "remove": "image_removed",
    "prune": "image_removed",
}
OPERATIONS: dict[str, Callable[[ImageRig, int, Any], Awaitable[Any]]] = {
    "tag": lambda rig, env, who: rig.images.actions.tag(
        who, env, image_id(ONE_TAG), "fresh:1", expected_replaced_id="none"
    ),
    "untag": lambda rig, env, who: rig.images.actions.untag(
        who, env, image_id(TWO_TAGS), "nginx:1", expected_last=False, expected_in_use=0
    ),
    "remove": lambda rig, env, who: rig.images.actions.remove(
        who,
        env,
        image_id(TWO_TAGS),
        expected_references=references_digest(NGINX_KEYS),
        expected_in_use=0,
    ),
    "prune": lambda rig, env, who: rig.images.actions.run_prune(
        who, env, "dangling", [PruneTarget(image_id(DANGLING), references_digest([]))]
    ),
}


@pytest.mark.parametrize("environment_id", [ENV_A, ENV_B], ids=["remote", "local"])
@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_a_standard_user_may_not_tag_untag_remove_or_prune_anywhere(
    daemon: ImageRig, operation: str, environment_id: int
) -> None:
    with pytest.raises(PermissionDenied):
        await OPERATIONS[operation](daemon, environment_id, STANDARD)

    assert mutations(daemon) == []
    assert await rows_for(daemon.db, ACTIONS[operation]) == []


@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_an_unknown_environment_is_not_found_for_every_removing_operation(
    daemon: ImageRig, operation: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await OPERATIONS[operation](daemon, 999, ADMIN)


async def candidates(rig: ImageRig, mode: str = "dangling") -> list[PruneTarget]:
    plan = await rig.images.queries.prune_plan(ADMIN, ENV_A, mode)  # type: ignore[arg-type]
    return [PruneTarget(c.id, c.references_digest) for c in plan.candidates]


async def test_a_prune_removes_exactly_the_reviewed_images_and_never_calls_the_engines_prune(
    daemon: ImageRig,
) -> None:
    listed = await candidates(daemon, "unused")
    daemon.host.add_image(None, full_id(UNUSED_TAGGED + 10), size=5)
    wanted = [t for t in listed if t.image_id != image_id(OTHER)]

    result = await daemon.images.actions.run_prune(ADMIN, ENV_A, "unused", wanted)

    assert [item.status for item in result.items] == ["removed"] * len(wanted)
    assert [item.image_id for item in result.items] == [t.image_id for t in wanted]
    assert full_id(OTHER) in daemon.host.image_store
    assert full_id(UNUSED_TAGGED + 10) in daemon.host.image_store
    assert not any("prune" in path for path in daemon.host.paths())
    assert result.removed == len(wanted)
    assert result.reclaimed_up_to == sum(item.size for item in result.items)


async def test_each_pruned_image_is_its_own_audited_removal_marked_as_pruned(
    daemon: ImageRig,
) -> None:
    await daemon.images.actions.run_prune(ADMIN, ENV_A, "dangling", await candidates(daemon))

    [row] = await rows_for(daemon.db, "image_removed")
    assert (row["target_id"], row["detail"]["pruned"], row["detail"]["size"]) == (
        image_id(DANGLING),
        True,
        70,
    )
    assert await rows_for(daemon.db, "image_pruned") == []


async def test_a_pruned_image_whose_references_changed_since_review_is_skipped(
    daemon: ImageRig,
) -> None:
    listed = await candidates(daemon, "unused")
    daemon.host.add_image("late:1", full_id(DANGLING))

    result = await daemon.images.actions.run_prune(ADMIN, ENV_A, "unused", listed)

    statuses = {item.image_id: item.status for item in result.items}
    assert statuses[image_id(DANGLING)] == "skipped_changed"
    assert full_id(DANGLING) in daemon.host.image_store
    assert statuses[image_id(OTHER)] == "removed"


async def test_a_pruned_image_a_container_began_using_is_skipped_not_forced(
    daemon: ImageRig,
) -> None:
    listed = await candidates(daemon)
    daemon.host.add(container_id(1), "late", image="x", image_id=full_id(DANGLING))

    result = await daemon.images.actions.run_prune(ADMIN, ENV_A, "dangling", listed)

    assert [item.status for item in result.items] == ["skipped_changed"]
    assert full_id(DANGLING) in daemon.host.image_store


async def test_a_pruned_image_that_is_locked_is_busy_and_the_run_goes_on(daemon: ImageRig) -> None:
    daemon.host.add_image(None, full_id(UNUSED_TAGGED), size=1)
    listed = await candidates(daemon)
    holder = LockHolder(UserActor(STANDARD_ID), "standard-user", "remove")
    lock = ObjectKey(ENV_A, "image", image_id(DANGLING))

    async with daemon.engine.locks.hold(lock, holder):
        result = await daemon.images.actions.run_prune(ADMIN, ENV_A, "dangling", listed)

    assert {item.image_id: item.status for item in result.items} == {
        image_id(DANGLING): "busy",
        image_id(UNUSED_TAGGED): "removed",
    }


async def test_a_failure_on_one_image_does_not_stop_the_run(daemon: ImageRig) -> None:
    daemon.host.add_image(None, full_id(UNUSED_TAGGED), size=1)
    listed = await candidates(daemon)
    daemon.host.fail("image_remove", 500, skip=1)

    result = await daemon.images.actions.run_prune(ADMIN, ENV_A, "dangling", listed)

    assert [item.status for item in result.items] == ["removed", "failed"]
    assert result.removed == 1


@pytest.mark.parametrize(
    ("mode", "targets", "field"),
    [
        pytest.param("everything", [PruneTarget(image_id(1), "x")], "mode", id="mode"),
        pytest.param("dangling", [], "images", id="no-images"),
        pytest.param(
            "dangling", [PruneTarget(image_id(n), "x") for n in range(1, 102)], "images", id="101"
        ),
        pytest.param(
            "dangling", [PruneTarget("sha256:" + image_id(1), "x")], "images", id="bad-id"
        ),
    ],
)
async def test_a_prune_selection_must_be_a_known_mode_and_between_one_and_100_valid_ids(
    daemon: ImageRig, mode: str, targets: list[PruneTarget], field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await daemon.images.actions.run_prune(ADMIN, ENV_A, mode, targets)  # type: ignore[arg-type]

    assert set(refused.value.fields) == {field}
    assert mutations(daemon) == []


async def test_a_repeated_target_is_pruned_once(daemon: ImageRig) -> None:
    target = (await candidates(daemon))[0]

    result = await daemon.images.actions.run_prune(ADMIN, ENV_A, "dangling", [target, target])

    assert [item.status for item in result.items] == ["removed"]
