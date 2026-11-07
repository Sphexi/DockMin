"""The exception set of the Engine client; no httpx exception crosses this boundary."""


class EngineError(Exception):
    """An Engine call failed; every subclass carries the environment it failed against.

    Messages never include request or response bodies, which can carry registry credentials.
    `engine_message` is the one place Engine-supplied text may live; it is never part of `str()`.
    """

    def __init__(
        self,
        environment_id: int,
        message: str,
        *,
        status_code: int | None = None,
        engine_message: str | None = None,
    ) -> None:
        super().__init__(f"environment {environment_id}: {message}")
        self.environment_id = environment_id
        self.status_code = status_code
        self.reason = message
        self.engine_message = engine_message


class EnvironmentUnreachable(EngineError):
    """The host could not be reached, timed out, or dropped the connection."""


class RequestOutcomeUnknown(EnvironmentUnreachable):
    """The request was sent and the host stopped answering, so the change may have happened."""


class ResponseTooLarge(EngineError):
    """The Engine's reply exceeded the size limit of the call that read it."""


class NotFound(EngineError):
    """The Engine has no such resource."""


class Conflict(EngineError):
    """The request conflicts with the resource's current state."""


class PermissionDenied(EngineError):
    """The Engine refused the request as unauthorised or forbidden."""


class VersionUnsupported(EngineError):
    """The daemon's API range does not overlap the range this client supports."""

    def __init__(self, environment_id: int, message: str, *, daemon_api_version: str) -> None:
        super().__init__(environment_id, message)
        self.daemon_api_version = daemon_api_version
