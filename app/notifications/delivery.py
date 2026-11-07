"""Delivery of notifications over HTTP, with failures reduced to product codes."""

import asyncio
import base64
import hashlib
import hmac
import re
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, Self

import httpcore
import httpx
from pydantic import SecretStr

from app import netpolicy
from app.logging import get_logger
from app.net_transport import policy_transport
from app.notifications import content
from app.notifications.content import NotificationEvent
from app.registry_client.tls import system_trust_context

CONNECT_TIMEOUT: Final = 10.0
REQUEST_TIMEOUT: Final = 10.0
TOTAL_TIMEOUT: Final = 30.0
MAX_RESPONSE_BYTES: Final = 64 * 1024
MAX_URL_CHARS: Final = 2048
MAX_TOKEN_CHARS: Final = 512
MAX_HEADER_VALUE_CHARS: Final = 1024
SIGNATURE_HEADER: Final = "X-Dockmin-Signature"
USER_AGENT: Final = "dockmin"

_log = get_logger(__name__)
_HEADER_NAME: Final = re.compile(r"[A-Za-z0-9-]{1,64}", re.ASCII)
_HEADER_VALUE: Final = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,1022}[\x21-\x7e])?", re.ASCII)
_TOKEN: Final = re.compile(r"[\x21-\x7e]{1,512}", re.ASCII)
_TOPIC: Final = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_URL_TEXT: Final = re.compile(r"[\x21-\x7e]+", re.ASCII)
_STATUS_CODE: Final = re.compile(r"status_[1-5][0-9]{2}", re.ASCII)
_REFUSED_HEADERS: Final = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        SIGNATURE_HEADER.lower(),
    }
)
_NAME_LOOKUP_FAILURES: Final = frozenset(
    {"name lookup failed", "name resolved to no usable address"}
)

MESSAGES: Final[dict[str, str]] = {
    "address_refused": netpolicy.REFUSAL_TEXT,
    "dns": "The host name of the receiver could not be resolved.",
    "tcp": "A connection to the receiver could not be made, or it was lost.",
    "tls": "The TLS connection to the receiver could not be established.",
    "certificate": "The certificate of the receiver was not accepted.",
    "timeout": "The receiver did not answer in time.",
    "protocol": "The receiver sent a reply that could not be understood.",
    "invalid_target": "The address or credentials stored for this channel are not usable.",
    "smtp_auth": "The mail server refused the user name or password.",
    "smtp_refused": "The mail server refused the message.",
}


def message_for(code: str) -> str:
    """Return the fixed text for a delivery error code; raise ValueError for an unknown code."""
    if _STATUS_CODE.fullmatch(code):
        return f"The receiver answered with HTTP status {code.removeprefix('status_')}."
    try:
        return MESSAGES[code]
    except KeyError:
        raise ValueError("unknown delivery error code") from None


class DeliveryError(Exception):
    """A delivery that did not succeed; `code` is a product code and `message` its fixed text.

    Codes: address_refused, dns, tcp, tls, certificate, timeout, protocol, invalid_target,
    smtp_auth, smtp_refused, and status_<nnn> for a non-2xx reply.
    """

    def __init__(self, code: str) -> None:
        text = message_for(code)
        super().__init__(text)
        self.code = code
        self.message = text


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    """The generic webhook: JSON body, optional HMAC signature, optional secret header."""

    url: str
    hmac_key: SecretStr | None = None
    header_name: str | None = None
    header_value: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class NtfyTarget:
    """An ntfy topic on a server, with an optional access token."""

    server_url: str
    topic: str
    token: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class GotifyTarget:
    """A Gotify server and an application token."""

    server_url: str
    token: SecretStr


@dataclass(frozen=True, slots=True)
class DiscordTarget:
    """A Discord webhook; the URL is the credential."""

    webhook_url: SecretStr


