"""Authorisation checks shared by the environment services."""

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal


def require_administrator(actor: Principal) -> None:
    """Raise PermissionDenied unless the actor is an administrator."""
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")
