"""Email delivery over SMTP in a worker thread, dialling only addresses the policy allows."""

import asyncio
import contextlib
import re
import smtplib
import socket
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Final, Literal

from pydantic import SecretStr

from app import netcheck, netpolicy
from app.logging import get_logger
from app.notifications import content
from app.notifications.content import NotificationEvent
from app.notifications.delivery import DeliveryError, classify_exception
from app.registry_client.tls import system_trust_context

type SmtpSecurity = Literal["starttls", "tls"]

DEFAULT_PORTS: Final[dict[SmtpSecurity, int]] = {"starttls": 587, "tls": 465}
CONNECT_TIMEOUT: Final = 10.0
IO_TIMEOUT: Final = 20.0
TOTAL_TIMEOUT: Final = 60.0
MAX_RECIPIENTS: Final = 10
MAX_CREDENTIAL_CHARS: Final = 256
EHLO_NAME: Final = "localhost"
MAX_REPLY_LINE_BYTES: Final = 8192
MAX_REPLY_LINES: Final = 100
MAX_REPLY_BYTES: Final = 64 * 1024

_log = get_logger(__name__)
_HOST_NAME: Final = re.compile(r"[A-Za-z0-9.-]{1,253}", re.ASCII)
_CREDENTIAL: Final = re.compile(r"[\x20-\x7e]{1,256}", re.ASCII)


@dataclass(frozen=True, slots=True)
class SmtpTarget:
    """A mail server and the envelope: `sender` and `recipients` are plain mailbox addresses."""

    host: str
    port: int
    security: SmtpSecurity
    sender: str
    recipients: tuple[str, ...]
    username: str | None = None
    password: SecretStr | None = None


def validate_target(target: SmtpTarget) -> None:
    """Raise ValueError unless the host, port, security, mailboxes and credentials are usable."""
    host = target.host
    if netpolicy.ip_literal(host) is None and (
        _HOST_NAME.fullmatch(host) is None or host.startswith((".", "-")) or ".." in host
    ):
        raise ValueError("host not allowed")
    if not 1 <= target.port <= 65535:
        raise ValueError("port not allowed")
    if target.security not in DEFAULT_PORTS:
        raise ValueError("security not allowed")
    content.validate_address(target.sender)
    if not 1 <= len(target.recipients) <= MAX_RECIPIENTS:
        raise ValueError("recipient count not allowed")
    for recipient in target.recipients:
        content.validate_address(recipient)
    if (target.username is None) != (target.password is None):
        raise ValueError("user name and password come as a pair")
    if target.username is not None and target.password is not None:
        if _CREDENTIAL.fullmatch(target.username) is None:
            raise ValueError("user name not allowed")
        if _CREDENTIAL.fullmatch(target.password.get_secret_value()) is None:
            raise ValueError("password not allowed")


