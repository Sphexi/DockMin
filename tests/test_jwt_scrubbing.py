"""A compact JWS is redacted wherever it appears in text, and a sentinel proves it."""

import io
import logging
import re

import pytest

import app.logging as app_logging
from app.logging import REDACTED, Scrubber, configure_logging
from tests.jwt_support import ec_signer, platform_claims

SENTINEL = "sentinel-claim-value"
HEADER_LINE = "X-Twingate-Jwt: {token}"


def _token() -> str:
    return ec_signer().token(platform_claims(email=f"{SENTINEL}@example.test"))


def _parts(token: str) -> list[str]:
    return [token, *token.split(".")]


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("{token}", id="bare"),
        pytest.param("request failed for {token} twice", id="in-a-sentence"),
        pytest.param("Authorization: Bearer {token}", id="bearer"),
        pytest.param("url=https://host.test/path?jwt={token}&x=1", id="in-a-query"),
        pytest.param('{{"value": "{token}"}}', id="in-json"),
        pytest.param("{token},{token}", id="repeated"),
        pytest.param("ValueError: bad token '{token}'", id="in-exception-text"),
    ],
)
def test_a_compact_jws_is_redacted_from_text(template: str) -> None:
    token = _token()

    scrubbed = Scrubber().scrub_text(template.format(token=token))

    assert [part for part in _parts(token) if part in scrubbed] == []
    assert REDACTED in scrubbed


def test_a_token_with_an_empty_signature_is_redacted() -> None:
    token = _token().rsplit(".", 1)[0] + "."

    assert Scrubber().scrub_text(f"seen {token} here") == f"seen {REDACTED} here"


@pytest.mark.parametrize(
    "text",
    [
        "eyJ only a header",
        "one.two.three is not a token",
        "version 1.2.3 released",
        "file.name.tar",
        "xeyJhbGciOiJFUzI1NiJ9.e30.AAAA",
    ],
)
def test_text_that_is_not_a_token_is_left_alone(text: str) -> None:
    assert Scrubber().scrub_text(text) == text


def test_a_long_run_without_separators_is_scrubbed_in_linear_time() -> None:
    text = "eyJ" + "a" * 200_000

    assert Scrubber().scrub_text(text) == text


def test_a_logged_token_never_reaches_the_output_and_the_check_sees_it_when_the_pattern_is_off(
    monkeypatch: pytest.MonkeyPatch, root_logger_restored: None
) -> None:
    token = _token()
    protected = io.StringIO()
    configure_logging("INFO", stream=protected)
    logging.getLogger("tests.jwt").info(
        "refused", extra={"header": HEADER_LINE.format(token=token)}
    )
    monkeypatch.setattr(app_logging, "_COMPACT_JWS", re.compile(r"(?!x)x"))
    exposed = io.StringIO()
    configure_logging("INFO", stream=exposed)

    logging.getLogger("tests.jwt").info(
        "refused", extra={"header": HEADER_LINE.format(token=token)}
    )

    assert [part for part in _parts(token) if part in protected.getvalue()] == []
    assert token in exposed.getvalue()
