"""Removing several images at once: the confirmation, the reviewed values and the cap."""

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.services.container_actions import MAX_BATCH
from app.services.image_views import ImageBatchTarget, ImageBatchTargetView
from tests.access_support import ADMIN, ENV_A, STANDARD, container_id
from tests.image_support import full_id, image_id, rows_for
from tests.services.conftest import ImageRig

TWO_TAGS = 1
USED = 3
DANGLING = 4
ABSENT = 99
CONTAINER_NAME = "web"
TWO_TAGS_SIZE = 300
DANGLING_SIZE = 70


@pytest.fixture
async def daemon(image_rig: ImageRig) -> ImageRig:
    """An image with two tags, one a container uses, and one with no tag."""
    host = image_rig.host
    host.add_image("nginx:1", full_id(TWO_TAGS), size=TWO_TAGS_SIZE)
    host.add_image("nginx:latest", full_id(TWO_TAGS))
    host.add_image("other:1", full_id(USED), size=10)
    host.add_image(None, full_id(DANGLING), size=DANGLING_SIZE)
    host.add(
        container_id(1), CONTAINER_NAME, image="other:1", image_id=full_id(USED), state="exited"
    )
    return image_rig


def reviewed(view: ImageBatchTargetView) -> ImageBatchTarget:
    return ImageBatchTarget(view.image_id, view.references_digest, view.in_use, view.label)


def deletions(rig: ImageRig) -> list[str]:
    return [r.path for r in rig.host.calls("DELETE")]


async def test_the_confirmation_names_each_image_its_references_and_its_users(
    daemon: ImageRig,
) -> None:
    ids = [image_id(TWO_TAGS), image_id(USED), image_id(DANGLING), image_id(ABSENT)]

    review = await daemon.images.queries.batch_remove_confirmation(ADMIN, ENV_A, ids)

    by_id = {t.image_id: t for t in review.targets}
    assert [t.image_id for t in review.targets] == ids[:3]
    assert review.unavailable == 1
    assert by_id[image_id(TWO_TAGS)].references == ("nginx:1", "nginx:latest")
    assert by_id[image_id(TWO_TAGS)].in_use == 0
    assert by_id[image_id(USED)].in_use == 1
    assert [c.name for c in by_id[image_id(USED)].containers] == [CONTAINER_NAME]
    assert by_id[image_id(DANGLING)].references == ()
    assert by_id[image_id(DANGLING)].label == by_id[image_id(DANGLING)].short_id
    token = by_id[image_id(USED)].token
    assert token == f"{image_id(USED)}:{by_id[image_id(USED)].references_digest}:1"


async def test_the_run_removes_the_reviewed_images_and_reports_one_in_use_without_removing_it(
    daemon: ImageRig,
) -> None:
    ids = [image_id(TWO_TAGS), image_id(USED), image_id(DANGLING)]
    review = await daemon.images.queries.batch_remove_confirmation(ADMIN, ENV_A, ids)

    result = await daemon.images.actions.run_batch_remove(
        ADMIN, ENV_A, [reviewed(t) for t in review.targets]
    )

    assert [(i.image_id, i.status) for i in result.items] == [
        (image_id(TWO_TAGS), "removed"),
        (image_id(USED), "in_use"),
        (image_id(DANGLING), "removed"),
    ]
    assert result.removed == 2
    assert result.reclaimed_up_to == TWO_TAGS_SIZE + DANGLING_SIZE
    assert full_id(USED) in daemon.host.image_store
    assert all(full_id(USED) not in path for path in deletions(daemon))
    rows = await rows_for(daemon.db, "image_removed")
    assert [(r["target_id"], r["result"], r["detail"]["batch"]) for r in rows] == [
        (image_id(TWO_TAGS), "success", True),
        (image_id(USED), "failure", True),
        (image_id(DANGLING), "success", True),
    ]
    assert rows[1]["detail"]["reason"] == "image_in_use"


