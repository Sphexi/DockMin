"""The activity log service: administrators only, names resolved, panels, status and exports."""

import csv
import io
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import pytest

import app.services.activity_log as activity_log
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.engine import Session
from app.db.repositories.activity_event import ActivityFilter
from app.services.activity_log import (
    MAX_CONCURRENT_EXPORTS,
    ActivityLogService,
    ExportBusy,
    ExportStream,
)
from tests.access_support import ADMIN, ENV_A, ENV_A_NAME, ENV_B, STANDARD
from tests.auth_support import ADMIN_USERNAME

INSERT_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
    "target_type, target_id, action, result, occurred_at) "
    "VALUES ('user_action', 'user', ?, ?, ?, ?, ?, 'success', ?)"
)
INSERT_EXPORTER = (
    "INSERT INTO automation_rule (name, subsystem, target_selector, created_at, updated_at) "
    "VALUES ('export', 'activity-log-exporter', '', ?, ?)"
)
STAMP = "2030-01-01T10:00:00Z"
NOW = datetime(2030, 3, 1, 12, 0, tzinfo=UTC)
RETENTION_DAYS = 30
OLD = "2030-01-15T10:00:00Z"
YOUNG = "2030-02-25T10:00:00Z"
EXPORT_PAGE = 2
ROWS = 5
PANEL_LIMIT = 3

type Call = Callable[[ActivityLogService], Awaitable[object]]


async def page_call(service: ActivityLogService) -> object:
    return await service.page(STANDARD, ActivityFilter(), limit=10)


async def get_call(service: ActivityLogService) -> object:
    return await service.get(STANDARD, 1)


async def panel_call(service: ActivityLogService) -> object:
    return await service.for_object(STANDARD, "environment", ENV_A, "")


async def export_call(service: ActivityLogService) -> object:
    return service.export(STANDARD, ActivityFilter())


async def status_call(service: ActivityLogService) -> object:
    return await service.retention_status(STANDARD)


async def user_call(service: ActivityLogService) -> object:
    return await service.user_id_of(STANDARD, ADMIN_USERNAME)


def open_exports(service: ActivityLogService, count: int) -> list[ExportStream]:
    return [service.export(ADMIN, ActivityFilter()) for _ in range(count)]


def parsed(chunks: list[bytes]) -> list[list[str]]:
    return list(csv.reader(io.StringIO(b"".join(chunks).decode("utf-8"), newline="")))


async def add_event(
    db: Session,
    *,
    target: tuple[str, str] = ("container", "c1"),
    environment: int | None = ENV_A,
    action: str = "container_started",
    at: str = STAMP,
) -> None:
    await db.execute(INSERT_EVENT, (ADMIN.user_id, environment, *target, action, at))


async def add_stack_events(db: Session, names: tuple[str, ...]) -> None:
    for name in names:
        await add_event(db, target=("stack", name), action="stack_redeployed")


async def add_container_events(db: Session, count: int) -> None:
    for number in range(count):
        await add_event(db, target=("container", f"c{number}"), at=f"2030-01-01T10:00:0{number}Z")


@pytest.fixture
def service(db: Session) -> ActivityLogService:
    return ActivityLogService(db=db, clock=lambda: NOW, retention_days=RETENTION_DAYS)


@pytest.fixture
async def streams() -> AsyncIterator[list[ExportStream]]:
    """Hold the exports a test opens and close them after, so the process-wide cap stays free."""
    opened: list[ExportStream] = []
    yield opened
    for stream in opened:
        await stream.aclose()


@pytest.mark.parametrize(
    "call",
    [page_call, get_call, panel_call, export_call, status_call, user_call],
    ids=["page", "get", "for_object", "export", "retention_status", "user_id_of"],
)
async def test_every_method_refuses_a_standard_user(
    service: ActivityLogService, call: Call
) -> None:
    with pytest.raises(PermissionDenied):
        await call(service)