async def send_email(
    target: SmtpTarget,
    events: Sequence[NotificationEvent],
    *,
    sent_at: datetime | None = None,
    resolver: netcheck.Resolver | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> None:
    """Send the events as one message; raise DeliveryError when it is not accepted.

    A message accepted for at least one recipient counts as sent.
    """
    try:
        validate_target(target)
        message = content.email_message(
            sender=target.sender,
            recipients=target.recipients,
            events=events,
            sent_at=sent_at or datetime.now(UTC),
        )
    except ValueError:
        raise DeliveryError("invalid_target") from None
    code: str | None = None
    connection = _Connection()
    try:
        context = ssl_context or system_trust_context()
        async with asyncio.timeout(TOTAL_TIMEOUT):
            connection.sock = await _connect(target, resolver or netcheck.system_resolver)
            await asyncio.to_thread(_converse, connection, target, message, context)
    except DeliveryError as failure:
        code = failure.code
    except TimeoutError:
        connection.abort()
        code = "timeout"
    except asyncio.CancelledError:
        connection.abort()
        raise
    except (OSError, smtplib.SMTPException) as exc:
        code = _failure_code(exc, connection.stage)
        _log.info("notification_smtp_failed", extra={"kind": code, "cause": type(exc).__name__})
    if code is not None:
        raise DeliveryError(code)


class _Connection:
    """What the worker thread holds open, so the caller can close it from outside."""

    def __init__(self) -> None:
        self.stage = "connect"
        self.sock: socket.socket | None = None
        self.client: smtplib.SMTP | None = None

    def abort(self) -> None:
        for held in (self.client.sock if self.client else None, self.sock):
            if held is not None:
                with contextlib.suppress(OSError):
                    held.shutdown(socket.SHUT_RDWR)
                held.close()


class _OversizeReply(smtplib.SMTPException):
    """A server reply longer, in lines or bytes, than a mail server sends."""


class _BoundedSmtp(smtplib.SMTP):
    """Reads replies under a cap on line length, line count and total size."""

    def getreply(self) -> tuple[int, bytes]:
        """Read one reply; raises _OversizeReply past the caps."""
        if self.file is None:
            self.file = self.sock.makefile("rb")  # type: ignore[union-attr]
        lines: list[bytes] = []
        total = 0
        code = -1
        while True:
            try:
                line = self.file.readline(MAX_REPLY_LINE_BYTES + 1)
            except OSError as exc:
                self.close()
                raise smtplib.SMTPServerDisconnected(
                    f"Connection unexpectedly closed: {type(exc).__name__}"
                ) from None
            if not line:
                self.close()
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            total += len(line)
            if (
                len(line) > MAX_REPLY_LINE_BYTES
                or len(lines) >= MAX_REPLY_LINES
                or total > MAX_REPLY_BYTES
            ):
                self.close()
                raise _OversizeReply("reply too large")
            lines.append(line[4:].strip(b" \t\r\n"))
            try:
                code = int(line[:3])
            except ValueError:
                code = -1
                break
            if line[3:4] != b"-":
                break
        return code, b"\n".join(lines)


class _PinnedSmtp(_BoundedSmtp):
    """Speaks over a socket that was dialled and checked beforehand."""

    def __init__(self, pinned: socket.socket, host: str, port: int) -> None:
        self._pinned = pinned
        super().__init__(host, port, local_hostname=EHLO_NAME, timeout=IO_TIMEOUT)

    def _get_socket(self, host: str, port: int, timeout: float | None) -> socket.socket:
        return self._pinned


class _PinnedSmtpTls(_BoundedSmtp, smtplib.SMTP_SSL):
    """Speaks TLS from the first byte over a socket that was dialled and checked beforehand."""

    def __init__(
        self, pinned: socket.socket, host: str, port: int, context: ssl.SSLContext
    ) -> None:
        self._pinned = pinned
        self._server_name = host
        super().__init__(host, port, local_hostname=EHLO_NAME, timeout=IO_TIMEOUT, context=context)

    def _get_socket(self, host: str, port: int, timeout: float | None) -> socket.socket:
        return self.context.wrap_socket(self._pinned, server_hostname=self._server_name)


async def _connect(target: SmtpTarget, resolver: netcheck.Resolver) -> socket.socket:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONNECT_TIMEOUT * netcheck.MAX_TCP_ATTEMPTS
    literal = netpolicy.ip_literal(target.host)
    if literal is not None:
        addresses = [netcheck.literal_address(literal, target.port)]
    else:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                infos = await resolver(target.host, target.port)
        except TimeoutError:
            raise DeliveryError("timeout") from None
        except OSError:
            raise DeliveryError("dns") from None
        addresses = netcheck.usable_addresses(infos)
        if not addresses:
            raise DeliveryError("dns")

    def budget(limit: float) -> tuple[float, bool]:
        remaining = deadline - loop.time()
        return min(limit, remaining), remaining < limit

    result = await netcheck.connect_first(addresses, attempt_limit=CONNECT_TIMEOUT, budget=budget)
    if result.sock is None:
        if set(result.kinds) == {"address_refused"}:
            raise DeliveryError("address_refused")
        if {"timeout", "overall"} & set(result.kinds):
            raise DeliveryError("timeout")
        raise DeliveryError("tcp")
    result.sock.settimeout(IO_TIMEOUT)
    return result.sock


def _converse(
    connection: _Connection, target: SmtpTarget, message: EmailMessage, context: ssl.SSLContext
) -> None:
    pinned = connection.sock
    assert pinned is not None
    client: smtplib.SMTP | None = None
    try:
        connection.stage = "greeting"
        if target.security == "tls":
            client = _PinnedSmtpTls(pinned, target.host, target.port, context)
        else:
            client = _PinnedSmtp(pinned, target.host, target.port)
        connection.client = client
        if target.security == "starttls":
            connection.stage = "starttls"
            client.starttls(context=context)
        if target.username is not None and target.password is not None:
            connection.stage = "auth"
            client.login(target.username, target.password.get_secret_value())
        connection.stage = "send"
        client.send_message(message, from_addr=target.sender, to_addrs=list(target.recipients))
        with contextlib.suppress(OSError, smtplib.SMTPException):
            client.quit()
    finally:
        if client is not None:
            client.close()
        pinned.close()


def _failure_code(exc: BaseException, stage: str) -> str:
    match exc:
        case _OversizeReply():
            return "protocol"
        case smtplib.SMTPAuthenticationError():
            return "smtp_auth"
        case smtplib.SMTPServerDisconnected():
            return classify_exception(exc)
        case smtplib.SMTPException() if stage == "starttls":
            return "tls"
        case smtplib.SMTPException() if stage == "auth":
            return "smtp_auth"
        case smtplib.SMTPException():
            return "smtp_refused"
        case _:
            return classify_exception(exc)
