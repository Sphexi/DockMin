"""The image pages: list, detail, pull, tag, push, untag, remove, prune and reap."""

import re
from html import unescape

from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse, Reply
from tests.image_support import (
    image_id,
    mark_missing,
)
from tests.web.environment_support import DOWN_NAME, World
from tests.web.helpers import inline_code_in, parse_tags
from tests.web.image_support import (
    BUILD_COMMAND,
    DANGLING,
    GHCR,
    GHCR_REFERENCE,
    NGINX,
    SECRET_ENV,
    SECRET_LABEL,
    USING_CONTAINER,
    assert_no_store,
    bind,
    image_path,
    images_path,
    links_in,
    make_set,
    text_of,
)
from tests.web.registry_support import HTMX

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
UNAVAILABLE = 503
MIB = 1024 * 1024


def repair_links(response: Response) -> list[str]:
    return [
        unescape(tag.attrs["href"])
        for tag in parse_tags(response.text)
        if tag.name == "a" and tag.attrs.get("aria-label", "").startswith("Repair the binding")
    ]


def forms_in(response: Response) -> set[str]:
    return {
        unescape(tag.attrs["action"])
        for tag in parse_tags(response.text)
        if tag.name == "form" and "action" in tag.attrs
    }


KNOWN = {
    "nginx": "nginx:1",
    "ghcr": GHCR_REFERENCE,
    "spare": "spare:1",
    "dangling": image_id(DANGLING)[:12],
}
FILTERS = {
    "q=GHCR": {"ghcr"},
    "dangling=yes": {"dangling"},
    "dangling=no": {"nginx", "ghcr", "spare"},
    "usage=used": {"nginx"},
    "usage=unused": {"ghcr", "spare", "dangling"},
    "binding=bound": {"ghcr"},
    "binding=unbound": {"nginx", "spare"},
}
INVALID_FILTERS = {
    "dangling=maybe": "Choose yes or no.",
    "usage=always": "Choose used or unused.",
    "binding=tied": "Choose one of the listed binding states.",
    "q=" + "q" * 129: "Search for at most 128 characters.",
    "cursor=not-a-cursor": "The page link is not valid.",
}


def shown(response: Response) -> set[str]:
    return {key for key, text in KNOWN.items() if text in response.text}


