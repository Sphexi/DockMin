"""Template catalogues: administration, the one fetch on enable and refresh, and the rows left."""

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.engine import Database
from app.netpolicy import REFUSAL_TEXT
from app.services.activity import ActivityRecorder
from app.services.template_catalogues import (
    LINUXSERVER_PRESET,
    MAX_CATALOGUES,
    CatalogueInput,
    RefreshResult,
    TemplateCatalogueService,
)
from app.services.template_fetch import FAILURE_TEXT, CatalogueFetchError
from tests.job_support import ADMIN, STANDARD
from tests.scheduler.support import Clock
from tests.template_support import (
    PORTAINER_URL,
    ScriptedFetcher,
    container,
    fixture_text,
    portainer_document,
)

REMOTE_TEXT = "remote-body-sentinel-text"
STEP = 5
MISSING_ID = 999
NAME_TAKEN = "A catalogue with this name already exists."
SENTINEL_CONTENT = '{"format":1,"templates":[]}'


def form(**changes: str) -> CatalogueInput:
    values = {"name": "mine", "kind": "portainer", "url": PORTAINER_URL} | changes
    return CatalogueInput(**values)


def two_templates() -> bytes:
    return portainer_document(container("One", id=1), container("Two", id=2), {"type": 2})


class Rig:
    """The service over a throwaway database, one session per call as a request would open."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.fetcher = ScriptedFetcher(two_templates())

    async def call(self, name: str, *arguments: Any, **options: Any) -> Any:
        async with self.database.session() as session:
            service = TemplateCatalogueService(
                session,
                self.clock,
                ActivityRecorder(session, self.clock),
                fetcher=self.fetcher,
            )
            return await getattr(service, name)(*arguments, **options)

    async def outcome(self, name: str, *arguments: Any, **options: Any) -> Any:
        try:
            await self.call(name, *arguments, **options)
        except ValidationFailed as refused:
            return refused.fields
        except PermissionDenied:
            return "denied"
        except ResourceNotFound:
            return "notfound"
        except CatalogueFetchError as failed:
            return failed.code
        return "ok"

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        connection = self.database.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def catalogue(self, catalogue_id: int = 1) -> dict[str, Any]:
        [row] = self.rows("SELECT * FROM template_catalogue WHERE id = ?", (catalogue_id,))
        return dict(row)

    def events(self, action: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else {}}
            for row in self.rows(
                "SELECT result, target_id, detail FROM activity_event WHERE action = ? ORDER BY id",
                (action,),
            )
        ]

    async def added(self, **changes: str) -> int:
        self.clock.jump(STEP)
        found = await self.call("create", ADMIN, form(**changes))
        assert isinstance(found, int)
        return found

    async def enabled(self) -> int:
        catalogue_id = await self.added()
        await self.call("enable", ADMIN, catalogue_id)
        return catalogue_id


@pytest.fixture
async def rig(provisioned_template: Path, tmp_path: Path) -> AsyncIterator[Rig]:
    path = tmp_path / "dockmin.db"
    shutil.copyfile(provisioned_template, path)
    database = Database(path)
    yield Rig(database)
    await database.aclose()


async def test_adding_a_catalogue_stores_it_disabled_fetches_nothing_and_audits_the_host_only(
    rig: Rig,
) -> None:
    await rig.call("create", ADMIN, form(url=PORTAINER_URL))

    stored = rig.catalogue()
    [event] = rig.events("template_catalogue_created")
    assert (stored["is_enabled"], stored["content"], stored["fetched_at"]) == (0, None, None)
    assert rig.fetcher.urls == []
    assert event["detail"]["url_host"] == "catalogue.example.test"
    assert "templates.json" not in json.dumps(event["detail"])


async def test_the_linuxserver_preset_is_a_valid_catalogue_input(rig: Rig) -> None:
    preset = LINUXSERVER_PRESET

    catalogue_id = await rig.call(
        "create", ADMIN, CatalogueInput(preset.name, preset.kind, preset.url)
    )

    assert rig.catalogue(catalogue_id)["url"].endswith("include_deprecated=false")
    assert rig.catalogue(catalogue_id)["kind"] == "linuxserver"


@pytest.mark.parametrize(
    ("changes", "field", "text"),
    [
        pytest.param({"url": "http://catalogue.example.test/x"}, "url", "https"),
        pytest.param({"url": "https://u:p@catalogue.example.test/x"}, "url", "user name"),
        pytest.param({"url": "https://catalogue.example.test/x#frag"}, "url", "fragment"),
        pytest.param({"url": f"{PORTAINER_URL}?token=abc"}, "url", "query string"),
        pytest.param({"url": "https://" + "a" * 2100}, "url", "2,048"),
        pytest.param({"url": "https://127.0.0.1/x"}, "url", REFUSAL_TEXT),
        pytest.param({"kind": "unraid"}, "kind", "Choose"),
        pytest.param({"name": " "}, "name", "Enter a name"),
        pytest.param({"name": "n" * 65}, "name", "at most 64"),
        pytest.param({"name": "a‮b"}, "name", "control"),
    ],
)
async def test_a_catalogue_that_breaks_a_rule_is_refused_with_the_field_and_stores_nothing(
    rig: Rig, changes: dict[str, str], field: str, text: str
) -> None:
    refused = await rig.outcome("create", ADMIN, form(**changes))

    assert isinstance(refused, dict) and text in refused[field]
    assert rig.rows("SELECT 1 FROM template_catalogue") == []


async def test_a_second_catalogue_with_the_same_name_in_any_case_is_refused(rig: Rig) -> None:
    await rig.added()

    assert await rig.outcome("create", ADMIN, form(name="MINE")) == {"name": NAME_TAKEN}


async def test_no_more_catalogues_than_the_limit_can_be_added(rig: Rig) -> None:
    for number in range(MAX_CATALOGUES):
        await rig.call("create", ADMIN, form(name=f"c{number}"))

    refused = await rig.outcome("create", ADMIN, form(name="one-too-many"))

    assert isinstance(refused, dict) and "At most" in refused["form"]


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        pytest.param("page", (), id="page"),
        pytest.param("get", (1,), id="get"),
        pytest.param("create", (form(),), id="create"),
        pytest.param("enable", (1,), id="enable"),
        pytest.param("refresh", (1,), id="refresh"),
        pytest.param("disable", (1,), id="disable"),
        pytest.param("delete_preview", (1,), id="delete-preview"),
    ],
)
async def test_a_standard_user_is_denied_every_operation_and_nothing_is_fetched(
    rig: Rig, method: str, arguments: tuple[Any, ...]
) -> None:
    await rig.added()

    assert await rig.outcome(method, STANDARD, *arguments) == "denied"
    assert rig.fetcher.urls == []


async def test_enabling_fetches_once_stores_the_templates_and_writes_one_refresh_row(
    rig: Rig,
) -> None:
    catalogue_id = await rig.added()

    result = await rig.call("enable", ADMIN, catalogue_id)

    stored = rig.catalogue()
    [event] = rig.events("template_catalogue_refreshed")
    assert isinstance(result, RefreshResult)
    assert (result.entry_count, result.skipped, result.content_changed) == (
        2,
        {"unsupported_stack_template": 1},
        True,
    )
    assert rig.fetcher.urls == [PORTAINER_URL]
    assert (stored["is_enabled"], stored["entry_count"], stored["last_failure"]) == (1, 2, None)
    assert stored["fetched_at"] and len(stored["content_digest"]) == 64
    assert event["result"] == "success" and event["target_id"] == str(catalogue_id)
    assert event["detail"]["entry_count"] == 2 and event["detail"]["content_changed"] is True
    assert event["detail"]["skipped"] == "unsupported_stack_template=1"
    assert event["detail"]["trigger"] == "enable"


async def test_refreshing_fetches_once_and_an_unchanged_body_writes_no_content(rig: Rig) -> None:
    catalogue_id = await rig.enabled()
    rig.rows("UPDATE template_catalogue SET content = ? WHERE id = ?", (SENTINEL_CONTENT, 1))
    rig.clock.jump(STEP)

    result = await rig.call("refresh", ADMIN, catalogue_id)

    events = rig.events("template_catalogue_refreshed")
    assert result.content_changed is False and result.entry_count == 2
    assert rig.fetcher.urls == [PORTAINER_URL, PORTAINER_URL]
    assert rig.catalogue()["content"] == SENTINEL_CONTENT
    assert [e["detail"]["trigger"] for e in events] == ["enable", "refresh"]
    assert events[1]["detail"]["content_changed"] is False
    assert rig.catalogue()["fetched_at"] != rig.catalogue()["created_at"]


async def test_a_changed_body_replaces_the_stored_templates(rig: Rig) -> None:
    catalogue_id = await rig.enabled()
    before = rig.catalogue()["content_digest"]
    rig.fetcher.outcomes = [portainer_document(container("Only", id=1))]
    rig.clock.jump(STEP)

    result = await rig.call("refresh", ADMIN, catalogue_id)

    stored = rig.catalogue()
    assert result.content_changed is True and stored["entry_count"] == 1
    assert stored["content_digest"] != before and "Only" in stored["content"]


async def test_refreshing_a_disabled_catalogue_and_enabling_an_enabled_one_fetch_nothing(
    rig: Rig,
) -> None:
    await rig.added()
    disabled = await rig.outcome("refresh", ADMIN, 1)
    await rig.call("enable", ADMIN, 1)
    fetched = len(rig.fetcher.urls)
    enabled = await rig.outcome("enable", ADMIN, 1)

    assert isinstance(disabled, dict) and "Enable the catalogue first" in disabled["catalogue"]
    assert isinstance(enabled, dict) and "already enabled" in enabled["catalogue"]
    assert fetched == 1 and len(rig.fetcher.urls) == 1


async def test_a_failed_enable_leaves_the_catalogue_disabled_and_records_a_code_not_remote_text(
    rig: Rig,
) -> None:
    rig.fetcher = ScriptedFetcher(CatalogueFetchError("status"))
    catalogue_id = await rig.added()

    code = await rig.outcome("enable", ADMIN, catalogue_id)

    stored = rig.catalogue()
    [event] = rig.events("template_catalogue_refreshed")
    assert code == "status"
    assert (stored["is_enabled"], stored["last_failure"], stored["content"]) == (0, "status", None)
    assert event["result"] == "failure" and event["detail"]["reason"] == "status"
    assert event["detail"]["message"] == FAILURE_TEXT["status"]
    assert REMOTE_TEXT not in json.dumps(event["detail"])


async def test_a_failed_refresh_keeps_the_stored_templates_and_a_later_success_clears_the_failure(
    rig: Rig,
) -> None:
    catalogue_id = await rig.enabled()
    kept = rig.catalogue()["content"]
    rig.fetcher.outcomes = [CatalogueFetchError("timeout")]

    failed = await rig.outcome("refresh", ADMIN, catalogue_id)
    after_failure = rig.catalogue()
    rig.fetcher.outcomes = [two_templates()]
    await rig.call("refresh", ADMIN, catalogue_id)

    assert failed == "timeout"
    assert (after_failure["last_failure"], after_failure["content"]) == ("timeout", kept)
    assert after_failure["is_enabled"] == 1
    assert rig.catalogue()["last_failure"] is None
    assert [e["result"] for e in rig.events("template_catalogue_refreshed")] == [
        "success",
        "failure",
        "success",
    ]


async def test_a_body_that_is_not_a_catalogue_is_a_product_failure_that_quotes_nothing(
    rig: Rig,
) -> None:
    rig.fetcher = ScriptedFetcher(REMOTE_TEXT.encode())
    catalogue_id = await rig.added()

    code = await rig.outcome("enable", ADMIN, catalogue_id)

    [event] = rig.events("template_catalogue_refreshed")
    assert code == "invalid_document" and rig.catalogue()["last_failure"] == "invalid_document"
    assert REMOTE_TEXT not in json.dumps(event["detail"])


async def test_a_linuxserver_document_is_normalised_on_enable_with_its_skips_counted(
    rig: Rig,
) -> None:
    rig.fetcher = ScriptedFetcher(fixture_text("linuxserver.json").encode())
    catalogue_id = await rig.added(kind="linuxserver")

    result = await rig.call("enable", ADMIN, catalogue_id)

    assert result.entry_count == 3 and result.skipped_total == 4


async def test_renaming_keeps_the_fetched_templates_and_a_new_address_discards_them(
    rig: Rig,
) -> None:
    catalogue_id = await rig.enabled()
    kept = rig.catalogue()["content"]

    rig.clock.jump(STEP)
    renamed = await rig.call(
        "update",
        ADMIN,
        catalogue_id,
        form(name="renamed"),
        expected_updated_at=rig.catalogue()["updated_at"],
    )
    after_rename = rig.catalogue()
    rig.clock.jump(STEP)
    moved = await rig.call(
        "update",
        ADMIN,
        catalogue_id,
        form(name="renamed", url="https://other.example.test/t.json"),
        expected_updated_at=after_rename["updated_at"],
    )

    after_move = rig.catalogue()
    assert renamed is False and (after_rename["is_enabled"], after_rename["content"]) == (1, kept)
    assert moved is True
    assert (after_move["is_enabled"], after_move["content"], after_move["entry_count"]) == (
        0,
        None,
        0,
    )
    assert (after_move["fetched_at"], after_move["last_failure"]) == (None, None)
    [rename_event, move_event] = rig.events("template_catalogue_updated")
    assert rename_event["detail"]["changed"] == "name"
    assert move_event["detail"]["content_discarded"] is True


async def test_saving_nothing_new_writes_no_row_and_a_stale_form_is_refused(rig: Rig) -> None:
    catalogue_id = await rig.added()
    stamp = rig.catalogue()["updated_at"]

    same = await rig.outcome("update", ADMIN, catalogue_id, form(), expected_updated_at=stamp)
    stale = await rig.outcome(
        "update", ADMIN, catalogue_id, form(name="x"), expected_updated_at="2001-01-01T00:00:00Z"
    )

    assert same == "ok"
    assert [e["result"] for e in rig.events("template_catalogue_updated")] == ["failure"]
    assert isinstance(stale, dict) and "Changed since" in stale["form"]


async def test_disabling_hides_nothing_but_keeps_the_fetched_templates(rig: Rig) -> None:
    catalogue_id = await rig.enabled()
    rig.clock.jump(STEP)

    await rig.call("disable", ADMIN, catalogue_id)

    stored = rig.catalogue()
    assert (stored["is_enabled"], stored["entry_count"]) == (0, 2) and stored["content"]
    assert rig.events("template_catalogue_updated")[0]["detail"]["changed"] == "is_enabled"
    assert len(rig.fetcher.urls) == 1


async def test_deleting_needs_the_reviewed_name_removes_the_row_and_is_audited(rig: Rig) -> None:
    catalogue_id = await rig.enabled()

    wrong = await rig.outcome("delete", ADMIN, catalogue_id, expected_name="other")
    gone = await rig.outcome("delete", ADMIN, catalogue_id, expected_name="mine")
    missing = await rig.outcome("delete", ADMIN, MISSING_ID, expected_name="mine")

    [refusal, event] = rig.events("template_catalogue_deleted")
    assert isinstance(wrong, dict) and "Changed since" in wrong["name"]
    assert (refusal["result"], event["result"]) == ("failure", "success")
    assert (gone, missing) == ("ok", "notfound")
    assert rig.rows("SELECT 1 FROM template_catalogue") == []
    assert event["detail"]["entry_count"] == 2


def test_no_scheduled_or_automated_code_reaches_the_catalogue_fetch() -> None:
    root = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        str(path.relative_to(root))
        for directory in ("scheduler", "subsystems")
        for path in (root / directory).rglob("*.py")
        if "template_" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
