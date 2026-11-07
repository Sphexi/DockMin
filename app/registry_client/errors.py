"""Errors raised by the registry client; every message is product-written and carries no input."""

from collections.abc import Mapping
from enum import StrEnum


class RegistryClientError(Exception):
    """Base of every error in this package."""


class AddressInvalid(RegistryClientError, ValueError):
    """A registry address that cannot be stored or connected to."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ExtrasInvalid(RegistryClientError, ValueError):
    """Provider extras that fail validation; `errors` maps a field name to its message."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{name}: {text}" for name, text in errors.items()))
        self.errors = dict(errors)


class ChallengeInvalid(RegistryClientError, ValueError):
    """A WWW-Authenticate header that is malformed or exceeds the parser's bounds."""


class TokenExchangeKind(StrEnum):
    """Why a credential exchange failed."""

    UNREACHABLE = "unreachable"
    ADDRESS_REFUSED = "address_refused"
    REFUSED_KEY = "refused_key"
    DENIED = "denied"
    CLOCK_SKEW = "clock_skew"
    MALFORMED = "malformed"
    OVERSIZED = "oversized"


class TokenExchangeFailed(RegistryClientError):
    """A credential exchange that did not produce a token; `message` is safe to show."""

    def __init__(self, kind: TokenExchangeKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
