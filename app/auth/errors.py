"""The product's error classes: the base class and the errors the services raise on purpose."""

from typing import Final, Self

SELF_PROTECTED_REASON: Final = "self_protected"
_INSTANCE_PAGE: Final = "Administration > Instance"


class ProductError(Exception):
    """Base for errors the web layer maps to a response."""


class ValidationFailed(ProductError):
    """Raised when input is unacceptable; fields maps a field name to a message for the user."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


class InvalidCredentials(ProductError):
    """Raised when a login fails; the message never says which half was wrong."""

    def __init__(self) -> None:
        super().__init__("Invalid username or password.")


class LockedOut(ProductError):
    """Raised when failures have locked a username or address out."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many failed attempts. Try again later.")
        self.retry_after = retry_after


class SetupClosed(ProductError):
    """Raised when bootstrap is attempted after an administrator exists."""


class SetupTokenRejected(ProductError):
    """Raised when the setup token is missing or wrong."""


class PermissionDenied(ProductError):
    """Raised when the caller's role does not allow the operation."""


class NotFound(ProductError):
    """Base for errors the web layer answers with 404."""


class UserNotFound(NotFound):
    """Raised when the named user does not exist."""


class ResourceNotFound(NotFound):
    """Raised for an object the caller may not see and for one that does not exist alike.

    The two must stay indistinguishable: telling them apart confirms the object exists.
    """


class TeamNotFound(NotFound):
    """Raised when a team does not exist or the caller has no standing to know of it."""


class TokenNotFound(NotFound):
    """Raised when a token does not exist or belongs to someone the caller may not manage."""


class ResourceControlNotFound(NotFound):
    """Raised when an object has no resource control to change or remove."""


class LastAdministratorError(ProductError):
    """Raised when an operation would leave the instance with no administrator able to sign in."""


class UserHasActivity(ProductError):
    """Raised when a user with recorded activity is deleted; the user can only be disabled."""


class SelfProtected(ProductError):
    """Raised when an action would take down the container dockmin runs in, or what it runs on."""

    reason = SELF_PROTECTED_REASON

    @classmethod
    def container(cls) -> Self:
        """Return the refusal for a stop, restart, kill, pause, recreate, removal or detach."""
        return cls(
            "This is the container dockmin runs in. It cannot be stopped, restarted, recreated "
            f"or removed from dockmin; restart it from {_INSTANCE_PAGE}."
        )

    @classmethod
    def detach(cls) -> Self:
        """Return the refusal for detaching the container dockmin runs in from a network."""
        return cls(
            "This is the container dockmin runs in. It cannot be detached from a network "
            "from dockmin, because dockmin could lose its own connection."
        )

    @classmethod
    def terminal(cls) -> Self:
        """Return the refusal for a terminal in the container dockmin runs in."""
        return cls(
            "This is the container dockmin runs in. A terminal cannot be opened in it from dockmin."
        )

    @classmethod
    def volume(cls, name: str) -> Self:
        """Return the refusal for a volume the container dockmin runs in has mounted."""
        return cls(
            f"The volume {name} is mounted in the container dockmin runs in. "
            "It cannot be removed from dockmin."
        )

    @classmethod
    def node(cls) -> Self:
        """Return the refusal for draining the Swarm node that runs dockmin's container."""
        return cls(
            "This node runs the container dockmin runs in. It cannot be drained from dockmin, "
            f"because the container would be stopped; restart dockmin from {_INSTANCE_PAGE} "
            "after moving it."
        )

    @classmethod
    def joins(cls) -> Self:
        """Return the refusal for joining the namespaces or volumes of dockmin's container."""
        return cls(
            "A container cannot join the process, IPC or network namespace of the container "
            "dockmin runs in, or inherit its volumes, from dockmin."
        )

    @classmethod
    def mount(cls, name: str) -> Self:
        """Return the refusal for mounting a volume that dockmin's container has mounted."""
        return cls(
            f"The volume {name} is mounted in the container dockmin runs in. "
            "It cannot be mounted in another container from dockmin."
        )

    @classmethod
    def stack(cls, name: str) -> Self:
        """Return the refusal for a Compose stack that contains the container dockmin runs in."""
        return cls(
            f"The stack {name} contains the container dockmin runs in. It cannot be stopped, "
            f"removed or redeployed from dockmin; restart dockmin from {_INSTANCE_PAGE}."
        )

    @classmethod
    def service(cls) -> Self:
        """Return the refusal for a Swarm service that runs the container dockmin runs in."""
        return cls(
            "This service runs the container dockmin runs in. It cannot be changed or removed "
            f"from dockmin; restart dockmin from {_INSTANCE_PAGE}."
        )


class RedirectRequired(Exception):
    """Raised by a dependency to answer the request with a redirect instead of running the route."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location
