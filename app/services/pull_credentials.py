"""Registry credentials for an image pull: the choice, the resolver protocol and its errors."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, Protocol

from pydantic import SecretStr

from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.registry_access import OfferedCredentialSet

type PullSource = Literal["choice", "binding", "only_match", "no_match"]
type SeenState = Literal["unbound", "bound", "credential_missing"]

CHOICE_FIELD = "credential_choice"
ANONYMOUS_CHOICE = "anonymous"
CHOICE_REQUIRED = "Choose which credential set to pull with."
SET_MISMATCH = "That credential set is for a different registry."
CANDIDATES_TOO_MANY = "Too many credential sets fit this registry to choose between."


@dataclass(frozen=True, slots=True)
class CredentialChoice:
    """A set the caller chose for a pull; None for the set means an anonymous pull, chosen."""

    credential_set_id: int | None


@dataclass(frozen=True, slots=True)
class SeenBinding:
    """The binding of a reference as a decision saw it; `set_id` is the set a row names or lost."""

    state: SeenState = "unbound"
    binding_id: int | None = None
    set_id: int | None = None


@dataclass(frozen=True, slots=True)
class PullCredentials:
    """The credential set chosen for a pull, its `X-Registry-Auth` value, and how it was chosen.

    `seen` is the binding the choice was made against; None when no decision was made.
    """

    credential_set_id: int | None = None
    registry_auth: SecretStr | None = field(default=None, repr=False)
    source: PullSource = "no_match"
    seen: SeenBinding | None = None


class CredentialChoiceRequired(ValidationFailed):
    """Several credential sets fit the image and nothing says which; `candidates` are the sets."""

    def __init__(self, candidates: Sequence[OfferedCredentialSet]) -> None:
        super().__init__({CHOICE_FIELD: CHOICE_REQUIRED})
        self.candidates = tuple(candidates)


class CredentialSetMismatch(ValidationFailed):
    """The chosen credential set is for another registry than the image's."""

    def __init__(self) -> None:
        super().__init__({CHOICE_FIELD: SET_MISMATCH})


class CandidateListTooLarge(ValidationFailed):
    """More credential sets fit the registry than can be listed to choose between."""

    def __init__(self) -> None:
        super().__init__({CHOICE_FIELD: CANDIDATES_TOO_MANY})


class PullCredentialResolver(Protocol):
    """Chooses the credentials an image reference is pulled with on an environment."""

    async def resolve(
        self,
        environment_id: int,
        reference: str,
        *,
        principal: Principal | None,
        choice: CredentialChoice | None = None,
        min_remaining: timedelta | None = None,
    ) -> PullCredentials:
        """Return the credentials for the reference; never call inside a write transaction.

        An anonymous pull has no set and no header. None for principal is the automation path,
        which never chooses by host. A derived token with less than `min_remaining` of life left
        is derived anew.
        """
        ...


class AnonymousPullCredentials:
    """Resolves every reference to an anonymous pull, for a runtime with no registry support."""

    async def resolve(
        self,
        environment_id: int,
        reference: str,
        *,
        principal: Principal | None,
        choice: CredentialChoice | None = None,
        min_remaining: timedelta | None = None,
    ) -> PullCredentials:
        """Return no credentials."""
        return PullCredentials(source="choice" if choice is not None else "no_match")
