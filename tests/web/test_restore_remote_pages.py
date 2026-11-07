"""The restore pages for an archive in a bucket: the listing, the fetch and the record check."""

import asyncio
import re
from dataclasses import dataclass
from functools import partial
from html import unescape
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.principal import Principal
from app.crypto.envelope import SecretLocation
from app.main import _restore_runtime, start_runtime, stop_runtime
from app.object_storage.errors import MESSAGES
from app.services.restore import CHOOSE_FROM_LIST, LIST_PERMISSION_NOTE, RestoreCoordinator
from tests.auth_support import csrf_form
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET
from tests.object_storage.wire_support import error_reply
from tests.restore_remote_support import RemoteBucket, archive_key
from tests.restore_support import real_archive, sha

OK = 200
SEE_OTHER = 303
BAD_REQUEST = 400
FORBIDDEN = 403
STAMP = "2026-01-01T00:00:00Z"
NEWEST = archive_key(30)
OLDER = archive_key(10)
KEY = archive_key(5)
OWNER = Principal(user_id=1, username="admin-user", role="administrator", session_id=1)
FETCH_POLLS = 200
FETCH_PAUSE = 0.05
OTHER_SHA = "9" * 64


@dataclass(frozen=True)
class Archive:
    data: bytes
    sha: str


