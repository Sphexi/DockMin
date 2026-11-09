"""The activity log pages: the filtered log, one event, the CSV download and the object panels."""

import asyncio
import csv
import gc
import io
import json
import re
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import AsyncExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from itertools import pairwise
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

import app.services.activity_log as activity_log
import app.services.activity_targets as activity_targets
from app.db.repositories.activity_event import ActivityFilter
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.services.assembly import app_activity_log_service
from app.web.activity_routes import _ExportResponse
from tests.auth_support import ADMIN_ID, csrf_form
from tests.container_support import STACK_LABEL, SWARM_LABEL, WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import WORKER, SwarmEngine
from tests.job_support import ADMIN, job_services
from tests.stack_support import stopped_stack_jobs  # noqa: F401
from tests.web.environment_support import World, seed_stack_rows
from tests.web.helpers import inline_code_in
from tests.web.html_tree import Page, parse
from tests.web.test_stack_pages import NAME as COMPOSE_STACK
from tests.web.test_stack_pages import compose_file, create_blog, stacks_path
from tests.web.test_swarm_stack_pages import NAME as SWARM_STACK
from tests.web.test_swarm_stack_pages import Browser, base, create_shop

OK = 200
BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
TOO_MANY = 429
ACTIVITY_LINK = '<a class="nav__link" href="/activity"'
FIRST_PAGE_ROWS = 50
SECOND_PAGE_ROWS = 13
SMALL_PAGE = 25
CONTAINER_EVENTS = 60
HTMX = {"HX-Request": "true"}
BOOSTED = {"HX-Request": "true", "HX-Boosted": "true"}
RESTORE = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
HOSTILE_USER = '=x,"y"\nz‮<b>'
RULE_NAME = "Nightly <b>health</b>"
HEALTH = "container-health-checker"
EXPORTER = "activity-log-exporter"
REQUEST_ID = "0123456789abcdef.-_"
RANGE_MESSAGE = "Give a time between 1970 and 9998 (UTC)."
FORMAT_MESSAGE = "enter a UTC time such as"
NEXT_LINK = re.compile(r'<a class="pagination__link" href="([^"]+)" rel="next"')
ROW_LINK = re.compile(r'<a href="/activity/(\d+)"')
INSERT_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, actor_subsystem, rule_id, "
    "job_run_id, environment_id, target_type, target_id, action, result, detail, occurred_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, backup_prefix, "
    "activity_log_prefix, retention_count, created_at, updated_at) VALUES (?, "
    "'http://127.0.0.1:29000', 'us-east-1', 'b', 'k', 'x', 1, 1, NULL, 'backups', 'logs', NULL, "
    "?, ?)"
)
INSERT_RULE = (
    "INSERT INTO automation_rule (name, subsystem, environment_id, target_selector, parameters, "
    "is_enabled, created_by_user_id, created_at, updated_at) VALUES "
    "(?, 'container-health-checker', NULL, '', '{}', 1, NULL, ?, ?)"
)
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES (?, 'x', 'standard', 1, ?)"
)
INSERT_TEAM = "INSERT INTO team (name, created_at) VALUES (?, ?)"
INSERT_CHANNEL = (
    "INSERT INTO notification_channel (name, kind, settings, created_at, updated_at) "
    "VALUES (?, 'ntfy', '{}', ?, ?)"
)
PLANTED_ID = "chosen-by-the-client"
SHARED_REQUEST = "shared-request-1"
REQUEST_HEADING = "Events recorded with request id"
REQUEST_NOTE = "A client can supply the request id"
REQUEST_MESSAGE = "Request id: use 1 to 64 letters, digits, dots, hyphens or underscores."
INSERT_REGISTRY = (
    "INSERT INTO registry_credential (name, provider, url, authentication_enabled, username, "
    "secret_kind, secret_ciphertext, provider_extras, created_at, updated_at) "
    "VALUES (?, 'custom', 'gone.example.com', 1, 'u', 'static', 'x', '{}', ?, ?)"
)


@dataclass(frozen=True, slots=True)
class Seeded:
    rule_id: int
    run_id: int


