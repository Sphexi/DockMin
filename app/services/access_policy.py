"""The instance-wide policy for objects that have no resource control."""

from typing import Literal

from app.auth.errors import PermissionDenied, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.setting import SettingRepository
from app.services.access import AccessLevel
from app.services.activity import ActivityRecorder, audited, current_audit

UncontrolledDefault = Literal["administrators_only", "read", "write"]

UNCONTROLLED_DEFAULT_KEY = "access.uncontrolled_default"
DEFAULT_UNCONTROLLED: UncontrolledDefault = "administrators_only"
UNCONTROLLED_DEFAULTS: tuple[UncontrolledDefault, ...] = ("administrators_only", "read", "write")


class AccessPolicyService:
    """Reads and changes how an object created outside this product is treated."""

    def __init__(
        self, *, db: Session, settings: SettingRepository, activity: ActivityRecorder
    ) -> None:
        self._db = db
        self._settings = settings
        self._activity = activity

    async def uncontrolled_default_setting(self) -> UncontrolledDefault:
        """Return the stored choice; a missing or unrecognised value is administrators-only."""
        row = await self._settings.get(UNCONTROLLED_DEFAULT_KEY)
        # Fails closed: a corrupted row must not widen access.
        for known in UNCONTROLLED_DEFAULTS:
            if row is not None and row.value == known:
                return known
        return DEFAULT_UNCONTROLLED

    async def uncontrolled_default(self) -> AccessLevel | None:
        """Return the level an uncontrolled object gives a standard user, or None for none."""
        match await self.uncontrolled_default_setting():
            case "read":
                return "read"
            case "write":
                return "write"
            case _:
                return None

    @audited(
        action="uncontrolled_default_changed",
        target_type="setting",
        target_id=UNCONTROLLED_DEFAULT_KEY,
    )
    async def set_uncontrolled_default(self, actor: Principal, value: str) -> UncontrolledDefault:
        """Change the default; administrators only."""
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        chosen = _parse(value)
        async with self._db.transaction():
            previous = await self.uncontrolled_default_setting()
            await self._settings.put(UNCONTROLLED_DEFAULT_KEY, "string", chosen)
            current_audit().detail.update(old=previous, new=chosen)
        return chosen


def _parse(value: str) -> UncontrolledDefault:
    for known in UNCONTROLLED_DEFAULTS:
        if value == known:
            return known
    raise ValidationFailed({"uncontrolled_default": "Choose administrators_only, read or write."})
