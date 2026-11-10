"""The container page's tabs: Automation, per-tab activity, Health, Last exit and Stats."""

from typing import Any

import pytest
from httpx import AsyncClient

from tests.access_support import container_id
from tests.auth_support import csrf_form
from tests.container_support import DB, JOB, WARM, WEB, WORKER
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Reply
from tests.docker_engine.helpers import STDERR, STDOUT, log_frame
from tests.web.environment_support import World
from tests.web.helpers import (
    activity_panel_in,
    automation_panel_present,
    facts_under,
    parse_tags,
    with_state,
)
from tests.web.html_tree import parse
from tests.web.test_activity_pages import HEALTH, event_row, insert_events
from tests.web.test_automation_pages import create, location
from tests.web.test_container_pages import containers

HTMX = {"HX-Request": "true"}
OK = 200
ADD_AUTOMATION = "Add to new automation"
TABS = ("overview", "labels", "mounts", "networks", "environment", "stats", "logs", "automation")
ADMIN_TAB_ORDER = [*TABS, "access"]
STANDARD_TAB_ORDER = ["overview", "labels", "mounts", "networks", "stats", "logs", "automation"]
CREATED = container_id(0xB1)
NEVER_RAN = container_id(0xB2)
KILLED = container_id(0xB3)
DEAD = container_id(0xB4)
ZERO_TIME = "0001-01-01T00:00:00Z"
START_FAILURE = "driver failed programming external connectivity: port is already allocated"
HIDDEN_FROM_STANDARD = "Visible to administrators"
ENGINE_ERROR_SENTENCE = "The Engine recorded an error. Its text is shown to administrators."
HEALTH_OUTPUT_SHOWN = 1000
LONG_OUTPUT = "x" * (HEALTH_OUTPUT_SHOWN + 500)
CHECK_FAILED = "curl: (7) connection refused"
CHECK_TIME = "2026-03-01T12:00:00.000000000Z"
NOT_RUNNING_FACTS = {"Finished", "Exit code"}


def detail_path(world: World, container: str, tab: str | None = None) -> str:
    suffix = "" if tab is None else f"?tab={tab}"
    return f"{containers(world)}/{container}{suffix}"


def tab_links(html: str) -> list[str]:
    nav = next(n for n in parse(html).walk() if n.attrs.get("aria-label") == "Container sections")
    return [a.text() for a in nav.walk() if a.tag == "a"]


def health_check(status: str, streak: int, *, output: str = CHECK_FAILED) -> dict[str, object]:
    return {
        "Status": status,
        "FailingStreak": streak,
        "Log": [{"Start": CHECK_TIME, "End": CHECK_TIME, "ExitCode": 7, "Output": output}],
    }


