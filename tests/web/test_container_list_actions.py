"""The action buttons on each container row: a fixed set in a fixed order, disabled when refused."""

from httpx import AsyncClient

from app.self_identity import HomeEnvironment, SelfIdentity
from tests.container_support import CACHE, DB, JOB, WARM, WEB, WORKER
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import World
from tests.web.html_tree import Node, parse
from tests.web.test_container_pages import containers

ENABLED = "a"
DISABLED = "button"
WEB_NAME = "web"

type Button = tuple[str, str]


def row_buttons(html: str, container: str) -> list[Button]:
    row = next(n for n in parse(html).walk() if n.attrs.get("id") == f"container-{container}")
    actions = next(n for n in row.walk() if "row-actions" in n.classes())
    return [
        (b.text(), _kind(b)) for b in actions.children if isinstance(b, Node) and b.tag != "form"
    ]


def _kind(button: Node) -> str:
    if button.tag == "button":
        assert "disabled" in button.attrs
        assert button.attrs["type"] == "button"
    return button.tag


async def test_every_row_shows_the_same_actions_in_the_same_order_and_the_unavailable_are_disabled(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(containers(world))

    rows = {
        state: row_buttons(page.text, id_)
        for state, id_ in {
            "running": WEB,
            "exited": JOB,
            "paused": CACHE,
            "restarting": WORKER,
            "created": WARM,
        }.items()
    }

    assert rows["running"] == [
        ("Stop", ENABLED),
        ("Restart", ENABLED),
        ("Kill", ENABLED),
        ("Pause", ENABLED),
        ("Remove", ENABLED),
        ("Recreate", ENABLED),
        ("Duplicate", ENABLED),
    ]
    assert rows["exited"] == [
        ("Start", ENABLED),
        ("Restart", ENABLED),
        ("Kill", DISABLED),
        ("Pause", DISABLED),
        ("Remove", ENABLED),
        ("Recreate", ENABLED),
        ("Duplicate", ENABLED),
    ]
    assert rows["paused"] == [
        ("Stop", ENABLED),
        ("Restart", ENABLED),
        ("Kill", ENABLED),
        ("Unpause", ENABLED),
        ("Remove", ENABLED),
        ("Recreate", ENABLED),
        ("Duplicate", ENABLED),
    ]
    assert rows["restarting"] == [
        ("Stop", ENABLED),
        ("Restart", DISABLED),
        ("Kill", ENABLED),
        ("Pause", DISABLED),
        ("Remove", ENABLED),
        ("Recreate", ENABLED),
        ("Duplicate", ENABLED),
    ]


async def test_a_disabled_action_is_a_named_button_that_posts_nothing(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(containers(world))

    row = next(n for n in parse(page.text).walk() if n.attrs.get("id") == f"container-{JOB}")
    kill = next(n for n in row.walk() if n.tag == "button" and n.text() == "Kill")
    assert kill.attrs["aria-label"] == "Kill job"
    assert not {"href", "hx-get", "hx-post", "form", "formaction"} & set(kill.attrs)
    assert "btn--danger" in kill.classes()


async def test_the_detail_page_action_bar_keeps_the_same_set_with_the_refused_ones_disabled(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(f"{containers(world)}/{JOB}")

    summary = next(n for n in parse(page.text).walk() if n.attrs.get("id") == "container-summary")
    actions = next(n for n in summary.walk() if "row-actions" in n.classes())
    shown = [(b.text(), b.tag) for b in actions.children if isinstance(b, Node)]
    assert shown == [
        ("Start", ENABLED),
        ("Restart", ENABLED),
        ("Kill", DISABLED),
        ("Pause", DISABLED),
        ("Remove", ENABLED),
    ]


async def test_the_row_of_dockmins_own_container_disables_the_actions_it_refuses(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    world.app.state.self_identity = SelfIdentity(
        container_id=WEB,
        source="mountinfo",
        verified=(HomeEnvironment(world.remote_id, "tcp_tls", WEB_NAME),),
    )

    page = await admin_client.get(containers(world))

    assert row_buttons(page.text, WEB) == [
        ("Stop", DISABLED),
        ("Restart", DISABLED),
        ("Kill", DISABLED),
        ("Pause", DISABLED),
        ("Remove", DISABLED),
        ("Recreate", DISABLED),
        ("Duplicate", ENABLED),
    ]
    assert row_buttons(page.text, DB)[0] == ("Stop", ENABLED)


async def test_a_standard_user_gets_the_set_only_on_a_row_they_may_change(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    page = await standard_client.get(containers(world))

    assert [label for label, _ in row_buttons(page.text, DB)][:5] == [
        "Stop",
        "Restart",
        "Kill",
        "Pause",
        "Remove",
    ]
    assert [label for label, _ in row_buttons(page.text, JOB) if label != "Duplicate"] == []
