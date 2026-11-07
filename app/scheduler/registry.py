"""The set of job handlers registered in this process."""

from app.scheduler.model import JobHandler
from app.services.activity import SUBSYSTEMS, UNSCHEDULED_SUBSYSTEMS


def _require_schedulable(name: str) -> None:
    if name not in SUBSYSTEMS:
        raise ValueError(f"{name!r} is not a registered subsystem")
    if name in UNSCHEDULED_SUBSYSTEMS:
        raise ValueError(f"{name!r} owns no jobs")


class HandlerRegistry:
    """Maps a subsystem name to the handler that runs its jobs."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, handler: JobHandler) -> None:
        """Add a handler; refuses a name outside the subsystem registry or one already taken."""
        _require_schedulable(handler.name)
        if handler.name in self._handlers:
            raise ValueError(f"{handler.name!r} already has a handler")
        self._handlers[handler.name] = handler

    def replace(self, handler: JobHandler) -> None:
        """Install a handler in place of the one registered under its name, or add it if none is."""
        _require_schedulable(handler.name)
        self._handlers[handler.name] = handler

    def get(self, name: str) -> JobHandler | None:
        """Return the handler for a subsystem, or None when this build has none."""
        return self._handlers.get(name)

    def names(self) -> frozenset[str]:
        """Return the names that have a handler."""
        return frozenset(self._handlers)
