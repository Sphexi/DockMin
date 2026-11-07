"""Repository base for tables that carry timestamps, with an injectable clock."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.db.engine import DatabaseError, Session, WriteResult
from app.db.repositories.base import Repository

Clock = Callable[[], datetime]

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def format_timestamp(moment: datetime) -> str:
    """Render an aware datetime in the stored text form."""
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(text: str) -> datetime:
    """Read a stored timestamp back into an aware UTC datetime."""
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


class ClockedRepository[T](Repository[T]):
    """Repository that stamps rows with ISO 8601 UTC text from a replaceable clock."""

    def __init__(self, session: Session, clock: Clock = utc_now) -> None:
        super().__init__(session)
        self._clock = clock

    def _now(self) -> str:
        return format_timestamp(self._clock())

    def _before_now(self, delta: timedelta) -> str:
        return format_timestamp(self._clock() - delta)

    def _after_now(self, delta: timedelta) -> str:
        return format_timestamp(self._clock() + delta)

    @staticmethod
    def _required(entity: T | None) -> T:
        if entity is None:
            raise DatabaseError("a row written by this call could not be read back")
        return entity

    async def _created(self, result: WriteResult, fetch: Callable[[int], Awaitable[T | None]]) -> T:
        if result.last_row_id is None:
            raise DatabaseError("an insert did not report a row id")
        return self._required(await fetch(result.last_row_id))
