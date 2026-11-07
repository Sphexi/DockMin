"""The authority an automation run passes to the audited services in place of a user."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from app.auth.principal import Principal
from app.services.activity import Actor, SubsystemActor, UserActor
from app.services.object_locks import ObjectKey


@dataclass(frozen=True, slots=True)
class AutomationAuthority:
    """What a subsystem run may do on a service's behalf: act as itself, check the rule owner.

    `authorise` raises PermissionDenied or ResourceNotFound
    unless the rule's owner may write to the object; `authorise_read` does the same for read. `hold`
    takes the object's lock, waiting up to `lock_wait`, and raises ObjectBusy or
    RunSkipped("suppressed") on entry. `owner_is_administrator` is the owner's role when the run
    started and is only meaningful after a successful `authorise`, which refuses once the role
    changes. `authorise_credential_set` raises CredentialSetUnavailable unless the owner may pull
    with the credential set on the rule's environment.
    """

    actor: SubsystemActor
    authorise: Callable[[ObjectKey], Awaitable[None]]
    hold: Callable[[ObjectKey, str], AbstractAsyncContextManager[None]]
    lock_wait: float
    authorise_read: Callable[[ObjectKey], Awaitable[None]]
    owner_is_administrator: bool
    authorise_credential_set: Callable[[int], Awaitable[None]]

    @property
    def subsystem_actor(self) -> SubsystemActor:
        """Return the actor the rows are recorded under."""
        return self.actor


type Acting = Principal | AutomationAuthority


def actor_of(acting: Acting) -> Actor:
    """Return the audit actor for a user or an automation run."""
    if isinstance(acting, AutomationAuthority):
        return acting.actor
    return UserActor.of(acting)


def is_administrator(acting: Acting) -> bool:
    """Report whether the caller holds an administrator's rights; ask only after authorising."""
    if isinstance(acting, AutomationAuthority):
        return acting.owner_is_administrator
    return acting.is_administrator


def holder_label(acting: Acting) -> str:
    """Return the name a lock held on the caller's behalf is shown under."""
    if isinstance(acting, AutomationAuthority):
        return acting.actor.name
    return acting.username
