"""Opens a configured object storage target and builds the keys the exports and backups use."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import SecretStr

from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.registry import OBJECT_STORAGE_SECRET_ACCESS_KEY
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.object_storage_target import ObjectStorageTargetRepository
from app.object_storage import (
    ListedObject,
    ObjectStorageClient,
    StorageCredentials,
    StorageEndpoint,
    StorageFailure,
    validate_key,
)

__all__ = [
    "BACKUP_KEY_NAME",
    "NANOS_PER_SECOND",
    "NDJSON_KEY_SUFFIX",
    "CredentialUnreadable",
    "ListedObject",
    "ObjectStorageClient",
    "ObjectStorageTargets",
    "OpenedTarget",
    "StorageFailure",
    "activity_log_key",
    "backup_folder",
    "backup_object_key",
    "container_log_folder",
    "container_log_key",
    "service_log_folder",
    "service_log_key",
]

ID_DIGITS: Final = 20
SHORT_ID_CHARS: Final = 12
NANOS_PER_SECOND: Final = 1_000_000_000
_NAME_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")
NO_TASK_FOLDER: Final = "no-task"
KEY_SUFFIX: Final = ".csv.gz"
NDJSON_KEY_SUFFIX: Final = ".ndjson.gz"
BACKUP_KEY_NAME: Final[re.Pattern[str]] = re.compile(r"dockmin-backup-\d{8}T\d{6}Z\.tar\.gz")


class CredentialUnreadable(Exception):
    """The target's stored secret cannot be decrypted."""


@dataclass(frozen=True, slots=True)
class OpenedTarget:
    """A target with its secret opened; nothing here appears in a repr."""

    id: int
    name: str
    endpoint: StorageEndpoint
    credentials: StorageCredentials
    activity_log_prefix: str
    backup_prefix: str
    retention_count: int | None


class ObjectStorageTargets:
    """Reads object storage targets and opens their secrets."""

    def __init__(self, db: Session, vault: Vault) -> None:
        self._targets = ObjectStorageTargetRepository(db)
        self._vault = vault

    async def prefix_of(self, target_id: int) -> tuple[str, str] | None:
        """Return the target's name and activity log prefix without opening its secret."""
        stored = await self._targets.get(target_id)
        return None if stored is None else (stored.name, stored.activity_log_prefix)

    async def backup_prefix_of(self, target_id: int) -> tuple[str, str, int | None] | None:
        """Return the target's name, backup prefix and retention count; the secret stays sealed."""
        stored = await self._targets.get(target_id)
        if stored is None:
            return None
        return stored.name, stored.backup_prefix, stored.retention_count

    async def open(self, target_id: int) -> OpenedTarget | None:
        """Return the target with its secret opened; None when there is no such target.

        Raises CredentialUnreadable when the secret cannot be decrypted.
        """
        stored = await self._targets.get(target_id)
        if stored is None:
            return None
        location = SecretLocation(
            OBJECT_STORAGE_SECRET_ACCESS_KEY.table,
            OBJECT_STORAGE_SECRET_ACCESS_KEY.column,
            stored.id,
        )
        try:
            secret = self._vault.open(stored.secret_access_key_ciphertext, location)
        except DecryptionError:
            raise CredentialUnreadable("the stored secret cannot be read") from None
        return OpenedTarget(
            id=stored.id,
            name=stored.name,
            endpoint=StorageEndpoint(
                endpoint_url=stored.endpoint_url,
                region=stored.region,
                bucket=stored.bucket,
                use_path_style=stored.use_path_style,
                verify_tls=stored.verify_tls,
                ca_bundle=stored.ca_bundle,
            ),
            credentials=StorageCredentials(
                access_key_id=stored.access_key_id, secret_access_key=SecretStr(secret)
            ),
            activity_log_prefix=stored.activity_log_prefix,
            backup_prefix=stored.backup_prefix,
            retention_count=stored.retention_count,
        )


def activity_log_key(
    prefix: str,
    rule_id: int,
    rule_created_at: str,
    first_id: int,
    first_at: str,
    export_format: Literal["csv", "ndjson"] = "csv",
) -> str:
    """Return the object key for the run of events starting at first_id.

    The key depends on the rule, its creation time and the first id alone, so a retry overwrites
    the same object and a later rule that reuses the id writes elsewhere. Raises ValueError when
    the prefix does not form a valid key.
    """
    moment = parse_timestamp(first_at).astimezone(UTC)
    created = parse_timestamp(rule_created_at).astimezone(UTC)
    suffix = NDJSON_KEY_SUFFIX if export_format == "ndjson" else KEY_SUFFIX
    folder = (
        f"rule-{rule_id}-{created:%Y%m%dT%H%M%S}/{moment:%Y/%m/%d}/{first_id:0{ID_DIGITS}d}{suffix}"
    )
    base = prefix.removesuffix("/")
    key = f"{base}/{folder}" if base else folder
    try:
        return validate_key(key)
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None


