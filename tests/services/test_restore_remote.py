"""Restoring from an archive in a bucket: the listing, the fetch, its failures and the review."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import app.backup.pipeline as pipeline_module
import app.services.restore as restore_module
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.object_storage.errors import MESSAGES
from app.services.restore import (
    CHOOSE_FROM_LIST,
    CREDENTIAL_UNREADABLE,
    LIST_PERMISSION_NOTE,
    OBJECT_CHANGED,
    REMOTE_PAGE_SIZE,
    UPLOAD_IN_PROGRESS,
    RestoreBusy,
)
from tests.object_storage.wire_support import error_reply
from tests.restore_remote_support import (
    FOLDER,
    Breaking,
    RemoteBucket,
    add_record,
    add_target,
    archive_key,
    make_remote_instance,
    until_fetched,
)
from tests.restore_support import (
    OPERATOR,
    OTHER_SESSION,
    STANDARD,
    Instance,
    confirm_arguments,
    settle,
    sha,
    source_archive,
)

PLACEHOLDER = b"x"
NEWEST = archive_key(30)
MIDDLE = archive_key(20)
OLDEST = archive_key(10, "backups/rule-2-20260102T000000/")
KEY = archive_key(5)
PAGES_BEFORE_TRUNCATION = 20
SMALL_CAP = 10
SECOND_PAGE = 2
BEYOND_LAST_PAGE = 9
GARBAGE_CIPHERTEXT = "v1:AAAAAAAAAAAAAAAAAAAAAAAA:BBBBBBBBBBBBBBBBBBBBBB=="
OTHER_SHA = "9" * 64
SERVER_ERROR = 500


@dataclass
class Remote:
    """A live instance with a restore begun, a target aimed at the stub, and the stub."""

    instance: Instance
    bucket: RemoteBucket
    target_id: int

    @property
    def coordinator(self) -> restore_module.RestoreCoordinator:
        assert self.instance.coordinator is not None
        return self.instance.coordinator

    @property
    def upload_directory(self) -> Path:
        restore_id = self.coordinator.status(OPERATOR, 1)
        assert restore_id is not None
        return self.instance.data_dir / "restore" / restore_id.restore_id

    def uploaded_files(self) -> list[str]:
        return sorted(entry.name for entry in self.upload_directory.glob("upload.tar.gz*"))

    async def list_all(self) -> None:
        await self.coordinator.list_remote(OPERATOR, 1, self.target_id)

    async def fetch(self, key: str = KEY) -> None:
        await self.list_all()
        await self.coordinator.fetch_archive(OPERATOR, 1, target_id=self.target_id, object_path=key)
        await until_fetched(self.instance)


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built, world = asyncio.run(source_archive(tmp_path_factory.mktemp("remote-source")))
    asyncio.run(world.database.aclose())
    return built


@pytest.fixture(scope="module")
def archive_data(archive: Path) -> bytes:
    return archive.read_bytes()


@pytest.fixture
async def remote(tmp_path: Path, archive_data: bytes) -> AsyncIterator[Remote]:
    bucket = RemoteBucket()
    bucket.objects[KEY] = archive_data
    instance = await make_remote_instance(tmp_path / "live", bucket)
    target_id = add_target(instance)
    assert instance.coordinator is not None
    await instance.coordinator.begin(OPERATOR, session_id=1)
    yield Remote(instance, bucket, target_id)
    await instance.close()


async def listed_keys(remote: Remote, **options: int | bool) -> list[str]:
    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id, **options)  # type: ignore[arg-type]
    return [entry.key for entry in listing.archives]


async def test_the_listing_shows_only_archives_under_the_backup_prefix_newest_first(
    remote: Remote,
) -> None:
    remote.bucket.objects = {
        MIDDLE: PLACEHOLDER,
        OLDEST: PLACEHOLDER,
        NEWEST: PLACEHOLDER,
        "backups/notes.txt": PLACEHOLDER,
        f"{FOLDER}dockmin-backup-bad.tar.gz": PLACEHOLDER,
        f"{FOLDER}sub/dockmin-backup-20260101T000040Z.tar.gz.bak": PLACEHOLDER,
        "other/dockmin-backup-20260101T000050Z.tar.gz": PLACEHOLDER,
    }

    keys = await listed_keys(remote)

    assert keys == [NEWEST, MIDDLE, OLDEST]
    assert remote.bucket.prefixes == ["backups/"]


async def test_each_row_names_its_folder_size_and_whether_this_instance_recorded_it(
    remote: Remote,
) -> None:
    remote.bucket.objects = {NEWEST: b"abc", MIDDLE: PLACEHOLDER}
    add_record(remote.instance, remote.target_id, NEWEST, OTHER_SHA)

    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)

    newest, middle = listing.archives
    assert (newest.folder, newest.size, newest.recorded) == (FOLDER.rstrip("/"), 3, True)
    assert (middle.size, middle.recorded) == (1, False)
    assert newest.last_modified is not None
    assert listing.target_name == "restore-bucket" and listing.failure is None


async def test_a_listing_longer_than_a_page_is_paged_and_a_page_past_the_end_shows_the_last(
    remote: Remote,
) -> None:
    remote.bucket.objects = {archive_key(second): PLACEHOLDER for second in range(55)}

    first = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)
    second = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id, page=SECOND_PAGE)
    past = await remote.coordinator.list_remote(
        OPERATOR, 1, remote.target_id, page=BEYOND_LAST_PAGE
    )

    assert (first.page, first.page_count, first.total, len(first.archives)) == (
        1,
        SECOND_PAGE,
        55,
        REMOTE_PAGE_SIZE,
    )
    assert first.archives[0].key == archive_key(54)
    assert (second.page, len(second.archives), past.page) == (SECOND_PAGE, 5, SECOND_PAGE)
    assert past.archives == second.archives


async def test_a_bucket_that_answers_in_several_pages_is_read_to_its_end(remote: Remote) -> None:
    remote.bucket.objects = {archive_key(second): PLACEHOLDER for second in range(5)}
    remote.bucket.page_size = 2

    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)

    assert (listing.total, listing.truncated) == (5, False)
    assert remote.bucket.list_requests == 3


async def test_a_bucket_with_more_pages_than_the_limit_is_cut_and_says_so(remote: Remote) -> None:
    remote.bucket.objects = {archive_key(second): PLACEHOLDER for second in range(25)}
    remote.bucket.page_size = 1

    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)

    assert (listing.total, listing.truncated) == (PAGES_BEFORE_TRUNCATION, True)
    assert remote.bucket.list_requests == PAGES_BEFORE_TRUNCATION


async def test_a_listing_is_kept_until_it_is_refreshed(remote: Remote) -> None:
    await listed_keys(remote)
    await listed_keys(remote)
    kept = remote.bucket.list_requests

    await listed_keys(remote, refresh=True)

    assert (kept, remote.bucket.list_requests) == (1, 2)


async def test_a_refused_listing_shows_the_storage_message_and_the_permission_note(
    remote: Remote,
) -> None:
    remote.bucket.listing_reply = error_reply(403, "AccessDenied")

    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)

    assert listing.archives == ()
    assert listing.failure == MESSAGES["refused"]
    assert listing.note == LIST_PERMISSION_NOTE


async def test_a_listing_that_cannot_connect_shows_the_storage_message_without_the_note(
    remote: Remote,
) -> None:
    remote.bucket.listing_reply = httpx.ConnectError("down")

    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)

    assert (listing.failure, listing.note) == (MESSAGES["unreachable"], None)


async def test_a_failed_listing_is_not_kept_so_the_next_read_asks_again(remote: Remote) -> None:
    remote.bucket.listing_reply = error_reply(SERVER_ERROR, "InternalError")
    await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)
    remote.bucket.listing_reply = None

    listing = await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id)

    assert listing.failure is None and listing.total == 1


async def test_a_target_whose_credential_cannot_be_read_is_not_contacted(remote: Remote) -> None:
    unreadable = add_target(remote.instance, name="unreadable", ciphertext=GARBAGE_CIPHERTEXT)

    listing = await remote.coordinator.list_remote(OPERATOR, 1, unreadable)

    assert listing.failure == CREDENTIAL_UNREADABLE
    assert remote.bucket.list_requests == 0


async def test_a_target_that_does_not_exist_is_not_found(remote: Remote) -> None:
    with pytest.raises(ResourceNotFound):
        await remote.coordinator.list_remote(OPERATOR, 1, remote.target_id + 100)


@pytest.mark.parametrize("caller", [OTHER_SESSION, STANDARD], ids=["another-session", "standard"])
async def test_only_the_owning_session_may_list_a_bucket(remote: Remote, caller: Principal) -> None:
    with pytest.raises(PermissionDenied):
        await remote.coordinator.list_remote(caller, caller.session_id or 0, remote.target_id)

    assert remote.bucket.list_requests == 0


@pytest.mark.parametrize("key", ["backups/rule-9/dockmin-backup-20260101T000099Z.tar.gz", KEY])
async def test_a_key_that_was_not_listed_is_refused_and_nothing_is_fetched(
    remote: Remote, key: str
) -> None:
    remote.bucket.objects.pop(KEY)
    remote.bucket.objects[MIDDLE] = PLACEHOLDER

    with pytest.raises(ValidationFailed) as raised:
        await remote.fetch(key)

    assert raised.value.fields == {"archive": CHOOSE_FROM_LIST}
    assert remote.bucket.object_requests == []


async def test_a_fetch_without_any_listing_is_refused(remote: Remote) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await remote.coordinator.fetch_archive(
            OPERATOR, 1, target_id=remote.target_id, object_path=KEY
        )

    assert raised.value.fields == {"archive": CHOOSE_FROM_LIST}


async def test_a_fetched_archive_lands_at_the_upload_path_and_awaits_the_key(
    remote: Remote, archive_data: bytes
) -> None:
    await remote.fetch()

    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None and status.fetch_error is None
    assert status.stage == "awaiting_key"
    assert (remote.upload_directory / "upload.tar.gz").read_bytes() == archive_data
    assert remote.uploaded_files() == ["upload.tar.gz"]


async def test_the_review_of_a_fetched_archive_names_the_bucket_object(
    remote: Remote, archive: Path
) -> None:
    await remote.fetch()

    review = await remote.coordinator.review(OPERATOR, 1)

    assert review.source == "object_storage" and review.object_path == KEY
    assert review.archive_sha256 == sha(archive)
    assert review.manifest is not None


async def test_the_review_says_the_archive_matches_the_record_of_that_key(
    remote: Remote, archive: Path
) -> None:
    add_record(remote.instance, remote.target_id, KEY, sha(archive))
    await remote.fetch()

    review = await remote.coordinator.review(OPERATOR, 1)

    assert (review.record_match, review.recorded_at is not None) == ("matches", True)


async def test_the_review_says_the_archive_differs_from_the_record_of_that_key(
    remote: Remote,
) -> None:
    add_record(remote.instance, remote.target_id, KEY, OTHER_SHA)
    await remote.fetch()

    review = await remote.coordinator.review(OPERATOR, 1)

    assert review.record_match == "differs"


async def test_the_review_says_there_is_no_record_when_this_instance_never_pushed_the_key(
    remote: Remote,
) -> None:
    await remote.fetch()

    review = await remote.coordinator.review(OPERATOR, 1)

    assert (review.record_match, review.recorded_at) == ("none", None)


async def test_a_restore_from_the_bucket_records_the_source_target_and_object_path(
    remote: Remote,
) -> None:
    await remote.fetch()
    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None

    await remote.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(remote.instance))
    progress = await settle(remote.instance, status.restore_id)

    [row] = remote.instance.actions("instance_restored")
    detail = json.loads(row["detail"])
    assert progress.outcome == "restored"
    assert detail["source"] == "object_storage"
    assert detail["object_storage_target_id"] == remote.target_id
    assert detail["object_path"] == KEY


async def test_a_restore_from_the_bucket_that_fails_records_the_source_on_the_failure_row(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: object, **options: object) -> None:
        raise OSError("stage failed")

    monkeypatch.setattr(pipeline_module, "stage_members", broken)
    await remote.fetch()
    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None

    await remote.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(remote.instance))
    await settle(remote.instance, status.restore_id)

    [row] = remote.instance.actions("instance_restore_failed")
    assert json.loads(row["detail"])["source"] == "object_storage"


async def assert_nothing_kept_and_gate_engaged(remote: Remote) -> None:
    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None
    assert (status.stage, remote.instance.gate.phase) == ("awaiting_archive", "engaged")
    assert remote.uploaded_files() == []


async def test_an_object_listed_above_the_cap_is_refused_before_any_byte_is_fetched(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_module, "MAX_ARCHIVE_BYTES", SMALL_CAP)

    with pytest.raises(ValidationFailed) as raised:
        await remote.fetch()

    assert "size limit" in raised.value.fields["archive"]
    assert remote.bucket.object_requests == []
    await assert_nothing_kept_and_gate_engaged(remote)


async def test_an_object_that_turns_out_larger_than_the_cap_is_refused_and_leaves_no_archive(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_module, "MAX_ARCHIVE_BYTES", SMALL_CAP)
    remote.bucket.listed_sizes[KEY] = SMALL_CAP

    await remote.fetch()

    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None and status.fetch_error == MESSAGES["storage_error"]
    await assert_nothing_kept_and_gate_engaged(remote)


async def test_an_object_whose_size_differs_from_the_listing_is_refused_and_leaves_no_archive(
    remote: Remote, archive_data: bytes
) -> None:
    remote.bucket.listed_sizes[KEY] = len(archive_data) + 1

    await remote.fetch()

    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None and status.fetch_error == OBJECT_CHANGED
    await assert_nothing_kept_and_gate_engaged(remote)


async def test_a_body_that_breaks_off_midway_leaves_no_archive_and_the_gate_engaged(
    remote: Remote, archive_data: bytes
) -> None:
    data = archive_data
    remote.bucket.served[KEY] = lambda: httpx.Response(
        200, headers={"content-length": str(len(data))}, stream=Breaking(data[:100])
    )

    await remote.fetch()

    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None and status.fetch_error == MESSAGES["unreachable"]
    await assert_nothing_kept_and_gate_engaged(remote)


async def test_an_object_that_vanished_since_the_listing_is_reported_and_leaves_no_archive(
    remote: Remote,
) -> None:
    remote.bucket.served[KEY] = error_reply(404, "NoSuchKey")

    await remote.fetch()

    status = remote.coordinator.status(OPERATOR, 1)
    assert status is not None and status.fetch_error == MESSAGES["object_missing"]
    await assert_nothing_kept_and_gate_engaged(remote)


async def test_a_fetch_is_refused_when_the_volume_has_too_little_free_space(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    await remote.list_all()
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1))

    with pytest.raises(ValidationFailed) as raised:
        await remote.coordinator.fetch_archive(
            OPERATOR, 1, target_id=remote.target_id, object_path=KEY
        )

    assert "free space" in raised.value.fields["archive"]
    assert remote.bucket.object_requests == []


async def test_a_second_fetch_during_a_fetch_is_refused(
    remote: Remote, archive_data: bytes
) -> None:
    data = archive_data
    release = asyncio.Event()
    remote.bucket.served[KEY] = lambda: httpx.Response(
        200,
        headers={"content-length": str(len(data))},
        stream=Breaking(data[:100], hold=release),
    )
    await remote.list_all()
    await remote.coordinator.fetch_archive(OPERATOR, 1, target_id=remote.target_id, object_path=KEY)

    with pytest.raises(RestoreBusy, match=UPLOAD_IN_PROGRESS):
        await remote.coordinator.fetch_archive(
            OPERATOR, 1, target_id=remote.target_id, object_path=KEY
        )

    await remote.coordinator.cancel(OPERATOR)


async def test_cancelling_during_a_fetch_leaves_no_archive_and_releases_the_gate(
    remote: Remote, archive_data: bytes
) -> None:
    data = archive_data
    remote.bucket.served[KEY] = lambda: httpx.Response(
        200,
        headers={"content-length": str(len(data))},
        stream=Breaking(data[:100], hold=asyncio.Event()),
    )
    await remote.list_all()
    await remote.coordinator.fetch_archive(OPERATOR, 1, target_id=remote.target_id, object_path=KEY)
    directory = remote.upload_directory

    await remote.coordinator.cancel(OPERATOR)

    assert remote.instance.gate.phase == "open"
    assert remote.coordinator.status(OPERATOR, 1) is None
    assert not directory.exists()
    assert not remote.instance.production.held
