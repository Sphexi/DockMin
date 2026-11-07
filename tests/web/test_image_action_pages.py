"""The confirmations and posts that change images: tag, untag, remove, force, prune and reap."""

from typing import Any

from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import binding_rows, full_id, image_id, rows_for
from tests.web.environment_support import World
from tests.web.flash_support import land, location
from tests.web.image_support import (
    DANGLING,
    GHCR,
    GHCR_REFERENCE,
    NGINX,
    SPARE,
    USING_CONTAINER,
    assert_no_store,
    bind,
    hidden_values,
    image_path,
    images_path,
    links_in,
    make_set,
    text_of,
)
from tests.web.registry_support import HTMX

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
BAD_GATEWAY = 502


def deletions(host: ContainerEngine) -> list[str]:
    return [r.path for r in host.calls("DELETE")]


def tag_calls(host: ContainerEngine) -> list[str]:
    return [r.path for r in host.calls("POST") if r.path.endswith("/tag")]


async def database_rows(world: World, action: str) -> list[dict[str, Any]]:
    async with world.app.state.database.session() as db:
        return await rows_for(db, action)


def confirmation_data(client: AsyncClient, page: Response, *names: str) -> dict[str, Any]:
    """The form data a browser would post from a confirmation page."""
    return csrf_form(client) | {name: hidden_values(page, name)[0] for name in names}