def container_log_folder(
    prefix: str,
    rule_id: int,
    rule_created_at: str,
    environment_id: int,
    container_name: str,
    container_id: str,
) -> str:
    """Return the folder, without a trailing slash, that holds one container's log objects.

    The folder depends on the rule and its creation time, so a later rule that reuses the id
    writes elsewhere. Raises ValueError when the prefix does not form a valid key.
    """
    created = parse_timestamp(rule_created_at).astimezone(UTC)
    name = _NAME_UNSAFE.sub("_", container_name)
    folder = (
        f"container-logs/rule-{rule_id}-{created:%Y%m%dT%H%M%S}/{environment_id}/"
        f"{name}-{container_id[:SHORT_ID_CHARS]}"
    )
    base = prefix.removesuffix("/")
    key = f"{base}/{folder}" if base else folder
    try:
        return validate_key(key)
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None


def container_log_key(
    prefix: str,
    rule_id: int,
    rule_created_at: str,
    environment_id: int,
    container_name: str,
    container_id: str,
    first_ns: int,
    continued_after: int = 0,
) -> str:
    """Return the object key of the log lines starting at `first_ns` Unix nanoseconds.

    The key depends on the rule, the container and the first line alone, so a retry overwrites
    the same object. `continued_after` is the number of lines already exported at that same
    nanosecond; it joins the name so a run that carries on from the cursor's own timestamp does
    not overwrite the object before it. Raises ValueError when the prefix does not form a valid
    key.
    """
    moment = datetime.fromtimestamp(first_ns // NANOS_PER_SECOND, UTC)
    folder = container_log_folder(
        prefix, rule_id, rule_created_at, environment_id, container_name, container_id
    )
    resumed = f"-{continued_after}" if continued_after else ""
    key = f"{folder}/{moment:%Y/%m/%d}/{first_ns:0{ID_DIGITS}d}{resumed}{NDJSON_KEY_SUFFIX}"
    try:
        return validate_key(key)
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None


def service_log_folder(
    prefix: str,
    rule_id: int,
    rule_created_at: str,
    environment_id: int,
    service_name: str,
    service_id: str,
    task_id: str | None,
) -> str:
    """Return the folder, without a trailing slash, that holds one task's log objects.

    `task_id` None names the folder for lines whose attributes named no task. The folder depends
    on the rule and its creation time, so a later rule that reuses the id writes elsewhere. Raises
    ValueError when the prefix does not form a valid key.
    """
    created = parse_timestamp(rule_created_at).astimezone(UTC)
    name = _NAME_UNSAFE.sub("_", service_name)
    folder = (
        f"service-logs/rule-{rule_id}-{created:%Y%m%dT%H%M%S}/{environment_id}/"
        f"{name}-{service_id[:SHORT_ID_CHARS]}/{task_id or NO_TASK_FOLDER}"
    )
    base = prefix.removesuffix("/")
    key = f"{base}/{folder}" if base else folder
    try:
        return validate_key(key)
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None


def service_log_key(
    prefix: str,
    rule_id: int,
    rule_created_at: str,
    environment_id: int,
    service_name: str,
    service_id: str,
    task_id: str | None,
    first_ns: int,
    continued_after: int = 0,
) -> str:
    """Return the object key of a task's log lines starting at `first_ns` Unix nanoseconds.

    The key depends on the rule, the task and the first line alone, so a retry overwrites the same
    object. `continued_after` is the number of lines already exported at that same nanosecond;
    it joins the name so a run that carries on from the cursor's own timestamp does not overwrite
    the object before it. Raises ValueError when the prefix does not form a valid key.
    """
    moment = datetime.fromtimestamp(first_ns // NANOS_PER_SECOND, UTC)
    folder = service_log_folder(
        prefix, rule_id, rule_created_at, environment_id, service_name, service_id, task_id
    )
    resumed = f"-{continued_after}" if continued_after else ""
    key = f"{folder}/{moment:%Y/%m/%d}/{first_ns:0{ID_DIGITS}d}{resumed}{NDJSON_KEY_SUFFIX}"
    try:
        return validate_key(key)
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None


def backup_folder(prefix: str, rule_id: int, rule_created_at: str) -> str:
    """Return the folder, without a trailing slash, that holds one rule's archives.

    The folder depends on the rule and its creation time, so a later rule that reuses the id
    writes elsewhere. Raises ValueError when the prefix does not form a valid key.
    """
    created = parse_timestamp(rule_created_at).astimezone(UTC)
    folder = f"rule-{rule_id}-{created:%Y%m%dT%H%M%S}"
    base = prefix.removesuffix("/")
    key = f"{base}/{folder}" if base else folder
    try:
        return validate_key(key)
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None


def backup_object_key(prefix: str, rule_id: int, rule_created_at: str, at: datetime) -> str:
    """Return the object key of the archive a rule's run makes at the given time.

    Raises ValueError when the prefix does not form a valid key.
    """
    name = f"dockmin-backup-{at.astimezone(UTC):%Y%m%dT%H%M%SZ}.tar.gz"
    try:
        return validate_key(f"{backup_folder(prefix, rule_id, rule_created_at)}/{name}")
    except StorageFailure:
        raise ValueError("the prefix is not a valid key prefix") from None
