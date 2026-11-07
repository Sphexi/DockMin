"""Tests for app/logging.py."""

import io
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.logging import (
    ACCESS_LOGGER,
    ACCESS_PATH_LIMIT,
    PINNED_CLIENT_LOGGERS,
    PINNED_WEBSOCKET_LOGGERS,
    REDACTED,
    REQUEST_ID_HEADER,
    AccessLogMiddleware,
    RequestIdMiddleware,
    Scrubber,
    WebSocketPayloadFilter,
    configure_logging,
)
from app.main import create_app
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_SESSION_TOKEN,
    CLIENT_ADDRESS,
    STANDARD_ID,
    STANDARD_SESSION_TOKEN,
    Browsers,
)
from tests.conftest import serving_app

SECRET = "hunter2-Zq9xK"
GENERATED_ID = re.compile(r"^[0-9a-f]{32}$")
SUPPLIED_ID = "req-abc.123"
CHAIN_PAIRS = 20_000
PRIVATE_KEY_BEGIN = "-----BEGIN PRIVATE KEY-----"
PRIVATE_KEY_END = "-----END PRIVATE KEY-----"
CREDENTIAL_FIELDS = [
    "X-Registry-Auth",
    "client_key_pem",
    "ca_cert_pem",
    "tls_material",
    "authorizationToken",
    "secretAccessKey",
    "sessionToken",
    "provider_extras",
]
REJECTED_ID = "not an id!"
RESTORE_ID = "restore-id-9f3a"
ID_WITH_NEWLINE = b"abc\n"
WEBHOOK_PREFIX = "abcd1234"
WEBHOOK_SECRET = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v"

ANSI_GREEN = "\x1b[32mgreen\x1b[0m"
ANSI_CSI = "\x9b31mred"
ADMIN_ONLY_PAGE = "/notifications"
QUERY_SENTINEL = "query-sentinel-8c1f"
ACCESS_RECORD_FIELDS = {"method", "path", "status", "duration_ms", "address", "user_id"}
LONG_PATH_LENGTH = 2000
ELLIPSIS = "\u2026"

logger = logging.getLogger("tests.logging")


@pytest.fixture
def scrubber() -> Scrubber:
    return Scrubber()


def test_scrub_redacts_a_top_level_sensitive_field(scrubber: Scrubber) -> None:
    result = scrubber.scrub({"api_key": SECRET, "user": "ana"})

    assert result == {"api_key": REDACTED, "user": "ana"}


def test_scrub_redacts_sensitive_fields_nested_in_dicts_and_lists(scrubber: Scrubber) -> None:
    payload = {"outer": {"db_password": SECRET}, "items": [{"token": SECRET, "id": 7}]}

    result = scrubber.scrub(payload)

    assert result == {"outer": {"db_password": REDACTED}, "items": [{"token": REDACTED, "id": 7}]}


@pytest.mark.parametrize(
    "text",
    [
        f"password={SECRET}",
        f"Error: password={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f'{{"api_key": "{SECRET}"}}',
        f"token: {SECRET}",
    ],
)
def test_scrub_text_redacts_sensitive_pairs_embedded_in_free_text(
    scrubber: Scrubber, text: str
) -> None:
    result = scrubber.scrub_text(text)

    assert SECRET not in result
    assert REDACTED in result


def test_scrub_text_redacts_a_pair_nested_inside_another_value(scrubber: Scrubber) -> None:
    result = scrubber.scrub_text(f"Error: password={SECRET}")

    assert result == f"Error: password={REDACTED}"


def test_scrub_text_keeps_surrounding_non_sensitive_pairs(scrubber: Scrubber) -> None:
    result = scrubber.scrub_text(f"password={SECRET} user=ana")

    assert result == f"password={REDACTED} user=ana"


@pytest.mark.parametrize("secret", ["a", SECRET, "x" * 200], ids=["short", "typical", "long"])
def test_redaction_is_identical_for_secrets_of_any_length(scrubber: Scrubber, secret: str) -> None:
    assert scrubber.scrub({"password": secret}) == {"password": REDACTED}
    assert scrubber.scrub_text(f"password={secret}") == f"password={REDACTED}"


@pytest.mark.parametrize(
    "name",
    ["primary_key", "DB_PASSWORD", "stripe-secret", "x.auth.token", "aws_credentials"],
)
def test_is_sensitive_matches_names_by_suffix(scrubber: Scrubber, name: str) -> None:
    assert scrubber.is_sensitive(name)


@pytest.mark.parametrize("name", ["Authorization", "set-cookie", "passphrase"])
def test_is_sensitive_matches_explicitly_listed_names(scrubber: Scrubber, name: str) -> None:
    assert scrubber.is_sensitive(name)