async def test_the_remove_confirmation_names_the_image_and_posting_it_removes_it(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    path = image_path(world, image_id(SPARE), "/remove")

    page = await admin_client.get(path)
    forced_page = await admin_client.get(f"{path}?force=1")
    removed = await admin_client.post(
        path, data=confirmation_data(admin_client, page, "expected_references", "expected_in_use")
    )
    listing = await land(admin_client, removed)

    assert page.status_code == OK
    assert "Remove image" in text_of(page) and image_id(SPARE)[:12] in text_of(page)
    assert "spare:1" in text_of(page)
    assert "Credential bindings are kept." in text_of(page)
    assert hidden_values(page, "expected_in_use") == ["0"]
    assert hidden_values(page, "force") == []
    assert image_path(world, image_id(SPARE)) in links_in(page)
    assert (forced_page.status_code, forced_page.headers["location"]) == (SEE_OTHER, path)
    assert (removed.status_code, location(removed)) == (SEE_OTHER, images_path(world))
    assert "Image removed." in text_of(listing)
    assert full_id(SPARE) not in daemon.image_store
    assert [r["target_id"] for r in await database_rows(world, "image_removed")] == [
        image_id(SPARE)
    ]
    assert_no_store(page, removed)


async def test_removal_needs_a_second_page_for_a_used_image_and_refuses_what_changed_or_failed(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    daemon.boxes[USING_CONTAINER].state = "exited"
    used = image_path(world, image_id(NGINX), "/remove")
    reviewed_path = image_path(world, image_id(GHCR), "/remove")
    refused_path = image_path(world, image_id(DANGLING), "/remove")
    reviewed = await admin_client.get(reviewed_path)
    refused_page = await admin_client.get(refused_path)
    daemon.add_image("ghcr.io/o/app:extra", full_id(GHCR))
    daemon.fail("image_remove", 500)

    stale = await admin_client.post(
        reviewed_path,
        data=confirmation_data(admin_client, reviewed, "expected_references", "expected_in_use"),
    )
    engine_refusal = await admin_client.post(
        refused_path,
        data=confirmation_data(
            admin_client, refused_page, "expected_references", "expected_in_use"
        ),
    )
    incomplete = await admin_client.post(reviewed_path, data=csrf_form(admin_client))
    daemon.clear_faults()
    first = await admin_client.get(used)
    second = await admin_client.get(f"{used}?force=1")
    unforced = await admin_client.post(
        used, data=confirmation_data(admin_client, second, "expected_references", "expected_in_use")
    )
    forced = await admin_client.post(
        used,
        data=confirmation_data(
            admin_client, second, "expected_references", "expected_in_use", "force"
        ),
    )

    assert stale.status_code == CONFLICT
    assert "The image changed" in text_of(stale) and "Review again" in text_of(stale)
    assert engine_refusal.status_code == BAD_GATEWAY
    assert "Docker refused the action" in text_of(engine_refusal)
    assert "engine detail" not in engine_refusal.text
    assert incomplete.status_code == BAD_REQUEST
    assert "The confirmation was incomplete" in text_of(incomplete)
    assert "This image is in use" in text_of(first)
    assert "Continue to force removal" in first.text
    assert 'name="expected_references"' not in first.text
    assert "uses-nginx" in text_of(first)
    assert unforced.status_code == CONFLICT
    assert "The image is in use" in text_of(unforced)
    assert deletions(daemon) == [
        f"/images/sha256:{image_id(DANGLING)}",
        f"/images/sha256:{image_id(NGINX)}",
    ]
    assert "What forcing does" in text_of(second)
    assert hidden_values(second, "force") == ["yes"]
    assert (forced.status_code, location(forced)) == (SEE_OTHER, images_path(world))
    assert full_id(NGINX) not in daemon.image_store
    assert full_id(GHCR) in daemon.image_store and full_id(DANGLING) in daemon.image_store


async def test_untagging_follows_the_reference_rules_for_each_kind_of_image(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    daemon.add_image("solo:1", full_id(GHCR + 20))
    daemon.add("c" * 64, "solo-user", image="solo:1", image_id=full_id(GHCR + 20))
    several = image_path(world, image_id(NGINX), "/untag")
    unused = image_path(world, image_id(SPARE), "/untag")
    used = image_path(world, image_id(GHCR + 20), "/untag")
    data = ("reference", "expected_last", "expected_in_use")

    several_page = await admin_client.get(f"{several}?reference=nginx:1")
    several_done = await admin_client.post(
        several, data=confirmation_data(admin_client, several_page, *data)
    )
    landed = await land(admin_client, several_done)
    unused_page = await admin_client.get(f"{unused}?reference=spare:1")
    unused_done = await admin_client.post(
        unused, data=confirmation_data(admin_client, unused_page, *data)
    )
    unused_landed = await land(admin_client, unused_done)
    first = await admin_client.get(f"{used}?reference=solo:1")
    second = await admin_client.get(f"{used}?reference=solo:1&force=1")
    used_done = await admin_client.post(
        used, data=confirmation_data(admin_client, second, *data, "force")
    )
    lacking = await admin_client.get(f"{several}?reference=spare:1")
    malformed = await admin_client.get(f"{several}?reference=Not A Reference")

    assert "The image keeps its other references and stays on the host." in text_of(several_page)
    assert hidden_values(several_page, "expected_last") == ["no"]
    assert location(several_done) == image_path(world, image_id(NGINX))
    assert "Reference removed." in text_of(landed)
    assert daemon.tags_of(full_id(NGINX)) == ["nginx:latest"]
    assert "This is the image's last reference. Removing it removes the image" in text_of(
        unused_page
    )
    assert "Remove reference and image" in unused_page.text
    assert location(unused_done) == images_path(world)
    assert "Image removed." in text_of(unused_landed)
    assert full_id(SPARE) not in daemon.image_store
    assert "This image is in use" in text_of(first)
    assert "Continue to force removal" in first.text and 'name="expected_last"' not in first.text
    assert "The image is not removed. It stays on the host with no reference" in text_of(second)
    assert used_done.status_code == SEE_OTHER
    assert full_id(GHCR + 20) in daemon.image_store
    assert daemon.tags_of(full_id(GHCR + 20)) == []
    assert lacking.status_code == NOT_FOUND
    assert malformed.status_code == BAD_REQUEST and "Check the reference" in text_of(malformed)


async def test_tagging_reviews_the_reference_names_the_image_it_moves_from_and_adds_it(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    spare = image_path(world, image_id(SPARE), "/tag")
    ghcr = image_path(world, image_id(GHCR), "/tag")
    data = ("reference", "expected_replaced_id")

    empty = await admin_client.get(spare)
    free = await admin_client.get(f"{spare}?reference=fresh:1")
    added = await admin_client.post(spare, data=confirmation_data(admin_client, free, *data))
    landed = await land(admin_client, added)
    already = await admin_client.get(f"{spare}?reference=spare:1")
    digest_target = await admin_client.get(f"{spare}?reference=app@sha256:{'a' * 64}")
    taken = await admin_client.get(f"{ghcr}?reference=nginx:1")
    stale = await admin_client.post(
        ghcr, data=csrf_form(admin_client, reference="nginx:1", expected_replaced_id="none")
    )
    moved = await admin_client.post(ghcr, data=confirmation_data(admin_client, taken, *data))

    assert "No image has this reference now." not in empty.text
    assert "No image has this reference now." in text_of(free)
    assert hidden_values(free, "expected_replaced_id") == ["none"]
    assert "binds no credential set" in text_of(free)
    assert location(added) == image_path(world, image_id(SPARE))
    assert "Reference added." in text_of(landed)
    assert "fresh:1" in daemon.tags_of(full_id(SPARE))
    async with world.app.state.database.session() as db:
        assert await binding_rows(db) == []
    assert "already has the reference" in text_of(already)
    assert 'name="expected_replaced_id"' not in already.text
    assert digest_target.status_code == BAD_REQUEST
    assert "A tag is needed; a digest cannot be assigned." in text_of(digest_target)
    assert "The reference moves from another image" in text_of(taken)
    assert image_id(NGINX)[:12] in text_of(taken) and "nginx:latest" in text_of(taken)
    assert "Move the reference to this image" in taken.text
    assert stale.status_code == CONFLICT and "The image changed" in text_of(stale)
    assert moved.status_code == SEE_OTHER
    assert "nginx:1" in daemon.tags_of(full_id(GHCR))
    assert len(tag_calls(daemon)) == 2


async def test_the_prune_confirmation_lists_exactly_what_the_run_removes_up_to_100_images(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    path = f"{images_path(world)}/prune"

    chooser = await admin_client.get(path)
    review = await admin_client.get(f"{path}?mode=dangling")
    done = await admin_client.post(
        path,
        data={
            "csrf_token": hidden_values(review, "csrf_token")[0],
            "mode": "dangling",
            "target": hidden_values(review, "target"),
        },
    )
    done_page = await land(admin_client, done)
    for number in range(100, 230):
        daemon.add_image(None, full_id(number), size=number)
    large = await admin_client.get(f"{path}?mode=dangling")
    unknown = await admin_client.get(f"{path}?mode=everything")
    no_mode = await admin_client.post(path, data=csrf_form(admin_client, mode="everything"))
    broken = await admin_client.post(
        path, data=csrf_form(admin_client, mode="dangling") | {"target": ["no-separator"]}
    )
    nothing = await admin_client.post(path, data=csrf_form(admin_client, mode="dangling"))

    assert "What to prune" in text_of(chooser) and "would be removed" not in text_of(chooser)
    assert "1 image would be removed" in text_of(review)
    assert image_id(DANGLING)[:12] in text_of(review)
    assert image_id(SPARE)[:12] not in text_of(review)
    assert "Credential bindings are kept." in text_of(review)
    assert done.status_code == SEE_OTHER and location(done).startswith("/results/")
    assert "1 of 1 image removed." in text_of(done_page) and "Removed" in text_of(done_page)
    assert full_id(DANGLING) not in daemon.image_store
    assert full_id(SPARE) in daemon.image_store
    assert not any("prune" in p for p in daemon.paths())
    large_text = text_of(large)
    assert "30 more images are not in this prune. Run it again afterwards" in large_text
    assert "100 images would be removed" in large_text and "more not shown" not in large_text
    assert len(hidden_values(large, "target")) == 100
    assert unknown.status_code == BAD_REQUEST
    assert "Choose dangling or unused." in text_of(unknown)
    assert (no_mode.status_code, broken.status_code, nothing.status_code) == (400, 400, 400)
    assert "The selection was incomplete" in text_of(broken)
    assert_no_store(chooser, review, large)


async def test_the_unused_mode_lists_tagged_images_too_and_skips_one_a_container_took_up(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    path = f"{images_path(world)}/prune"
    review = await admin_client.get(f"{path}?mode=unused")
    daemon.add("d" * 64, "late", image="spare:1", image_id=full_id(SPARE))

    done = await admin_client.post(
        path,
        data={
            "csrf_token": hidden_values(review, "csrf_token")[0],
            "mode": "unused",
            "target": hidden_values(review, "target"),
        },
    )

    done_page = await land(admin_client, done)

    assert "spare:1" in text_of(review) and "ghcr.io/o/app:2" in text_of(review)
    assert "nginx:1" not in text_of(review)
    assert "Skipped: changed since you reviewed it" in text_of(done_page)
    assert full_id(SPARE) in daemon.image_store
    assert full_id(GHCR) not in daemon.image_store


async def test_the_reap_lists_bindings_whose_image_is_gone_and_removes_only_those_still_gone(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    alpha = await make_set(admin_client, world, "alpha", address="ghcr.io")
    path = f"{images_path(world)}/bindings/reap"
    nothing = await admin_client.get(path)
    await bind(world, alpha, "ghcr.io/o/removed:1")
    await bind(world, alpha, "ghcr.io/o/back:1")
    await bind(world, alpha, GHCR_REFERENCE)
    review = await admin_client.get(path)
    daemon.add_image("ghcr.io/o/back:1", full_id(SPARE + 20))

    done = await admin_client.post(
        path,
        data={
            "csrf_token": hidden_values(review, "csrf_token")[0],
            "binding": hidden_values(review, "binding"),
        },
    )
    landed = await land(admin_client, done)
    await bind(world, alpha, "ghcr.io/o/unverified:1")
    daemon.add("e" * 64, "by-id", image=image_id(9), image_id=full_id(9))
    daemon.fail("inspect", 500, target="e" * 64)
    unverified = await admin_client.get(path)

    async with world.app.state.database.session() as db:
        remaining = [r["image_reference"] for r in await binding_rows(db)]
    assert "No bindings to remove" in text_of(nothing) and 'name="binding"' not in nothing.text
    assert "ghcr.io/o/removed:1" in text_of(review) and "ghcr.io/o/back:1" in text_of(review)
    assert GHCR_REFERENCE not in text_of(review)
    assert "Bound to alpha" in text_of(review) and "Remove 2 bindings" in text_of(review)
    assert location(done) == images_path(world)
    assert "Removed 1 binding." in text_of(landed)
    assert sorted(remaining) == sorted(
        [GHCR_REFERENCE, "ghcr.io/o/back:1", "ghcr.io/o/unverified:1"]
    )
    assert "No bindings to remove" in text_of(unverified)
    assert_no_store(review, unverified)


ADMIN_ONLY_GETS = (
    "/{spare}/tag",
    "/{spare}/untag?reference=spare:1",
    "/{spare}/remove",
    "/prune",
    "/prune?mode=dangling",
    "/bindings/reap",
)
ADMIN_ONLY_POSTS = (
    ("/{spare}/tag", {"reference": "fresh:1", "expected_replaced_id": "none"}),
    (
        "/{spare}/untag",
        {"reference": "spare:1", "expected_last": "yes", "expected_in_use": "0"},
    ),
    ("/{spare}/remove", {"expected_references": "x", "expected_in_use": "0"}),
    ("/prune", {"mode": "dangling"}),
    ("/bindings/reap", {"binding": "1:ghcr.io/o/removed:1"}),
)


def under(world: World, template: str, environment_id: int | None = None) -> str:
    return images_path(world, environment_id) + template.format(spare=image_id(SPARE))


async def test_a_standard_user_is_forbidden_every_confirmation_and_post_that_changes_an_image(
    standard_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    gets = {
        (env, t): (await standard_client.get(under(world, t, env))).status_code
        for env in (world.remote_id, world.local_id)
        for t in ADMIN_ONLY_GETS
    }
    posts = {
        (env, t): (
            await standard_client.post(
                under(world, t, env), data=csrf_form(standard_client, **form)
            )
        ).status_code
        for env in (world.remote_id, world.local_id)
        for t, form in ADMIN_ONLY_POSTS
    }

    assert set(gets.values()) == {FORBIDDEN}
    assert set(posts.values()) == {FORBIDDEN}
    assert daemon.mutations() == []


async def test_a_hidden_environment_answers_those_pages_exactly_as_a_missing_one_does(
    standard_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    hidden = {
        t: (await standard_client.get(under(world, t, world.down_id))).status_code
        for t in ADMIN_ONLY_GETS
    }
    missing = {
        t: (await standard_client.get(under(world, t, 99999))).status_code for t in ADMIN_ONLY_GETS
    }

    assert hidden == missing
    assert daemon.requests == []


async def test_every_post_that_changes_an_image_is_refused_without_the_token(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    daemon.boxes[USING_CONTAINER].state = "exited"
    posts = [
        *ADMIN_ONLY_POSTS,
        ("/pull", {"reference": "busybox:1"}),
        ("/pull/choices", {"reference": "busybox:1"}),
        ("/updates", {"reference": "nginx:1"}),
    ]

    plain = {
        t: (await admin_client.post(under(world, t), data=form)).status_code for t, form in posts
    }
    wrong = {
        t: (
            await admin_client.post(under(world, t), data=form | {"csrf_token": "W" * 43})
        ).status_code
        for t, form in posts
    }
    htmx = {
        t: (await admin_client.post(under(world, t), data=form, headers=HTMX)).status_code
        for t, form in posts
    }

    assert set(plain.values()) | set(wrong.values()) | set(htmx.values()) == {FORBIDDEN}
    assert daemon.mutations() == []
    assert len(world.app.state.engine.image_jobs) == 0
