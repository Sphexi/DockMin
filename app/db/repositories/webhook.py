"""Repository for the webhook table: the token hash, call bookkeeping and the calls a run takes."""

import sqlite3
from dataclasses import dataclass, field
from typing import Final, Literal

from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors

type RevokeReason = Literal["manual", "account_recovery"]

MAX_PENDING_CALLS: Final = 1_000_000
MAX_ADDRESS_CHARS: Final = 64
_COLUMNS = (
    "rule_id, token_prefix, token_hash, issued_at, revoked_at, revoked_reason, "
    "last_called_at, last_call_address, pending_calls"
)
_SELECT = f"SELECT {_COLUMNS} FROM webhook"


@dataclass(frozen=True, slots=True)
class PendingCalls:
    """The calls waiting on a webhook and the address of the last caller."""

    calls: int
    caller_address: str | None


@dataclass(frozen=True, slots=True)
class Webhook:
    """A webhook row; token_hash is kept out of repr and is None once the token is revoked."""

    rule_id: int
    token_prefix: str
    token_hash: str | None = field(repr=False)
    issued_at: str
    revoked_at: str | None
    revoked_reason: RevokeReason | None
    last_called_at: str | None
    last_call_address: str | None
    pending_calls: int

    @property
    def is_revoked(self) -> bool:
        """Report whether the token has been revoked."""
        return self.token_hash is None


class WebhookRepository(ClockedRepository[Webhook]):
    """Reads and writes webhook rows."""

    def _map_row(self, row: sqlite3.Row) -> Webhook:
        return Webhook(
            rule_id=row["rule_id"],
            token_prefix=row["token_prefix"],
            token_hash=row["token_hash"],
            issued_at=row["issued_at"],
            revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"],
            last_called_at=row["last_called_at"],
            last_call_address=row["last_call_address"],
            pending_calls=row["pending_calls"],
        )

    async def get(self, rule_id: int) -> Webhook | None:
        """Fetch a rule's webhook."""
        return await self._one(f"{_SELECT} WHERE rule_id = ?", (rule_id,))

    async def get_by_prefix(self, prefix: str) -> Webhook | None:
        """Fetch the webhook a presented token's prefix names."""
        return await self._one(f"{_SELECT} WHERE token_prefix = ?", (prefix,))

    async def create(self, rule_id: int, prefix: str, token_hash: str) -> Webhook:
        """Insert a rule's webhook; raise DuplicateError on a taken prefix."""
        with constraints_as_domain_errors("webhook"):
            await self._execute(
                "INSERT INTO webhook (rule_id, token_prefix, token_hash, issued_at) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, prefix, token_hash, self._now()),
            )
        return self._required(await self.get(rule_id))

    async def rotate(
        self, rule_id: int, *, expected_prefix: str, prefix: str, token_hash: str
    ) -> bool:
        """Replace the token of the webhook holding expected_prefix; report whether it did.

        The previous token stops working with this write, and calls made with it are dropped:
        the rule's job is disarmed and the waiting count reset. A revoked webhook is re-issued.
        Raises DuplicateError on a taken prefix.
        """
        with constraints_as_domain_errors("webhook"):
            result = await self._execute(
                "UPDATE webhook SET token_prefix = ?, token_hash = ?, issued_at = ?, "
                "revoked_at = NULL, revoked_reason = NULL, pending_calls = 0 "
                "WHERE rule_id = ? AND token_prefix = ?",
                (prefix, token_hash, self._now(), rule_id, expected_prefix),
            )
        if result.rowcount == 0:
            return False
        await self._disarm("rule_id = ?", (rule_id,))
        return True

    async def revoke(self, rule_id: int, *, expected_prefix: str, reason: RevokeReason) -> bool:
        """Revoke the live token holding expected_prefix, keeping its prefix; report if it did.

        The rule's job is disarmed and the waiting count reset in the same write.
        """
        result = await self._execute(
            "UPDATE webhook SET token_hash = NULL, revoked_at = ?, revoked_reason = ?, "
            "pending_calls = 0 WHERE rule_id = ? AND token_prefix = ? AND token_hash IS NOT NULL",
            (self._now(), reason, rule_id, expected_prefix),
        )
        if result.rowcount == 0:
            return False
        await self._disarm("rule_id = ?", (rule_id,))
        return True

    async def revoke_all_for_owner(self, user_id: int, reason: RevokeReason) -> int:
        """Revoke every live token of the webhooks a user owns; return how many were revoked.

        Every job of those webhooks is disarmed and every waiting count reset in the same write.
        """
        owned = "(SELECT id FROM automation_rule WHERE created_by_user_id = ?)"
        result = await self._execute(
            "UPDATE webhook SET token_hash = NULL, revoked_at = ?, revoked_reason = ?, "
            f"pending_calls = 0 WHERE token_hash IS NOT NULL AND rule_id IN {owned}",
            (self._now(), reason, user_id),
        )
        await self._disarm(f"rule_id IN {owned}", (user_id,))
        return result.rowcount

    async def record_call(self, rule_id: int, address: str, *, armed: bool) -> None:
        """Stamp an accepted call: when, from where, and the count waiting.

        `armed` starts the count at one, for a call that armed the job while no run of it was open;
        any other call adds one, so a call between a run's claim and its start is still counted.
        """
        await self._execute(
            "UPDATE webhook SET last_called_at = ?, last_call_address = ?, "
            "pending_calls = CASE WHEN ? THEN 1 ELSE min(pending_calls + 1, ?) END "
            "WHERE rule_id = ?",
            (self._now(), address[:MAX_ADDRESS_CHARS], armed, MAX_PENDING_CALLS, rule_id),
        )

    async def take_pending(self, rule_id: int) -> PendingCalls:
        """Return the waiting call count and last caller's address, and reset the count.

        Opens its own transaction. A rule with no webhook row has no calls.
        """
        async with self._session.transaction():
            row = await self._session.fetch_one(
                "SELECT pending_calls, last_call_address FROM webhook WHERE rule_id = ?",
                (rule_id,),
            )
            if row is None:
                return PendingCalls(0, None)
            await self._execute(
                "UPDATE webhook SET pending_calls = 0 WHERE rule_id = ?", (rule_id,)
            )
        return PendingCalls(int(row["pending_calls"]), row["last_call_address"])

    async def _disarm(self, condition: str, parameters: tuple[int, ...]) -> None:
        await self._execute(
            "UPDATE scheduled_job SET next_run_at = NULL "
            f"WHERE schedule_kind = 'webhook' AND {condition}",
            parameters,
        )