@dataclass(frozen=True, slots=True)
class SlackTarget:
    """A Slack incoming webhook; the URL is the credential."""

    webhook_url: SecretStr


@dataclass(frozen=True, slots=True)
class TeamsTarget:
    """A Microsoft Teams incoming webhook; the URL is the credential."""

    webhook_url: SecretStr


type HttpTarget = (
    WebhookTarget | NtfyTarget | GotifyTarget | DiscordTarget | SlackTarget | TeamsTarget
)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedRequest:
    """A POST ready to send; `url` and `headers` may hold secrets, so the repr shows neither."""

    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __repr__(self) -> str:
        return f"PreparedRequest(body={len(self.body)} bytes)"


def validate_endpoint_url(url: str, *, allow_query: bool = True) -> str:
    """Return an http or https URL unchanged, or raise ValueError.

    Refused: user information, a fragment, whitespace or non-ASCII characters, no host, and
    (when `allow_query` is false) a query.
    """
    if len(url) > MAX_URL_CHARS or _URL_TEXT.fullmatch(url) is None:
        raise ValueError("not a usable URL")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise ValueError("not a usable URL") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.host
        or parsed.userinfo
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError("not a usable URL")
    return url


def validate_secret_header(name: str, value: str) -> None:
    """Raise ValueError unless the pair is a header the webhook may send."""
    if _HEADER_NAME.fullmatch(name) is None or name.lower() in _REFUSED_HEADERS:
        raise ValueError("header name not allowed")
    if _HEADER_VALUE.fullmatch(value) is None:
        raise ValueError("header value not allowed")


def validate_token(token: str) -> None:
    """Raise ValueError unless the token is visible ASCII without spaces."""
    if _TOKEN.fullmatch(token) is None:
        raise ValueError("token not allowed")


def validate_topic(topic: str) -> None:
    """Raise ValueError unless the topic is 1 to 64 letters, digits, underscores or hyphens."""
    if _TOPIC.fullmatch(topic) is None:
        raise ValueError("topic not allowed")


def sign(key: SecretStr, body: bytes) -> str:
    """Return the signature header value: `sha256=` and the hex HMAC-SHA256 of the body."""
    secret = key.get_secret_value().encode("utf-8")
    if not secret:
        raise ValueError("empty signing key")
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def prepare(
    target: HttpTarget,
    events: Sequence[NotificationEvent],
    *,
    channel_name: str,
    sent_at: datetime,
) -> PreparedRequest:
    """Render the request for a target; raise ValueError when the target or events are unusable."""
    match target:
        case WebhookTarget():
            return _webhook(target, events, channel_name, sent_at)
        case NtfyTarget():
            return _ntfy(target, events)
        case GotifyTarget():
            validate_token(target.token.get_secret_value())
            url = _joined(target.server_url, "message")
            headers = (("X-Gotify-Key", target.token.get_secret_value()),)
            return _json_request(url, headers, content.gotify_payload(events))
        case DiscordTarget():
            return _chat(target.webhook_url, content.discord_payload(events))
        case SlackTarget():
            return _chat(target.webhook_url, content.slack_payload(events))
        case TeamsTarget():
            return _chat(target.webhook_url, content.teams_payload(events))


