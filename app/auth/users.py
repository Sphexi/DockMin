"""User management: administrator operations and the self-service profile and password change."""

from dataclasses import dataclass

from app.auth.current_password import verify_current_password
from app.auth.errors import (
    LastAdministratorError,
    PermissionDenied,
    UserHasActivity,
    UserNotFound,
    ValidationFailed,
)
from app.auth.passwords import PasswordHasher
from app.auth.policy import PasswordPolicy, check_external_identity, check_username
from app.auth.principal import Principal
from app.auth.sessions import SessionService
from app.auth.throttle import LoginThrottleService
from app.auth.tokens import ApiTokenService
from app.db.engine import Session
from app.db.repositories.errors import DuplicateError, UserHasActivityError
from app.db.repositories.pagination import Page
from app.db.repositories.user import Role, User, UserRepository
from app.db.repositories.webhook import WebhookRepository
from app.services.activity import ActivityRecorder, audited, current_audit

ROLES: tuple[Role, ...] = ("administrator", "standard")
CHANGED_SINCE_REVIEW = "The account changed since you reviewed it; nothing was deleted."


@dataclass(frozen=True, slots=True)
class UserView:
    """A user as templates see it; there is no field that could hold a password hash."""

    id: int
    username: str
    role: Role
    is_enabled: bool
    created_at: str
    last_login_at: str | None
    session_count: int | None = None
    lockout_retry_after: int = 0
    external_identity: str | None = None

    @classmethod
    def of(
        cls, user: User, session_count: int | None = None, lockout_retry_after: int = 0
    ) -> "UserView":
        """Project a user, dropping the hash."""
        return cls(
            user.id,
            user.username,
            user.role,
            user.is_enabled,
            user.created_at,
            user.last_login_at,
            session_count,
            lockout_retry_after,
            user.external_identity,
        )


