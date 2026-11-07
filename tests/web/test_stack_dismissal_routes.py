"""Dismissing and restoring unmanaged projects and namespaces through the stack lists."""

import re
from html import unescape
from typing import Any

import pytest
from httpx import AsyncClient, Response

import app.services.stack_dismissal_assembly as assembly
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.services.stack_dismissal_views import HostMembers
from app.services.stack_dismissals import MAX_DISMISSED_OBJECTS
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import VOLUME_CREATED, ContainerEngine, swarm_network_id
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.stack_support import PROJECT_LABEL
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.environment_support import World, rows
from tests.web.flash_support import flash_texts, land
from tests.web.helpers import hx_triggers
from tests.web.registry_support import visible_text

OK, SEE_OTHER, NO_CONTENT, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = (
    200,
    303,
    204,
    400,
    403,
    404,
    409,
)
WRONG_TOKEN = "W" * 43
HTMX = {"HX-Request": "true"}
GONE = "gone"
ODD = "odd"
HELD = "held"
OLD = "Old"
OTHER = "Other"
LIVE = "Live"
GONE_NETWORK = 0x51
LONG_NAME = "v" * 200
HOSTILE_NAME = "evil/<b>x</b>"
STALE_DIGEST = "0" * 64
HELD_CONTAINER = f"{0xA7:064x}"
DISMISSED_NOTICE = "Entry dismissed. It stays out of the list until dismissed entries are shown."
RESTORED_NOTICE = "Entry restored. It is in the list again."
CHANGED_NOTE = "Dismissed earlier; it has changed since"
ANCHOR = re.compile(r'<a class="mono cell-wrap" href="([^"]*)">([^<]*)</a>')
PLAIN = re.compile(r'<span class="mono cell-wrap">([^<]*)</span>')
ACTIONS = "'stack_dismissed', 'stack_dismissal_cleared'"


def compose_base(world: World) -> str:
    return f"/environments/{world.remote_id}/stacks"


