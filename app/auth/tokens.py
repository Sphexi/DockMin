"""API tokens: issue once, store a hash, and turn a presented value back into a principal."""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.auth.errors import PermissionDenied, TokenNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.token_format import PREFIX_ATTEMPTS, new_token, token_pattern
from app.db.engine import Session
from app.db.repositories.api_token import ApiToken, ApiTokenRepository
from app.db.repositories.clocked import Clock, parse_timestamp
from app.db.repositories.errors import DuplicateError
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page
from app.db.repositories.user import UserRepository
from app.services.activity import ActivityRecorder, audited, current_audit

TOKEN_MARK = "dkm"
LABEL_MAX = 64
_TOKEN_PATTERN = token_pattern(TOKEN_MARK)

# Use is written at most this often, so a busy client is not a stream of writes.
TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class TokenView:
    """A token record as a template sees it; there is no field that could hold the value."""

    id: int
    label: str
    display_prefix: str
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None
    user_id: int

    @classmethod
    def of(cls, token: ApiToken) -> "TokenView":
        """Project a record, dropping the hash."""
        return cls(
            token.id,
            token.label,
            f"{TOKEN_MARK}_{token.prefix}",
            token.created_at,
            token.last_used_at,
            token.expires_at,
            token.revoked_at,
            token.user_id,
        )


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A new token; value is the only copy that will ever exist and is kept out of repr."""

    value: str = field(repr=False)
    record: TokenView


def hash_token(value: str) -> str:
    """Return the stored form of a token."""
    # The secret is 256 random bits, so a plain SHA-256 is the whole stored form.
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class ApiTokenService:
    """Creates, lists and revokes tokens, and authenticates a presented one."""

    def __init__(
        self,
        *,
        db: Session,
        tokens: ApiTokenRepository,
        users: UserRepository,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._db = db
        self._tokens = tokens
        self._users = users
        self._activity = activity
        self._clock = clock

    @audited(
        action="token_created",
        target_type="user",
        target_id=lambda a: str(a["actor"].user_id),
        category="authentication",
    )
    async def create(
        self,
        actor: Principal,
        *,
        label: str,
        expires_at: datetime | None,
        remote_address: str,
    ) -> IssuedToken:
        """Issue a token for the caller; the returned value is shown once and then gone."""
        checked = _check_label(label)
        if expires_at is not None and (expires_at.tzinfo is None or expires_at <= self._clock()):
            raise ValidationFailed({"expires_at": "The expiry must be a time in the future."})
        async with self._db.transaction():
            if await self._tokens.label_exists(actor.user_id, checked):
                raise ValidationFailed({"label": "You already have a token with that label."})
            value, record = await self._insert(actor.user_id, checked, expires_at)
            current_audit().detail.update(label=checked, prefix=record.prefix)
        return IssuedToken(value, TokenView.of(record))

    async def list_page(
        self, actor: Principal, *, user_id: int | None = None, limit: int, cursor: str | None
    ) -> Page[TokenView]:
        """Return a page of the caller's tokens, or another user's for an administrator."""
        owner = actor.user_id if user_id is None else user_id
        if owner != actor.user_id and not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        page = await self._tokens.list_for_user(owner, limit=limit, cursor=cursor)
        return Page([TokenView.of(token) for token in page.items], page.next_cursor)

    @audited(action="token_revoked", target_type="user", category="authentication")
    async def revoke(
        self,
        actor: Principal,
        token_id: int,
        *,
        remote_address: str,
        owner_id: int | None = None,
    ) -> None:
        """Revoke a token; it fails authentication from the next request on.

        owner_id, when given, must be the token's owner; any other token is treated as absent.
        """
        async with self._db.transaction():
            token = await self._tokens.get(token_id)
            if (
                token is None
                or (owner_id is not None and token.user_id != owner_id)
                or (token.user_id != actor.user_id and not actor.is_administrator)
            ):
                raise TokenNotFound(f"token {token_id} does not exist")
            audit = current_audit()
            audit.target_id = str(token.user_id)
            if await self._tokens.revoke(token_id):
                audit.detail.update(label=token.label, prefix=token.prefix)
            else:
                audit.unchanged()

    async def revoke_all_for_user(self, user_id: int) -> int:
        """Revoke every live token of a user; return how many were revoked.

        Runs inside the caller's transaction and records nothing; the caller records the revocation.
        """
        revoked = 0
        cursor: str | None = None
        while True:
            page = await self._tokens.list_for_user(user_id, limit=MAX_PAGE_SIZE, cursor=cursor)
            for token in page.items:
                if await self._tokens.revoke(token.id):
                    revoked += 1
            if page.next_cursor is None:
                return revoked
            cursor = page.next_cursor

    async def resolve(self, presented: str) -> Principal | None:
        """Return the principal a token belongs to, or None if it cannot be used.

        Everything is read from the database on every call, so a revocation, an expiry, a
        disabled account or a changed role takes effect on the very next request.
        """
        match = _TOKEN_PATTERN.fullmatch(presented)
        if match is None:
            return None
        record = await self._tokens.get_by_prefix(match.group(1))
        if record is None:
            return None
        if not hmac.compare_digest(record.token_hash, hash_token(presented)):
            return None
        if record.revoked_at is not None:
            return None
        if record.expires_at is not None and parse_timestamp(record.expires_at) <= self._clock():
            return None
        user = await self._users.get(record.user_id)
        if user is None or not user.is_enabled:
            return None
        await self._tokens.touch(record.id, min_interval=TOUCH_INTERVAL)
        # No session id: a token principal must be indistinguishable from a session principal.
        return Principal(user.id, user.username, user.role)

    async def _insert(
        self, user_id: int, label: str, expires_at: datetime | None
    ) -> tuple[str, ApiToken]:
        for _ in range(PREFIX_ATTEMPTS):
            value, prefix = new_token(TOKEN_MARK)
            try:
                # A savepoint, so a prefix collision undoes only itself.
                async with self._db.savepoint():
                    record = await self._tokens.create(
                        user_id, label, prefix, hash_token(value), expires_at=expires_at
                    )
            except DuplicateError:
                continue
            return value, record
        raise RuntimeError("could not allocate a unique token prefix")


def _check_label(label: str) -> str:
    stripped = label.strip()
    if not stripped or len(stripped) > LABEL_MAX or any(ord(char) < 32 for char in stripped):
        raise ValidationFailed({"label": f"Use 1 to {LABEL_MAX} printable characters."})
    return stripped