async def test_the_list_shows_each_reference_with_its_binding_and_pages_through_the_host(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    alpha = await make_set(admin_client, world, "alpha", address="ghcr.io")
    gone = await make_set(admin_client, world, "gone", address="docker.io")
    await bind(world, alpha, GHCR_REFERENCE)
    await bind(world, None, "docker.io/library/nginx:1")
    await bind(world, gone, "docker.io/library/nginx:latest")
    await bind(world, alpha, "ghcr.io/o/removed:1")
    async with world.app.state.database.session() as db:
        await mark_missing(db, gone)

    page = await admin_client.get(images_path(world))
    first = await admin_client.get(f"{images_path(world)}?limit=2")
    second = await admin_client.get(next(h for h in links_in(first) if "cursor=" in h))
    environment = await admin_client.get(f"/environments/{world.remote_id}")

    text = text_of(page)
    assert page.status_code == OK
    assert "4 images." in text
    assert "Bound to alpha" in text
    assert "Anonymous pull" in text
    assert "Credential set deleted" in text and "was gone" in text
    assert "Dangling" in text
    assert f"{image_id(NGINX)[:12]}" in text
    assert repair_links(page) == [
        f"{images_path(world)}/bindings/rebind?reference=docker.io%2Flibrary%2Fnginx%3Alatest"
    ]
    assert {"Pull image", "Prune images"} <= set(re.findall(r">([^<]+)</a>", page.text))
    assert "uses-nginx" in page.text
    assert f"/environments/{world.remote_id}/containers/{USING_CONTAINER}" in links_in(page)
    assert "Not used" in page.text
    assert "refers to images that are no longer on this host" in text
    assert f"{images_path(world)}/bindings/reap" in links_in(page)
    assert "ghcr.io/o/app:2" in first.text and "nginx:1" in first.text
    assert "spare:1" in second.text and "nginx:1" not in second.text
    assert image_id(DANGLING)[:12] in second.text
    assert images_path(world) in links_in(environment)
    assert_no_store(page)


async def test_an_unreachable_host_is_a_503_page_with_a_retry_link_not_a_partial_list(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    daemon.fail("image_list", 500)

    page = await admin_client.get(images_path(world))

    assert page.status_code == 503
    assert "Images cannot be listed" in page.text
    assert "spare:1" not in page.text


async def test_each_filter_narrows_the_list_and_a_bad_filter_says_what_to_change(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    alpha = await make_set(admin_client, world, "alpha", address="ghcr.io")
    await bind(world, alpha, GHCR_REFERENCE)
    path = images_path(world)

    pages = {query: await admin_client.get(f"{path}?{query}") for query in FILTERS}
    fragments = {q: await admin_client.get(f"{path}?{q}", headers=HTMX) for q in FILTERS}
    invalid = {q: await admin_client.get(f"{path}?{q}") for q in INVALID_FILTERS}

    assert {query: shown(page) for query, page in pages.items()} == FILTERS
    assert {query: shown(fragment) for query, fragment in fragments.items()} == FILTERS
    assert not any("<html" in fragment.text for fragment in fragments.values())
    assert all('id="images-table"' in fragment.text for fragment in fragments.values())
    assert "1 image matches these filters." in text_of(pages["binding=bound"])
    assert {q: p.status_code for q, p in invalid.items()} == dict.fromkeys(INVALID_FILTERS, 400)
    assert {q: m in text_of(invalid[q]) for q, m in INVALID_FILTERS.items()} == dict.fromkeys(
        INVALID_FILTERS, True
    )


async def test_a_standard_user_sees_no_removal_or_prune_and_cannot_see_hidden_environments(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    alpha = await make_set(admin_client, world, "alpha", address="ghcr.io", users=True)
    await bind(world, alpha, "ghcr.io/o/removed:1")

    remote = await standard_client.get(images_path(world))
    local = await standard_client.get(images_path(world, world.local_id))
    hidden = await standard_client.get(images_path(world, world.down_id))
    missing = await standard_client.get(images_path(world, 99999))
    hidden_pull = await standard_client.get(f"{images_path(world, world.down_id)}/pull")
    hidden_detail = await standard_client.get(
        image_path(world, image_id(NGINX), environment_id=world.down_id)
    )

    links = links_in(remote)
    assert remote.status_code == OK
    assert "Pull image" in remote.text and "Prune images" not in remote.text
    assert not any(link.endswith("/remove") for link in links)
    assert "uses-nginx" not in remote.text and "1 container" in text_of(remote)
    assert "no longer on this host" not in text_of(remote)
    assert local.status_code == OK
    assert "Pull image" not in local.text and not any("/pull" in link for link in links_in(local))
    assert (hidden.status_code, missing.status_code) == (NOT_FOUND, NOT_FOUND)
    assert (hidden_pull.status_code, hidden_detail.status_code) == (NOT_FOUND, NOT_FOUND)
    assert DOWN_NAME not in hidden.text
    assert daemon.mutations() == []


NOT_HELD_IDS = [
    image_id(0xAB).upper(),
    image_id(NGINX)[:63],
    f"sha256:{image_id(NGINX)}",
    "nginx",
    image_id(0x77),
]


async def test_the_detail_page_shows_each_tab_to_each_role_and_refuses_ids_it_does_not_hold(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    alpha = await make_set(admin_client, world, "alpha", address="ghcr.io")
    await bind(world, alpha, GHCR_REFERENCE)
    path = image_path(world, image_id(GHCR))

    overview = await admin_client.get(path)
    configuration = await admin_client.get(f"{path}?tab=configuration")
    history = await admin_client.get(f"{path}?tab=history")
    fragment = await admin_client.get(f"{path}?tab=history", headers=HTMX)
    by_standard = await standard_client.get(path)
    standard_configuration = await standard_client.get(f"{path}?tab=configuration")
    standard_history = await standard_client.get(f"{path}?tab=history")
    unknown_tab = await admin_client.get(f"{path}?tab=secrets")
    refused = [
        r.status_code
        for client in (admin_client, standard_client)
        for r in [await client.get(image_path(world, i)) for i in NOT_HELD_IDS]
    ]
    removal = [
        (await admin_client.get(image_path(world, i, "/remove"))).status_code for i in NOT_HELD_IDS
    ]

    assert "Bound to alpha" in text_of(overview)
    assert "digest" in overview.text
    assert "Check for updates" in overview.text
    assert "serve" in configuration.text and "/entry" in configuration.text
    assert "DB_PASSWORD" in configuration.text and SECRET_ENV not in configuration.text
    assert "maintainer" in configuration.text and "ops" in configuration.text
    assert SECRET_LABEL in configuration.text
    assert BUILD_COMMAND in history.text
    assert "<html" not in fragment.text and 'id="image-tabs"' in fragment.text
    assert by_standard.status_code == OK
    assert "Not shown for your role" in standard_configuration.text
    standard_main = standard_configuration.text.split("<main", 1)[1]
    assert "serve" not in standard_main and "/entry" not in standard_main
    assert SECRET_ENV not in standard_configuration.text
    assert SECRET_LABEL not in standard_configuration.text
    assert "Redacted" in standard_configuration.text
    assert BUILD_COMMAND not in standard_history.text
    assert not any(
        link.endswith(("/remove", "/tag")) or "/untag" in link for link in links_in(by_standard)
    )
    assert unknown_tab.status_code == NOT_FOUND
    assert set(refused) == {NOT_FOUND} and set(removal) == {NOT_FOUND}
    assert_no_store(overview, configuration, history)


PULL_REFERENCE = "registry.example.com/app:1"
PULL_KEY = PULL_REFERENCE
JOB_PATH = re.compile(r"/environments/\d+/images/jobs/([A-Za-z0-9_-]{22})")
ASK = "Choose which credential set to pull with."


def pull_path(world: World, environment_id: int | None = None) -> str:
    return f"{images_path(world, environment_id)}/pull"


def job_of(response: Response) -> str:
    assert response.status_code == SEE_OTHER, response.text
    found = JOB_PATH.fullmatch(response.headers["location"])
    assert found is not None, response.headers["location"]
    return found.group(1)


def checked_choice(response: Response) -> list[str]:
    return [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input"
        and tag.attrs.get("name") == "credential_choice"
        and "checked" in tag.attrs
    ]


def offered_choices(response: Response) -> list[str]:
    return [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input" and tag.attrs.get("name") == "credential_choice"
    ]


async def test_the_pull_dialog_offers_the_sets_that_fit_and_asks_again_until_one_is_chosen(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    first = await make_set(admin_client, world, "alpha", default=True)
    second = await make_set(admin_client, world, "beta")
    await make_set(admin_client, world, "elsewhere", address="mirror.example.com")
    choices_path = f"{pull_path(world)}/choices"

    empty = await admin_client.get(pull_path(world))
    named = await admin_client.get(f"{pull_path(world)}?reference={PULL_REFERENCE}")
    swap = await admin_client.post(
        choices_path, data=csrf_form(admin_client, reference=PULL_REFERENCE), headers=HTMX
    )
    plain = await admin_client.post(
        choices_path, data=csrf_form(admin_client, reference=PULL_REFERENCE)
    )
    refused_swap = await admin_client.post(
        choices_path, data=csrf_form(admin_client, reference="Not A Reference"), headers=HTMX
    )
    open_choice = await admin_client.post(
        pull_path(world), data=csrf_form(admin_client, reference=PULL_REFERENCE)
    )
    malformed = await admin_client.post(
        pull_path(world), data=csrf_form(admin_client, reference="Not A Reference")
    )
    nonsense = await admin_client.post(
        pull_path(world),
        data=csrf_form(admin_client, reference=PULL_REFERENCE, credential_choice="x"),
    )

    assert "Name an image above" in text_of(empty)
    assert offered_choices(empty) == []
    assert offered_choices(named) == ["anonymous", str(first), str(second)]
    assert checked_choice(named) == [str(first)]
    assert "Default for this registry" in text_of(named)
    assert "Not bound yet" in text_of(named)
    assert "Anonymous (no credentials)" in text_of(named)
    assert "elsewhere" not in named.text
    assert swap.status_code == OK
    assert 'id="pull-choice"' in swap.text and "<html" not in swap.text
    assert len(offered_choices(swap)) == 3
    assert plain.status_code == SEE_OTHER
    assert plain.headers["location"].startswith(f"{pull_path(world)}?reference=")
    assert refused_swap.status_code == BAD_REQUEST
    assert "Image is not a valid image reference." in refused_swap.text
    assert open_choice.status_code == BAD_REQUEST and ASK in text_of(open_choice)
    assert (malformed.status_code, nonsense.status_code) == (BAD_REQUEST, BAD_REQUEST)
    assert "Choose one of the listed credential sets." in text_of(nonsense)
    assert len(world.app.state.engine.image_jobs) == 0
    assert daemon.calls("POST") == []
    assert_no_store(empty, named)


async def test_bound_and_credential_missing_references_are_changed_by_administrators_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    await make_set(admin_client, world, "alpha", default=True, users=True)
    bound = await make_set(admin_client, world, "beta", users=True)
    other = await make_set(admin_client, world, "gamma", users=True)
    await bind(world, bound, PULL_KEY)
    missing_reference = "registry.example.com/missing:1"
    await bind(world, other, missing_reference)
    async with world.app.state.database.session() as db:
        await mark_missing(db, other)
    bound_path = f"{pull_path(world)}?reference={PULL_REFERENCE}"
    missing_path = f"{pull_path(world)}?reference={missing_reference}"

    admin_bound = await admin_client.get(bound_path)
    admin_missing = await admin_client.get(missing_path)
    standard_bound = await standard_client.get(bound_path)
    standard_missing = await standard_client.get(missing_path)
    rebind = await standard_client.post(
        pull_path(world),
        data=csrf_form(standard_client, reference=PULL_REFERENCE, credential_choice=str(other)),
    )
    repair = await standard_client.post(
        pull_path(world), data=csrf_form(standard_client, reference=missing_reference)
    )
    local = await standard_client.get(pull_path(world, world.local_id))
    local_post = await standard_client.post(
        pull_path(world, world.local_id),
        data=csrf_form(standard_client, reference=PULL_REFERENCE),
    )

    assert checked_choice(admin_bound) == [str(bound)]
    assert "Bound to beta" in text_of(admin_bound)
    assert "changes the binding for every user" in text_of(admin_bound)
    assert "Choose another set, or an anonymous pull, to repair the binding." in text_of(
        admin_missing
    )
    assert checked_choice(admin_missing) == []
    assert offered_choices(standard_bound) == [str(bound)]
    assert "Only an administrator can change which credential set" in text_of(standard_bound)
    assert offered_choices(standard_missing) == []
    assert "Only an administrator can change which credential set" in text_of(standard_missing)
    assert rebind.status_code == FORBIDDEN
    assert "Only an administrator can change which credential set" in text_of(rebind)
    assert repair.status_code == CONFLICT and "has been deleted" in text_of(repair)
    assert (local.status_code, local_post.status_code) == (FORBIDDEN, FORBIDDEN)
    assert len(world.app.state.engine.image_jobs) == 0
    assert daemon.calls("POST") == []


STATUS_CHECKED_AT = "2026-01-02T03:04:05Z"
STATUS_DIGEST = "sha256:" + "a" * 64


async def test_the_list_shows_the_stored_update_status_of_each_reference(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    unchecked = text_of(await admin_client.get(images_path(world)))
    async with world.app.state.database.session() as db:
        await db.execute(
            "INSERT INTO image_update_status (environment_id, image_reference, state, reason, "
            "remote_digest, checked_at) VALUES (?, 'docker.io/library/nginx:1', "
            "'update_available', NULL, ?, ?)",
            (world.remote_id, STATUS_DIGEST, STATUS_CHECKED_AT),
        )
        await db.execute(
            "INSERT INTO image_update_status (environment_id, image_reference, state, reason, "
            "checked_at) VALUES (?, 'docker.io/library/spare:1', 'unknown', 'timeout', ?)",
            (world.remote_id, STATUS_CHECKED_AT),
        )

    page = await admin_client.get(images_path(world))

    text = text_of(page)
    assert "Not checked" in unchecked and "Update available" not in unchecked
    assert "Update available" in text and "checked 2026-01-02 03:04 UTC" in text
    assert "Unknown" in text
    assert inline_code_in(page.text) == []


async def test_a_host_whose_image_list_is_too_large_is_a_503_page_not_a_server_error(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    daemon.route("GET", "/images/json", Reply(200, b"[]", {"Content-Length": str(20 * MIB)}))

    page = await admin_client.get(images_path(world))

    assert page.status_code == 503
    assert "16 MiB" in page.text
    assert "Internal Server Error" not in page.text


async def test_an_unreachable_host_answers_a_script_request_for_an_image_page_with_the_fragment(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    spare = image_id(NGINX)
    daemon.route("GET", "/images/json", Refuse())
    daemon.route("GET", f"/images/sha256:{spare}/json", Refuse())

    detail = await admin_client.get(image_path(world, spare), headers=HTMX)
    removal = await admin_client.get(image_path(world, spare, "/remove"), headers=HTMX)
    listing = await admin_client.get(images_path(world), headers=HTMX)

    assert [r.status_code for r in (detail, removal, listing)] == [UNAVAILABLE] * 3
    assert not any("<html" in r.text or "Traceback" in r.text for r in (detail, removal, listing))
