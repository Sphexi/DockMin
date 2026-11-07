"""Repository for the user table."""

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

from app.db.repositories.base import like_prefix, row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import (
    FOREIGN_KEY_PREFIX,
    UserHasActivityError,
    constraints_as_domain_errors,
)
from app.db.repositories.pagination import Page

Role = Literal["administrator", "standard"]

_SELECT = (
    "SELECT id, username, password_hash, role, is_enabled, created_at, last_login_at, "
    "external_identity FROM user"
)


@dataclass(frozen=True, slots=True)
class User:
    """An account; password_hash is kept out of repr so it cannot reach a log line."""

    id: int
    username: str
    password_hash: str = field(repr=False)
    role: Role
    is_enabled: bool
    created_at: str
    last_login_at: str | None
    external_identity: str | None = None


class UserRepository(ClockedRepository[User]):
    """Reads and writes users; accounts are retired with disable, not delete."""

    def _map_row(self, row: sqlite3.Row) -> User:
        return row_as(User, {**dict(row), "is_enabled": bool(row["is_enabled"])})

    async def create(
        self,
        username: str,
        password_hash: str,
        role: Role,
        *,
        external_identity: str | None = None,
    ) -> User:
        """Insert an enabled user; raise DuplicateError if the username or identity is taken."""
        with constraints_as_domain_errors("user"):
            result = await self._execute(
                "INSERT INTO user (username, password_hash, role, is_enabled, created_at, "
                "external_identity) VALUES (?, ?, ?, 1, ?, ?)",
                (username, password_hash, role, self._now(), external_identity),
            )
        return await self._created(result, self.get)

    async def get(self, user_id: int) -> User | None:
        """Fetch a user by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (user_id,))

    async def get_by_username(self, username: str) -> User | None:
        """Fetch a user by username, ignoring ASCII case, so a lookup of Admin finds admin."""
        return await self._one(f"{_SELECT} WHERE username = ? COLLATE NOCASE", (username,))

    async def get_by_external_identity(self, identity: str) -> User | None:
        """Fetch the user linked to an access platform identity, ignoring ASCII case."""
        return await self._one(f"{_SELECT} WHERE external_identity = ?", (identity,))

    async def usernames(self, user_ids: Collection[int]) -> dict[int, str]:
        """Map each id that names an existing user to its current username."""
        distinct = sorted(set(user_ids))
        if not distinct:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT id, username FROM user WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(distinct),),
            limit=len(distinct),
        )
        return {int(row["id"]): str(row["username"]) for row in rows}

    async def list_page(
        self, *, limit: int, cursor: str | None = None, name_prefix: str | None = None
    ) -> Page[User]:
        """Return one page of users ordered by username, ignoring case; `name_prefix` narrows it."""
        if name_prefix is None:
            return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="username")
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="username",
            where="username LIKE ? ESCAPE '\\'",
            params=(like_prefix(name_prefix),),
        )

    async def count(self) -> int:
        """Count every user."""
        row = await self._session.fetch_one("SELECT COUNT(*) AS total FROM user")
        return 0 if row is None else int(row["total"])

    async def count_administrators(self, *, enabled_only: bool = False) -> int:
        """Count administrators, optionally only those who can currently sign in."""
        sql = "SELECT COUNT(*) AS total FROM user WHERE role = 'administrator'"
        if enabled_only:
            sql += " AND is_enabled = 1"
        row = await self._session.fetch_one(sql)
        return 0 if row is None else int(row["total"])

    async def set_username(self, user_id: int, username: str) -> bool:
        """Rename a user; raise DuplicateError if the name is taken in any case."""
        with constraints_as_domain_errors("user"):
            result = await self._execute(
                "UPDATE user SET username = ? WHERE id = ?", (username, user_id)
            )
        return result.rowcount > 0

    async def set_external_identity(self, user_id: int, identity: str | None) -> bool:
        """Link or unlink an access platform identity; raise DuplicateError if it is taken."""
        with constraints_as_domain_errors("user"):
            result = await self._execute(
                "UPDATE user SET external_identity = ? WHERE id = ?", (identity, user_id)
            )
        return result.rowcount > 0

    async def set_role(self, user_id: int, role: Role) -> bool:
        """Change a user's role; return False if the user does not exist."""
        result = await self._execute("UPDATE user SET role = ? WHERE id = ?", (role, user_id))
        return result.rowcount > 0

    async def set_password_hash(self, user_id: int, password_hash: str) -> bool:
        """Replace a user's password hash; return False if the user does not exist."""
        result = await self._execute(
            "UPDATE user SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )
        return result.rowcount > 0

    async def record_login(self, user_id: int) -> bool:
        """Stamp last_login_at with the current time; return False if the user does not exist."""
        result = await self._execute(
            "UPDATE user SET last_login_at = ? WHERE id = ?", (self._now(), user_id)
        )
        return result.rowcount > 0

    async def disable(self, user_id: int) -> bool:
        """Disable an account; return False if the user does not exist."""
        return await self._set_enabled(user_id, enabled=False)

    async def enable(self, user_id: int) -> bool:
        """Re-enable an account; return False if the user does not exist."""
        return await self._set_enabled(user_id, enabled=True)

    async def delete(self, user_id: int) -> bool:
        """Delete a user; raise UserHasActivityError if the user has ever acted."""
        try:
            result = await self._execute("DELETE FROM user WHERE id = ?", (user_id,))
        except sqlite3.IntegrityError as exc:
            # activity_event.actor_user_id is the only RESTRICT reference to user.
            if str(exc).startswith(FOREIGN_KEY_PREFIX):
                raise UserHasActivityError(
                    "user has recorded activity and can only be disabled"
                ) from exc
            raise
        return result.rowcount > 0

    async def _set_enabled(self, user_id: int, *, enabled: bool) -> bool:
        result = await self._execute(
            "UPDATE user SET is_enabled = ? WHERE id = ?", (int(enabled), user_id)
        )
        return result.rowcount > 0