def stamp(minutes_ago: int) -> str:
    return (BASE - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def flat(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def event_row(
    action: str,
    minutes: int,
    *,
    category: str = "user_action",
    actor: str = "user",
    user: int | None = ADMIN_ID,
    subsystem: str | None = None,
    rule: int | None = None,
    run: int | None = None,
    environment: int | None = None,
    kind: str = "container",
    target: str | None = None,
    result: str = "success",
    detail: str | None = None,
) -> tuple[Any, ...]:
    return (
        category, actor, user, subsystem, rule, run, environment, kind, target, action, result,
        detail, stamp(minutes),
    )  # fmt: skip


def insert_events(app: FastAPI, rows: list[tuple[Any, ...]]) -> None:
    connection = app.state.database.connect()
    try:
        connection.execute("BEGIN")
        connection.executemany(INSERT_EVENT, rows)
        connection.execute("COMMIT")
    finally:
        connection.close()


async def seed(app: FastAPI, world: World) -> Seeded:
    """63 events: 60 of a user's, one of a subsystem's, a failed login and an environment's own."""
    async with app.state.database.session() as db, db.transaction():
        clock = app.state.auth.clock
        rule = await AutomationRuleRepository(db, clock).insert(
            name=RULE_NAME,
            subsystem=HEALTH,
            environment_id=world.local_id,
            target_selector="",
            parameters="{}",
            is_enabled=True,
            created_by_user_id=ADMIN_ID,
        )
        job = await ScheduledJobRepository(db, clock).insert(
            subsystem=HEALTH,
            rule_id=rule.id,
            schedule_kind="interval",
            schedule_expression="600",
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=5,
            next_run_at="2030-01-01T00:00:00Z",
        )
        runs = JobRunRepository(db, clock)
        run = await runs.start(job.id, trigger="schedule", scheduled_for=None)
        await runs.finish(run.id, outcome="success", reason=None, message=None, detail={})
    containers = [
        event_row(
            "container_restarted" if n % 3 else "container_stopped",
            100 + n,
            environment=world.local_id,
            target=f"c{n}",
            result="failure" if n % 4 == 0 else "success",
        )
        for n in range(CONTAINER_EVENTS)
    ]
    exhausted = event_row(
        "container_restart_budget_exhausted",
        1,
        category="automation",
        actor="subsystem",
        user=None,
        subsystem=HEALTH,
        rule=rule.id,
        run=run.id,
        environment=world.local_id,
        target="webc",
        result="failure",
        detail=json.dumps({"rule_id": rule.id, "rule_name": RULE_NAME, "reason": "budget"}),
    )
    login = event_row(
        "login_failed",
        2,
        category="authentication",
        actor="anonymous",
        user=None,
        kind="user",
        result="failure",
        detail=json.dumps({"username": HOSTILE_USER}),
    )
    environment_event = event_row(
        "environment_tested", 3, kind="environment", target=str(world.remote_id)
    )
    insert_events(app, [*containers, exhausted, login, environment_event])
    return Seeded(rule.id, run.id)


@contextmanager
def all_slots_taken() -> Iterator[None]:
    taken = [
        activity_log._EXPORT_SLOTS.acquire() for _ in range(activity_log.MAX_CONCURRENT_EXPORTS)
    ]
    try:
        yield
    finally:
        for slot in taken:
            slot.release()


async def refusals(client: AsyncClient, wanted: dict[str, str]) -> dict[str, tuple[Any, ...]]:
    """Request the log once per query and describe how each refusal looks."""
    answers: dict[str, tuple[Any, ...]] = {}
    for query, text in wanted.items():
        response = await client.get("/activity?" + query)
        answers[query] = (
            response.status_code,
            text in response.text,
            'class="panel panel--alert error-summary"' in response.text,
            "<table" in response.text,
            response.headers["cache-control"],
        )
    return answers


async def ids_for(client: AsyncClient, query: str) -> list[str]:
    """Follow the log's next links for a query and return every row id."""
    found: list[str] = []
    path = f"/activity?{query}&limit=200"
    while path:
        response = await client.get(path)
        assert response.status_code == OK, (query, response.status_code)
        found += ROW_LINK.findall(response.text)
        link = NEXT_LINK.search(response.text)
        path = unescape(link.group(1)) if link else ""
    return found


async def test_the_log_lists_newest_first_in_pages_and_shows_hostile_text_inert(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)

    first = await admin_client.get("/activity")
    link = NEXT_LINK.search(first.text)
    second = await admin_client.get(unescape(link.group(1)))  # type: ignore[union-attr]
    small = await admin_client.get(f"/activity?limit={SMALL_PAGE}")
    bad_size = await admin_client.get("/activity?limit=33")

    shown = ROW_LINK.findall(first.text)
    assert first.status_code == OK
    assert (first.headers["cache-control"], "HX-Request" in first.headers["vary"]) == (
        "no-store",
        True,
    )
    assert inline_code_in(first.text) == []
    assert "Retention is off" in first.text
    assert ACTIVITY_LINK in first.text
    assert len(shown) == FIRST_PAGE_ROWS
    assert len(ROW_LINK.findall(second.text)) == SECOND_PAGE_ROWS
    assert not set(shown) & set(ROW_LINK.findall(second.text))
    assert "First page" in second.text
    assert "U+202E" in first.text and "U+000A" in first.text
    assert "‮" not in first.text
    assert "&lt;b&gt;" in first.text
    assert "Download CSV" in first.text and "/activity/-/export" in first.text
    assert len(ROW_LINK.findall(small.text)) == SMALL_PAGE
    assert 'value="25" selected' in small.text
    assert "limit=25" in unescape(NEXT_LINK.search(small.text).group(1))  # type: ignore[union-attr]
    assert bad_size.status_code == UNPROCESSABLE and "Page size" in bad_size.text


async def test_each_filter_selects_what_it_names(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    seeded = await seed(app, world)
    expected = {
        "category=automation": 1,
        "category=authentication": 1,
        "actor_type=anonymous": 1,
        "actor_type=subsystem": 1,
        f"subsystem={HEALTH}": 1,
        "result=failure": 15 + 2,
        "action=container_stopped": 20,
        "username=admin-user": CONTAINER_EVENTS + 1,
        f"rule_id={seeded.rule_id}": 1,
        f"job_run_id={seeded.run_id}": 1,
        f"environment_id={world.local_id}": CONTAINER_EVENTS + 1,
        f"environment_id={world.remote_id}": 1,
        f"environment_id={world.local_id}&target_type=container&target_id=c5": 1,
        f"environment_id={world.local_id}&target_type=container": CONTAINER_EVENTS + 1,
        "from=2026-03-01T10:11&to=2026-03-01T10:21": 10,
        "from=2026-03-01T10:11:00&to=2026-03-01T10:21:00": 10,
    }

    found = {query: len(await ids_for(admin_client, query)) for query in expected}

    assert found == expected


async def test_a_filter_that_cannot_be_used_is_a_422_page_naming_the_field_with_no_table(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)
    wanted = {
        "category=zzz": "Category: choose",
        "actor_type=zzz": "Actor: choose",
        "result=zzz": "Result: choose",
        "subsystem=nope": "Subsystem: choose",
        "action=Bad Action": "Action: use lower",
        "target_type=a-b": "Target type",
        "rule_id=0": "Rule: enter",
        "rule_id=abc": "Rule: enter",
        f"environment_id={2**63}": "Environment: enter",
        "job_run_id=1.5": "Run: enter",
        "from=yesterday": "From: enter",
        "to=2026-13-01T00:00": "To: enter",
        "from=0001-01-01T00:00": RANGE_MESSAGE,
        "username=nobody": "no user has this name",
        "target_id=x": "needs a target type",
        "target_type=container": "needs a target id",
        "target_id=" + "x" * 300 + "&target_type=container": "Use at most 256",
        "cursor=!!!": "page link is not valid",
        "cursor=e30=": "page link is not valid",
    }

    answers = await refusals(admin_client, wanted)

    assert answers == dict.fromkeys(wanted, (UNPROCESSABLE, True, True, False, "no-store"))


async def test_a_field_with_an_error_is_marked_and_what_was_typed_is_kept(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)

    half_target = await admin_client.get("/activity?target_id=x")
    kept = await admin_client.get("/activity?username=nobody&category=user_action")
    unknown_environment = await admin_client.get("/activity?environment_id=999")

    assert 'aria-invalid="true"' in half_target.text
    assert 'id="field-target_type-error"' in half_target.text
    assert 'value="nobody"' in kept.text and 'value="user_action" selected' in kept.text
    assert unknown_environment.status_code == OK
    assert "Nothing matches these filters" in unknown_environment.text
    assert "Environment 999" in unknown_environment.text


@pytest.mark.parametrize(
    ("field", "value", "message", "absent"),
    [
        ("from", "0001-01-01T00:00", RANGE_MESSAGE, FORMAT_MESSAGE),
        ("to", "9999-12-31T00:00", RANGE_MESSAGE, FORMAT_MESSAGE),
        ("from", "1969-12-31T23:59", RANGE_MESSAGE, FORMAT_MESSAGE),
        ("from", "yesterday", FORMAT_MESSAGE, RANGE_MESSAGE),
    ],
)
async def test_a_time_outside_1970_to_9998_or_malformed_says_which_in_the_log_and_the_download(
    admin_client: AsyncClient, field: str, value: str, message: str, absent: str
) -> None:
    log = await admin_client.get("/activity", params={field: value})
    download = await admin_client.get("/activity/-/export", params={field: value})

    assert (log.status_code, download.status_code) == (UNPROCESSABLE, UNPROCESSABLE)
    assert message in log.text and message in download.text
    assert absent not in log.text


async def test_the_bounds_of_the_time_range_are_accepted(admin_client: AsyncClient) -> None:
    lower = await admin_client.get("/activity", params={"from": "1970-01-01T00:00"})
    upper = await admin_client.get("/activity", params={"to": "9998-12-31T23:59"})

    assert (lower.status_code, upper.status_code) == (OK, OK)


async def test_htmx_requests_get_fragments_and_boosted_or_restored_ones_get_the_page(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)

    results = await admin_client.get("/activity?category=automation", headers=HTMX)
    table = await admin_client.get(
        "/activity?limit=25", headers={**HTMX, "HX-Target": "activity-table"}
    )
    boosted = await admin_client.get("/activity", headers=BOOSTED)
    restored = await admin_client.get("/activity", headers=RESTORE)
    full = await admin_client.get("/activity")

    assert "<!doctype html>" not in results.text
    assert 'id="activity-results"' in results.text and 'id="activity-table"' in results.text
    assert "hx-swap-oob" not in results.text and 'id="activity-filters"' not in results.text
    assert "<!doctype html>" not in table.text
    assert 'id="activity-results"' not in table.text and 'id="activity-table"' in table.text
    assert "<!doctype html>" in boosted.text and "<!doctype html>" in restored.text
    assert full.text.count('id="activity-filters"') == 1 and "hx-swap-oob" not in full.text
    assert 'hx-target="#activity-results"' in full.text and 'method="get"' in full.text


async def test_an_htmx_error_swaps_the_form_with_its_field_errors_into_place(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)

    one = await admin_client.get("/activity?category=zzz", headers=HTMX)
    several = await admin_client.get(
        "/activity?username=nobody&rule_id=0&category=zzz", headers=HTMX
    )
    table_error = await admin_client.get(
        "/activity?cursor=!!", headers={**HTMX, "HX-Target": "activity-table"}
    )

    assert one.status_code == UNPROCESSABLE and "<!doctype html>" not in one.text
    assert 'id="activity-filters"' in one.text and 'hx-swap-oob="true"' in one.text
    assert re.search(r'<select[^>]*id="field-category"[^>]*aria-invalid="true"', one.text)
    assert 'id="activity-results"' in one.text and "field-category-error" in one.text
    assert several.text.count('class="field__error"') == 3
    assert " open" in several.text.split("<details", 1)[1].split(">", 1)[0]
    assert table_error.status_code == UNPROCESSABLE
    assert "<!doctype html>" not in table_error.text


async def test_only_an_administrator_reaches_the_log_and_the_navigation_link_follows(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    anonymous_client: AsyncClient,
    world: World,
) -> None:
    paths = ["/activity", "/activity?category=zzz", "/activity/1", "/activity/-/export"]
    paths += ["/activity/-/export?category=zzz", "/activity/abc"]

    standard = {path: (await standard_client.get(path)).status_code for path in paths}
    standard_htmx = {
        path: (await standard_client.get(path, headers=HTMX)).status_code for path in paths
    }
    anonymous = {
        path: (await anonymous_client.get(path)).status_code for path in paths[:1] + paths[2:4]
    }

    assert set(standard.values()) == {FORBIDDEN} == set(standard_htmx.values())
    assert set(anonymous.values()) <= {303, 401, 403}
    assert ACTIVITY_LINK not in (await standard_client.get("/")).text
    assert ACTIVITY_LINK in (await admin_client.get("/")).text


async def test_an_anonymous_events_detail_shows_what_was_tried_with_control_characters_visible(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)
    [anonymous] = await ids_for(admin_client, "actor_type=anonymous")
    [environment_event] = await ids_for(
        admin_client, f"target_type=environment&environment_id={world.remote_id}"
    )

    page = await admin_client.get(f"/activity/{anonymous}")
    own = await admin_client.get(f"/activity/{environment_event}")

    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert "U+202E" in page.text and "‮" not in page.text and "&lt;b&gt;" in page.text
    assert "Anonymous" in page.text and "Tried:" in page.text
    assert inline_code_in(page.text) == []
    assert own.status_code == OK


async def test_the_event_page_shows_the_request_id_in_monospace_and_a_dash_when_there_is_none(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)
    connection = app.state.database.connect()
    try:
        connection.execute("BEGIN")
        connection.execute(
            "UPDATE activity_event SET request_id = ? WHERE action = 'login_failed'", (REQUEST_ID,)
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    [with_id] = await ids_for(admin_client, "actor_type=anonymous")
    [without_id] = await ids_for(
        admin_client, f"target_type=environment&environment_id={world.remote_id}"
    )

    shown = await admin_client.get(f"/activity/{with_id}")
    absent = await admin_client.get(f"/activity/{without_id}")

    assert (
        f'<dt>Request</dt>\n    <dd class="mono"><a href="/activity?request_id={REQUEST_ID}">'
        f"{REQUEST_ID}</a></dd>"
    ) in shown.text
    assert '<dt>Request</dt>\n    <dd class="mono">&mdash;</dd>' in absent.text


async def tag_events(app: FastAPI, request_id: str, *where: str) -> None:
    """Stamp the request id on the events whose action is one of those named."""
    connection = app.state.database.connect()
    try:
        connection.execute("BEGIN")
        connection.execute(
            "UPDATE activity_event SET request_id = ? "
            f"WHERE action IN ({', '.join('?' * len(where))})",
            (request_id, *where),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()


async def test_the_log_filtered_by_a_request_id_lists_exactly_its_events_under_a_heading_and_note(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)
    await tag_events(app, SHARED_REQUEST, "login_failed", "container_stopped")
    query = f"request_id={SHARED_REQUEST}"

    shown = await admin_client.get(f"/activity?{query}")
    fragment = await admin_client.get(f"/activity?{query}", headers=HTMX)
    plain = await admin_client.get("/activity")
    exported = await admin_client.get(f"/activity/-/export?{query}&format=ndjson")

    listed = await ids_for(admin_client, query)
    records = [json.loads(line) for line in exported.text.split("\n")[:-1]]
    text = flat(shown.text)
    assert len(listed) == len(records) == CONTAINER_EVENTS // 3 + 1
    assert {record["request_id"] for record in records} == {SHARED_REQUEST}
    assert f"{REQUEST_HEADING} {SHARED_REQUEST}" in text and REQUEST_NOTE in text
    assert f'<input type="hidden" name="request_id" value="{SHARED_REQUEST}">' in shown.text
    assert f"{REQUEST_HEADING} {SHARED_REQUEST}" in flat(fragment.text)
    assert "<body" not in fragment.text
    assert REQUEST_HEADING not in plain.text and 'name="request_id"' not in plain.text
    assert f"request_id={SHARED_REQUEST}&amp;format=csv" in shown.text


async def test_an_id_outside_the_grammar_is_refused_with_a_field_message_on_page_and_download(
    admin_client: AsyncClient,
) -> None:
    ids = {"has a space": "a%20b", "too long": "x" * 65, "slash": "a%2Fb"}

    pages = {
        name: await admin_client.get(f"/activity?request_id={value}") for name, value in ids.items()
    }
    downloads = {
        name: await admin_client.get(f"/activity/-/export?request_id={value}")
        for name, value in ids.items()
    }

    assert {name: r.status_code for name, r in pages.items()} == dict.fromkeys(ids, UNPROCESSABLE)
    assert {name: r.status_code for name, r in downloads.items()} == dict.fromkeys(
        ids, UNPROCESSABLE
    )
    assert all(REQUEST_MESSAGE in r.text for r in [*pages.values(), *downloads.values()])
    assert all("<table" not in r.text and REQUEST_HEADING not in r.text for r in pages.values())
    assert all(
        "attachment" not in r.headers.get("content-disposition", "") for r in downloads.values()
    )


async def test_a_client_chosen_request_id_lists_its_events_and_never_claims_one_request(
    anonymous_client: AsyncClient, admin_client: AsyncClient
) -> None:
    await anonymous_client.get("/login")
    for _attempt in range(2):
        await anonymous_client.post(
            "/login",
            data=csrf_form(anonymous_client, username="nobody-here", password="not-the-password"),
            headers={"X-Request-Id": PLANTED_ID},
        )

    listed = await ids_for(admin_client, f"request_id={PLANTED_ID}")
    shown = await admin_client.get(f"/activity?request_id={PLANTED_ID}")
    event = await admin_client.get(f"/activity/{listed[0]}")

    assert len(listed) == 2
    assert "same request" not in flat(shown.text).lower()
    assert "same request" not in flat(event.text).lower()
    assert f"{REQUEST_HEADING} {PLANTED_ID}" in flat(shown.text)
    assert f'href="/activity?request_id={PLANTED_ID}"' in event.text


async def test_a_subsystem_events_detail_links_its_rule_and_run_until_the_rule_is_deleted(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    seeded = await seed(app, world)
    [subsystem_event] = await ids_for(admin_client, "actor_type=subsystem")
    linked = await admin_client.get(f"/activity/{subsystem_event}")

    async with job_services(app) as services:
        await services.rules.delete(ADMIN, seeded.rule_id, expected_name=RULE_NAME)
    unlinked = await admin_client.get(f"/activity/{subsystem_event}")

    assert (
        f'href="/automation/rules/{seeded.rule_id}?from=activity%3A-%3A{subsystem_event}"'
        in linked.text
    )
    assert f'href="/jobs/-/runs/{seeded.run_id}"' in linked.text
    assert "Nightly &lt;b&gt;health&lt;/b&gt;" in linked.text and "Deleted" not in linked.text
    assert 'href="/environments/' in linked.text
    assert "Deleted" in unlinked.text
    assert "/automation/rules/" not in unlinked.text and "/jobs/-/runs/" not in unlinked.text
    assert "Nightly &lt;b&gt;health&lt;/b&gt;" in unlinked.text


HEX_ONE, HEX_TWO, HEX_THREE, HEX_FOUR, HEX_FIVE = (digit * 64 for digit in "abcde")
SWARM_ID_ONE = "q" * 25
COMPOSE_NAME, SWARM_NAME, UNMANAGED_NAME, NAMESPACE_NAME = "blog", "Shop", "ghost", "mesh"
BEFORE_EVENTS = stamp(1000)
AFTER_EVENTS = stamp(-60)
Events = dict[str, tuple[str, str | None, int | None]]


def target_cell(html: str) -> tuple[str | None, bool]:
    """Return the link the event page gives its target id and whether it carries Deleted."""
    for facts in Page(OK, html).tags("dl"):
        cells = [child for child in facts.children if not isinstance(child, str)]
        for term, value in pairwise(cells):
            if term.tag == "dt" and term.text() == "Target id":
                anchors = [node.attrs["href"] for node in value.walk() if node.tag == "a"]
                return (anchors[0] if anchors else None, "Deleted" in value.text())
    raise AssertionError("the page has no target id")


def target_href(html: str) -> str | None:
    """Return the link the event page gives its target id, None when the id is plain text."""
    return target_cell(html)[0]


async def record_events(app: FastAPI, events: Events) -> dict[str, int]:
    """Record one event per target and return the event id by label."""
    insert_events(
        app,
        [
            event_row("container_restarted", index, kind=kind, target=target, environment=env)
            for index, (kind, target, env) in enumerate(events.values())
        ],
    )
    async with app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT id, target_type, target_id, environment_id FROM activity_event "
            "ORDER BY id DESC",
            (),
            limit=len(events),
        )
    stored = {(r["target_type"], r["target_id"], r["environment_id"]): r["id"] for r in rows}
    return {label: stored[event] for label, event in events.items()}


async def target_cells(
    client: AsyncClient, ids: Mapping[str, int]
) -> dict[str, tuple[str | None, bool]]:
    return {
        label: target_cell((await client.get(f"/activity/{event_id}")).text)
        for label, event_id in ids.items()
    }


async def link_targets(client: AsyncClient, app: FastAPI, events: Events) -> dict[str, str | None]:
    """Record one event per target, open each event page and return the target link by label."""
    cells = await target_cells(client, await record_events(app, events))
    return {label: href for label, (href, _) in cells.items()}


async def insert_row(app: FastAPI, sql: str, *params: str) -> int:
    async with app.state.database.session() as db:
        written = await db.execute(sql, params)
    row_id: int | None = written.last_row_id
    assert row_id is not None
    return row_id


async def plant_rows(app: FastAPI, name: str, created_at: str) -> dict[str, int]:
    """Store one row of each kind the event page checks by id, created at the given time."""
    return {
        "automation_rule": await insert_row(app, INSERT_RULE, name, created_at, created_at),
        "user": await insert_row(app, INSERT_USER, name, created_at),
        "team": await insert_row(app, INSERT_TEAM, name, created_at),
        "registry_credential": await insert_row(app, INSERT_REGISTRY, name, created_at, created_at),
        "object_storage_target": await insert_row(app, INSERT_TARGET, name, created_at, created_at),
    }


def plant_host(host: ContainerEngine) -> None:
    """Put one object of each kind the event page looks up on the daemon."""
    host.add(HEX_ONE, "web")
    host.add_network("net", 1, network_id=HEX_TWO)
    host.add_image("app:1", f"sha256:{HEX_THREE}")
    host.add_image(None, f"sha256:{HEX_FOUR}")
    host.add_volume("data_vol")
    host.add(HEX_FIVE, "ghost-web", labels={STACK_LABEL: UNMANAGED_NAME})


async def test_every_target_type_links_while_it_exists_and_shows_deleted_once_removed(
    admin_client: AsyncClient, world: World, app: FastAPI, swarm_host: SwarmEngine
) -> None:
    e = world.remote_id
    rows = await plant_rows(app, "before", BEFORE_EVENTS)
    plant_host(swarm_host)
    service_id = swarm_host.named_service("api")
    swarm_host.named_service("mesh_web", labels={SWARM_LABEL: NAMESPACE_NAME})
    async with app.state.database.session() as db:
        await seed_stack_rows(db, e, (COMPOSE_NAME, "compose"), (SWARM_NAME, "swarm"))
    events: Events = {
        **{kind: (kind, str(row_id), None) for kind, row_id in rows.items()},
        "container": ("container", HEX_ONE, e),
        "network": ("network", HEX_TWO, e),
        "image with prefix": ("image", f"sha256:{HEX_THREE}", e),
        "image without prefix": ("image", HEX_FOUR, e),
        "volume": ("volume", "data_vol", e),
        "service": ("service", service_id, e),
        "node": ("node", WORKER, e),
        "compose stack": ("stack", COMPOSE_NAME, e),
        "swarm stack": ("stack", SWARM_NAME, e),
        "swarm stack in another case": ("stack", SWARM_NAME.upper(), e),
        "stack with a container": ("stack", UNMANAGED_NAME, e),
        "stack with a service": ("stack", NAMESPACE_NAME, e),
    }
    ids = await record_events(app, events)

    existing = await target_cells(admin_client, ids)
    async with app.state.database.session() as db:
        for table in ("automation_rule", "user", "team", "registry_credential"):
            await db.execute(f"DELETE FROM {table} WHERE id = ?", (rows[table],))
        await db.execute(
            "DELETE FROM object_storage_target WHERE id = ?", (rows["object_storage_target"],)
        )
        await db.execute("DELETE FROM stack WHERE environment_id = ?", (e,))
    swarm_host.boxes.clear()
    swarm_host.networks.pop(HEX_TWO)
    swarm_host.remove_image(f"sha256:{HEX_THREE}")
    swarm_host.remove_image(f"sha256:{HEX_FOUR}")
    swarm_host.volumes.pop("data_vol")
    swarm_host.services.clear()
    swarm_host.nodes.pop(WORKER)
    removed = await target_cells(admin_client, ids)

    base = f"/environments/{e}"
    paths = {
        "automation_rule": f"/automation/rules/{rows['automation_rule']}",
        "user": f"/users/{rows['user']}",
        "team": f"/teams/{rows['team']}",
        "registry_credential": f"/registries/{rows['registry_credential']}",
        "object_storage_target": f"/object-storage/{rows['object_storage_target']}",
        "container": f"{base}/containers/{HEX_ONE}",
        "network": f"{base}/networks/{HEX_TWO}",
        "image with prefix": f"{base}/images/{HEX_THREE}",
        "image without prefix": f"{base}/images/{HEX_FOUR}",
        "volume": f"{base}/volumes/data_vol",
        "service": f"{base}/swarm/services/{service_id}",
        "node": f"{base}/swarm/nodes/{WORKER}",
        "compose stack": f"{base}/stacks/{COMPOSE_NAME}",
        "swarm stack": f"{base}/swarm/stacks/{SWARM_NAME}",
        "swarm stack in another case": f"{base}/swarm/stacks/{SWARM_NAME}",
        "stack with a container": f"{base}/stacks/{UNMANAGED_NAME}",
        "stack with a service": f"{base}/stacks/{NAMESPACE_NAME}",
    }
    without_origin = {"registry_credential"}
    assert {label: href for label, (href, _) in existing.items()} == {
        label: path if label in without_origin else f"{path}?from=activity%3A-%3A{ids[label]}"
        for label, path in paths.items()
    }
    assert not any(marked for _, marked in existing.values())
    assert removed == dict.fromkeys(events, (None, True))


async def test_the_log_links_targets_with_the_log_as_origin_and_a_stack_only_once_managed(
    admin_client: AsyncClient, world: World, app: FastAPI, container_host: ContainerEngine
) -> None:
    e = world.remote_id
    async with app.state.database.session() as db:
        await seed_stack_rows(db, e, (COMPOSE_NAME, "compose"))
    await record_events(
        app,
        {
            "container": ("container", HEX_ONE, e),
            "compose stack": ("stack", COMPOSE_NAME, e),
            "unmanaged stack": ("stack", UNMANAGED_NAME, e),
            "config": ("config", SWARM_ID_ONE, e),
            "secret": ("secret", "r" * 25, e),
            "channel": ("notification_channel", "7", None),
        },
    )

    page = parse((await admin_client.get("/activity")).text)
    linked = {node.text(): node.attrs["href"] for node in page.walk() if node.tag == "a"}

    origin = "from=list%3A-%3Aactivity"
    assert linked[HEX_ONE] == f"/environments/{e}/containers/{HEX_ONE}?{origin}"
    assert linked[COMPOSE_NAME] == f"/environments/{e}/stacks/{COMPOSE_NAME}?{origin}"
    assert UNMANAGED_NAME not in linked
    assert linked[SWARM_ID_ONE] == f"/environments/{e}/swarm/configs/{SWARM_ID_ONE}?{origin}"
    assert linked["r" * 25] == f"/environments/{e}/swarm/secrets/{'r' * 25}?{origin}"
    assert linked["7"] == f"/notifications/7?{origin}"


async def test_a_reused_row_id_created_after_the_event_shows_deleted(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    older = await plant_rows(app, "older", BEFORE_EVENTS)
    newer = await plant_rows(app, "newer", AFTER_EVENTS)
    events: Events = {
        **{f"{kind} before": (kind, str(row_id), None) for kind, row_id in older.items()},
        **{f"{kind} after": (kind, str(row_id), None) for kind, row_id in newer.items()},
    }

    cells = await target_cells(admin_client, await record_events(app, events))

    assert {label: marked for label, (_, marked) in cells.items()} == {
        **{f"{kind} before": False for kind in older},
        "automation_rule after": True,
        "user after": True,
        "team after": True,
        "object_storage_target after": True,
        "registry_credential after": False,
    }
    assert all((href is None) == marked for href, marked in cells.values())


async def test_an_unreachable_environment_or_a_standalone_host_makes_no_host_read(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    events: Events = {
        "container on an unreachable host": ("container", HEX_ONE, world.down_id),
        "volume on an unreachable host": ("volume", "data_vol", world.down_id),
        "stack on an unreachable host": ("stack", UNMANAGED_NAME, world.down_id),
        "service on a standalone host": ("service", SWARM_ID_ONE, world.local_id),
        "node on a standalone host": ("node", WORKER, world.local_id),
    }

    cells = await target_cells(admin_client, await record_events(app, events))

    assert all(href is not None and not marked for href, marked in cells.values())
    assert world.engine_clients == []


async def test_a_host_read_past_the_budget_leaves_the_link_unmarked_and_reads_nothing_more(
    admin_client: AsyncClient,
    world: World,
    app: FastAPI,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets: list[asyncio.Timeout] = []
    real_timeout = asyncio.timeout

    def recording(delay: float | None) -> asyncio.Timeout:
        budget = real_timeout(delay)
        if delay == activity_targets.HOST_BUDGET_SECONDS:
            budgets.append(budget)
        return budget

    def spend_budget() -> None:
        [budget] = budgets
        budget.reschedule(asyncio.get_running_loop().time())

    monkeypatch.setattr(asyncio, "timeout", recording)
    container_host.on("inspect", spend_budget)
    container_host.hold("inspect")
    container_host.add(HEX_ONE, "web")
    ids = await record_events(app, {"container": ("container", HEX_ONE, world.remote_id)})

    cells = await target_cells(admin_client, ids)

    assert cells == {
        "container": (
            f"/environments/{world.remote_id}/containers/{HEX_ONE}"
            f"?from=activity%3A-%3A{ids['container']}",
            False,
        )
    }
    reads = [
        path for path in container_host.paths() if "/_ping" not in path and "/version" not in path
    ]
    assert reads == [f"GET /containers/{HEX_ONE}/json"]


INSERT_BACKUP_RECORD = (
    "INSERT INTO backup_record (origin, created_at, key_version, key_fingerprint, outcome) "
    "VALUES ('download', ?, 1, 'fingerprint', 'success')"
)


async def test_config_secret_and_channel_targets_link_while_present_and_show_deleted_once_removed(
    admin_client: AsyncClient, world: World, app: FastAPI, swarm_host: SwarmEngine
) -> None:
    e = world.remote_id
    config_id = swarm_host.add_config("app-config")
    secret_id = swarm_host.add_secret("db-password")
    channel_id = await insert_row(app, INSERT_CHANNEL, "alerts", BEFORE_EVENTS, BEFORE_EVENTS)
    events: Events = {
        "config": ("config", config_id, e),
        "secret": ("secret", secret_id, e),
        "notification_channel": ("notification_channel", str(channel_id), None),
    }
    ids = await record_events(app, events)

    existing = await target_cells(admin_client, ids)
    swarm_host.configs.clear()
    swarm_host.secrets.clear()
    async with app.state.database.session() as db:
        await db.execute("DELETE FROM notification_channel WHERE id = ?", (channel_id,))
    removed = await target_cells(admin_client, ids)

    base = f"/environments/{e}/swarm"
    assert existing == {
        "config": (f"{base}/configs/{config_id}?from=activity%3A-%3A{ids['config']}", False),
        "secret": (f"{base}/secrets/{secret_id}?from=activity%3A-%3A{ids['secret']}", False),
        "notification_channel": (f"/notifications/{channel_id}", False),
    }
    assert removed == dict.fromkeys(events, (None, True))


async def test_a_config_or_secret_on_an_unreachable_or_standalone_environment_is_linked_unmarked(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    events: Events = {
        "config on an unreachable host": ("config", SWARM_ID_ONE, world.down_id),
        "secret on an unreachable host": ("secret", SWARM_ID_ONE, world.down_id),
        "config on a standalone host": ("config", SWARM_ID_ONE, world.local_id),
        "secret on a standalone host": ("secret", SWARM_ID_ONE, world.local_id),
    }

    cells = await target_cells(admin_client, await record_events(app, events))

    assert all(href is not None and not marked for href, marked in cells.values())
    assert world.engine_clients == []


async def test_a_config_id_its_page_cannot_take_is_plain_text_and_unmarked(
    admin_client: AsyncClient, world: World, app: FastAPI, swarm_host: SwarmEngine
) -> None:
    e = world.remote_id
    events: Events = {
        "config id too short": ("config", "q" * 24, e),
        "secret id with capitals": ("secret", "Q" * 25, e),
        "channel id not a number": ("notification_channel", "abc", None),
    }

    cells = await target_cells(admin_client, await record_events(app, events))

    assert cells == dict.fromkeys(events, (None, False))


async def test_a_backup_target_is_marked_deleted_once_its_record_has_left_the_backups_list(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    kept = await insert_row(app, INSERT_BACKUP_RECORD, BEFORE_EVENTS)
    gone = await insert_row(app, INSERT_BACKUP_RECORD, BEFORE_EVENTS)
    reused = await insert_row(app, INSERT_BACKUP_RECORD, AFTER_EVENTS)
    events: Events = {
        "present": ("backup", str(kept), None),
        "removed": ("backup", str(gone), None),
        "created after the event": ("backup", str(reused), None),
        "row from before ids were recorded": ("backup", None, None),
    }
    ids = await record_events(app, events)
    async with app.state.database.session() as db:
        await db.execute("DELETE FROM backup_record WHERE id = ?", (gone,))

    cells = await target_cells(admin_client, ids)

    assert cells == {
        "present": (None, False),
        "removed": (None, True),
        "created after the event": (None, True),
        "row from before ids were recorded": (None, False),
    }


async def test_a_target_with_no_page_or_an_id_its_page_cannot_take_is_plain_text(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    e = world.remote_id
    events = {
        "type with no page": ("swarm_config", SWARM_ID_ONE, e),
        "environment type": ("environment", str(e), None),
        "scheduled job": ("scheduled_job", "1", None),
        "container id too short": ("container", "c5", e),
        "container without an environment": ("container", HEX_ONE, None),
        "image id too short": ("image", "sha256:abc", e),
        "user id not a number": ("user", "abc", None),
        "user id zero": ("user", "0", None),
        "volume name with a slash": ("volume", "a/b", e),
        "stack name with a space": ("stack", "bad name", e),
        "service id too short": ("service", "q" * 24, e),
        "no target": ("container", None, e),
    }

    links = await link_targets(admin_client, app, events)

    assert links == dict.fromkeys(events)


NOT_STORED_IDS = ["0", "abc", "99999999999999999999", str(2**63), "-1", "1.5"]


async def test_an_event_id_that_is_not_a_stored_id_is_not_found(
    admin_client: AsyncClient,
) -> None:
    answers = {
        event_id: (await admin_client.get(f"/activity/{event_id}")).status_code
        for event_id in NOT_STORED_IDS
    }

    assert answers == dict.fromkeys(NOT_STORED_IDS, NOT_FOUND)


async def test_the_download_is_a_csv_that_reads_back_and_neutralises_a_hostile_cell(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)

    anonymous = await admin_client.get("/activity/-/export?actor_type=anonymous")
    everything = await admin_client.get("/activity/-/export")
    one = await admin_client.get(
        f"/activity/-/export?environment_id={world.local_id}&target_type=container&target_id=c5"
    )

    rows = list(csv.reader(io.StringIO(anonymous.text, newline="")))
    header, [first] = rows[0], rows[1:]
    row = dict(zip(header, first, strict=True))
    assert anonymous.headers["content-type"] == "text/csv; charset=utf-8"
    assert anonymous.headers["cache-control"] == "no-store"
    assert re.fullmatch(
        r'attachment; filename="activity-\d{8}T\d{6}Z\.csv"',
        anonymous.headers["content-disposition"],
    )
    assert header[:3] == ["id", "occurred_at", "category"]
    assert row["action"] == "login_failed"
    assert '=x,\\"y\\"\\nz\\u202e<b>' in row["detail"]
    assert len(list(csv.reader(io.StringIO(everything.text, newline="")))) == 1 + 63
    assert len(list(csv.reader(io.StringIO(one.text, newline="")))) == 2


async def test_the_ndjson_download_is_typed_records_and_csv_stays_the_default(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)

    ndjson = await admin_client.get("/activity/-/export?format=ndjson&actor_type=anonymous")
    everything = await admin_client.get("/activity/-/export?format=ndjson")
    named_csv = await admin_client.get("/activity/-/export?format=csv&actor_type=anonymous")
    default_csv = await admin_client.get("/activity/-/export?actor_type=anonymous")
    unknown = await admin_client.get("/activity/-/export?format=xml")

    [record] = [json.loads(line) for line in ndjson.text.split("\n")[:-1]]
    assert ndjson.headers["content-type"] == "application/x-ndjson"
    assert ndjson.headers["cache-control"] == "no-store"
    assert re.fullmatch(
        r'attachment; filename="activity-\d{8}T\d{6}Z\.ndjson"',
        ndjson.headers["content-disposition"],
    )
    assert record["schema_version"] == 2
    assert list(record)[:3] == ["schema_version", "id", "occurred_at"]
    assert record["action"] == "login_failed" and record["actor_user_id"] is None
    assert record["detail"]["username"] == HOSTILE_USER
    assert len(everything.text.split("\n")) == 63 + 1
    assert named_csv.text == default_csv.text
    assert default_csv.headers["content-type"] == "text/csv; charset=utf-8"
    assert unknown.status_code == BAD_REQUEST
    assert "Format: choose csv or ndjson." in unknown.text
    assert "attachment" not in unknown.headers.get("content-disposition", "")


async def test_a_download_with_unusable_filters_is_the_log_page_not_a_file(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    bad = await admin_client.get("/activity/-/export?category=zzz")
    denied = await standard_client.get("/activity/-/export?result=failure")

    assert bad.status_code == UNPROCESSABLE and "text/html" in bad.headers["content-type"]
    assert "attachment" not in bad.headers.get("content-disposition", "")
    assert "Category: choose" in bad.text
    assert denied.status_code == FORBIDDEN


async def test_downloads_beyond_the_cap_are_refused_with_a_retry_hint_until_one_ends(
    admin_client: AsyncClient, world: World, app: FastAPI
) -> None:
    await seed(app, world)
    with all_slots_taken():
        busy = await admin_client.get("/activity/-/export")
    free = await admin_client.get("/activity/-/export?limit=25")

    assert busy.status_code == TOO_MANY and busy.headers["retry-after"] == "5"
    assert "Too many exports" in busy.text and "Back to the log" in busy.text
    assert busy.headers["cache-control"] == "no-store"
    assert free.status_code == OK
    assert activity_log._EXPORT_SLOTS._taken == 0


@pytest.mark.parametrize(
    ("fails_after", "disconnects"),
    [(None, True), (0, False), (2, False), (None, False)],
    ids=["client-gone-at-once", "send-fails-at-start", "send-fails-mid-body", "completes"],
)
async def test_the_download_gives_back_its_slot_on_every_way_out(
    world: World, app: FastAPI, fails_after: int | None, disconnects: bool
) -> None:
    await seed(app, world)
    stack = AsyncExitStack()
    service = await stack.enter_async_context(app_activity_log_service(app))
    response = _ExportResponse(service.export(ADMIN, ActivityFilter()), stack, "activity.csv")
    sent = 0

    async def receive() -> dict[str, Any]:
        if disconnects:
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()
        return {"type": "http.request"}

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal sent
        sent += 1
        if fails_after is not None and sent > fails_after:
            raise OSError("client gone")

    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.3"}}
    with suppress(OSError):
        await response(scope, receive, send)
    del response, service
    gc.collect()

    assert activity_log._EXPORT_SLOTS._taken == 0


async def test_a_container_page_shows_its_own_recent_events_to_an_administrator_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: Any,
    app: FastAPI,
) -> None:
    insert_events(
        app,
        [
            event_row("container_restarted", 1, environment=world.remote_id, target=WEB),
            event_row("container_stopped", 2, environment=world.remote_id, target="x" * 64),
        ],
    )
    path = f"/environments/{world.remote_id}/containers/{WEB}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(f"{path}?tab=overview", headers=HTMX)
    other_tab = await admin_client.get(f"{path}?tab=labels", headers=HTMX)
    boosted = await admin_client.get(path, headers=BOOSTED)
    standard = await standard_client.get(path)

    assert "Recent activity" in page.text and "container_restarted" in page.text
    assert "container_stopped" not in page.text
    assert (
        f"/activity?environment_id={world.remote_id}&amp;target_type=container&amp;target_id={WEB}"
        in page.text
    )
    assert "All activity" in page.text
    assert "Recent activity" in fragment.text and "container_restarted" in fragment.text
    assert "<html" not in fragment.text
    assert "Recent activity" not in other_tab.text
    assert "Recent activity" in boosted.text
    assert "Recent activity" not in standard.text


async def test_an_environment_page_shows_its_own_events_and_an_empty_one_says_so(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    app: FastAPI,
    container_host: Any,
) -> None:
    await seed(app, world)
    insert_events(
        app, [event_row("container_restarted", 1, environment=world.remote_id, target=WEB)]
    )

    own = await admin_client.get(f"/environments/{world.remote_id}")
    empty = await admin_client.get(f"/environments/{world.down_id}")
    standard = await standard_client.get(f"/environments/{world.local_id}")

    assert "Recent activity" in own.text and "environment_tested" in own.text
    assert "container_restarted" in own.text
    assert f'/activity?environment_id={world.remote_id}"' in own.text
    assert "Recent activity" in empty.text and "Nothing has been recorded" in empty.text
    assert standard.status_code == OK and "Recent activity" not in standard.text


async def test_a_compose_stack_page_shows_its_events_and_links_to_the_filtered_log(
    admin_client: AsyncClient,
    world: World,
    stack_host: Any,
    stopped_stack_jobs: Any,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    path = f"{stacks_path(world)}/{COMPOSE_STACK}"
    link = f"/activity?environment_id={world.remote_id}&amp;target_type=stack"

    page = await admin_client.get(path)
    fragment = await admin_client.get(f"{path}?tab=variables", headers=HTMX)
    log = await admin_client.get(
        f"/activity?environment_id={world.remote_id}&target_type=stack&target_id={COMPOSE_STACK}"
    )

    assert "Recent activity" in page.text and "stack_created" in page.text
    assert f"{link}&amp;target_id={COMPOSE_STACK}" in page.text
    assert "Recent activity" not in fragment.text
    assert log.status_code == OK and "stack_created" in log.text


async def test_a_swarm_stack_page_shows_its_events_to_an_administrator(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: Any
) -> None:
    world = swarm_stacks_world
    await create_shop(Browser(admin_client), world)
    path = f"{base(world)}/{SWARM_STACK}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(f"{path}?tab=variables", headers=HTMX)
    boosted = await admin_client.get(path, headers=BOOSTED)

    assert "Recent activity" in page.text and "stack_created" in page.text
    assert "Recent activity" not in fragment.text
    assert "Recent activity" in boosted.text


async def test_a_run_and_a_rule_link_to_the_events_recorded_under_them_for_an_administrator(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, app: FastAPI
) -> None:
    seeded = await seed(app, world)

    run = await admin_client.get(f"/jobs/-/runs/{seeded.run_id}")
    rule = await admin_client.get(f"/automation/rules/{seeded.rule_id}")
    standard_rule = await standard_client.get(f"/automation/rules/{seeded.rule_id}")

    assert f'href="/activity?job_run_id={seeded.run_id}"' in run.text
    assert f'href="/activity?rule_id={seeded.rule_id}"' in rule.text
    assert "/activity" not in standard_rule.text.replace('href="/activity/', "")
