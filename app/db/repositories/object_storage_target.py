"""Repositories for object_storage_target: reads, the referencing rules, and the only writer."""

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.crypto.registry import OBJECT_STORAGE_SECRET_ACCESS_KEY
from app.db.repositories.base import Repository
from app.db.repositories.clocked import format_timestamp, parse_timestamp
from app.db.repositories.encrypted import EncryptedStr, SecretRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

_SECRET = EncryptedStr(OBJECT_STORAGE_SECRET_ACCESS_KEY)
_TABLE = "object_storage_target"
_SELECT = (
    "SELECT id, name, endpoint_url, region, bucket, access_key_id, secret_access_key_ciphertext, "
    "use_path_style, verify_tls, ca_bundle, backup_prefix, activity_log_prefix, retention_count, "
    "created_at, updated_at FROM object_storage_target"
)
_REFERENCING_SUBSYSTEMS = ("activity-log-exporter", "backup-syncer", "container-log-exporter")


@dataclass(frozen=True, slots=True)
class StoredObjectStorageTarget:
    """A configured bucket as stored; the secret stays sealed and out of repr."""

    id: int
    name: str
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key_ciphertext: str = field(repr=False)
    use_path_style: bool
    verify_tls: bool
    ca_bundle: str | None
    backup_prefix: str
    activity_log_prefix: str
    retention_count: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TargetFields:
    """The columns an administrator edits, already validated; the secret travels separately."""

    name: str
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    use_path_style: bool
    verify_tls: bool
    ca_bundle: str | None
    backup_prefix: str
    activity_log_prefix: str
    retention_count: int | None


@dataclass(frozen=True, slots=True)
class RuleReference:
    """An automation rule whose parameters name a target."""

    rule_id: int
    name: str
    subsystem: str


def _map_target(row: sqlite3.Row) -> StoredObjectStorageTarget:
    return StoredObjectStorageTarget(
        id=int(row["id"]),
        name=str(row["name"]),
        endpoint_url=str(row["endpoint_url"]),
        region=str(row["region"]),
        bucket=str(row["bucket"]),
        access_key_id=str(row["access_key_id"]),
        secret_access_key_ciphertext=str(row["secret_access_key_ciphertext"]),
        use_path_style=bool(row["use_path_style"]),
        verify_tls=bool(row["verify_tls"]),
        ca_bundle=row["ca_bundle"],
        backup_prefix=str(row["backup_prefix"]),
        activity_log_prefix=str(row["activity_log_prefix"]),
        retention_count=row["retention_count"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _plain_values(fields: TargetFields) -> dict[str, Any]:
    return {
        "name": fields.name,
        "endpoint_url": fields.endpoint_url,
        "region": fields.region,
        "bucket": fields.bucket,
        "access_key_id": fields.access_key_id,
        "use_path_style": int(fields.use_path_style),
        "verify_tls": int(fields.verify_tls),
        "ca_bundle": fields.ca_bundle,
        "backup_prefix": fields.backup_prefix,
        "activity_log_prefix": fields.activity_log_prefix,
        "retention_count": fields.retention_count,
    }


def _required_secret(secret_access_key: str) -> str:
    if not secret_access_key:
        raise ValueError("a target needs a secret access key")
    return secret_access_key


class ObjectStorageTargetRepository(Repository[StoredObjectStorageTarget]):
    """Reads object storage targets and the rules that name them."""

    def _map_row(self, row: sqlite3.Row) -> StoredObjectStorageTarget:
        return _map_target(row)

    async def get(self, target_id: int) -> StoredObjectStorageTarget | None:
        """Fetch a target by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (target_id,))

    async def summaries(self, *, limit: int) -> list[tuple[int, str]]:
        """List (id, name) of up to limit targets, by name."""
        rows = await self._session.fetch_limited(
            "SELECT id, name FROM object_storage_target ORDER BY name, id LIMIT ?",
            (limit,),
            limit=limit,
        )
        return [(int(row["id"]), str(row["name"])) for row in rows]

    async def page_targets(
        self, *, limit: int, cursor: str | None = None
    ) -> Page[StoredObjectStorageTarget]:
        """Return one page of targets, by name."""
        return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="name")

    async def count_targets(self) -> int:
        """Count every target."""
        row = await self._session.fetch_one("SELECT COUNT(*) AS total FROM object_storage_target")
        return 0 if row is None else int(row["total"])

    async def rules_referencing(self, target_id: int, *, limit: int) -> list[RuleReference]:
        """List up to limit rules, by name, whose parameters name the target."""
        marks = ", ".join("?" * len(_REFERENCING_SUBSYSTEMS))
        rows = await self._session.fetch_limited(
            "SELECT id, name, subsystem FROM automation_rule "
            f"WHERE subsystem IN ({marks}) "
            "AND json_extract(parameters, '$.object_storage_target_id') = ? "
            "ORDER BY name, id LIMIT ?",
            (*_REFERENCING_SUBSYSTEMS, target_id, limit),
            limit=limit,
        )
        return [
            RuleReference(
                rule_id=int(row["id"]), name=str(row["name"]), subsystem=str(row["subsystem"])
            )
            for row in rows
        ]


class ObjectStorageTargetStore(SecretRepository[StoredObjectStorageTarget]):
    """Writes object storage targets; the only code that seals their secret."""

    _secret_columns = (_SECRET,)

    def _map_row(self, row: sqlite3.Row) -> StoredObjectStorageTarget:
        return _map_target(row)

    async def add_target(self, fields: TargetFields, *, secret_access_key: str) -> int:
        """Insert a target and seal its secret; return its id. DuplicateError on a taken name."""
        values = _plain_values(fields)
        now = self._now()
        values["created_at"] = now
        values["updated_at"] = now
        with constraints_as_domain_errors("object storage target"):
            return await self._insert_with_secrets(
                _TABLE, values, {_SECRET: _required_secret(secret_access_key)}
            )

    async def change_target(
        self,
        target_id: int,
        fields: TargetFields,
        *,
        secret_access_key: str | None,
        expected_updated_at: str,
    ) -> bool:
        """Change a target if it is unchanged since expected_updated_at; None keeps the secret.

        False when the target is gone or was changed meanwhile. DuplicateError on a taken name.
        """
        values = _plain_values(fields)
        values["updated_at"] = self._next_stamp(expected_updated_at)
        secrets = (
            {} if secret_access_key is None else {_SECRET: _required_secret(secret_access_key)}
        )
        with constraints_as_domain_errors("object storage target"):
            return await self._update_with_secrets(
                _TABLE, target_id, values, secrets, expect={"updated_at": expected_updated_at}
            )

    async def remove_target(self, target_id: int, *, expected_name: str) -> bool:
        """Delete a target only if its name is exactly expected_name; False otherwise."""
        result = await self._execute(
            "DELETE FROM object_storage_target WHERE id = ? AND name = ? COLLATE BINARY",
            (target_id, expected_name),
        )
        return result.rowcount > 0

    def _next_stamp(self, expected_updated_at: str) -> str:
        # Stamps have one-second resolution; a stale expected_updated_at must never match again.
        now = self._now()
        if now > expected_updated_at:
            return now
        return format_timestamp(parse_timestamp(expected_updated_at) + timedelta(seconds=1))