@pytest.fixture(scope="module")
def archive(provisioned_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Archive:
    path = asyncio.run(real_archive(tmp_path_factory.mktemp("remote-pages"), provisioned_template))
    return Archive(path.read_bytes(), sha(path))


def text_of(response: Response) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", response.text))


@dataclass
class Aimed:
    target_id: int
    bucket: RemoteBucket
    coordinator: RestoreCoordinator


async def aim_at(application: FastAPI, bucket: RemoteBucket) -> Aimed:
    coordinator = RestoreCoordinator(
        gate=application.state.restore_gate,
        production=application.state.backup_production,
        data_dir=application.state.settings.data_dir,
        runtime=partial(_restore_runtime, application),
        stop_runtime=partial(stop_runtime, application),
        start_runtime=partial(start_runtime, application),
        clock=lambda: application.state.auth.clock(),
        transport=httpx.MockTransport(bucket),
    )
    application.state.restore = coordinator
    async with application.state.database.session() as session:
        await session.execute(
            "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
            "secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, backup_prefix, "
            "activity_log_prefix, retention_count, created_at, updated_at) VALUES "
            "('pages-bucket', ?, 'us-east-1', ?, ?, 'x', 1, 1, NULL, 'backups/', 'logs/', NULL, "
            "?, ?)",
            (ENDPOINT, BUCKET, ACCESS, STAMP, STAMP),
        )
        row = await session.fetch_one("SELECT max(id) AS id FROM object_storage_target")
        assert row is not None
        target_id = int(row["id"])
        sealed = application.state.vault.seal(
            SECRET,
            SecretLocation("object_storage_target", "secret_access_key_ciphertext", target_id),
        )
        await session.execute(
            "UPDATE object_storage_target SET secret_access_key_ciphertext = ? WHERE id = ?",
            (sealed, target_id),
        )
    return Aimed(target_id, bucket, coordinator)


async def record_comparison(
    application: FastAPI, target_id: int, key: str, archive: Archive, recorded: str | None
) -> None:
    digests = {"same": archive.sha, "other": OTHER_SHA}
    if recorded is None:
        return
    async with application.state.database.session() as session:
        await session.execute(
            "INSERT INTO backup_record (origin, object_storage_target_id, created_at, size_bytes, "
            "key_version, key_fingerprint, destination_key, archive_sha256, schema_version, "
            "outcome, error) VALUES ('sync', ?, ?, 1, 1, 'fp', ?, ?, 1, 'success', NULL)",
            (target_id, STAMP, key, digests[recorded]),
        )


async def begin(client: AsyncClient) -> None:
    response = await client.post("/restore/begin", data=csrf_form(client, confirm_offline="yes"))
    assert response.status_code == SEE_OTHER


async def fetch(client: AsyncClient, aimed: Aimed, key: str) -> Response:
    return await client.post(
        "/restore/archive/remote",
        data=csrf_form(client, target_id=str(aimed.target_id), object_path=key),
    )


async def fetched(aimed: Aimed) -> None:
    for _ in range(FETCH_POLLS):
        status = aimed.coordinator.status(OWNER, 1)
        if status is None or status.stage != "fetching":
            return
        await asyncio.sleep(FETCH_PAUSE)
    raise TimeoutError("the fetch did not finish")


async def test_the_listing_page_shows_the_archives_newest_first_with_a_button_to_use_each(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    bucket = RemoteBucket()
    bucket.objects = {OLDER: b"x", NEWEST: b"x", "backups/notes.txt": b"x"}
    aimed = await aim_at(started_app, bucket)
    await begin(admin_client)

    page = await admin_client.get(f"/restore/remote?target_id={aimed.target_id}")

    assert page.status_code == OK
    assert page.text.index(NEWEST.rpartition("/")[2]) < page.text.index(OLDER.rpartition("/")[2])
    assert "notes.txt" not in page.text
    assert page.text.count("Use this archive") == 2
    assert "Refresh list" in page.text


async def test_a_refused_listing_page_shows_the_storage_message_and_the_permission_note(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    bucket = RemoteBucket()
    bucket.listing_reply = error_reply(403, "AccessDenied")
    aimed = await aim_at(started_app, bucket)
    await begin(admin_client)

    page = await admin_client.get(f"/restore/remote?target_id={aimed.target_id}")

    assert page.status_code == OK
    assert MESSAGES["refused"] in text_of(page)
    assert LIST_PERMISSION_NOTE in text_of(page)
    assert "Use this archive" not in page.text


async def test_fetching_a_key_that_is_not_in_the_listing_is_a_bad_request_naming_the_list(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    bucket = RemoteBucket()
    bucket.objects = {KEY: b"x"}
    aimed = await aim_at(started_app, bucket)
    await begin(admin_client)
    await admin_client.get(f"/restore/remote?target_id={aimed.target_id}")

    response = await fetch(admin_client, aimed, archive_key(59))

    assert response.status_code == BAD_REQUEST
    assert CHOOSE_FROM_LIST in text_of(response)
    assert bucket.object_requests == []


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        (None, "No record of this archive on this instance."),
        ("same", "Matches the archive this instance pushed on"),
        ("other", "Differs from the archive this instance pushed on"),
    ],
    ids=["no-record", "matching-record", "differing-record"],
)
async def test_the_review_of_a_fetched_archive_says_how_it_compares_with_the_record(
    admin_client: AsyncClient,
    started_app: FastAPI,
    archive: Archive,
    recorded: str | None,
    expected: str,
) -> None:
    bucket = RemoteBucket()
    bucket.objects = {KEY: archive.data}
    aimed = await aim_at(started_app, bucket)
    await record_comparison(started_app, aimed.target_id, KEY, archive, recorded)
    await begin(admin_client)
    await admin_client.get(f"/restore/remote?target_id={aimed.target_id}")

    started = await fetch(admin_client, aimed, KEY)
    await fetched(aimed)
    review = await admin_client.get("/restore/review")

    assert (started.status_code, started.headers["location"]) == (SEE_OTHER, "/restore/review")
    assert review.status_code == OK
    assert expected in text_of(review)
    assert KEY in text_of(review)


async def test_a_standard_user_is_refused_both_remote_routes(
    standard_client: AsyncClient,
) -> None:
    listing = await standard_client.get("/restore/remote?target_id=1")
    fetched_response = await standard_client.post(
        "/restore/archive/remote",
        data=csrf_form(standard_client, target_id="1", object_path=KEY),
    )

    assert (listing.status_code, fetched_response.status_code) == (FORBIDDEN, FORBIDDEN)
