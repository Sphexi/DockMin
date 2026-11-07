"""backup-syncer against a real S3-compatible server; skipped unless DOCKMIN_TEST_S3_* is set.

The credential needs Read, Write and List on the bucket; each test removes what it stored.
"""

import hashlib
import os
import secrets
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.backup import DATABASE_NAME, read_manifest
from app.config import Settings
from app.crypto.keys import MasterKeys
from app.crypto.startup import establish_vault
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.backup_record import BackupRecordRepository
from app.db.repositories.clocked import format_timestamp
from app.logging import Scrubber
from app.object_storage import (
    ObjectStorageClient,
    StorageCredentials,
    StorageEndpoint,
)
from app.object_storage.addressing import resolve
from app.registry_client import sigv4
from app.scheduler.setup import SchedulerRuntime, build_scheduler_runtime
from app.services import assembly
from app.services.activity import ActivityRecorder
from app.services.authorisation import UnavailableInspector
from app.services.backups import BackupProduction
from app.services.object_locks import ObjectLockRegistry
from app.services.object_storage_admin import ObjectStorageTargetService, TargetInput
from app.subsystems.backup_syncer import BackupSyncer
from app.subsystems.base import SubsystemDeps
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME
from tests.scheduler.support import Clock

VARIABLE = "DOCKMIN_TEST_S3_"
REQUIRED = ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
SERVER = {
    name: os.environ.get(f"{VARIABLE}{name}", "") for name in (*REQUIRED, "VIRTUAL_HOST_ENDPOINT")
}
SYNCER = "backup-syncer"
INTERVAL = timedelta(hours=1)
EARLIER_RUN = timedelta(minutes=1)
KEEP_ONE = "1"
REQUEST_TIMEOUT = 30.0
ADMIN = Principal(user_id=ADMIN_ID, username=ADMIN_USERNAME, role="administrator")

pytestmark = pytest.mark.skipif(
    not all(SERVER[name] for name in REQUIRED),
    reason=f"set {VARIABLE}{{{','.join(REQUIRED)}}} to run against an S3-compatible server",
)

