"""Errors raised by the Compose wrapper; messages are product text and never carry output."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.compose_cli.parsers import ProgressEvent


class ComposeError(Exception):
    """Base class for every error the Compose wrapper raises."""


class ComposeUnavailable(ComposeError):
    """No usable Compose binary exists on this server."""

    def __init__(self, message: str = "Docker Compose was not found on this server.") -> None:
        super().__init__(message)


class ComposeBinaryInvalid(ComposeError):
    """The configured Compose binary is not a trusted absolute path to an executable file."""

    def __init__(self) -> None:
        super().__init__(
            "The configured Docker Compose binary must be an absolute path to an executable "
            "file owned by root or the server's user, with no group or world write access to "
            "the file or its directory."
        )


class ComposeVersionUnsupported(ComposeError):
    """The installed Compose is older than the required floor."""

    def __init__(self, installed: str, required: str) -> None:
        super().__init__(
            f"Docker Compose {installed} is installed on this server; "
            f"dockmin requires {required} or later."
        )
        self.installed = installed
        self.required = required


class ComposeHostRefused(ComposeError):
    """The remote environment's host is not one a Compose process may be aimed at."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ComposeBusy(ComposeError):
    """No Compose process slot became free in the time allowed."""

    def __init__(self) -> None:
        super().__init__("The server is busy. Try again shortly.")


class ComposeOutputInvalid(ComposeError):
    """Compose produced output that is not the machine-readable shape expected."""

    def __init__(self) -> None:
        super().__init__("Docker Compose returned output that could not be understood.")


class ComposeOutputTooLarge(ComposeError):
    """Compose wrote more to stdout than the cap allows."""

    def __init__(self) -> None:
        super().__init__("Docker Compose produced more output than dockmin accepts.")


class ComposeCommandFailed(ComposeError):
    """A Compose process exited non-zero, or did not finish."""

    def __init__(
        self,
        command: tuple[str, ...],
        exit_code: int | None,
        stderr: str,
        events: "tuple[ProgressEvent, ...]" = (),
        *,
        message: str | None = None,
    ) -> None:
        super().__init__(message or f"Docker Compose exited with status {exit_code}.")
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        self.events = events

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(command={self.command!r}, exit_code={self.exit_code!r}, "
            f"events={len(self.events)})"
        )


class ComposeTimedOut(ComposeCommandFailed):
    """A Compose process was killed after running past its timeout."""

    def __init__(
        self,
        command: tuple[str, ...],
        exit_code: int | None,
        stderr: str,
        events: "tuple[ProgressEvent, ...]" = (),
    ) -> None:
        super().__init__(
            command, exit_code, stderr, events, message="Docker Compose did not finish in time."
        )
