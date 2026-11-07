"""The restore pages: access, upload, review, the key, cancel and one real restore."""

import asyncio
import base64
import re
import sqlite3
from dataclasses import dataclass, field
from functools import partial
from html import unescape
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

import app.main as main_module
from app.auth.principal import Principal
from app.db.engine import DatabaseClosed
from app.main import _restore_runtime, create_app, start_runtime, stop_runtime
from app.services.restore import RestoreCoordinator
from app.web.csrf import STREAMED_UPLOAD_ROUTES
from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_SESSION_TOKEN,
    ADMIN_USERNAME,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    Browsers,
    csrf_form,
)
from tests.conftest import serving_app
from tests.job_support import execute, rows
from tests.restore_support import (
    LIVE_ENVIRONMENT,
    real_archive,
    sha,
    target_settings,
    target_template,
)
from tests.support import VALID_KEY
from tests.web.flash_support import flash_texts, land, location
from tests.web.helpers import hidden_inputs, parse_tags

TOKEN = "C" * 43
HTMX = {"HX-Request": "true"}
OK = 200
SEE_OTHER = 303
BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
CONFLICT = 409
UNPROCESSABLE = 422
UNAVAILABLE = 503
CANCELLED_NOTICE = "The restore is cancelled. This instance accepts changes again."
RESUMED_NOTICE = "Automation is resumed. Scheduled jobs run again."
EMPTY_BANNER = re.compile(r'<div id="restore-banner">\s*</div>')
BOUNDARY = "xx-restore-boundary-xx"
JUNK_BYTES = 200_000
WRONG_KEY = base64.b64encode(bytes(range(100, 132))).decode()
OWNER = Principal(user_id=1, username="admin-user", role="administrator", session_id=1)
MEMBERS = 9
OUTSIDE_FILE_CAP = 70_000
POLLS = 3
MINIMUM_DATABASE_BYTES = 50_000
SETTLE_POLLS = 1200
SETTLE_PAUSE = 0.05

STANDARD_GETS = ["/restore", "/restore/review"]
STANDARD_POSTS = ["/restore/begin", "/restore/archive", "/restore/confirm", "/restore/cancel"]


@dataclass(frozen=True)
class Archive:
    """An archive made by a real instance, held in memory for the async tests."""

    data: bytes
    sha: str


