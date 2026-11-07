"""The authenticated caller, and the credentials a provider is asked to check."""

from dataclasses import dataclass, field

from app.db.repositories.user import Role
from app.db.repositories.user_session import SignInMethod


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making the request; carries no secret, so it is safe to hand to a template."""

    user_id: int
    username: str
    role: Role
    # None marks a token principal. Nothing may rely on that for a security decision: a session
    # lookup that returned a principal without its id would be indistinguishable from a token.
    session_id: int | None = None
    # None for a token principal, which no session opened. It takes no part in comparison.
    sign_in_method: SignInMethod | None = field(default=None, compare=False)
    # A display preference only: it names no authority and every use re-checks visibility.
    remembered_environment_id: int | None = field(default=None, compare=False)

    @property
    def is_administrator(self) -> bool:
        """Report whether the caller holds the administrator role."""
        return self.role == "administrator"


@dataclass(frozen=True, slots=True)
class PasswordCredentials:
    """A username and password as typed into the login form."""

    username: str
    password: str = field(repr=False)


type Credentials = PasswordCredentials