async def test_a_page_names_the_actor_and_the_environment_as_they_are_now(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db)
    await add_event(db, environment=None, target=("user", "1"))

    page = await service.page(ADMIN, ActivityFilter(), limit=10)

    assert [(view.actor_username, view.environment_name) for view in page.items] == [
        (ADMIN_USERNAME, None),
        (ADMIN_USERNAME, ENV_A_NAME),
    ]


async def test_one_event_is_found_by_id_and_a_missing_one_is_not_found(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db)

    view = await service.get(ADMIN, 1)

    assert (view.event.id, view.actor_username) == (1, ADMIN_USERNAME)
    with pytest.raises(ResourceNotFound):
        await service.get(ADMIN, 2)


async def test_a_subsystem_filter_naming_no_registered_subsystem_is_a_field_error(
    service: ActivityLogService,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await service.page(ADMIN, ActivityFilter(actor_subsystem="scheduler"), limit=10)

    assert set(refused.value.fields) == {"actor_subsystem"}


async def test_a_container_panel_lists_only_that_container_in_that_environment(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db, target=("container", "c1"))
    await add_event(db, target=("container", "c2"))
    await add_event(db, target=("container", "c1"), environment=ENV_B)

    views = await service.for_object(ADMIN, "container", ENV_A, "c1")

    assert [(view.event.target_id, view.event.environment_id) for view in views] == [("c1", ENV_A)]


async def test_an_environment_panel_includes_the_environments_own_events_and_ignores_the_target(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db, target=("container", "c1"))
    await add_event(db, environment=None, target=("environment", str(ENV_A)))
    await add_event(db, environment=None, target=("environment", str(ENV_B)))

    views = await service.for_object(ADMIN, "environment", ENV_A, "ignored")

    assert [view.event.target_id for view in views] == [str(ENV_A), "c1"]


async def test_a_stack_panel_matches_the_name_in_any_case_newest_first_up_to_the_limit(
    service: ActivityLogService, db: Session
) -> None:
    await add_stack_events(db, ("Web", "web", "api", "WEB", "WEB", "wEb"))
    await add_event(db, target=("container", "web"))
    await add_event(db, target=("stack", "WEB"), environment=ENV_B)

    views = await service.for_object(ADMIN, "stack", ENV_A, "wEb", limit=PANEL_LIMIT)
    nothing = await service.for_object(ADMIN, "stack", ENV_A, "missing")

    assert [view.event.target_id for view in views] == ["wEb", "WEB", "WEB"]
    assert nothing == []


async def test_the_status_reports_the_setting_and_no_hold_without_an_exporter(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db, at=OLD)

    status = await service.retention_status(ADMIN)

    assert (status.days, status.held_since, status.exporter_rules) == (RETENTION_DAYS, None, 0)


async def test_the_status_names_how_far_back_an_exporter_holds_events_past_the_cap(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db, at=OLD)
    await add_event(db, at=YOUNG)
    await db.execute(INSERT_EXPORTER, (STAMP, STAMP))

    status = await service.retention_status(ADMIN)

    assert (status.held_since, status.exporter_rules) == (OLD, 1)


async def test_the_status_reports_no_hold_when_everything_unexported_is_within_the_cap(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db, at=YOUNG)
    await db.execute(INSERT_EXPORTER, (STAMP, STAMP))

    status = await service.retention_status(ADMIN)

    assert status.held_since is None


async def test_the_status_reports_no_hold_when_retention_is_off(db: Session) -> None:
    await add_event(db, at=OLD)
    await db.execute(INSERT_EXPORTER, (STAMP, STAMP))

    status = await ActivityLogService(db=db, clock=lambda: NOW).retention_status(ADMIN)

    assert (status.days, status.held_since) == (None, None)


async def test_the_status_reports_no_hold_when_the_cursor_is_past_every_old_event(
    service: ActivityLogService, db: Session
) -> None:
    await add_event(db, at=OLD)
    rule = (await db.execute(INSERT_EXPORTER, (STAMP, STAMP))).last_row_id
    await db.execute(
        "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, 1)", (rule,)
    )

    status = await service.retention_status(ADMIN)

    assert status.held_since is None


async def test_the_user_id_of_a_name_is_found_or_none(service: ActivityLogService) -> None:
    assert await service.user_id_of(ADMIN, ADMIN_USERNAME) == ADMIN.user_id
    assert await service.user_id_of(ADMIN, "nobody") is None


async def test_an_export_streams_the_header_once_then_one_chunk_per_page_newest_first(
    service: ActivityLogService,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    streams: list[ExportStream],
) -> None:
    await add_container_events(db, ROWS)
    monkeypatch.setattr(activity_log, "EXPORT_PAGE_SIZE", EXPORT_PAGE)
    stream = service.export(ADMIN, ActivityFilter())
    streams.append(stream)

    chunks = [chunk async for chunk in stream]

    assert len(chunks) == 1 + 3
    rows = parsed(chunks)
    assert rows[0][0] == "id"
    assert [row[0] for row in rows[1:]] == ["5", "4", "3", "2", "1"]


async def test_an_export_of_a_filter_nothing_matches_is_the_header_alone(
    service: ActivityLogService, streams: list[ExportStream]
) -> None:
    stream = service.export(ADMIN, ActivityFilter(action="nothing_like_this"))
    streams.append(stream)

    chunks = [chunk async for chunk in stream]

    assert len(parsed(chunks)) == 1


async def test_an_export_with_an_unknown_subsystem_is_refused_before_it_takes_a_slot(
    service: ActivityLogService, streams: list[ExportStream]
) -> None:
    with pytest.raises(ValidationFailed):
        service.export(ADMIN, ActivityFilter(actor_subsystem="scheduler"))

    streams.extend(open_exports(service, MAX_CONCURRENT_EXPORTS))


async def test_no_more_than_the_cap_of_exports_run_at_once(
    service: ActivityLogService, streams: list[ExportStream]
) -> None:
    streams.extend(open_exports(service, MAX_CONCURRENT_EXPORTS))

    with pytest.raises(ExportBusy):
        service.export(ADMIN, ActivityFilter())


async def test_closing_an_unread_export_frees_its_slot_and_closing_twice_is_harmless(
    service: ActivityLogService, streams: list[ExportStream]
) -> None:
    held = open_exports(service, MAX_CONCURRENT_EXPORTS)
    streams.extend(held)

    await held[0].aclose()
    await held[0].aclose()
    streams.append(service.export(ADMIN, ActivityFilter()))

    with pytest.raises(ExportBusy):
        service.export(ADMIN, ActivityFilter())


async def test_reading_an_export_to_its_end_frees_its_slot(
    service: ActivityLogService, streams: list[ExportStream]
) -> None:
    finished = service.export(ADMIN, ActivityFilter())
    streams.append(finished)
    streams.extend(
        service.export(ADMIN, ActivityFilter()) for _ in range(MAX_CONCURRENT_EXPORTS - 1)
    )

    chunks = [chunk async for chunk in finished]

    assert chunks
    streams.append(service.export(ADMIN, ActivityFilter()))


async def test_an_export_whose_read_fails_frees_its_slot(
    service: ActivityLogService,
    monkeypatch: pytest.MonkeyPatch,
    streams: list[ExportStream],
) -> None:
    async def failing(*args: object, **kwargs: object) -> object:
        raise RuntimeError("read failed")

    monkeypatch.setattr(service._events, "page", failing)
    broken = service.export(ADMIN, ActivityFilter())
    streams.append(broken)

    with pytest.raises(RuntimeError, match="read failed"):
        async for _chunk in broken:
            pass

    streams.extend(open_exports(service, MAX_CONCURRENT_EXPORTS))