@pytest.fixture(scope="module")
def archive(provisioned_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Archive:
    path = asyncio.run(real_archive(tmp_path_factory.mktemp("restore-pages"), provisioned_template))
    return Archive(path.read_bytes(), sha(path))


def restore_directory(app: FastAPI) -> Path:
    returned: Path = next((app.state.settings.data_dir / "restore").iterdir())
    return returned


def uploads_in(directory: Path) -> list[Path]:
    return list(directory.rglob("upload*"))


def multipart(parts: list[tuple[str, str | None, bytes]]) -> tuple[bytes, dict[str, str]]:
    body = b""
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        body += f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n".encode()
        if filename:
            body += b"Content-Type: application/gzip\r\n"
        body += b"\r\n" + data + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


def upload_of(
    data: bytes, *, token: str = TOKEN, file_first: bool = False
) -> tuple[bytes, dict[str, str]]:
    parts: list[tuple[str, str | None, bytes]] = [
        ("csrf_token", None, token.encode()),
        ("archive", "backup.tar.gz", data),
    ]
    return multipart(parts[::-1] if file_first else parts)


def text_of(response: Response | str) -> str:
    html = response if isinstance(response, str) else response.text
    return unescape(re.sub(r"<[^>]+>", " ", html))


async def engage(client: AsyncClient) -> Response:
    return await client.post("/restore/begin", data=csrf_form(client, confirm_offline="yes"))


async def upload(client: AsyncClient, archive_bytes: bytes) -> Response:
    body, headers = upload_of(archive_bytes)
    return await client.post("/restore/archive", content=body, headers=headers)


async def reviewed_hash(client: AsyncClient) -> str:
    return hidden_inputs((await client.get("/restore/review")).text)["archive_sha256"]


async def test_a_standard_user_is_refused_every_restore_page_and_action(
    standard_client: AsyncClient,
) -> None:
    gets = [(await standard_client.get(path)).status_code for path in STANDARD_GETS]
    posts = [
        (await standard_client.post(path, data=csrf_form(standard_client))).status_code
        for path in STANDARD_POSTS
    ]

    assert gets == [FORBIDDEN] * len(STANDARD_GETS)
    assert posts == [FORBIDDEN] * len(STANDARD_POSTS)


async def test_an_anonymous_visitor_is_sent_to_sign_in_before_any_body_is_read(
    anonymous_client: AsyncClient,
) -> None:
    page = await anonymous_client.get("/restore")
    upload_response = await anonymous_client.post(
        "/restore/archive",
        content=b"x",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )

    assert page.status_code in (302, 303, 307) and "/login" in page.headers["location"]
    assert upload_response.status_code in (303, 401)


async def test_the_idle_page_explains_the_restore_and_offers_to_begin_one(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/restore")
    review = await admin_client.get("/restore/review")
    unknown = await admin_client.get(f"/restore/progress/{'0' * 32}")
    malformed = await admin_client.get("/restore/progress/notanid")

    html = response.text
    tags = parse_tags(html)
    checkbox = [t for t in tags if t.attrs.get("name") == "confirm_offline"]
    assert response.status_code == OK and "How a restore runs" in html
    assert 'action="/restore/begin"' in html and checkbox[0].attrs["value"] == "yes"
    assert response.headers["cache-control"] == "no-store"
    assert EMPTY_BANNER.search(html) is not None
    assert (review.status_code, review.headers["location"]) == (SEE_OTHER, "/restore")
    assert (unknown.status_code, malformed.status_code) == (NOT_FOUND, NOT_FOUND)


async def test_beginning_needs_the_offline_confirmation_and_a_second_begin_is_a_conflict_page(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    gate = started_app.state.restore_gate

    unconfirmed = await admin_client.post("/restore/begin", data=csrf_form(admin_client))
    phase_unconfirmed = gate.phase
    begun = await engage(admin_client)
    second = await engage(admin_client)

    assert unconfirmed.status_code == UNPROCESSABLE
    assert "Confirm that this instance goes offline" in text_of(unconfirmed)
    assert phase_unconfirmed == "open"
    assert (begun.status_code, begun.headers["location"], gate.phase) == (
        SEE_OTHER,
        "/restore",
        "engaged",
    )
    assert second.status_code == CONFLICT and "already in progress" in second.text


async def test_the_banner_reaches_every_page_and_links_to_the_restore_for_administrators_only(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    await engage(admin_client)

    as_admin = await admin_client.get("/backups")
    as_standard = await standard_client.get("/")
    elsewhere = await admin_client.post("/object-storage", data=csrf_form(admin_client, name="x"))

    assert "restore-banner" in as_admin.text and "offline for a restore" in as_admin.text
    assert "Open the restore" in as_admin.text and 'href="/restore"' in as_admin.text
    assert "restore-banner" in as_standard.text and "Open the restore" not in as_standard.text
    assert elsewhere.status_code == UNAVAILABLE and "Offline for a restore" in elsewhere.text
    assert "style=" not in elsewhere.text


async def test_the_upload_form_puts_the_token_before_the_file_and_review_waits_for_an_archive(
    admin_client: AsyncClient,
) -> None:
    await engage(admin_client)

    response = await admin_client.get("/restore")
    review = await admin_client.get("/restore/review")

    html = response.text
    form_at = html.index('action="/restore/archive"')
    assert "Upload the archive" in html and 'enctype="multipart/form-data"' in html
    assert form_at < html.index('name="csrf_token"', form_at) < html.index('type="file"', form_at)
    assert 'action="/restore/cancel"' in html
    assert (review.status_code, review.headers["location"]) == (SEE_OTHER, "/restore")


async def test_an_upload_with_the_file_first_a_wrong_token_or_a_missing_part_is_refused(
    admin_client: AsyncClient, archive: Archive
) -> None:
    data = archive.data
    await engage(admin_client)
    token = TOKEN.encode()
    cases = {
        "file-first": upload_of(data, file_first=True),
        "wrong-token": upload_of(data, token="W" * 43),
        "extra-part": multipart(
            [("csrf_token", None, token), ("archive", "a.tar.gz", data), ("note", None, b"x")]
        ),
        "long-token": multipart([("csrf_token", None, b"T" * 500), ("archive", "a.tar.gz", data)]),
        "no-file": multipart([("csrf_token", None, token)]),
        "large-preamble": (b"x" * OUTSIDE_FILE_CAP + upload_of(data)[0], upload_of(data)[1]),
    }

    statuses = {
        label: (
            await admin_client.post("/restore/archive", content=body, headers=headers)
        ).status_code
        for label, (body, headers) in cases.items()
    }
    plain = await admin_client.post(
        "/restore/archive",
        content=b"a=b",
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": TOKEN},
    )

    assert statuses == {
        "file-first": BAD_REQUEST,
        "wrong-token": FORBIDDEN,
        "extra-part": BAD_REQUEST,
        "long-token": BAD_REQUEST,
        "no-file": BAD_REQUEST,
        "large-preamble": BAD_REQUEST,
    }
    assert plain.status_code == BAD_REQUEST


async def test_an_upload_without_an_origin_is_refused_and_stores_nothing(
    started_app: FastAPI, browsers: Browsers, admin_client: AsyncClient, archive: Archive
) -> None:
    await engage(admin_client)
    no_origin = browsers.open(session_token=ADMIN_SESSION_TOKEN, origin=None)
    body, headers = upload_of(archive.data)

    response = await no_origin.post("/restore/archive", content=body, headers=headers)

    assert response.status_code == FORBIDDEN
    assert uploads_in(started_app.state.settings.data_dir / "restore") == []


async def test_the_form_size_cap_holds_on_every_other_route(admin_client: AsyncClient) -> None:
    response = await admin_client.post("/login", data={"csrf_token": TOKEN, "pad": "x" * 300_000})

    assert response.status_code == 413


async def test_a_file_that_is_not_a_backup_is_refused_beside_the_upload_and_kept_nowhere(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    await engage(admin_client)
    body, headers = upload_of(b"\x1f\x8b" + b"junk" * (JUNK_BYTES // 4))

    response = await admin_client.post("/restore/archive", content=body, headers=headers)

    restore_dir = restore_directory(started_app)
    assert response.status_code == UNPROCESSABLE
    assert 'id="field-archive-error"' in response.text and "Upload the archive" in response.text
    assert not (restore_dir / "upload.tar.gz").exists()


async def test_the_real_archive_is_stored_intact_and_the_page_offers_review(
    admin_client: AsyncClient, started_app: FastAPI, archive: Archive
) -> None:
    await engage(admin_client)

    response = await upload(admin_client, archive.data)

    restore_dir = restore_directory(started_app)
    idle = await admin_client.get("/restore")
    assert (response.status_code, response.headers["location"]) == (SEE_OTHER, "/restore/review")
    assert sha(restore_dir / "upload.tar.gz") == archive.sha
    assert "Review the archive and enter the key" in idle.text
    assert "Upload a different archive" in idle.text


async def test_the_review_page_shows_the_manifest_the_live_counts_the_paths_and_a_key_field(
    admin_client: AsyncClient, started_app: FastAPI, archive: Archive
) -> None:
    await engage(admin_client)
    await upload(admin_client, archive.data)

    response = await admin_client.get("/restore/review")

    html = response.text
    text = text_of(html)
    data_dir = started_app.state.settings.data_dir
    restore_dir = restore_directory(started_app)
    key = next(t for t in parse_tags(html) if t.attrs.get("name") == "key")
    assert response.status_code == OK and archive.sha in text
    assert re.search(r"Key fingerprint\s+[0-9a-f]{16}", text)
    assert "Schema version" in text and "admin-user" in text
    assert "matches an enabled administrator" in text
    assert "Unverified until the key is checked" in text
    assert "Environments" in text and str(data_dir / "dockmin.db") in text
    assert str(restore_dir / "previous") in text and str(restore_dir / "staging") in text
    assert "dockmin never deletes it" in text
    assert "DOCKMIN_SECRET_KEY_FILE" in text and re.search(r"\bDOCKMIN_SECRET_KEY\b", text)
    assert key.attrs["type"] == "password" and key.attrs["autocomplete"] == "off"
    assert "value" not in key.attrs
    assert hidden_inputs(html)["archive_sha256"] == archive.sha
    assert 'action="/restore/cancel"' in html and 'name="replace_confirmed"' in html
    assert "<script>" not in html and "style=" not in html


async def confirm(client: AsyncClient, digest: str, **changes: str) -> Response:
    fields = {"key": VALID_KEY, "archive_sha256": digest, "replace_confirmed": "yes"} | changes
    return await client.post("/restore/confirm", data=csrf_form(client, **fields))


async def test_a_key_the_archive_refuses_is_a_field_error_that_is_never_echoed_or_stored(
    admin_client: AsyncClient, started_app: FastAPI, archive: Archive
) -> None:
    await engage(admin_client)
    await upload(admin_client, archive.data)
    digest = await reviewed_hash(admin_client)

    wrong = await confirm(admin_client, digest, key=WRONG_KEY)
    missing = await confirm(admin_client, digest, key="")
    unconfirmed = await confirm(admin_client, digest, replace_confirmed="")
    changed = await confirm(admin_client, digest, archive_sha256="0" * 64)

    rejected = await rows(
        started_app,
        "SELECT detail FROM activity_event WHERE action = 'instance_restore_key_rejected'",
    )
    assert wrong.status_code == missing.status_code == UNPROCESSABLE
    assert "key version" in text_of(wrong) and WRONG_KEY not in wrong.text
    assert "will be replaced" in text_of(unconfirmed) and VALID_KEY not in unconfirmed.text
    assert "changed since the review" in text_of(changed) and VALID_KEY not in changed.text
    assert started_app.state.restore_gate.phase == "engaged"
    assert len(rejected) == 2 and not any(WRONG_KEY in row["detail"] for row in rejected)


async def test_another_session_sees_who_began_the_restore_and_may_only_cancel_it(
    admin_client: AsyncClient, browsers: Browsers, started_app: FastAPI, archive: Archive
) -> None:
    await engage(admin_client)
    await upload(admin_client, archive.data)
    digest = await reviewed_hash(admin_client)
    other = await browsers.sign_in()

    page = await other.get("/restore")
    review = await other.get("/restore/review")
    attempt = await confirm(other, digest)
    cancelled = await other.post("/restore/cancel", data=csrf_form(other))
    notice = await land(other, cancelled)

    recorded = await rows(
        started_app, "SELECT 1 FROM activity_event WHERE action = 'instance_restore_failed'"
    )
    assert "Another session began this restore" in page.text
    assert 'action="/restore/cancel"' in page.text and 'action="/restore/archive"' not in page.text
    assert (review.status_code, attempt.status_code) == (FORBIDDEN, FORBIDDEN)
    assert (cancelled.status_code, location(cancelled)) == (SEE_OTHER, "/restore")
    assert started_app.state.restore_gate.phase == "open"
    assert flash_texts(notice.text) == [CANCELLED_NOTICE]
    assert EMPTY_BANNER.search(notice.text) is not None
    assert len(recorded) == 1


async def test_an_operator_the_archive_does_not_list_sees_the_refusal_and_no_way_to_confirm(
    admin_client: AsyncClient, started_app: FastAPI, archive: Archive
) -> None:
    await execute(started_app, "UPDATE user SET username = 'someone-else' WHERE id = 1")
    await engage(admin_client)
    await upload(admin_client, archive.data)

    review = await admin_client.get("/restore/review")
    forged = await confirm(admin_client, archive.sha)

    text = text_of(review)
    assert "This archive cannot be restored here" in text
    assert "no enabled administrator with your user name" in text
    assert 'action="/restore/confirm"' not in review.text and 'name="key"' not in review.text
    assert 'action="/restore/cancel"' in review.text
    assert forged.status_code == UNPROCESSABLE
    assert "no enabled administrator" in text_of(forged)


async def test_a_request_that_fails_the_token_check_leaves_the_reviewed_upload_in_place(
    admin_client: AsyncClient, started_app: FastAPI, archive: Archive
) -> None:
    await engage(admin_client)
    data = archive.data
    await upload(admin_client, data)
    restore_dir = restore_directory(started_app)
    kept = sha(restore_dir / "upload.tar.gz")
    body, headers = upload_of(data, token="W" * 43)

    refused = await admin_client.post("/restore/archive", content=body, headers=headers)

    assert refused.status_code == FORBIDDEN
    assert sha(restore_dir / "upload.tar.gz") == kept
    assert OWNER.session_id is not None
    assert started_app.state.restore.status(OWNER, 1).stage == "awaiting_key"


async def test_the_banner_follows_the_gate_phase_and_links_the_progress_to_administrators_only(
    started_app: FastAPI, admin_client: AsyncClient, anonymous_client: AsyncClient
) -> None:
    gate = started_app.state.restore_gate
    gate.phase, gate.restore_id = "swapping", "a" * 32
    unknown = f"/restore/progress/{'0' * 32}"

    anonymous_swapping = await anonymous_client.get(unknown)
    admin_swapping = await admin_client.get(unknown)
    gate.phase = "failed"
    failed = await anonymous_client.get(unknown)
    gate.phase = "open"

    assert gate.banner_message is None
    assert "Follow the restore" not in anonymous_swapping.text
    assert "/restore/progress/" + "a" * 32 not in anonymous_swapping.text
    assert "Follow the restore" in admin_swapping.text
    assert 'data-tone="danger"' in failed.text and "could not be put back" in failed.text


class Quiet:
    async def __call__(self) -> None:
        return None


async def test_a_restore_that_fails_at_the_swap_ends_on_a_progress_page_with_a_way_back(
    admin_client: AsyncClient, started_app: FastAPI, archive: Archive
) -> None:
    started_app.state.restore = RestoreCoordinator(
        gate=started_app.state.restore_gate,
        production=started_app.state.backup_production,
        data_dir=started_app.state.settings.data_dir,
        runtime=partial(_restore_runtime, started_app),
        stop_runtime=Quiet(),
        start_runtime=Quiet(),
        clock=lambda: started_app.state.auth.clock(),
    )
    data_dir = started_app.state.settings.data_dir
    await engage(admin_client)
    await upload(admin_client, archive.data)
    restore_id = started_app.state.restore.status(OWNER, 1).restore_id
    (data_dir / "restore" / restore_id / "previous" / "stacks").mkdir(parents=True)
    digest = await reviewed_hash(admin_client)

    started = await confirm(admin_client, digest)
    url = started.headers["location"]
    page = await finished_fragment(admin_client, url)
    full = await admin_client.get(url)
    boosted = await admin_client.get(url, headers={**HTMX, "HX-Boosted": "true"})
    idle = await admin_client.get("/restore")

    text = text_of(page)
    assert "hx-trigger" not in page.text and "hx-get" not in page.text
    assert "Failed" in text and "swap_failed" in text and 'href="/restore"' in page.text
    assert 'data-step-state="failed"' in page.text
    assert str(data_dir / "restore" / restore_id / "previous") in page.text
    assert 'id="restore-banner" hx-swap-oob="true"' in page.text
    assert "hx-swap-oob" not in full.text
    assert "<!doctype html>" in full.text.lower() and 'id="restore-progress"' in full.text
    assert "<!doctype html>" in boosted.text.lower()
    assert started_app.state.restore_gate.phase == "open"
    assert "How a restore runs" in idle.text


async def finished_fragment(client: AsyncClient, url: str) -> Response:
    page = await client.get(url, headers=HTMX)
    for _ in range(400):
        if page.status_code == OK and "hx-trigger" not in page.text:
            return page
        await asyncio.sleep(0.05)
        page = await client.get(url, headers=HTMX)
    return page


@dataclass
class EndToEnd:
    """What a restore through the pages and the real runtime left behind."""

    outcome: str = ""
    reason: str | None = None
    poll_statuses: list[int] = field(default_factory=list)
    database_attribute_during_swap: bool = True
    old_database_closed: bool = False
    old_database_refuses: bool = False
    file_size_before_polls: int = 0
    file_size_after_polls: int = 0
    anonymous_status_before_swap: int = 0
    final_status: int = 0
    final_text: str = ""
    final_html: str = ""
    final_cache_control: str = ""
    step_states: list[str] = field(default_factory=list)
    stale_cookie_redirects_to_login: bool = False
    gate_phase: str = ""
    signed_in_status: int = 0
    environments: list[str] = field(default_factory=list)
    stacks: set[tuple[str, str]] = field(default_factory=set)
    restored_rows: int = 0
    previous_environments: list[str] = field(default_factory=list)
    suspended_after_restore: int = -1
    banner_after_restore: str = ""
    standard_resume_status: int = 0
    resume_status: int = 0
    resume_location: str = ""
    resume_notices: list[str] = field(default_factory=list)
    resume_banner_empty: bool = False
    suspended_after_resume: int = -1
    resumed_rows: int = 0
    live_environment_name: str = LIVE_ENVIRONMENT


async def end_to_end(root: Path, provisioned: Path, archive: Archive) -> EndToEnd:
    facts = EndToEnd()
    settings = target_settings(root, "target")
    template = target_template(root, provisioned)
    application = create_app(settings)
    gate = application.state.restore_gate
    browsers = Browsers(application)
    seen: dict[str, object] = {}

    async def start_after_polling() -> None:
        stale = browsers.open(session_token=ADMIN_SESSION_TOKEN)
        facts.file_size_before_polls = settings.db_path.stat().st_size
        facts.database_attribute_during_swap = hasattr(application.state, "database")
        for _ in range(POLLS):
            facts.poll_statuses.append((await stale.get(str(seen["url"]))).status_code)
        facts.file_size_after_polls = settings.db_path.stat().st_size
        old = seen["old_database"]
        facts.old_database_closed = old.closed  # type: ignore[attr-defined]
        try:
            async with old.session():  # type: ignore[attr-defined]
                facts.old_database_refuses = False
        except DatabaseClosed:
            facts.old_database_refuses = True
        await start_runtime(application)

    application.state.restore = RestoreCoordinator(
        gate=gate,
        production=application.state.backup_production,
        data_dir=settings.data_dir,
        runtime=partial(_restore_runtime, application),
        stop_runtime=partial(stop_runtime, application),
        start_runtime=start_after_polling,
        clock=lambda: application.state.auth.clock(),
    )
    coordinator = application.state.restore
    async with serving_app(application, settings, template):
        seen["old_database"] = application.state.database
        admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
        anonymous = browsers.open()
        await engage(admin)
        await upload(admin, archive.data)
        digest = await reviewed_hash(admin)
        restore_id = coordinator.status(OWNER, 1).restore_id
        seen["url"] = f"/restore/progress/{restore_id}"
        facts.anonymous_status_before_swap = (await anonymous.get(str(seen["url"]))).status_code
        started = await confirm(admin, digest)
        assert started.headers["location"] == seen["url"]
        for _ in range(SETTLE_POLLS):
            progress = coordinator.progress(restore_id, session_id=None)
            if progress is not None and progress.outcome != "running":
                break
            await asyncio.sleep(SETTLE_PAUSE)
        assert progress is not None
        facts.outcome, facts.reason = progress.outcome, progress.reason
        final = await anonymous.get(str(seen["url"]))
        facts.final_status, facts.final_html = final.status_code, final.text
        facts.final_text = text_of(final)
        facts.final_cache_control = final.headers.get("cache-control", "")
        facts.step_states = re.findall(r'data-step-state="(\w+)"', final.text)
        facts.stale_cookie_redirects_to_login = "/login" in (
            await admin.get("/backups")
        ).headers.get("location", "")
        facts.gate_phase = gate.phase
        fresh = await browsers.sign_in(ADMIN_USERNAME, ADMIN_PASSWORD)
        facts.signed_in_status = (await fresh.get("/restore")).status_code
        database = application.state.database
        async with database.session() as session:
            found = await session.fetch_limited("SELECT name FROM environment", (), limit=100)
            kinds = await session.fetch_limited("SELECT name, kind FROM stack", (), limit=100)
            rows_ = await session.fetch_limited(
                "SELECT 1 FROM activity_event WHERE action = 'instance_restored'", (), limit=10
            )
            paused = await session.fetch_limited("SELECT 1", (), limit=1)
        facts.environments = [row["name"] for row in found]
        facts.stacks = {(row["name"], row["kind"]) for row in kinds}
        facts.restored_rows = len(rows_)
        assert paused
        previous = settings.data_dir / "restore" / restore_id / "previous" / "dockmin.db"
        facts.previous_environments = previous_environments(previous)
        facts.suspended_after_restore = application.state.scheduler.scheduler._suspended
        facts.banner_after_restore = text_of(await fresh.get("/restore"))
        standard = await browsers.sign_in(STANDARD_USERNAME, STANDARD_PASSWORD)
        facts.standard_resume_status = (
            await standard.post("/restore/resume-automation", data=csrf_form(standard))
        ).status_code
        resumed = await fresh.post("/restore/resume-automation", data=csrf_form(fresh))
        facts.resume_status = resumed.status_code
        facts.resume_location = resumed.headers.get("location", "")
        landing = await fresh.get(facts.resume_location)
        facts.resume_notices = flash_texts(landing.text)
        facts.resume_banner_empty = EMPTY_BANNER.search(landing.text) is not None
        facts.suspended_after_resume = application.state.scheduler.scheduler._suspended
        async with application.state.database.session() as session:
            resumed_rows = await session.fetch_limited(
                "SELECT 1 FROM activity_event WHERE action = 'instance_automation_resumed'",
                (),
                limit=10,
            )
        facts.resumed_rows = len(resumed_rows)
        await browsers.close()
    return facts


def previous_environments(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return [row[0] for row in connection.execute("SELECT name FROM environment")]
    finally:
        connection.close()


@pytest.fixture(scope="module")
def restored(
    provisioned_template: Path, archive: Archive, tmp_path_factory: pytest.TempPathFactory
) -> EndToEnd:
    root = tmp_path_factory.mktemp("restore-end-to-end")
    return asyncio.run(end_to_end(root, provisioned_template, archive))


def test_a_restore_through_the_pages_and_the_real_runtime_finishes_as_restored(
    restored: EndToEnd,
) -> None:
    assert (restored.outcome, restored.reason) == ("restored", None)
    assert restored.gate_phase == "open"


def test_progress_polls_with_a_stale_session_during_the_swap_are_answered_not_failed(
    restored: EndToEnd,
) -> None:
    assert restored.poll_statuses == [OK] * POLLS
    assert restored.anonymous_status_before_swap == NOT_FOUND


def test_the_database_is_dropped_and_closed_for_good_during_the_swap_and_never_recreated_empty(
    restored: EndToEnd,
) -> None:
    assert not restored.database_attribute_during_swap
    assert restored.old_database_closed and restored.old_database_refuses
    assert (
        restored.file_size_after_polls == restored.file_size_before_polls > MINIMUM_DATABASE_BYTES
    )


def test_the_finished_progress_page_is_served_by_id_alone_and_points_to_sign_in(
    restored: EndToEnd,
) -> None:
    assert restored.final_status == OK and "Restored" in restored.final_text
    assert 'href="/login"' in restored.final_html and "Sign in" in restored.final_text
    assert "hx-trigger" not in restored.final_html
    assert "previous" in restored.final_text.lower() and "never deletes it" in restored.final_text
    assert restored.step_states == ["done"] * MEMBERS
    assert restored.final_cache_control == "no-store"


def test_every_old_session_is_gone_and_an_administrator_of_the_archive_signs_in_again(
    restored: EndToEnd,
) -> None:
    assert restored.stale_cookie_redirects_to_login
    assert restored.signed_in_status == OK


def test_the_restored_data_replaced_the_instances_own_and_lists_both_kinds_of_stack(
    restored: EndToEnd,
) -> None:
    assert restored.live_environment_name not in restored.environments
    assert {("stack-one", "compose"), ("swarm-one", "swarm")} <= restored.stacks
    assert restored.restored_rows == 1


def test_the_data_it_replaced_is_kept_beside_the_restore(restored: EndToEnd) -> None:
    assert restored.live_environment_name in restored.previous_environments


def test_automation_stays_suspended_with_a_banner_until_an_administrator_resumes_it(
    restored: EndToEnd,
) -> None:
    assert restored.suspended_after_restore == 1
    assert "Automation is paused after a restore" in restored.banner_after_restore
    assert "Resume automation" in restored.banner_after_restore
    assert restored.standard_resume_status == FORBIDDEN
    assert (restored.resume_status, restored.resume_location) == (SEE_OTHER, "/restore")
    assert restored.resume_notices == [RESUMED_NOTICE]
    assert restored.resume_banner_empty
    assert (restored.suspended_after_resume, restored.resumed_rows) == (0, 1)


async def test_a_request_still_running_at_the_swap_fails_it_and_the_instance_keeps_its_data(
    provisioned_template: Path,
    archive: Archive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "DRAIN_SECONDS", 0.3)
    settings = target_settings(tmp_path, "drain")
    application = create_app(settings)
    gate = application.state.restore_gate
    application.state.restore = RestoreCoordinator(
        gate=gate,
        production=application.state.backup_production,
        data_dir=settings.data_dir,
        runtime=partial(_restore_runtime, application),
        stop_runtime=partial(stop_runtime, application),
        start_runtime=partial(start_runtime, application),
        clock=lambda: application.state.auth.clock(),
    )
    coordinator = application.state.restore
    browsers = Browsers(application)

    async with serving_app(application, settings, target_template(tmp_path, provisioned_template)):
        admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
        await engage(admin)
        await upload(admin, archive.data)
        restore_id = coordinator.status(OWNER, 1).restore_id
        digest = await reviewed_hash(admin)
        gate.enter()
        await confirm(admin, digest)
        for _ in range(SETTLE_POLLS):
            progress = coordinator.progress(restore_id, session_id=None)
            if progress is not None and progress.outcome != "running":
                break
            await asyncio.sleep(SETTLE_PAUSE)
        gate.leave()
        names = await environment_names(application)
        await browsers.close()

    assert progress is not None
    assert (progress.outcome, progress.reason) == ("failed", "swap_failed")
    assert LIVE_ENVIRONMENT in names
    assert gate.phase == "open" and not coordinator.automation_paused
    assert not (settings.data_dir / "restore" / restore_id).exists()


async def environment_names(application: FastAPI) -> list[str]:
    async with application.state.database.session() as session:
        found = await session.fetch_limited("SELECT name FROM environment", (), limit=100)
    return [row["name"] for row in found]


async def test_only_the_archive_route_skips_the_form_token_check(
    admin_client: AsyncClient,
) -> None:
    changes = {
        path: (await admin_client.post(path, data={})).status_code
        for path in ("/restore/begin", "/restore/confirm", "/restore/cancel")
    }
    archive_route = await admin_client.post(
        "/restore/archive", content=b"", headers={"Content-Type": "text/plain"}
    )

    assert changes == dict.fromkeys(changes, FORBIDDEN)
    assert archive_route.status_code == BAD_REQUEST
    assert {("POST", "/restore/archive")} == STREAMED_UPLOAD_ROUTES
