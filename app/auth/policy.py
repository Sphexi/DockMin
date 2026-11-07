"""Password and username acceptance rules, applied wherever either is set."""

import re
from dataclasses import dataclass

from app.auth.errors import ValidationFailed
from app.auth.passwords import MAX_PASSWORD_LENGTH
from app.config import Settings

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{2,63}")
MAX_EXTERNAL_IDENTITY = 320


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """Minimum length and, when configured, how many character classes a password must span."""

    min_length: int
    required_classes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "PasswordPolicy":
        """Read the configured rules."""
        return cls(settings.password_min_length, settings.password_required_classes)

    def problems(self, password: str, username: str) -> list[str]:
        """Return every rule the password breaks; empty means acceptable."""
        found: list[str] = []
        if len(password) < self.min_length:
            found.append(f"Use at least {self.min_length} characters.")
        if len(password) > MAX_PASSWORD_LENGTH:
            found.append(f"Use at most {MAX_PASSWORD_LENGTH} characters.")
        if password.casefold() == username.casefold():
            found.append("The password must not be the same as the username.")
        if self.required_classes and _classes(password) < self.required_classes:
            found.append(
                f"Mix at least {self.required_classes} of: lower case, upper case, digits, symbols."
            )
        return found

    def check(self, password: str, username: str, *, field_name: str = "password") -> None:
        """Raise ValidationFailed against field_name unless the password is acceptable."""
        found = self.problems(password, username)
        if found:
            raise ValidationFailed({field_name: " ".join(found)})


def _classes(password: str) -> int:
    return sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )


def check_external_identity(identity: str, *, field_name: str = "external_identity") -> None:
    """Raise ValidationFailed unless the identity is 1 to 320 characters without control codes."""
    if not 1 <= len(identity) <= MAX_EXTERNAL_IDENTITY or any(
        ord(char) < 32 or ord(char) == 127 for char in identity
    ):
        raise ValidationFailed(
            {field_name: f"Use 1 to {MAX_EXTERNAL_IDENTITY} characters without control codes."}
        )


def check_username(username: str) -> None:
    """Raise ValidationFailed unless the username is 3 to 64 plain characters."""
    if USERNAME_PATTERN.fullmatch(username) is None:
        raise ValidationFailed(
            {
                "username": "Use 3 to 64 letters, digits, dots, hyphens, underscores or at signs, "
                "starting with a letter or digit."
            }
        )
