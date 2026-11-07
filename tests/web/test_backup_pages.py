"""The Backups page and the archive download."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from app.config import MANAGED_KEY_FILENAME, SECRET_KEY_FILE_VAR, SECRET_KEY_VAR
from tests.auth_support import csrf_form
from tests.job_support import rows

HTMX = {"HX-Request": "true"}
OK = 200
TOO_MANY = 429
UNAVAILABLE = 503
GZIP_MAGIC = b"\x1f\x8b"
ATTACHMENT = re.compile(r'attachment; filename="dockmin-backup-\d{8}T\d{6}Z\.tar\.gz"')
RETRY_AFTER = "30"
RESTORE_ID = "r" * 32
FORBIDDEN = 403
KEY_COPY_NOTICE = "Copy the key file somewhere apart from the data volume."


async def test_the_backups_page_offers_the_download_and_names_what_is_not_in_the_archive(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/backups")

    html = response.text
    assert response.status_code == OK and response.headers["cache-control"] == "no-store"
    assert 'action="/backups/download"' in html and 'name="csrf_token"' in html
    assert "No backups yet" in html and "not in the archive" in html
    assert "subsystem=backup-syncer" in html and 'href="/restore"' in html


async def test_a_download_streams_a_gzip_file_with_its_length_and_leaves_no_work_directory(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    response = await admin_client.post("/backups/download", data=csrf_form(admin_client))

    body = response.content
    work = started_app.state.settings.data_dir / "backup-work"
    assert response.status_code == OK and body[:2] == GZIP_MAGIC
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-length"] == str(len(body))
    assert ATTACHMENT.fullmatch(response.headers["content-disposition"])
    assert response.headers["cache-control"] == "no-store"
    assert not work.exists() or list(work.iterdir()) == []
    assert not started_app.state.backup_production.held


async def test_a_download_is_recorded_listed_with_its_digest_and_audited(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    download = await admin_client.post("/backups/download", data=csrf_form(admin_client))
    digest = hashlib.sha256(download.content).hexdigest()

    page = await admin_client.get("/backups")
    fragment = await admin_client.get("/backups", headers=HTMX)
    events = await rows(
        started_app, "SELECT detail FROM activity_event WHERE action = 'backup_downloaded'"
    )

    assert digest in page.text and "Download" in page.text and "Succeeded" in page.text
    assert "<html" not in fragment.text and 'id="backups-table"' in fragment.text
    assert len(events) == 1 and digest in events[0]["detail"]


async def test_a_download_while_a_production_is_running_is_refused_as_too_many_requests(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    assert started_app.state.backup_production.try_hold()

    response = await admin_client.post("/backups/download", data=csrf_form(admin_client))

    assert response.status_code == TOO_MANY and response.headers["retry-after"] == RETRY_AFTER
    assert "A backup or a restore is in progress" in response.text


async def test_a_download_while_the_gate_is_engaged_is_refused_by_the_gate(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    started_app.state.restore_gate.engage(
        RESTORE_ID, datetime(2026, 1, 1, tzinfo=UTC), purpose="restore"
    )

    response = await admin_client.post("/backups/download", data=csrf_form(admin_client))

    assert response.status_code == UNAVAILABLE


async def test_the_backups_page_names_where_each_key_source_is_held_and_refuses_a_standard_user(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    started_app: FastAPI,
    tmp_path: Path,
) -> None:
    fingerprint = started_app.state.vault.key_fingerprint()
    settings = started_app.state.settings
    supplied = tmp_path / "supplied.key"
    key_dir = tmp_path / "volume"
    from_file = settings.model_copy(update={"secret_key": None, "secret_key_file": supplied})
    generated = settings.model_copy(update={"secret_key": None, "secret_key_dir": key_dir})

    variable_page = (await admin_client.get("/backups")).text
    started_app.state.settings = from_file
    file_page = (await admin_client.get("/backups")).text
    started_app.state.settings = generated
    generated_page = (await admin_client.get("/backups")).text
    fragment = await admin_client.get("/backups", headers=HTMX)
    refused = await standard_client.get("/backups")

    assert all(fingerprint in page for page in (variable_page, file_page, generated_page))
    assert f'The <span class="mono">{SECRET_KEY_VAR}</span> environment variable' in variable_page
    assert KEY_COPY_NOTICE not in variable_page and MANAGED_KEY_FILENAME not in variable_page
    assert f'The file <span class="mono">{supplied}</span>, named by' in file_page
    assert f'<span class="mono">{SECRET_KEY_FILE_VAR}</span>' in file_page
    assert KEY_COPY_NOTICE not in file_page
    key_held = f'<span class="mono">{key_dir / MANAGED_KEY_FILENAME}</span> in the key volume'
    assert key_held in generated_page
    assert KEY_COPY_NOTICE in generated_page
    assert "Keep a copy apart from the archives." in variable_page + file_page
    assert "Encryption key" not in fragment.text
    assert refused.status_code == FORBIDDEN