ADDRESSING = [
    pytest.param((SERVER["ENDPOINT"], True), id="path-style"),
    pytest.param(
        (SERVER["VIRTUAL_HOST_ENDPOINT"], False),
        id="virtual-host",
        marks=pytest.mark.skipif(
            not SERVER["VIRTUAL_HOST_ENDPOINT"],
            reason=f"set {VARIABLE}VIRTUAL_HOST_ENDPOINT to run the virtual-host case",
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class Bucket:
    """The configured bucket under a prefix of this test's own."""

    endpoint: StorageEndpoint
    credentials: StorageCredentials
    prefix: str

    def client(self) -> ObjectStorageClient:
        return ObjectStorageClient(self.endpoint, self.credentials)


@dataclass(frozen=True, slots=True)
class Instance:
    """A seeded instance whose scheduler is driven by hand on a fake clock."""

    database: Database
    vault: Vault
    clock: Clock
    auth: AuthRuntime
    scheduler: SchedulerRuntime
    now: datetime


@dataclass(frozen=True, slots=True)
class Synced:
    """A backup-syncer rule that has stored one archive and is due for its scheduled run."""

    folder: str
    first_key: str
    second_key: str


@pytest.fixture(params=ADDRESSING)
async def bucket(request: pytest.FixtureRequest) -> AsyncIterator[Bucket]:
    """Return the bucket for one addressing style, and remove the test's objects afterwards."""
    endpoint_url, path_style = request.param
    built = Bucket(
        endpoint=StorageEndpoint(
            endpoint_url=endpoint_url,
            region=SERVER["REGION"],
            bucket=SERVER["BUCKET"],
            use_path_style=path_style,
            verify_tls=True,
            ca_bundle=None,
        ),
        credentials=StorageCredentials(
            access_key_id=SERVER["ACCESS_KEY_ID"],
            secret_access_key=SecretStr(SERVER["SECRET_ACCESS_KEY"]),
        ),
        prefix=f"dockmin-test-{secrets.token_hex(6)}",
    )
    yield built
    async with built.client() as client:
        listing = await client.list_objects(f"{built.prefix}/")
        for listed in listing.objects:
            await client.delete_object(listed.key)


@pytest.fixture
async def instance(settings: Settings, provisioned_template: Path) -> AsyncIterator[Instance]:
    """Seed an instance holding an administrator, with the syncer registered on its scheduler.

    The clock starts one interval before the real time, so runs sign their requests at the time
    the server keeps.
    """
    shutil.copyfile(provisioned_template, settings.db_path)
    database = Database(settings.db_path)
    vault = await establish_vault(database, MasterKeys.from_settings(settings), Scrubber())
    now = datetime.now(UTC).replace(microsecond=0)
    clock = Clock(now - INTERVAL)
    locks = ObjectLockRegistry(clock)
    scheduler = build_scheduler_runtime(
        database=database, settings=settings, clock=clock, locks=locks
    )
    auth = AuthRuntime.from_settings(settings)
    auth.clock = clock
    engine: Any = SimpleNamespace(
        vault=vault, locks=locks, refresher=SimpleNamespace(poke=lambda _id: None)
    )
    deps = SubsystemDeps(
        database=database,
        engine=engine,
        auth=auth,
        settings=settings,
        suppressions=scheduler.suppressions,
        backup_production=BackupProduction(settings.data_dir),
    )
    scheduler.handlers.register(BackupSyncer(deps))
    yield Instance(database, vault, clock, auth, scheduler, now)
    await scheduler.scheduler.aclose()
    await database.aclose()


@pytest.fixture
async def synced(instance: Instance, bucket: Bucket) -> Synced:
    """Write a target keeping one archive and an hourly rule, then run the rule once by hand."""
    async with instance.database.session() as db:
        targets = ObjectStorageTargetService(
            db, instance.vault, instance.clock, ActivityRecorder(db, instance.clock)
        )
        target_id = await targets.create(ADMIN, target_form(bucket))
    async with instance.database.session() as db:
        rules = job_services(db, instance).rules
        rule = await rules.create(
            ADMIN,
            subsystem=SYNCER,
            name="hourly",
            environment_id=None,
            target_selector="",
            parameters={"object_storage_target_id": target_id},
            schedule_kind="interval",
            schedule_expression=str(int(INTERVAL.total_seconds())),
        )
    async with instance.database.session() as db:
        await job_services(db, instance).rules.enable(ADMIN, rule.id)
    instance.clock.set(instance.now - EARLIER_RUN)
    async with instance.database.session() as db:
        await job_services(db, instance).rules.run_now(ADMIN, rule.id)
    await instance.scheduler.scheduler.idle()
    instance.clock.set(instance.now)
    folder = f"{bucket.prefix}/rule-{rule.id}-{instance.now - INTERVAL:%Y%m%dT%H%M%S}/"
    return Synced(
        folder=folder,
        first_key=f"{folder}{archive_name(instance.now - EARLIER_RUN)}",
        second_key=f"{folder}{archive_name(instance.now)}",
    )


def target_form(bucket: Bucket) -> TargetInput:
    return TargetInput(
        name="integration",
        endpoint_url=bucket.endpoint.endpoint_url,
        bucket=bucket.endpoint.bucket,
        access_key_id=bucket.credentials.access_key_id,
        backup_prefix=bucket.prefix,
        activity_log_prefix=f"{bucket.prefix}-activity",
        region=bucket.endpoint.region,
        secret_access_key=bucket.credentials.secret_access_key,
        use_path_style=bucket.endpoint.use_path_style,
        retention_count=KEEP_ONE,
        confirm_retention=True,
    )


def job_services(db: Any, instance: Instance) -> assembly.JobServices:
    activity = ActivityRecorder(db, instance.clock)
    access = assembly.build_access_services(db, instance.auth, UnavailableInspector(), activity)
    return assembly.build_job_services(db, instance.auth, access, instance.scheduler, activity)


def archive_name(moment: datetime) -> str:
    return f"dockmin-backup-{moment:%Y%m%dT%H%M%SZ}.tar.gz"


async def listed_keys(bucket: Bucket, folder: str) -> list[str]:
    async with bucket.client() as client:
        listing = await client.list_objects(folder)
    return [listed.key for listed in listing.objects]


async def download(bucket: Bucket, key: str) -> bytes:
    """GET one object with a request signed the way the product signs its own."""
    target = resolve(
        endpoint_url=bucket.endpoint.endpoint_url,
        bucket=bucket.endpoint.bucket,
        region=bucket.endpoint.region,
        use_path_style=bucket.endpoint.use_path_style,
    )
    path = target.path(key)
    headers = sigv4.sign_request(
        method="GET",
        host=target.host,
        path=path,
        headers={},
        body=b"",
        access_key_id=bucket.credentials.access_key_id,
        secret_access_key=bucket.credentials.secret_access_key,
        region=bucket.endpoint.region,
        service="s3",
        moment=datetime.now(UTC),
        content_sha256_header=True,
    )
    async with httpx.AsyncClient(trust_env=False, timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(target.url(path), headers={**headers, "Host": target.host})
    response.raise_for_status()
    return response.content


def runs(instance: Instance) -> list[tuple[str, str]]:
    connection = instance.database.connect()
    try:
        found = connection.execute("SELECT trigger, outcome FROM job_run ORDER BY id").fetchall()
    finally:
        connection.close()
    return [(row["trigger"], row["outcome"]) for row in found]


def pruned_paths(instance: Instance) -> list[str]:
    connection = instance.database.connect()
    try:
        found = connection.execute(
            "SELECT json_extract(detail, '$.deleted_paths') AS paths FROM activity_event "
            "WHERE action = 'backup_remote_pruned' AND result = 'success' ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [row["paths"] for row in found]


async def recorded_sha256(instance: Instance, key: str) -> list[str | None]:
    async with instance.database.session() as db:
        page = await BackupRecordRepository(db, instance.clock).page_backup_records(limit=10)
    return [record.archive_sha256 for record in page.items if record.destination_key == key]


async def test_the_scheduled_run_stores_a_readable_archive_and_keeps_only_the_newest(
    instance: Instance, synced: Synced, bucket: Bucket, tmp_path: Path
) -> None:
    started = await instance.scheduler.scheduler.tick()
    await instance.scheduler.scheduler.idle()

    stored = await download(bucket, synced.second_key)
    archive = tmp_path / "downloaded.tar.gz"
    archive.write_bytes(stored)
    manifest, _ = read_manifest(archive)
    assert started == 1
    assert runs(instance) == [("manual", "success"), ("schedule", "success")]
    assert await listed_keys(bucket, synced.folder) == [synced.second_key]
    assert pruned_paths(instance) == [synced.first_key.removeprefix(synced.folder)]
    assert await recorded_sha256(instance, synced.second_key) == [
        hashlib.sha256(stored).hexdigest()
    ]
    assert manifest.created_at == format_timestamp(instance.now)
    assert manifest.key_fingerprint == instance.vault.key_fingerprint()
    assert manifest.members[0].name == DATABASE_NAME