def swarm_base(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm/stacks"


def dismiss_path(base: str, name: str) -> str:
    return f"{base}/-/unmanaged/{name}/dismiss"


def restore_path(base: str, name: str) -> str:
    return f"{base}/-/unmanaged/{name}/restore"


def digest_of(html: str, action: str) -> str:
    found = re.search(
        rf'action="{re.escape(action)}"[^>]*>\s*<input type="hidden" name="csrf_token" '
        r'value="[^"]*">\s*<input type="hidden" name="expected_objects" value="([^"]*)"',
        html,
    )
    assert found is not None, f"no dismiss form for {action}"
    return found.group(1)


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def links_in(html: str) -> dict[str, str]:
    return {unescape(href): unescape(label) for href, label in ANCHOR.findall(html)}


def plain_in(html: str) -> list[str]:
    return [unescape(label) for label in PLAIN.findall(html)]


async def dismissal_events(world: World) -> list[tuple[str, str, str]]:
    found = await rows(
        world,
        "SELECT action, target_id, result FROM activity_event "
        f"WHERE action IN ({ACTIONS}) ORDER BY id",
    )
    return [(row["action"], row["target_id"], row["result"]) for row in found]


async def stored_dismissals(world: World) -> int:
    return len(await rows(world, "SELECT 1 FROM stack_dismissal"))


async def seed_dismissal(world: World, kind: Any, name: str, objects: list[str]) -> None:
    async with world.app.state.database.session() as db:
        await StackDismissalRepository(db, world.app.state.auth.clock).put_dismissal(
            world.remote_id, kind, name, objects, None
        )


async def post_form(
    client: AsyncClient, path: str, headers: dict[str, str] | None = None, **fields: str
) -> Response:
    return await client.post(path, data=csrf_form(client, **fields), headers=headers)


def leave_compose_objects(host: ContainerEngine) -> str:
    """Leave a project of only objects, one of odd names, and one with a stopped container."""
    network_id = host.add_network("gone_default", GONE_NETWORK, Labels={PROJECT_LABEL: GONE})
    host.add_volume("gone_data", labels={PROJECT_LABEL: GONE})
    host.add_volume(HOSTILE_NAME, labels={PROJECT_LABEL: ODD})
    host.add_volume(LONG_NAME, labels={PROJECT_LABEL: ODD})
    host.add(HELD_CONTAINER, "held-web-1", state="exited", labels={PROJECT_LABEL: HELD})
    host.add_volume("held_data", labels={PROJECT_LABEL: HELD})
    return network_id


def leave_swarm_objects(host: SwarmEngine) -> dict[str, str]:
    old, other = {NAMESPACE_LABEL: OLD}, {NAMESPACE_LABEL: OTHER}
    network_id = swarm_network_id(GONE_NETWORK)
    host.add_network("old_net", 0, "overlay", network_id=network_id, Scope="swarm", Labels=old)
    host.add_volume("old_data", labels=old)
    host.add_volume(HOSTILE_NAME, labels=other)
    host.add_service(plain_spec("live_web", Labels={NAMESPACE_LABEL: LIVE}))
    host.add_volume("live_data", labels={NAMESPACE_LABEL: LIVE})
    return {
        "config": host.add_config("old-conf", labels=old),
        "secret": host.add_secret("old-sec", labels=old),
        "other_config": host.add_config("other-conf", labels=other),
        "network": network_id,
    }


async def test_the_project_list_names_and_links_each_leftover_and_offers_dismissal_to_them(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    network_id = leave_compose_objects(stack_host)
    rid = world.remote_id

    listing = await admin_client.get(compose_base(world))

    links, plain, text = links_in(listing.text), plain_in(listing.text), text_of(listing)
    assert listing.status_code == OK
    assert links[f"/environments/{rid}/volumes/gone_data"] == "gone_data"
    assert links[f"/environments/{rid}/networks/{network_id}"] == "gone_default"
    assert links[f"/environments/{rid}/volumes/{LONG_NAME}"] == LONG_NAME[:128]
    assert HOSTILE_NAME in plain and not any("evil" in href for href in links)
    assert "<b>x</b>" not in listing.text and "&lt;b&gt;x&lt;/b&gt;" in listing.text
    assert "No containers. Left behind: 1 volume, 1 network" in text
    assert "Left behind: 2 volumes" in text
    assert 'aria-label="Dismiss gone"' in listing.text
    assert 'aria-label="Dismiss odd"' in listing.text
    assert 'aria-label="Dismiss held"' not in listing.text
    assert 'aria-label="Dismiss shop"' not in listing.text


async def test_dismissing_a_project_hides_and_counts_it_and_restoring_shows_it_again(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    leave_compose_objects(stack_host)
    base, rid = compose_base(world), world.remote_id
    action = dismiss_path(base, GONE)
    form = digest_of((await admin_client.get(base)).text, action)

    dismissed = await post_form(admin_client, action, HTMX, expected_objects=form)
    after = await admin_client.get(dismissed.headers["hx-redirect"])
    shown = await admin_client.get(f"{base}?dismissed=1")
    volumes = await admin_client.get(f"/environments/{rid}/volumes")
    networks = await admin_client.get(f"/environments/{rid}/networks")
    restored = await post_form(admin_client, restore_path(base, GONE))
    back = await land(admin_client, restored)
    again = await post_form(admin_client, restore_path(base, GONE))

    assert dismissed.status_code == NO_CONTENT
    assert dismissed.headers["hx-redirect"].startswith(base)
    assert "counts-changed" not in hx_triggers(dismissed)
    assert flash_texts(after.text) == [DISMISSED_NOTICE]
    assert 'id="project-gone"' not in after.text and 'id="project-odd"' in after.text
    assert "1 dismissed entry is hidden." in text_of(after)
    assert 'id="project-gone"' in shown.text
    assert "Showing 1 dismissed entry." in text_of(shown)
    assert 'aria-label="Restore gone to the list"' in shown.text and "Dismissed" in text_of(shown)
    assert 'id="project-odd"' not in shown.text
    assert "gone_data" in volumes.text and "gone_default" in networks.text
    assert flash_texts(back.text) == [RESTORED_NOTICE]
    assert 'id="project-gone"' in back.text and "dismissed entry is hidden" not in text_of(back)
    assert again.status_code == SEE_OTHER
    assert await dismissal_events(world) == [
        ("stack_dismissed", GONE, "success"),
        ("stack_dismissal_cleared", GONE, "success"),
    ]


async def test_a_dismissed_project_lists_again_with_a_note_when_an_object_or_a_container_appears(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    leave_compose_objects(stack_host)
    base = compose_base(world)
    await seed_dismissal(
        world,
        "compose",
        GONE,
        [f"network:{GONE_NETWORK:064x}", f"volume:gone_data:{VOLUME_CREATED}"],
    )
    hidden = await admin_client.get(base)
    stack_host.add_network("gone_extra", GONE_NETWORK + 1, Labels={PROJECT_LABEL: GONE})
    with_object = await admin_client.get(base)
    stack_host.add(f"{0xA8:064x}", "gone-web-1", state="exited", labels={PROJECT_LABEL: GONE})
    with_container = await admin_client.get(base)

    assert 'id="project-gone"' not in hidden.text
    assert "1 dismissed entry is hidden." in text_of(hidden)
    assert 'id="project-gone"' in with_object.text and CHANGED_NOTE in text_of(with_object)
    assert 'aria-label="Dismiss gone"' in with_object.text
    assert "dismissed entry" not in text_of(with_object)
    assert 'id="project-gone"' in with_container.text and CHANGED_NOTE in text_of(with_container)
    assert 'aria-label="Dismiss gone"' not in with_container.text


async def test_a_project_with_a_container_a_stale_digest_or_too_many_objects_is_refused(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leave_compose_objects(stack_host)
    base = compose_base(world)
    digest = digest_of((await admin_client.get(base)).text, dismiss_path(base, GONE))

    with_container = await post_form(
        admin_client, dismiss_path(base, HELD), expected_objects=digest
    )
    stale = await post_form(admin_client, dismiss_path(base, GONE), expected_objects=STALE_DIGEST)

    async def flood(environment_id: int, key: str) -> HostMembers:
        return HostMembers(0, tuple(f"network:{n}" for n in range(MAX_DISMISSED_OBJECTS + 1)))

    monkeypatch.setattr(assembly, "_compose_reader", lambda hosts: flood)
    many = await post_form(admin_client, dismiss_path(base, GONE), expected_objects=digest)

    assert with_container.status_code == CONFLICT
    assert "still has containers or services" in text_of(with_container)
    assert stale.status_code == CONFLICT and "The entry changed" in text_of(stale)
    assert many.status_code == BAD_REQUEST and "holds too many objects" in text_of(many)
    assert await stored_dismissals(world) == 0
    assert [(a, t, r) for a, t, r in await dismissal_events(world)] == [
        ("stack_dismissed", HELD, "failure"),
        ("stack_dismissed", GONE, "failure"),
        ("stack_dismissed", GONE, "failure"),
    ]


async def test_a_standard_user_cannot_dismiss_or_restore_and_malformed_names_are_not_found(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    leave_compose_objects(stack_host)
    base = compose_base(world)
    await seed_dismissal(world, "compose", ODD, ["volume:x:y"])
    digest = digest_of((await admin_client.get(base)).text, dismiss_path(base, GONE))

    listing = await standard_client.get(base)
    dismiss = await post_form(standard_client, dismiss_path(base, GONE), expected_objects=digest)
    restore = await post_form(standard_client, restore_path(base, ODD))
    malformed = [
        (
            await post_form(admin_client, dismiss_path(base, name), expected_objects=digest)
        ).status_code
        for name in ("-dash", "a%20b", "x" * 64)
    ]

    assert listing.status_code == OK and "Compose projects not managed here" not in listing.text
    assert "Dismiss" not in text_of(listing)
    assert (dismiss.status_code, restore.status_code) == (NOT_FOUND, NOT_FOUND)
    assert malformed == [NOT_FOUND] * 3
    assert await stored_dismissals(world) == 1
    assert await dismissal_events(world) == []


async def test_the_namespace_list_names_and_links_each_leftover_and_reads_the_volumes_once(
    admin_client: AsyncClient, world: World, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    ids = leave_swarm_objects(swarm_host)
    base, rid = swarm_base(world), world.remote_id
    before = len(swarm_host.calls("GET", "/volumes"))

    listing = await admin_client.get(base)

    links, plain, text = links_in(listing.text), plain_in(listing.text), text_of(listing)
    assert listing.status_code == OK
    assert len(swarm_host.calls("GET", "/volumes")) - before == 1
    assert links[f"/environments/{rid}/swarm/configs/{ids['config']}"] == "old-conf"
    assert links[f"/environments/{rid}/swarm/secrets/{ids['secret']}"] == "old-sec"
    assert links[f"/environments/{rid}/networks/{ids['network']}"] == "old_net"
    assert links[f"/environments/{rid}/volumes/old_data"] == "old_data"
    assert HOSTILE_NAME in plain and not any("evil" in href for href in links)
    assert (
        "No services. Left behind: 1 config, 1 secret, 1 network, 1 volume visible on this manager"
        in text
    )
    assert 'aria-label="Dismiss Old"' in listing.text
    assert 'aria-label="Dismiss Other"' in listing.text
    assert 'aria-label="Dismiss Live"' not in listing.text


async def test_dismissing_a_namespace_hides_and_counts_it_and_restoring_shows_it_again(
    admin_client: AsyncClient, world: World, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    ids = leave_swarm_objects(swarm_host)
    base, rid = swarm_base(world), world.remote_id
    action = dismiss_path(base, OLD)
    form = digest_of((await admin_client.get(base)).text, action)

    dismissed = await post_form(admin_client, action, HTMX, expected_objects=form)
    after = await admin_client.get(dismissed.headers["hx-redirect"])
    shown = await admin_client.get(f"{base}?dismissed=1")
    configs = await admin_client.get(f"/environments/{rid}/swarm/configs")
    swarm_host.add_config("old-new", labels={NAMESPACE_LABEL: OLD})
    changed = await admin_client.get(base)
    restored = await post_form(admin_client, restore_path(base, OLD))
    back = await land(admin_client, restored)
    again = await post_form(admin_client, restore_path(base, OLD))

    assert dismissed.status_code == NO_CONTENT
    assert dismissed.headers["hx-redirect"].startswith(base)
    assert "counts-changed" not in hx_triggers(dismissed)
    assert flash_texts(after.text) == [DISMISSED_NOTICE]
    assert 'aria-label="Dismiss Old"' not in after.text
    assert 'aria-label="Dismiss Other"' in after.text
    assert "1 dismissed entry is hidden." in text_of(after)
    assert "Showing 1 dismissed entry." in text_of(shown)
    assert 'aria-label="Restore Old to the list"' in shown.text
    assert ids["config"] in configs.text and "old-conf" in configs.text
    assert CHANGED_NOTE in text_of(changed) and 'aria-label="Dismiss Old"' in changed.text
    assert "dismissed entry" not in text_of(changed)
    assert flash_texts(back.text) == [RESTORED_NOTICE]
    assert 'aria-label="Restore Old to the list"' not in back.text
    assert again.status_code == SEE_OTHER
    assert await dismissal_events(world) == [
        ("stack_dismissed", "old", "success"),
        ("stack_dismissal_cleared", "old", "success"),
    ]


async def test_a_namespace_with_a_service_a_stale_digest_or_a_standard_user_is_refused(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    anonymous_client: AsyncClient,
    world: World,
    swarm_stacks_world: World,
    swarm_host: SwarmEngine,
) -> None:
    leave_swarm_objects(swarm_host)
    base = swarm_base(world)
    digest = digest_of((await admin_client.get(base)).text, dismiss_path(base, OLD))

    with_service = await post_form(admin_client, dismiss_path(base, LIVE), expected_objects=digest)
    stale = await post_form(admin_client, dismiss_path(base, OLD), expected_objects=STALE_DIGEST)
    dismiss = await post_form(standard_client, dismiss_path(base, OLD), expected_objects=digest)
    restore = await post_form(standard_client, restore_path(base, OLD))
    paths = (dismiss_path(base, OLD), restore_path(base, OLD))
    untokened = [(await admin_client.post(path, data={})).status_code for path in paths]
    wrong = [
        (await admin_client.post(path, data={"csrf_token": WRONG_TOKEN})).status_code
        for path in paths
    ]
    signed_out = [(await post_form(anonymous_client, path)).status_code for path in paths]

    assert with_service.status_code == CONFLICT
    assert "still has containers or services" in text_of(with_service)
    assert stale.status_code == CONFLICT and "The entry changed" in text_of(stale)
    assert (dismiss.status_code, restore.status_code) == (NOT_FOUND, NOT_FOUND)
    assert untokened == [FORBIDDEN] * 2 and wrong == [FORBIDDEN] * 2
    assert signed_out == [SEE_OTHER] * 2
    assert await stored_dismissals(world) == 0
    assert await dismissal_events(world) == [
        ("stack_dismissed", LIVE.lower(), "failure"),
        ("stack_dismissed", OLD.lower(), "failure"),
    ]