class UserService:
    """Applies the rules around accounts; every method takes the acting principal."""

    def __init__(
        self,
        *,
        db: Session,
        users: UserRepository,
        sessions: SessionService,
        activity: ActivityRecorder,
        hasher: PasswordHasher,
        policy: PasswordPolicy,
        throttle: LoginThrottleService,
        tokens: ApiTokenService,
        webhooks: WebhookRepository,
    ) -> None:
        self._db = db
        self._users = users
        self._sessions = sessions
        self._activity = activity
        self._hasher = hasher
        self._policy = policy
        self._throttle = throttle
        self._tokens = tokens
        self._webhooks = webhooks

    async def list_page(
        self, actor: Principal, *, limit: int, cursor: str | None, name_prefix: str | None = None
    ) -> Page[UserView]:
        """Return one page of users, ordered by username; `name_prefix` narrows it."""
        _require_administrator(actor)
        page = await self._users.list_page(limit=limit, cursor=cursor, name_prefix=name_prefix)
        return Page([UserView.of(user) for user in page.items], page.next_cursor)

    async def count(self, actor: Principal) -> int:
        """Count the users list_page shows the caller."""
        _require_administrator(actor)
        return await self._users.count()

    async def get(self, actor: Principal, user_id: int) -> UserView:
        """Return one user with their live session count and any sign-in lock."""
        _require_administrator(actor)
        user = await self._load(user_id)
        return UserView.of(
            user,
            await self._sessions.count_for_user(user_id),
            await self._throttle.retry_after(user.username, None),
        )

    async def profile(self, actor: Principal) -> UserView:
        """Return the caller's own account."""
        return UserView.of(await self._load(actor.user_id))

    @audited(action="user_created", target_type="user")
    async def create(
        self, actor: Principal, *, username: str, password: str, role: str, remote_address: str
    ) -> UserView:
        """Create an enabled account."""
        _require_administrator(actor)
        username = username.strip()
        check_username(username)
        checked_role = _parse_role(role)
        self._policy.check(password, username)
        password_hash = await self._hasher.hash(password)
        async with self._db.transaction():
            try:
                user = await self._users.create(username, password_hash, checked_role)
            except DuplicateError:
                raise ValidationFailed({"username": "That username is taken."}) from None
            audit = current_audit()
            audit.target_id = str(user.id)
            audit.detail["role"] = checked_role
        return UserView.of(user)

    @audited(action="user_updated", target_type="user", target_id=lambda a: str(a["user_id"]))
    async def update(
        self, actor: Principal, user_id: int, *, username: str, role: str, remote_address: str
    ) -> UserView:
        """Change a user's username and role."""
        _require_administrator(actor)
        username = username.strip()
        check_username(username)
        checked_role = _parse_role(role)
        async with self._db.transaction():
            user = await self._load(user_id)
            if user.role == "administrator" and checked_role != "administrator":
                await self._require_another_enabled_administrator(user)
            try:
                if user.username != username:
                    await self._users.set_username(user_id, username)
            except DuplicateError:
                raise ValidationFailed({"username": "That username is taken."}) from None
            if user.role != checked_role:
                await self._users.set_role(user_id, checked_role)
                current_audit().detail.update(old_role=user.role, new_role=checked_role)
            updated = await self._load(user_id)
        return UserView.of(updated)

    @audited(action="user_updated", target_type="user", target_id=lambda a: str(a["user_id"]))
    async def set_external_identity(
        self, actor: Principal, user_id: int, *, identity: str, remote_address: str
    ) -> UserView:
        """Link an access platform identity to a user, or unlink it when `identity` is blank."""
        _require_administrator(actor)
        identity = identity.strip()
        if identity:
            check_external_identity(identity)
        async with self._db.transaction():
            user = await self._load(user_id)
            if (user.external_identity or "") == identity:
                current_audit().unchanged()
                return UserView.of(user)
            try:
                await self._users.set_external_identity(user_id, identity or None)
            except DuplicateError:
                raise ValidationFailed(
                    {"external_identity": "That identity is linked to another account."}
                ) from None
            detail = current_audit().detail
            if identity:
                detail["external_identity_set"] = identity
                if user.external_identity is not None:
                    detail["previous_external_identity"] = user.external_identity
            else:
                detail["external_identity_cleared"] = user.external_identity
            updated = await self._load(user_id)
        return UserView.of(updated)

    @audited(
        action=lambda a: "user_enabled" if a["enabled"] else "user_disabled",
        target_type="user",
        target_id=lambda a: str(a["user_id"]),
    )
    async def set_enabled(
        self, actor: Principal, user_id: int, *, enabled: bool, remote_address: str
    ) -> UserView:
        """Disable an account and end its sessions, or re-enable it."""
        _require_administrator(actor)
        async with self._db.transaction():
            user = await self._load(user_id)
            if not enabled:
                if user.role == "administrator" and user.is_enabled:
                    await self._require_another_enabled_administrator(user)
                await self._users.disable(user_id)
                await self._sessions.end_all_for_user(user_id)
            else:
                await self._users.enable(user_id)
            updated = await self._load(user_id)
        return UserView.of(updated)

    @audited(action="user_deleted", target_type="user", target_id=lambda a: str(a["user_id"]))
    async def delete(
        self, actor: Principal, user_id: int, *, expected_name: str, remote_address: str
    ) -> None:
        """Delete an account that has never acted, while its username is still expected_name.

        An account with recorded activity can only be disabled.
        """
        _require_administrator(actor)
        async with self._db.transaction():
            user = await self._load(user_id)
            if user.username != expected_name:
                raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
            if user.role == "administrator":
                if await self._users.count_administrators() <= 1:
                    raise LastAdministratorError("The last administrator cannot be deleted.")
                if user.is_enabled:
                    await self._require_another_enabled_administrator(user)
            try:
                await self._users.delete(user_id)
            except UserHasActivityError:
                raise UserHasActivity(
                    "This user has recorded activity and cannot be deleted. Disable the account "
                    "instead."
                ) from None

    @audited(
        action="password_reset",
        target_type="user",
        target_id=lambda a: str(a["user_id"]),
        category="authentication",
    )
    async def reset_password(
        self, actor: Principal, user_id: int, *, new_password: str, remote_address: str
    ) -> None:
        """Set a user's password, end all of their sessions and revoke all of their tokens."""
        _require_administrator(actor)
        user = await self._load(user_id)
        self._policy.check(new_password, user.username, field_name="new_password")
        password_hash = await self._hasher.hash(new_password)
        async with self._db.transaction():
            await self._users.set_password_hash(user_id, password_hash)
            await self._sessions.end_all_for_user(user_id)
            await self._revoke_credentials(user_id)

    @audited(
        action="sessions_revoked",
        target_type="user",
        target_id=lambda a: str(a["user_id"]),
        category="authentication",
    )
    async def revoke_sessions(self, actor: Principal, user_id: int, *, remote_address: str) -> int:
        """End every session of a user and revoke their API tokens; return the sessions ended."""
        _require_administrator(actor)
        await self._load(user_id)
        async with self._db.transaction():
            ended = await self._sessions.end_all_for_user(user_id)
            await self._revoke_credentials(user_id)
            return ended

    @audited(
        action="profile_updated", target_type="user", target_id=lambda a: str(a["actor"].user_id)
    )
    async def update_profile(
        self, actor: Principal, *, username: str, remote_address: str
    ) -> UserView:
        """Change the caller's own username."""
        username = username.strip()
        check_username(username)
        async with self._db.transaction():
            try:
                await self._users.set_username(actor.user_id, username)
            except DuplicateError:
                raise ValidationFailed({"username": "That username is taken."}) from None
            updated = await self._load(actor.user_id)
        return UserView.of(updated)

    @audited(action="lockout_cleared", target_type="user", target_id=lambda a: str(a["user_id"]))
    async def clear_lockout(self, actor: Principal, user_id: int, *, remote_address: str) -> bool:
        """Lift the sign-in lock on an account; return whether there was one to lift."""
        _require_administrator(actor)
        user = await self._load(user_id)
        async with self._throttle.guard(user.username, None), self._db.transaction():
            return await self._throttle.clear_username(user.username) > 0

    @audited(
        action="password_change",
        target_type="user",
        target_id=lambda a: str(a["actor"].user_id),
        category="authentication",
    )
    async def change_password(
        self,
        actor: Principal,
        *,
        current_password: str,
        new_password: str,
        new_password_confirm: str,
        remote_address: str,
    ) -> None:
        """Change the caller's own password, end their other sessions and revoke their tokens.

        Wrong guesses at the current password count toward the same lockout as sign-in.
        """
        user = await self._load(actor.user_id)
        await verify_current_password(
            user,
            current_password,
            remote_address,
            throttle=self._throttle,
            hasher=self._hasher,
        )
        if new_password != new_password_confirm:
            raise ValidationFailed({"new_password_confirm": "The passwords do not match."})
        self._policy.check(new_password, user.username, field_name="new_password")
        password_hash = await self._hasher.hash(new_password)
        async with self._db.transaction():
            await self._users.set_password_hash(user.id, password_hash)
            await self._sessions.end_all_for_user(user.id, keep=actor.session_id)
            await self._revoke_credentials(user.id)

    async def _revoke_credentials(self, user_id: int) -> None:
        detail = current_audit().detail
        detail["tokens_revoked"] = await self._tokens.revoke_all_for_user(user_id)
        detail["webhook_tokens_revoked"] = await self._webhooks.revoke_all_for_owner(
            user_id, reason="account_recovery"
        )

    async def _load(self, user_id: int) -> User:
        user = await self._users.get(user_id)
        if user is None:
            raise UserNotFound(f"user {user_id} does not exist")
        return user

    async def _require_another_enabled_administrator(self, user: User) -> None:
        if not user.is_enabled:
            return
        if await self._users.count_administrators(enabled_only=True) <= 1:
            raise LastAdministratorError(
                "This is the last administrator who can sign in, so it cannot be demoted, "
                "disabled or deleted."
            )


def _require_administrator(actor: Principal) -> None:
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")


def _parse_role(role: str) -> Role:
    for known in ROLES:
        if role == known:
            return known
    raise ValidationFailed({"role": "Choose administrator or standard."})
