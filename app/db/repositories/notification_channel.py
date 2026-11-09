"""Repository for notification channels, their event type subscriptions and environment scope."""

import json
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.db.engine import DatabaseError
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors

ChannelKind = Literal["webhook", "ntfy", "gotify", "discord", "slack", "teams", "email"]
EnvironmentScope = Literal["all", "listed"]

MAX_CHANNELS: Final = 200
MAX_LAST_ERROR: Final = 512
MAX_SUBSCRIPTIONS: Final = 64
MAX_SCOPED_ENVIRONMENTS: Final = 10_000

_COLUMNS = (
    "id, name, kind, settings, secrets_ciphertext, is_enabled, environment_scope, "
    "last_success_at, last_error_at, last_error, created_at, updated_at"
)
_SELECT = f"SELECT {_COLUMNS} FROM notification_channel"


@dataclass(frozen=True, slots=True)
class NotificationChannel:
    """A channel as stored; `secrets_ciphertext` is sealed text and stays out of repr."""

    id: int
    name: str
    kind: ChannelKind
    settings: dict[str, Any]
    secrets_ciphertext: str | None = field(repr=False)
    is_enabled: bool
    environment_scope: EnvironmentScope
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


class NotificationChannelRepository(ClockedRepository[NotificationChannel]):
    """Reads and writes channels; the secret column is opaque sealed text to this class.

    A secret is sealed against the channel's id, so a new channel is created without one and the
    caller stores the sealed text with set_secrets in the same transaction.
    """

    def _map_row(self, row: sqlite3.Row) -> NotificationChannel:
        return NotificationChannel(
            id=int(row["id"]),
            name=str(row["name"]),
            kind=row["kind"],
            settings=json.loads(row["settings"]),
            secrets_ciphertext=row["secrets_ciphertext"],
            is_enabled=bool(row["is_enabled"]),
            environment_scope=row["environment_scope"],
            last_success_at=row["last_success_at"],
            last_error_at=row["last_error_at"],
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def get(self, channel_id: int) -> NotificationChannel | None:
        """Fetch a channel by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (channel_id,))

    async def exists(self, channel_id: int) -> bool:
        """Report whether a channel with this id is stored."""
        row = await self._session.fetch_one(
            "SELECT 1 AS found FROM notification_channel WHERE id = ?", (channel_id,)
        )
        return row is not None

    async def list_channels(self, *, limit: int = MAX_CHANNELS) -> list[NotificationChannel]:
        """List up to limit channels, by name."""
        rows = await self._session.fetch_limited(
            f"{_SELECT} ORDER BY lower(name), id LIMIT ?", (limit,), limit=limit
        )
        return [self._map_row(row) for row in rows]

    async def count(self) -> int:
        """Count every channel."""
        row = await self._session.fetch_one("SELECT count(*) AS total FROM notification_channel")
        return 0 if row is None else int(row["total"])

    async def create(
        self,
        *,
        name: str,
        kind: ChannelKind,
        settings: dict[str, Any],
        is_enabled: bool,
        environment_scope: EnvironmentScope,
    ) -> NotificationChannel:
        """Insert a channel with no secret. DuplicateError on a name already taken."""
        now = self._now()
        with constraints_as_domain_errors("notification channel"):
            result = await self._execute(
                "INSERT INTO notification_channel (name, kind, settings, is_enabled, "
                "environment_scope, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, kind, json.dumps(settings), int(is_enabled), environment_scope, now, now),
            )
        return await self._created(result, self.get)

    async def update(
        self,
        channel_id: int,
        *,
        name: str,
        settings: dict[str, Any],
        is_enabled: bool,
        environment_scope: EnvironmentScope,
    ) -> bool:
        """Change a channel's editable columns; False when it does not exist.

        DuplicateError on a name already taken.
        """
        with constraints_as_domain_errors("notification channel"):
            result = await self._execute(
                "UPDATE notification_channel SET name = ?, settings = ?, is_enabled = ?, "
                "environment_scope = ?, updated_at = ? WHERE id = ?",
                (
                    name,
                    json.dumps(settings),
                    int(is_enabled),
                    environment_scope,
                    self._now(),
                    channel_id,
                ),
            )
        return result.rowcount > 0

    async def set_secrets(self, channel_id: int, secrets_ciphertext: str | None) -> bool:
        """Store the sealed secrets (None clears them); False when the channel does not exist."""
        result = await self._execute(
            "UPDATE notification_channel SET secrets_ciphertext = ?, updated_at = ? WHERE id = ?",
            (secrets_ciphertext, self._now(), channel_id),
        )
        return result.rowcount > 0

    async def delete(self, channel_id: int) -> bool:
        """Delete a channel with its subscriptions, scope and queued deliveries."""
        result = await self._execute("DELETE FROM notification_channel WHERE id = ?", (channel_id,))
        return result.rowcount > 0

    async def record_success(self, channel_id: int) -> None:
        """Stamp the time of the last delivery that succeeded."""
        await self._execute(
            "UPDATE notification_channel SET last_success_at = ? WHERE id = ?",
            (self._now(), channel_id),
        )

    async def record_failure(self, channel_id: int, error: str) -> None:
        """Store the last delivery error, cut to the column's limit, and when it happened."""
        await self._execute(
            "UPDATE notification_channel SET last_error_at = ?, last_error = ? WHERE id = ?",
            (self._now(), error[:MAX_LAST_ERROR], channel_id),
        )

    async def subscriptions(self, channel_id: int) -> frozenset[str]:
        """Return the event type keys a channel subscribes to."""
        found = await self.subscriptions_of((channel_id,))
        return found.get(channel_id, frozenset())

    async def subscriptions_of(self, channel_ids: Collection[int]) -> dict[int, frozenset[str]]:
        """Return the subscribed keys of each channel that has any."""
        if not channel_ids:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT channel_id, event_type FROM notification_subscription "
            "WHERE channel_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(channel_ids)),),
            limit=MAX_SUBSCRIPTIONS * len(channel_ids),
        )
        grouped: dict[int, set[str]] = {}
        for row in rows:
            grouped.setdefault(int(row["channel_id"]), set()).add(str(row["event_type"]))
        return {channel_id: frozenset(keys) for channel_id, keys in grouped.items()}

    async def replace_subscriptions(self, channel_id: int, event_types: Iterable[str]) -> None:
        """Make the channel's subscriptions exactly these keys; runs inside a transaction.

        MissingReferenceError when the channel does not exist.
        """
        self._require_transaction()
        keys = sorted(set(event_types))
        if len(keys) > MAX_SUBSCRIPTIONS:
            raise ValueError(f"a channel subscribes to at most {MAX_SUBSCRIPTIONS} event types")
        await self._execute(
            "DELETE FROM notification_subscription WHERE channel_id = ?", (channel_id,)
        )
        with constraints_as_domain_errors("notification channel"):
            await self._execute(
                "INSERT INTO notification_subscription (channel_id, event_type) "
                "SELECT ?, value FROM json_each(?)",
                (channel_id, json.dumps(keys)),
            )

    async def environment_ids(self, channel_id: int) -> frozenset[int]:
        """Return the environments a channel lists; they apply only when its scope is listed."""
        found = await self.environment_ids_of((channel_id,))
        return found.get(channel_id, frozenset())

    async def environment_ids_of(self, channel_ids: Collection[int]) -> dict[int, frozenset[int]]:
        """Return the listed environments of each channel that has any."""
        if not channel_ids:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT channel_id, environment_id FROM notification_channel_environment "
            "WHERE channel_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(channel_ids)),),
            limit=MAX_SCOPED_ENVIRONMENTS * len(channel_ids),
        )
        grouped: dict[int, set[int]] = {}
        for row in rows:
            grouped.setdefault(int(row["channel_id"]), set()).add(int(row["environment_id"]))
        return {channel_id: frozenset(ids) for channel_id, ids in grouped.items()}

    async def replace_environments(self, channel_id: int, environment_ids: Iterable[int]) -> None:
        """Make the channel's listed environments exactly these; runs inside a transaction.

        MissingReferenceError when the channel or an environment does not exist.
        """
        self._require_transaction()
        ids = sorted(set(environment_ids))
        if len(ids) > MAX_SCOPED_ENVIRONMENTS:
            raise ValueError(f"a channel lists at most {MAX_SCOPED_ENVIRONMENTS} environments")
        await self._execute(
            "DELETE FROM notification_channel_environment WHERE channel_id = ?", (channel_id,)
        )
        with constraints_as_domain_errors("notification channel"):
            await self._execute(
                "INSERT INTO notification_channel_environment (channel_id, environment_id) "
                "SELECT ?, value FROM json_each(?)",
                (channel_id, json.dumps(ids)),
            )

    def _require_transaction(self) -> None:
        if not self._session.in_transaction:
            raise DatabaseError("a replacement of a channel's rows runs inside a transaction")
