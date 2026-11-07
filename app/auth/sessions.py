"""Server-side sessions: issue, resolve and revoke, and the cookie that carries them."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from starlette.responses import Response

from app.auth.principal import Principal
from app.config import Settings
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import SignInMethod, UserSession, UserSessionRepository

# The __Host- prefix makes a browser refuse a copy set by a sibling subdomain or over plaintext. It
# requires the Secure attribute, so a deployment that disables Secure falls back to the bare name.
SESSION_COOKIE = "__Host-dockmin_session"
INSECURE_SESSION_COOKIE = "dockmin_session"
SIGNED_OUT_COOKIE = "dockmin_signed_out"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")

# Activity is written at most this often, so a page of requests is not a page of writes.
TOUCH_INTERVAL = timedelta(minutes=1)

# Wide enough that only the absolute lifetime and revocation, never idleness, fail a lookup.
_NO_IDLE_LIMIT = timedelta(days=3650)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session; token is the only copy of the cookie value and is never stored."""

    token: str
    session_id: int


@dataclass(frozen=True, slots=True)
class SessionStanding:
    """Whether a session is usable: `expired` ran out of idle time or lifetime, `ended` is gone."""

    state: Literal["active", "expired", "ended"]
    principal: Principal | None = None


def session_cookie_name(settings: Settings) -> str:
    """Return the name the session cookie carries under the configured Secure setting."""
    return SESSION_COOKIE if settings.cookie_secure else INSECURE_SESSION_COOKIE


def hash_token(token: str) -> str:
    """Return the stored form of a cookie value."""
    # The value is 256 random bits, so a plain SHA-256 is the whole stored form.
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SessionService:
    """Creates sessions for users and turns a cookie back into the principal it belongs to."""

    def __init__(
        self,
        sessions: UserSessionRepository,
        users: UserRepository,
        *,
        lifetime: timedelta,
        idle_timeout: timedelta,
    ) -> None:
        self._sessions = sessions
        self._users = users
        self._lifetime = lifetime
        self._idle_timeout = idle_timeout

    async def issue(
        self,
        user_id: int,
        *,
        user_agent: str | None,
        remote_address: str | None,
        sign_in_method: SignInMethod = "password",
    ) -> IssuedSession:
        """Create a session and return the cookie value that names it."""
        token = secrets.token_urlsafe(32)
        row = await self._sessions.create(
            user_id,
            hash_token(token),
            lifetime=self._lifetime,
            user_agent=user_agent,
            remote_address=remote_address,
            sign_in_method=sign_in_method,
        )
        return IssuedSession(token, row.id)

    async def resolve(self, token: str | None) -> Principal | None:
        """Return the principal for a cookie value, or None if it is absent, dead or disabled."""
        if token is None or _TOKEN_PATTERN.fullmatch(token) is None:
            return None
        found = await self._lookup(hash_token(token))
        if found is None:
            return None
        row, principal = found
        await self._sessions.touch(row.id, min_interval=TOUCH_INTERVAL)
        return principal

    async def check(self, token_hash: str) -> Principal | None:
        """Return the principal of a session by its stored hash without recording activity.

        For a long-lived connection that must notice a sign-out or a disabled user but must not
        keep the web session alive by doing so.
        """
        found = await self._lookup(token_hash)
        return None if found is None else found[1]

    async def standing(self, token_hash: str, session_id: int, *, touch: bool) -> SessionStanding:
        """Report a session's standing, recording activity when `touch` is set.

        For a long-lived connection whose own traffic stands in for requests: touching extends the
        idle expiry exactly as a request does. A dead session is `expired` when it ran out of idle
        time or lifetime, and `ended` when it was deleted or its user was disabled.
        """
        found = await self._lookup(token_hash)
        if found is not None:
            row, principal = found
            if touch:
                await self._sessions.touch(row.id, min_interval=TOUCH_INTERVAL)
            return SessionStanding("active", principal)
        idle = await self._sessions.get_active(token_hash, idle_timeout=_NO_IDLE_LIMIT)
        if idle is not None:
            user = await self._users.get(idle.user_id)
            return SessionStanding("expired" if user is not None and user.is_enabled else "ended")
        remaining = await self._sessions.get(session_id)
        return SessionStanding("ended" if remaining is None else "expired")

    async def _lookup(self, token_hash: str) -> tuple[UserSession, Principal] | None:
        row = await self._sessions.get_active(token_hash, idle_timeout=self._idle_timeout)
        if row is None:
            return None
        # The user is read on every request so a disable or role change takes effect at once.
        user = await self._users.get(row.user_id)
        if user is None or not user.is_enabled:
            return None
        principal = Principal(
            user.id,
            user.username,
            user.role,
            row.id,
            row.sign_in_method,
            row.nav_environment_id,
        )
        return row, principal

    async def end(self, session_id: int) -> bool:
        """Delete one session, making its cookie useless; return False if it was already gone."""
        return await self._sessions.delete(session_id)

    async def end_all_for_user(self, user_id: int, *, keep: int | None = None) -> int:
        """Delete a user's sessions, sparing the one named by keep; return how many ended."""
        return await self._sessions.delete_for_user(user_id, except_session_id=keep)

    async def count_for_user(self, user_id: int) -> int:
        """Count a user's sessions that can still be used."""
        return await self._sessions.count_for_user(user_id, idle_timeout=self._idle_timeout)

    async def reap(self) -> None:
        """Delete sessions that can no longer be used."""
        await self._sessions.delete_dead(idle_timeout=self._idle_timeout)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach the session cookie with its three protective attributes."""
    response.set_cookie(
        session_cookie_name(settings),
        token,
        max_age=int(settings.session_lifetime.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Instruct the browser to drop the session cookie."""
    response.delete_cookie(
        session_cookie_name(settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def set_signed_out_cookie(response: Response, settings: Settings) -> None:
    """Mark this browser as signed out of the access platform until it chooses to continue."""
    response.set_cookie(
        SIGNED_OUT_COOKIE,
        "1",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_signed_out_cookie(response: Response, settings: Settings) -> None:
    """Instruct the browser to drop the signed-out marker."""
    response.delete_cookie(
        SIGNED_OUT_COOKIE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