@pytest.mark.parametrize("name", ["username", "monkey", "request_id", "keyboard"])
def test_is_sensitive_leaves_ordinary_names_alone(scrubber: Scrubber, name: str) -> None:
    assert not scrubber.is_sensitive(name)


def test_is_sensitive_honours_configured_extra_names() -> None:
    assert Scrubber(extra_names=["session-id"]).is_sensitive("SESSION_ID")


@pytest.mark.parametrize("name", CREDENTIAL_FIELDS)
def test_scrub_redacts_the_value_of_a_docker_credential_field(
    scrubber: Scrubber, name: str
) -> None:
    result = scrubber.scrub({name: SECRET})

    assert result == {name: REDACTED}


def test_scrub_text_redacts_the_token_in_an_aws_authorisation_response_body(
    scrubber: Scrubber,
) -> None:
    body = json.dumps({"authorizationData": [{"authorizationToken": SECRET, "expiresAt": 1}]})

    result = scrubber.scrub_text(body)

    assert SECRET not in result
    assert "expiresAt" in result


@pytest.mark.parametrize("name", CREDENTIAL_FIELDS)
def test_scrub_text_redacts_a_docker_credential_field_in_free_text(
    scrubber: Scrubber, name: str
) -> None:
    result = scrubber.scrub_text(f"{name}={SECRET}")

    assert result == f"{name}={REDACTED}"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(f"dkwh_{WEBHOOK_PREFIX}_{WEBHOOK_SECRET}", id="whole"),
        pytest.param(f"dkwh_{WEBHOOK_PREFIX}_{WEBHOOK_SECRET[:10]}", id="truncated"),
        pytest.param(f"dkwh%5F{WEBHOOK_PREFIX}%5f{WEBHOOK_SECRET}", id="percent-encoded"),
    ],
)
def test_scrub_text_redacts_a_webhook_token_down_to_its_prefix(
    scrubber: Scrubber, token: str
) -> None:
    result = scrubber.scrub_text(f"POST /api/v1/hooks/{token} failed")

    assert result == f"POST /api/v1/hooks/dkwh_{WEBHOOK_PREFIX}_{REDACTED} failed"


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(f"{PRIVATE_KEY_BEGIN}\nMIIB{SECRET}\n{PRIVATE_KEY_END}", id="terminated"),
        pytest.param(f"{PRIVATE_KEY_BEGIN}\nMIIB{SECRET}", id="truncated"),
        pytest.param(
            f"-----BEGIN EC PRIVATE KEY-----\nMIIB{SECRET}\n-----END EC PRIVATE KEY-----",
            id="typed-header",
        ),
    ],
)
def test_scrub_text_redacts_a_private_key_block_embedded_in_prose(
    scrubber: Scrubber, block: str
) -> None:
    result = scrubber.scrub_text(f"could not load {block} for the environment")

    assert SECRET not in result
    assert result.startswith(f"could not load {REDACTED}")


def test_scrub_text_handles_a_very_long_chain_of_pairs_without_recursing(
    scrubber: Scrubber,
) -> None:
    result = scrubber.scrub_text("a=" * CHAIN_PAIRS + f"password={SECRET}")

    assert result == "a=" * CHAIN_PAIRS + f"password={REDACTED}"


def test_emitted_message_with_a_very_long_chain_of_pairs_is_still_scrubbed(
    log_stream: io.StringIO,
) -> None:
    logger.info("a=" * CHAIN_PAIRS + f"password={SECRET}")

    assert SECRET not in log_stream.getvalue()
    assert REDACTED in log_stream.getvalue()


def test_emitted_message_never_contains_the_secret(log_stream: io.StringIO) -> None:
    logger.info("login failed for password=%s", SECRET)

    assert SECRET not in log_stream.getvalue()
    assert REDACTED in log_stream.getvalue()