def classify_exception(exc: BaseException) -> str:
    """Reduce a transport failure to a product code by walking its cause chain."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 8 and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for link in chain:
        if isinstance(link, netpolicy.AddressRefused):
            return "address_refused"
        if isinstance(link, ssl.SSLCertVerificationError):
            return "certificate"
        if isinstance(link, ssl.SSLError):
            return "tls"
        if isinstance(link, socket.gaierror) or (
            isinstance(link, httpx.ConnectError | httpcore.ConnectError)
            and str(link) in _NAME_LOOKUP_FAILURES
        ):
            return "dns"
        if isinstance(link, TimeoutError | httpx.TimeoutException | httpcore.TimeoutException):
            return "timeout"
    if any(isinstance(link, OSError | httpx.NetworkError) for link in chain):
        return "tcp"
    return "protocol"


class HttpDeliverer:
    """Sends rendered notifications to HTTP receivers through the outbound address policy.

    Redirects are not followed and the environment's proxy settings are ignored. Call `aclose`
    when finished.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connections and the transport's resolver threads."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def deliver(
        self,
        target: HttpTarget,
        events: Sequence[NotificationEvent],
        *,
        channel_name: str,
    ) -> None:
        """Send the events as one message; raise DeliveryError unless the receiver answers 2xx."""
        try:
            request = prepare(target, events, channel_name=channel_name, sent_at=self._clock())
        except ValueError:
            raise DeliveryError("invalid_target") from None
        await self._post(request)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = self._transport or policy_transport(
                verify=system_trust_context(),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
            )
            self._client = httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
            )
        return self._client

    async def _post(self, request: PreparedRequest) -> None:
        code: str | None = None
        try:
            client = self._http_client()
            async with asyncio.timeout(TOTAL_TIMEOUT):
                status = await _exchange(client, request)
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            OSError,
            TimeoutError,
            netpolicy.AddressRefused,
        ) as exc:
            code = classify_exception(exc)
            _log.info(
                "notification_request_failed", extra={"kind": code, "cause": type(exc).__name__}
            )
        except ValueError:
            code = "invalid_target"
        else:
            if 200 <= status < 300:
                return
            code = f"status_{status}" if 100 <= status <= 599 else "protocol"
        raise DeliveryError(code)


async def _exchange(client: httpx.AsyncClient, request: PreparedRequest) -> int:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **dict(request.headers)}
    async with client.stream("POST", request.url, headers=headers, content=request.body) as reply:
        received = 0
        async for chunk in reply.aiter_raw():
            received += len(chunk)
            if received > MAX_RESPONSE_BYTES:
                break
        return reply.status_code


def _webhook(
    target: WebhookTarget,
    events: Sequence[NotificationEvent],
    channel_name: str,
    sent_at: datetime,
) -> PreparedRequest:
    url = validate_endpoint_url(target.url)
    body = content.webhook_body(channel_name, events, sent_at)
    headers = [("Content-Type", "application/json")]
    if (target.header_name is None) != (target.header_value is None):
        raise ValueError("header name and value come as a pair")
    if target.header_name is not None and target.header_value is not None:
        value = target.header_value.get_secret_value()
        validate_secret_header(target.header_name, value)
        headers.append((target.header_name, value))
    if target.hmac_key is not None:
        headers.append((SIGNATURE_HEADER, sign(target.hmac_key, body)))
    return PreparedRequest(url, tuple(headers), body)


def _ntfy(target: NtfyTarget, events: Sequence[NotificationEvent]) -> PreparedRequest:
    validate_topic(target.topic)
    url = _joined(target.server_url, target.topic)
    message = content.ntfy_message(events)
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Title", _header_text(message.title)),
        ("Tags", message.tags),
    ]
    if target.token is not None:
        token = target.token.get_secret_value()
        validate_token(token)
        headers.append(("Authorization", f"Bearer {token}"))
    return PreparedRequest(url, tuple(headers), message.body.encode("utf-8"))


def _chat(webhook_url: SecretStr, payload: content.Rendering) -> PreparedRequest:
    return _json_request(validate_endpoint_url(webhook_url.get_secret_value()), (), payload)


def _json_request(
    url: str, headers: tuple[tuple[str, str], ...], payload: content.Rendering
) -> PreparedRequest:
    return PreparedRequest(
        url, (("Content-Type", "application/json"), *headers), content.encode_json(payload)
    )


def _joined(server_url: str, segment: str) -> str:
    validate_endpoint_url(server_url, allow_query=False)
    return str(httpx.URL(server_url.rstrip("/") + "/" + segment))


def _header_text(text: str) -> str:
    if text.isascii():
        return text
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="