async def test_the_automation_tab_holds_the_rules_panel_and_no_other_tab_renders_it(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    pages = {tab: await admin_client.get(detail_path(world, WEB, tab)) for tab in ADMIN_TAB_ORDER}
    fragments = {
        tab: await admin_client.get(detail_path(world, WEB, tab), headers=HTMX)
        for tab in ("labels", "automation")
    }

    assert {tab: automation_panel_present(page.text) for tab, page in pages.items()} == {
        tab: tab == "automation" for tab in ADMIN_TAB_ORDER
    }
    assert {tab: ADD_AUTOMATION in page.text for tab, page in pages.items()} == {
        tab: tab == "automation" for tab in ADMIN_TAB_ORDER
    }
    assert tab_links(pages["overview"].text) == [t.capitalize() for t in ADMIN_TAB_ORDER]
    assert automation_panel_present(fragments["automation"].text)
    assert not automation_panel_present(fragments["labels"].text)
    assert "<html" not in fragments["automation"].text


async def test_a_standard_user_gets_the_automation_tab_and_not_the_administrator_tabs(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    overview = await standard_client.get(detail_path(world, DB))
    automation = await standard_client.get(detail_path(world, DB, "automation"))

    assert tab_links(overview.text) == [t.capitalize() for t in STANDARD_TAB_ORDER]
    assert automation_panel_present(automation.text) and ADD_AUTOMATION in automation.text


async def test_pausing_and_resuming_a_rule_from_its_panel_return_to_the_automation_tab(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule = await create(admin_client, world)
    back_to = f"container:{DB}"

    paused = await admin_client.post(
        f"/automation/rules/{rule}/pause",
        data=csrf_form(admin_client, duration="1h", return_to=back_to),
    )
    resumed = await admin_client.post(
        f"/automation/rules/{rule}/resume", data=csrf_form(admin_client, return_to=back_to)
    )

    assert location(paused) == location(resumed) == detail_path(world, DB, "automation")


async def test_each_tab_shows_only_the_events_that_belong_to_it(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    on_web: dict[str, Any] = {"environment": world.remote_id, "target": WEB}
    insert_events(
        world.app,
        [
            event_row("container_restarted", 1, **on_web),
            event_row("network_connected", 2, **on_web),
            event_row("resource_control_updated", 3, **on_web),
            event_row(
                "container_restart_budget_exhausted",
                4,
                category="automation",
                actor="subsystem",
                user=None,
                subsystem=HEALTH,
                **on_web,
            ),
            event_row("container_restarted", 5, environment=world.remote_id, target=DB),
        ],
    )

    panels = {
        tab: activity_panel_in((await admin_client.get(detail_path(world, WEB, tab))).text)
        for tab in ADMIN_TAB_ORDER
    }
    fragment = await admin_client.get(detail_path(world, WEB, "networks"), headers=HTMX)

    assert panels["overview"] is not None
    assert panels["overview"][0] == "Recent activity"
    assert sorted(panels["overview"][1]) == [
        "container_restart_budget_exhausted",
        "container_restarted",
        "network_connected",
        "resource_control_updated",
    ]
    assert panels["networks"] == ("Network changes", ["network_connected"])
    assert panels["access"] == ("Access changes", ["resource_control_updated"])
    assert panels["automation"] == ("Automated actions", ["container_restart_budget_exhausted"])
    assert [panels[tab] for tab in ("labels", "mounts", "environment", "stats", "logs")] == [
        None
    ] * 5
    assert activity_panel_in(fragment.text) == ("Network changes", ["network_connected"])


async def test_a_standard_user_sees_no_activity_panel_on_any_tab(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    insert_events(
        world.app, [event_row("network_connected", 1, environment=world.remote_id, target=DB)]
    )

    panels = [
        activity_panel_in((await standard_client.get(detail_path(world, DB, tab))).text)
        for tab in ("overview", "networks", "automation")
    ]

    assert panels == [None, None, None]


async def test_the_health_section_reports_each_state_and_the_absence_of_a_check(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_state(monkeypatch, container_host.boxes[DB], Health=health_check("unhealthy", 3))
    with_state(monkeypatch, container_host.boxes[WARM], Health=health_check("starting", 0))
    healthy = container_host.add(container_id(0xB5), "ok", health="healthy")
    pages = {
        name: facts_under((await admin_client.get(detail_path(world, id_))).text, "Health")
        for name, id_ in (
            ("none", WEB),
            ("unhealthy", DB),
            ("starting", WARM),
            ("healthy", healthy.id),
        )
    }

    assert pages["none"] == {
        "Status": "No health check",
        "Failing streak": "—",
        "Last check": "—",
        "Last exit code": "—",
    }
    assert pages["unhealthy"]["Status"] == "Unhealthy"
    assert pages["unhealthy"]["Failing streak"] == "3"
    assert pages["unhealthy"]["Last exit code"] == "7"
    assert pages["unhealthy"]["Last check"].startswith("2026-03-01 12:00")
    assert pages["unhealthy"]["Last output"] == CHECK_FAILED
    assert pages["starting"]["Status"] == "Starting"
    assert pages["healthy"]["Status"] == "Healthy"
    assert pages["healthy"]["Failing streak"] == "0"
    assert pages["healthy"]["Last check"] == "—"
    assert "Last output" not in pages["healthy"]


async def test_health_output_is_shown_to_administrators_cut_to_a_length_and_hidden_from_others(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_state(
        monkeypatch, shared_host.boxes[DB], Health=health_check("unhealthy", 3, output=LONG_OUTPUT)
    )

    as_admin = await admin_client.get(detail_path(world, DB))
    as_standard = await standard_client.get(detail_path(world, DB))

    assert facts_under(as_admin.text, "Health")["Last output"] == "x" * HEALTH_OUTPUT_SHOWN
    standard_facts = facts_under(as_standard.text, "Health")
    assert standard_facts["Last output"] == HIDDEN_FROM_STANDARD
    assert standard_facts["Status"] == "Unhealthy"
    assert "xxxxx" not in as_standard.text


async def test_last_exit_reports_the_cause_of_each_way_a_container_stopped(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_host.add(CREATED, "failed-start", state="created", exit_code=127)
    with_state(monkeypatch, container_host.boxes[CREATED], Error=START_FAILURE, StartedAt=ZERO_TIME)
    container_host.add(NEVER_RAN, "never-ran", state="created")
    with_state(monkeypatch, container_host.boxes[NEVER_RAN], StartedAt=ZERO_TIME)
    container_host.add(KILLED, "oom", state="exited", exit_code=137)
    with_state(monkeypatch, container_host.boxes[KILLED], OOMKilled=True)
    container_host.add(DEAD, "dead", state="dead", exit_code=2)
    container_host.route(
        "GET",
        f"/containers/{JOB}/logs",
        Reply(
            200,
            log_frame(STDOUT, b"2026-10-19T12:00:01.000000001Z shutting down\n")
            + log_frame(STDERR, b"2026-10-19T12:00:02.000000001Z <b>fatal</b>\n"),
            {"Content-Type": "application/vnd.docker.multiplexed-stream"},
        ),
    )

    ran = await admin_client.get(detail_path(world, JOB))
    failed_start = await admin_client.get(detail_path(world, CREATED))
    never_ran = await admin_client.get(detail_path(world, NEVER_RAN))
    killed = await admin_client.get(detail_path(world, KILLED))
    dead = await admin_client.get(detail_path(world, DEAD))

    assert facts_under(ran.text, "Last exit")["Exit code"] == "1"
    assert set(facts_under(ran.text, "Last exit")) == NOT_RUNNING_FACTS
    assert "shutting down" in ran.text and "&lt;b&gt;fatal&lt;/b&gt;" in ran.text
    assert "<b>fatal</b>" not in ran.text
    assert facts_under(failed_start.text, "Last exit") == {
        "Exit code": "Never started",
        "Engine error": START_FAILURE,
    }
    assert facts_under(never_ran.text, "Last exit") == {"Exit code": "Never started"}
    assert facts_under(killed.text, "Last exit")["Cause"] == "Killed: out of memory"
    assert facts_under(killed.text, "Last exit")["Exit code"] == "137"
    assert facts_under(dead.text, "Last exit")["Exit code"] == "2"


async def test_the_engine_error_is_withheld_from_a_standard_user_as_a_sentence(
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_state(monkeypatch, shared_host.boxes[JOB], Error=START_FAILURE)

    page = await standard_client.get(detail_path(world, JOB))

    assert facts_under(page.text, "Last exit")["Engine error"] == ENGINE_ERROR_SENTENCE
    assert START_FAILURE not in page.text


async def test_last_exit_appears_only_for_a_container_that_is_stopped(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    states = {
        "running": WEB,
        "paused": container_id(0xA3),
        "restarting": WORKER,
        "stopped": JOB,
    }

    shown = {
        state: bool(
            facts_under((await admin_client.get(detail_path(world, id_))).text, "Last exit")
        )
        for state, id_ in states.items()
    }

    assert shown == {"running": False, "paused": False, "restarting": False, "stopped": True}


async def test_the_stats_panel_carries_the_reading_for_the_charts_and_the_charts_are_hidden(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(detail_path(world, WEB, "stats"))

    tags = parse_tags(page.text)
    sample = next(t for t in tags if "data-stats-sample" in t.attrs)
    charts = next(t for t in tags if "data-stats-charts" in t.attrs)
    assert {k: v for k, v in sample.attrs.items() if k.startswith("data-")} == {
        "data-stats-sample": "",
        "data-read-microsecond": "0",
        "data-cpu": "2.0",
        "data-mem-used": "52427776",
        "data-mem-limit": "1073741824",
        "data-net-rx": "0",
        "data-net-tx": "0",
        "data-blk-read": "4096",
        "data-blk-write": "8192",
    }
    assert "hidden" in charts.attrs
    assert [t.attrs["data-chart"] for t in tags if t.name == "figure"] == [
        "cpu",
        "mem",
        "net",
        "blk",
    ]


async def test_a_stopped_container_has_no_charts(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(detail_path(world, JOB, "stats"))

    assert "data-stats-charts" not in page.text
    assert "not running" in page.text