def test_emitted_extra_fields_are_redacted_including_nested_ones(
    log_stream: io.StringIO, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    logger.info("event", extra={"api_key": SECRET, "ctx": {"db_password": SECRET}, "user": "ana"})

    [record] = logged("event")
    assert SECRET not in log_stream.getvalue()
    assert record["api_key"] == REDACTED
    assert record["ctx"] == {"db_password": REDACTED}
    assert record["user"] == "ana"


def test_emitted_traceback_text_never_contains_the_secret(log_stream: io.StringIO) -> None:
    with pytest.raises(ValueError) as caught:
        raise ValueError(f"Error: password={SECRET}")

    logger.error("failed", exc_info=caught.value)

    assert SECRET not in log_stream.getvalue()
    assert f"password={REDACTED}" in log_stream.getvalue()


async def test_request_id_header_echoes_a_well_formed_incoming_id(client: AsyncClient) -> None:
    response = await client.get("/api/health", headers={REQUEST_ID_HEADER: SUPPLIED_ID})

    assert response.headers[REQUEST_ID_HEADER] == SUPPLIED_ID


async def test_request_id_header_replaces_a_malformed_incoming_id(client: AsyncClient) -> None:
    response = await client.get("/api/health", headers={REQUEST_ID_HEADER: REJECTED_ID})

    assert GENERATED_ID.match(response.headers[REQUEST_ID_HEADER])


async def test_request_id_header_with_a_trailing_newline_is_replaced() -> None:
    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        return None

    scope: Scope = {
        "type": "http",
        "headers": [(REQUEST_ID_HEADER.encode(), ID_WITH_NEWLINE)],
    }

    await RequestIdMiddleware(_respond_204)(scope, receive, send)

    assert GENERATED_ID.fullmatch(scope["state"]["request_id"])


@pytest.fixture
def websocket_loggers_restored() -> Iterator[None]:
    levels = {name: logging.getLogger(name).level for name in PINNED_WEBSOCKET_LOGGERS}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("configured", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_the_websocket_loggers_stay_at_warning_or_above_whatever_level_is_configured(
    root_logger_restored: None, websocket_loggers_restored: None, configured: str
) -> None:
    configure_logging(configured, stream=io.StringIO())

    levels = {name: logging.getLogger(name).level for name in PINNED_WEBSOCKET_LOGGERS}

    assert set(levels.values()) == {max(logging.WARNING, getattr(logging, configured))}


def record_from(path: str, level: int) -> logging.LogRecord:
    return logging.LogRecord("any.logger", level, path, 1, "frame", (), None)


@pytest.mark.parametrize(
    ("path", "level", "kept"),
    [
        ("/venv/lib/websockets/protocol.py", logging.DEBUG, False),
        ("/venv/lib/websockets/sansio/server.py", logging.DEBUG, False),
        ("/venv/lib/websockets/protocol.py", logging.INFO, True),
        ("/venv/lib/websockets/protocol.py", logging.WARNING, True),
        ("/venv/lib/websockets/protocol.py", logging.ERROR, True),
        ("/srv/app/services/terminals.py", logging.DEBUG, True),
        ("/venv/lib/other_websockets_tool.py", logging.DEBUG, True),
    ],
    ids=[
        "library-debug",
        "library-subpackage-debug",
        "library-info",
        "library-warning",
        "library-error",
        "application-debug",
        "similar-name-debug",
    ],
)
def test_debug_records_from_inside_the_websockets_package_are_dropped_on_any_logger(
    path: str, level: int, kept: bool
) -> None:
    assert WebSocketPayloadFilter().filter(record_from(path, level)) is kept


@pytest.fixture
def client_loggers_restored() -> Iterator[None]:
    levels = {name: logging.getLogger(name).level for name in PINNED_CLIENT_LOGGERS}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("configured", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_the_http_client_loggers_stay_at_warning_or_above_whatever_level_is_configured(
    root_logger_restored: None, client_loggers_restored: None, configured: str
) -> None:
    configure_logging(configured, stream=io.StringIO())

    levels = {name: logging.getLogger(name).level for name in PINNED_CLIENT_LOGGERS}

    assert set(levels.values()) == {max(logging.WARNING, getattr(logging, configured))}


def test_a_request_url_with_a_credential_is_not_logged_by_the_http_client_at_info(
    root_logger_restored: None, client_loggers_restored: None
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    logging.getLogger("httpx").info("HTTP Request: GET https://user:pw-marker@registry.test/v2/")
    logging.getLogger("httpcore.connection").debug("connect_tcp host='registry.test'")

    assert "pw-marker" not in stream.getvalue()


Logged = Callable[[str], list[dict[str, Any]]]


def test_color_messages_are_not_written_and_ansi_in_other_extras_is_escaped(
    logged: Logged,
) -> None:
    logger.info(
        "event",
        extra={"color_message": "plain", "tinted": ANSI_GREEN, "csi": ANSI_CSI, "kept": "plain"},
    )

    [record] = logged("event")
    assert "color_message" not in record
    assert record["kept"] == "plain"
    assert record["tinted"] == "\\u001b[32mgreen\\u001b[0m"
    assert record["csi"] == "\\u009b31mred"


def test_records_of_the_uvicorn_error_logger_are_written_as_uvicorn_server(
    logged: Logged,
) -> None:
    logging.getLogger("uvicorn.error").info("listening")
    logging.getLogger("uvicorn").info("other")

    [listening] = logged("listening")
    [other] = logged("other")
    assert (listening["logger"], other["logger"]) == ("uvicorn.server", "uvicorn")


def _scope(path: str, *, kind: str = "http", user_id: int | None = None) -> Scope:
    state = {} if user_id is None else {"authenticated_user_id": user_id}
    return {
        "type": kind,
        "method": "GET",
        "path": path,
        "client": (CLIENT_ADDRESS, 4000),
        "state": state,
    }


async def _drive(app: ASGIApp, scope: Scope) -> None:
    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        return None

    await AccessLogMiddleware(app)(scope, receive, send)


async def _respond_204(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _raise_before_sending(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError("boom")


async def test_the_access_log_cuts_an_overlong_path_and_records_the_status_sent(
    logged: Logged,
) -> None:
    await _drive(_respond_204, _scope("/" + "a" * LONG_PATH_LENGTH, user_id=ADMIN_ID))

    [record] = logged("http_request")
    assert record["path"] == "/" + "a" * (ACCESS_PATH_LIMIT - 1) + ELLIPSIS
    assert record["status"] == 204
    assert record["logger"] == ACCESS_LOGGER


async def test_the_access_log_keeps_a_path_carrying_an_escape_character_with_it_escaped(
    logged: Logged,
) -> None:
    await _drive(_respond_204, _scope("/x\x1b[31m/\x9bend"))

    [record] = logged("http_request")
    assert record["path"] == "/x\\u001b[31m/\\u009bend"


async def test_the_access_log_redacts_the_restore_id_in_the_progress_path(
    logged: Logged, log_stream: io.StringIO
) -> None:
    await _drive(_respond_204, _scope(f"/restore/progress/{RESTORE_ID}"))

    [record] = logged("http_request")
    assert record["path"] == f"/restore/progress/{REDACTED}"
    assert RESTORE_ID not in log_stream.getvalue()


async def test_the_access_log_records_500_and_lets_the_exception_through_when_the_app_raised(
    logged: Logged,
) -> None:
    with pytest.raises(RuntimeError):
        await _drive(_raise_before_sending, _scope("/x"))

    [record] = logged("http_request")
    assert (record["status"], record["user_id"]) == (500, None)


async def test_the_access_log_ignores_scopes_that_are_not_http(logged: Logged) -> None:
    await _drive(_respond_204, _scope("/ws", kind="websocket"))

    assert logged("http_request") == []


async def test_requests_are_not_logged_when_the_access_log_is_off(
    client: AsyncClient, logged: Logged
) -> None:
    await client.get("/login")

    assert logged("http_request") == []


@pytest.fixture
async def access_browsers(
    settings: Settings, provisioned_template: Path, log_stream: io.StringIO
) -> AsyncIterator[Browsers]:
    configured = settings.model_copy(update={"access_log": True})
    application = create_app(configured)
    configure_logging(configured.log_level, stream=log_stream)
    async with serving_app(application, configured, provisioned_template):
        browsers = Browsers(application)
        yield browsers
        await browsers.close()


async def test_each_request_writes_one_access_line_without_the_query_and_probes_write_none(
    access_browsers: Browsers, log_stream: io.StringIO, logged: Logged
) -> None:
    admin = access_browsers.open(session_token=ADMIN_SESSION_TOKEN)

    page = await admin.get(ADMIN_ONLY_PAGE, params={"probe": QUERY_SENTINEL})
    await admin.get("/api/health")
    await admin.get("/api/ready")

    [record] = logged("http_request")
    assert QUERY_SENTINEL not in log_stream.getvalue()
    assert record["request_id"] == page.headers[REQUEST_ID_HEADER]
    assert {key: record[key] for key in ACCESS_RECORD_FIELDS} == {
        "method": "GET",
        "path": ADMIN_ONLY_PAGE,
        "status": page.status_code,
        "duration_ms": record["duration_ms"],
        "address": CLIENT_ADDRESS,
        "user_id": ADMIN_ID,
    }
    assert isinstance(record["duration_ms"], int)


async def test_a_standard_user_refused_with_404_parity_still_has_a_user_id_on_the_access_line(
    access_browsers: Browsers, logged: Logged
) -> None:
    standard = access_browsers.open(session_token=STANDARD_SESSION_TOKEN)
    anonymous = access_browsers.open()

    await standard.get(ADMIN_ONLY_PAGE)
    await anonymous.get(ADMIN_ONLY_PAGE)

    standard_line, anonymous_line = logged("http_request")
    assert (standard_line["status"], standard_line["user_id"]) == (404, STANDARD_ID)
    assert anonymous_line["user_id"] is None
