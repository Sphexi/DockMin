"""Local receivers for notification tests: an HTTP server and an SMTP sink on loopback."""

import asyncio
import base64
import json
import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email import message_from_string
from email.message import Message
from typing import Any, Final, Literal, Self

from app.db.engine import Database
from app.db.repositories.clocked import Clock
from app.db.repositories.notification_channel import EnvironmentScope, NotificationChannelRepository
from app.notifications.content import NotificationEvent
from tests.registry_client.loopback import CRLF, LOOPBACK, Identity, http_reply, server_context

SMTP_USER: Final = "mailer"
SMTP_PASSWORD: Final = "smtp-password-sentinel"
SMTP_REPLY_LINES: Final = 500
SMTP_LONG_LINE: Final = 9000
EVENT_TIME: Final = datetime(2030, 1, 2, 3, 0, 0, tzinfo=UTC)


def sample_event(**changes: Any) -> NotificationEvent:
    """Return a successful update event; keyword arguments replace its fields."""
    base = NotificationEvent(
        id=7,
        type="update_applied",
        label="Update applied",
        result="success",
        actor_type="subsystem",
        actor_name="container-updater",
        actor_rule="nightly",
        target_type="container",
        target_name="web",
        environment_id=3,
        environment_name="prod",
        time=EVENT_TIME,
    )
    return replace(base, **changes)


@dataclass(frozen=True, slots=True)
class Received:
    """One request an HTTP receiver got."""

    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class HttpReceiver:
    """Answers every POST with a fixed status and keeps what it received."""

    def __init__(
        self,
        *,
        status: int = 204,
        reply_body: bytes = b"",
        headers: dict[str, str] | None = None,
        delay: float = 0.0,
        identity: Identity | None = None,
    ) -> None:
        self.status = status
        self.reply_body = reply_body
        self.headers = headers or {}
        self.delay = delay
        self.identity = identity
        self.requests: list[Received] = []
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> Self:
        context = None if self.identity is None else server_context(self.identity)
        self._server = await asyncio.start_server(self._serve, LOOPBACK, 0, ssl=context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        await self._server.wait_closed()

    def url(self, path: str = "/") -> str:
        scheme = "http" if self.identity is None else "https"
        return f"{scheme}://{LOOPBACK}:{self.port}{path}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            head = await reader.readuntil(CRLF + CRLF)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {
                name.lower(): value.strip()
                for name, _, value in (line.partition(":") for line in lines[1:] if line)
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.requests.append(Received(method, target, headers, body))
            if self.delay:
                await asyncio.sleep(self.delay)
            writer.write(http_reply(self.status, self.reply_body, self.headers))
            await writer.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.transport.abort()


@dataclass(frozen=True, slots=True)
class MailReceived:
    """One message an SMTP sink accepted, with the facts of the connection it came over."""

    sender: str
    recipients: tuple[str, ...]
    text: str
    encrypted: bool
    authenticated_as: str | None

    def parsed(self) -> Message:
        return message_from_string(self.text)


type SmtpSecurity = Literal["starttls", "tls"]
type Misbehaviour = Literal["many_lines", "long_line", "silent"]


@dataclass(slots=True)
class SmtpSink:
    """A minimal SMTP server: STARTTLS or implicit TLS, optional AUTH, and scripted faults."""

    security: SmtpSecurity
    identity: Identity
    offer_starttls: bool = True
    refused_recipients: frozenset[str] = frozenset()
    misbehave: Misbehaviour | None = None
    credentials: tuple[str, str] = (SMTP_USER, SMTP_PASSWORD)
    messages: list[MailReceived] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    port: int = 0
    _server: asyncio.Server | None = None
    _writers: set[asyncio.StreamWriter] = field(default_factory=set)

    async def start(self) -> Self:
        context = server_context(self.identity)
        self._server = await asyncio.start_server(
            lambda r, w: self._session(r, w, context),
            LOOPBACK,
            0,
            ssl=context if self.security == "tls" else None,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        await self._server.wait_closed()

    async def _session(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, context: ssl.SSLContext
    ) -> None:
        self._writers.add(writer)
        try:
            await self._converse(reader, writer, context)
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.transport.abort()

    async def _converse(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, context: ssl.SSLContext
    ) -> None:
        async def say(line: str) -> None:
            writer.write(line.encode() + CRLF)
            await writer.drain()

        if self.misbehave == "silent":
            await reader.read()
            return
        if self.misbehave == "many_lines":
            writer.write((b"220-greeting" + CRLF) * SMTP_REPLY_LINES)
            await writer.drain()
            await reader.read(1)
            return
        if self.misbehave == "long_line":
            writer.write(b"220 " + b"a" * SMTP_LONG_LINE + CRLF)
            await writer.drain()
            await reader.read(1)
            return
        encrypted = self.security == "tls"
        user: str | None = None
        sender = ""
        recipients: list[str] = []
        await say("220 sink ESMTP")
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            self.commands.append(verb)
            if verb == "EHLO":
                features = ["sink", "SIZE 1000000"]
                if self.security == "starttls" and not encrypted and self.offer_starttls:
                    features.append("STARTTLS")
                if encrypted:
                    features.append("AUTH PLAIN")
                for feature in features[:-1]:
                    await say(f"250-{feature}")
                await say(f"250 {features[-1]}")
            elif verb == "STARTTLS":
                await say("220 ready")
                await writer.start_tls(context)
                encrypted = True
            elif verb == "AUTH":
                _, _, payload = line.split(" ", 2)
                _, name, password = base64.b64decode(payload).split(b"\0")
                if (name.decode(), password.decode()) == self.credentials:
                    user = name.decode()
                    await say("235 accepted")
                else:
                    await say("535 5.7.8 credentials refused")
            elif verb == "MAIL":
                sender = line.partition(":")[2].strip().strip("<>").split(">")[0]
                recipients = []
                await say("250 ok")
            elif verb == "RCPT":
                recipient = line.partition(":")[2].strip().strip("<>")
                if recipient in self.refused_recipients:
                    await say("550 no such mailbox")
                else:
                    recipients.append(recipient)
                    await say("250 ok")
            elif verb == "DATA":
                await say("354 go ahead")
                lines: list[str] = []
                while (data := (await reader.readline()).decode().rstrip("\r\n")) != ".":
                    lines.append(data[1:] if data.startswith("..") else data)
                self.messages.append(
                    MailReceived(sender, tuple(recipients), "\n".join(lines), encrypted, user)
                )
                await say("250 queued")
            elif verb == "QUIT":
                await say("221 bye")
                return
            else:
                await say("250 ok")


async def add_channel(
    database: Database,
    clock: Clock,
    name: str,
    event_types: Iterable[str],
    *,
    scope: EnvironmentScope = "all",
    environments: Iterable[int] = (),
    enabled: bool = True,
    url: str = "https://hooks.example.test/receive",
) -> int:
    """Insert a webhook channel with these subscriptions and environments; return its id."""
    async with database.session() as db:
        channels = NotificationChannelRepository(db, clock)
        async with db.transaction():
            channel = await channels.create(
                name=name,
                kind="webhook",
                settings={"url": url, "header_name": "", "secrets": []},
                is_enabled=enabled,
                environment_scope=scope,
            )
            await channels.replace_subscriptions(channel.id, event_types)
            await channels.replace_environments(channel.id, environments)
    return channel.id
