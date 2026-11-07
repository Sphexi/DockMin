"""The Basic token and the X-Registry-Auth header encodings."""

import base64
import json

import pytest
from pydantic import SecretStr

from app.registry_client.auth_config import (
    MAX_TOKEN_LENGTH,
    basic_token,
    encode_registry_auth,
    split_basic_token,
)

USERNAME = "service"
PASSWORD = "pass:word/with>odd?chars"
SERVER = "registry.example.com:5000"


def test_a_basic_token_round_trips_a_password_that_contains_a_colon() -> None:
    token = basic_token(USERNAME, SecretStr(PASSWORD))

    user, password = split_basic_token(token)

    assert (user, password.get_secret_value()) == (USERNAME, PASSWORD)
    assert base64.b64decode(token.get_secret_value()) == f"{USERNAME}:{PASSWORD}".encode()


def test_a_user_name_containing_a_colon_cannot_be_encoded() -> None:
    with pytest.raises(ValueError, match="colon"):
        basic_token("a:b", SecretStr(PASSWORD))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not base64 !",
        base64.b64encode(b"no-separator").decode(),
        base64.b64encode(b":no-user").decode(),
        base64.b64encode(b"\xff\xfe:bad-utf8").decode(),
        "A" * (MAX_TOKEN_LENGTH + 4),
    ],
    ids=["empty", "not-base64", "no-colon", "no-user", "not-utf8", "oversized"],
)
def test_text_that_is_not_a_basic_token_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a Basic token"):
        split_basic_token(SecretStr(text))


def test_the_registry_auth_header_is_url_safe_base64_of_the_three_documented_fields() -> None:
    header = encode_registry_auth(
        username=USERNAME, password=SecretStr("???>>>"), server_address=SERVER
    )

    decoded = json.loads(base64.urlsafe_b64decode(header.get_secret_value()))
    assert decoded == {"username": USERNAME, "password": "???>>>", "serveraddress": SERVER}
    assert not set("+/") & set(header.get_secret_value())


def test_the_registry_auth_header_and_the_basic_token_hide_their_value_in_a_repr() -> None:
    header = encode_registry_auth(
        username=USERNAME, password=SecretStr(PASSWORD), server_address=SERVER
    )
    token = basic_token(USERNAME, SecretStr(PASSWORD))

    assert header.get_secret_value() not in repr(header)
    assert token.get_secret_value() not in f"{token!r} {token}"