async def test_an_image_that_gained_a_tag_since_the_review_is_reported_changed_and_left_alone(
    daemon: ImageRig,
) -> None:
    review = await daemon.images.queries.batch_remove_confirmation(
        ADMIN, ENV_A, [image_id(TWO_TAGS), image_id(DANGLING)]
    )
    daemon.host.add_image("nginx:stable", full_id(TWO_TAGS))

    result = await daemon.images.actions.run_batch_remove(
        ADMIN, ENV_A, [reviewed(t) for t in review.targets]
    )

    assert [(i.image_id, i.status) for i in result.items] == [
        (image_id(TWO_TAGS), "changed"),
        (image_id(DANGLING), "removed"),
    ]
    assert full_id(TWO_TAGS) in daemon.host.image_store


async def test_an_image_removed_since_the_review_is_reported_gone(daemon: ImageRig) -> None:
    review = await daemon.images.queries.batch_remove_confirmation(
        ADMIN, ENV_A, [image_id(DANGLING)]
    )
    daemon.host.remove_image(full_id(DANGLING))

    result = await daemon.images.actions.run_batch_remove(
        ADMIN, ENV_A, [reviewed(t) for t in review.targets]
    )

    assert [i.status for i in result.items] == ["gone"]


async def test_the_label_the_reviewer_saw_names_the_item_but_never_selects_the_image(
    daemon: ImageRig,
) -> None:
    target = ImageBatchTarget(image_id(DANGLING), "wrong", 0, label="nginx:latest")

    result = await daemon.images.actions.run_batch_remove(ADMIN, ENV_A, [target])

    assert [(i.image_id, i.label, i.status) for i in result.items] == [
        (image_id(DANGLING), "nginx:latest", "changed")
    ]
    assert full_id(TWO_TAGS) in daemon.host.image_store


async def test_a_repeated_image_is_removed_once(daemon: ImageRig) -> None:
    review = await daemon.images.queries.batch_remove_confirmation(
        ADMIN, ENV_A, [image_id(DANGLING)]
    )
    target = reviewed(review.targets[0])

    result = await daemon.images.actions.run_batch_remove(ADMIN, ENV_A, [target, target])

    assert [i.status for i in result.items] == ["removed"]
    assert len(await rows_for(daemon.db, "image_removed")) == 1


@pytest.mark.parametrize("count", [0, MAX_BATCH + 1])
async def test_a_selection_that_is_empty_or_over_the_cap_is_refused_before_anything_runs(
    daemon: ImageRig, count: int
) -> None:
    ids = [image_id(0x100 + n) for n in range(count)]
    targets = [ImageBatchTarget(i, "digest", 0) for i in ids]

    with pytest.raises(ValidationFailed) as on_review:
        await daemon.images.queries.batch_remove_confirmation(ADMIN, ENV_A, ids)
    with pytest.raises(ValidationFailed) as on_run:
        await daemon.images.actions.run_batch_remove(ADMIN, ENV_A, targets)

    assert "images" in on_review.value.fields
    assert "images" in on_run.value.fields
    assert deletions(daemon) == []
    assert await rows_for(daemon.db, "image_removed") == []


async def test_exactly_the_cap_is_accepted(daemon: ImageRig) -> None:
    ids = [image_id(0x100 + n) for n in range(MAX_BATCH)]

    review = await daemon.images.queries.batch_remove_confirmation(ADMIN, ENV_A, ids)

    assert (len(review.targets), review.unavailable) == (0, MAX_BATCH)


async def test_a_malformed_image_id_in_the_run_is_refused_whole(daemon: ImageRig) -> None:
    good = ImageBatchTarget(image_id(DANGLING), "digest", 0)

    with pytest.raises(ValidationFailed):
        await daemon.images.actions.run_batch_remove(
            ADMIN, ENV_A, [good, ImageBatchTarget("not-an-id", "digest", 0)]
        )

    assert deletions(daemon) == []


async def test_a_standard_user_may_neither_review_nor_run_an_image_batch(daemon: ImageRig) -> None:
    with pytest.raises(PermissionDenied):
        await daemon.images.queries.batch_remove_confirmation(STANDARD, ENV_A, [image_id(DANGLING)])
    with pytest.raises(PermissionDenied):
        await daemon.images.actions.run_batch_remove(
            STANDARD, ENV_A, [ImageBatchTarget(image_id(DANGLING), "digest", 0)]
        )

    assert deletions(daemon) == []
