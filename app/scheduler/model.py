"""Types shared by the scheduler loop and the subsystems it runs."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol

from app.db.repositories.activity_event import DetailValue
from app.db.repositories.job_run import JobRun, check_run_detail, is_reason
from app.db.repositories.scheduled_job import ScheduledJob

type EventPolicy = Literal["per_action", "failures_only", "none"]

_FALLBACK_REASON = "unspecified"


def subsystem_setting_key(name: str) -> str:
    """Return the setting key that holds a subsystem's enabled flag."""
    return f"subsystem.{name}.enabled"


def setting_means_enabled(value: str | None) -> bool:
    """Read a stored flag: a missing setting means enabled."""
    return value is None or value.strip().lower() not in {"false", "0", "no", "off"}


def reason_or_fallback(text: str | None) -> str:
    """Return the text if it is a valid reason code, else a fixed code."""
    return text if text is not None and is_reason(text) else _FALLBACK_REASON


def reason_of(error: BaseException) -> str:
    """Derive a reason code from an exception's class name; its text is never used."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return re.sub(r"[^a-z_]", "", spaced)[:64] or _FALLBACK_REASON


class DetailInvalid(ValueError):
    """A run reported detail that cannot be stored; recorded as a failure with this reason."""


def _checked_detail(detail: Mapping[str, DetailValue]) -> Mapping[str, DetailValue]:
    try:
        check_run_detail(detail)
    except ValueError as error:
        raise DetailInvalid(str(error)) from None
    return MappingProxyType(dict(detail))


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One run handed to a handler."""

    job: ScheduledJob
    run: JobRun
    now: datetime
    dry_run: bool


@dataclass(frozen=True, slots=True)
class RunResult:
    """What a run did; failure and skipped carry a reason code, and message is product-written."""

    outcome: Literal["success", "partial", "failure", "skipped"]
    attempted: int = 0
    reason: str | None = None
    message: str | None = None
    detail: Mapping[str, DetailValue] = MappingProxyType({})

    def __post_init__(self) -> None:
        """Refuse detail the run history cannot store, while the handler can still react."""
        object.__setattr__(self, "detail", _checked_detail(self.detail))


class RunSkipped(Exception):
    """Raised by a handler that does not act; the run is recorded as skipped."""

    def __init__(
        self, reason: str, message: str, detail: Mapping[str, DetailValue] | None = None
    ) -> None:
        if not is_reason(reason):
            raise ValueError("a skip reason is 1 to 64 lowercase letters and underscores")
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = _checked_detail(detail or {})


class RunFailed(Exception):
    """Raised by a handler for a failure whose text the product wrote, so it may be stored."""

    def __init__(self, reason: str, message: str) -> None:
        if not is_reason(reason):
            raise ValueError("a failure reason is 1 to 64 lowercase letters and underscores")
        super().__init__(message)
        self.reason = reason
        self.message = message


class JobHandler(Protocol):
    """What the scheduler needs from the code that runs a job."""

    @property
    def name(self) -> str:
        """Return the subsystem name, which is also the activity actor."""
        ...

    @property
    def records_events(self) -> bool:
        """Report whether the subsystem's runs write activity events."""
        ...

    @property
    def supports_dry_run(self) -> bool:
        """Report whether a dry run of this subsystem changes nothing."""
        ...

    @property
    def cancellation_safe(self) -> bool:
        """Report whether a run may be cancelled mid-flight without leaving host state unknown."""
        ...

    @property
    def run_timeout(self) -> timedelta:
        """Return how long one run may take."""
        ...

    @property
    def max_concurrent_runs(self) -> int | None:
        """Return how many runs of this subsystem may be in flight at once, or None for no cap."""
        ...

    async def dispatch(self, dispatch: Dispatch) -> RunResult:
        """Run the job once."""
        ...
