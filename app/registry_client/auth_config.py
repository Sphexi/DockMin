"""Encodings of a registry login: the Docker auth configuration header and the RFC 7617 token."""

import base64
import binascii
import json

from pydantic import SecretStr

MAX_TOKEN_LENGTH = 16384
MAX_ISSUED_TOKEN_CHARS = 8 * 1024


def basic_token(username: str, password: SecretStr) -> SecretStr:
    """Return base64(username:password); a user name containing a colon cannot be encoded."""
    if ":" in username:
        raise ValueError("a user name cannot contain a colon")
    raw = f"{username}:{password.get_secret_value()}".encode()
    return SecretStr(base64.b64encode(raw).decode("ascii"))


def split_basic_token(token: SecretStr) -> tuple[str, SecretStr]:
    """Split a Basic token into user name and password; raise ValueError if it is not one."""
    value = token.get_secret_value()
    if not value or len(value) > MAX_TOKEN_LENGTH:
        raise ValueError("not a Basic token")
    text: str | None
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        text = None
    if text is None:
        raise ValueError("not a Basic token")
    username, separator, password = text.partition(":")
    if not separator or not username:
        raise ValueError("not a Basic token")
    return username, SecretStr(password)


def encode_registry_auth(*, username: str, password: SecretStr, server_address: str) -> SecretStr:
    """Return the value of the Engine API's X-Registry-Auth header."""
    document = {
        "username": username,
        "password": password.get_secret_value(),
        "serveraddress": server_address,
    }
    raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return SecretStr(base64.urlsafe_b64encode(raw).decode("ascii"))
