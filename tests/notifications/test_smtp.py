"""Email delivery: STARTTLS and implicit TLS to a local sink, failures as product codes."""

import io
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from pydantic import SecretStr

from app import netcheck
from app.notifications import smtp
from app.notifications.delivery import DeliveryError
from app.notifications.smtp import SmtpTarget, send_email, validate_target
from tests.notification_fixtures import StartSink
from tests.notification_support import (
    EVENT_TIME,
    SMTP_PASSWORD,
    SMTP_USER,
    SmtpSecurity,
    sample_event,
)
from tests.registry_client.loopback import Certificates

SENDER = "dockmin@example.test"
FIRST = "ops@example.test"
SECOND = "dev@example.test"
SETTING = "DOCKMIN_PERMITTED_INTERNAL_NETWORKS"
SHORT_TIMEOUT = 0.2
REFUSED_REPLY = "credentials refused"


def target_for(port: int, security: SmtpSecurity = "starttls", **changes: Any) -> SmtpTarget:
    base = SmtpTarget(
        host="127.0.0.1", port=port, security=security, sender=SENDER, recipients=(FIRST,)
    )
    return replace(base, **changes)


def credentials() -> dict[str, Any]:
    return {"username": SMTP_USER, "password": SecretStr(SMTP_PASSWORD)}


async def send(target: SmtpTarget, trusting: ssl.SSLContext, **options: Any) -> None:
    await send_email(
        target,
        [sample_event()],
        sent_at=EVENT_TIME,
        ssl_context=trusting,
        **options,
    )


async def code_of(target: SmtpTarget, trusting: ssl.SSLContext, **options: Any) -> str:
    with pytest.raises(DeliveryError) as raised:
        await send(target, trusting, **options)
    return raised.value.code


async def test_a_message_over_starttls_is_signed_in_and_delivered_encrypted(
    sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("starttls")
    target = target_for(sink.port, recipients=(FIRST, SECOND), **credentials())

    await send(target, trusting)

    [mail] = sink.messages
    message = mail.parsed()
    assert (mail.encrypted, mail.authenticated_as) == (True, SMTP_USER)
    assert (mail.sender, mail.recipients) == (SENDER, (FIRST, SECOND))
    assert message["Subject"] == "dockmin: Update applied"
    assert message["To"] == f"{FIRST}, {SECOND}"
    assert "Target: container web" in message.get_payload()
    assert sink.commands.index("STARTTLS") < sink.commands.index("AUTH")
    assert sink.commands.index("AUTH") < sink.commands.index("MAIL")


async def test_a_message_over_implicit_tls_is_delivered_encrypted(
    sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("tls")

    await send(target_for(sink.port, "tls", **credentials()), trusting)

    [mail] = sink.messages
    assert (mail.encrypted, mail.authenticated_as) == (True, SMTP_USER)
    assert "STARTTLS" not in sink.commands


async def test_a_server_that_needs_no_sign_in_is_never_sent_credentials(
    sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("starttls")

    await send(target_for(sink.port), trusting)

    assert "AUTH" not in sink.commands and sink.messages[0].authenticated_as is None


async def test_a_message_accepted_for_one_recipient_counts_as_sent(
    sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("starttls", refused_recipients=frozenset({SECOND}))

    await send(target_for(sink.port, recipients=(FIRST, SECOND)), trusting)

    assert sink.messages[0].recipients == (FIRST,)


async def test_a_server_that_refuses_every_recipient_is_a_refusal(
    sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("starttls", refused_recipients=frozenset({FIRST}))

    assert await code_of(target_for(sink.port), trusting) == "smtp_refused"
    assert sink.messages == []


async def test_a_wrong_password_is_an_authentication_failure_that_repeats_nothing_back(
    sinks: StartSink, trusting: ssl.SSLContext, log_stream: io.StringIO
) -> None:
    sink = await sinks("starttls")
    target = target_for(sink.port, username=SMTP_USER, password=SecretStr("not-the-password"))

    with pytest.raises(DeliveryError) as raised:
        await send(target, trusting)

    shown = f"{raised.value} {raised.value!r} {log_stream.getvalue()}"
    assert raised.value.code == "smtp_auth"
    assert "not-the-password" not in shown and REFUSED_REPLY not in shown
    assert sink.messages == []


async def test_a_server_that_does_not_offer_starttls_is_a_tls_failure_before_any_sign_in(
    sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("starttls", offer_starttls=False)

    code = await code_of(target_for(sink.port, **credentials()), trusting)

    assert code == "tls"
    assert not {"AUTH", "MAIL", "DATA"} & set(sink.commands)
    assert sink.messages == []


@pytest.mark.parametrize("security", ["starttls", "tls"])
async def test_a_certificate_the_context_does_not_trust_is_a_certificate_failure(
    sinks: StartSink, certificates: Certificates, trusting: ssl.SSLContext, security: SmtpSecurity
) -> None:
    sink = await sinks(security, identity=certificates.rogue_server)

    code = await code_of(target_for(sink.port, security, **credentials()), trusting)

    assert code == "certificate"
    assert "AUTH" not in sink.commands


@pytest.mark.parametrize("misbehaviour", ["many_lines", "long_line"])
async def test_a_reply_longer_than_a_mail_server_sends_is_a_protocol_failure(
    sinks: StartSink, trusting: ssl.SSLContext, misbehaviour: str
) -> None:
    sink = await sinks("starttls", misbehave=misbehaviour)

    assert await code_of(target_for(sink.port), trusting) == "protocol"


async def test_a_server_that_never_greets_is_a_timeout(
    sinks: StartSink, trusting: ssl.SSLContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smtp, "IO_TIMEOUT", SHORT_TIMEOUT)
    sink = await sinks("starttls", misbehave="silent")

    assert await code_of(target_for(sink.port), trusting) == "timeout"


async def test_a_port_nothing_listens_on_is_a_tcp_failure(
    trusting: ssl.SSLContext, permit_loopback: None
) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]

    assert await code_of(target_for(port), trusting) == "tcp"


async def test_a_name_that_does_not_resolve_is_a_dns_failure(trusting: ssl.SSLContext) -> None:
    async def unresolvable(host: str, port: int) -> Sequence[netcheck.AddrInfo]:
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    target = target_for(25, host="mail.example.test")

    assert await code_of(target, trusting, resolver=unresolvable) == "dns"


async def test_an_internal_server_is_refused_with_the_setting_named_and_never_spoken_to(
    sinks: StartSink, trusting: ssl.SSLContext, address_policy: Callable[..., None]
) -> None:
    sink = await sinks("starttls")
    address_policy()

    with pytest.raises(DeliveryError) as raised:
        await send(target_for(sink.port), trusting)

    assert raised.value.code == "address_refused" and SETTING in raised.value.message
    assert sink.commands == []


async def test_an_internal_server_is_spoken_to_once_the_permit_covers_it(
    sinks: StartSink, trusting: ssl.SSLContext, address_policy: Callable[..., None]
) -> None:
    sink = await sinks("starttls")
    address_policy("127.0.0.0/8")

    await send(target_for(sink.port), trusting)

    assert len(sink.messages) == 1


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"recipients": ()}, id="no-recipient"),
        pytest.param({"recipients": ("ops@example.test",) * 11}, id="too-many-recipients"),
        pytest.param({"recipients": ("a@b\r\nBcc: x@y",)}, id="header-smuggling"),
        pytest.param({"sender": "not a mailbox"}, id="sender"),
        pytest.param({"port": 0}, id="port"),
        pytest.param({"host": "bad host"}, id="host"),
        pytest.param({"username": SMTP_USER}, id="user-without-password"),
        pytest.param({"password": SecretStr("x")}, id="password-without-user"),
    ],
)
async def test_a_target_the_client_cannot_use_is_refused_before_any_connection(
    sinks: StartSink, trusting: ssl.SSLContext, changes: dict[str, Any]
) -> None:
    sink = await sinks("starttls")
    target = replace(target_for(sink.port), **changes)

    assert await code_of(target, trusting) == "invalid_target"
    assert sink.commands == []


def test_a_target_with_a_host_port_security_mailboxes_and_credentials_is_valid() -> None:
    validate_target(target_for(smtp.DEFAULT_PORTS["tls"], "tls", **credentials()))


@pytest.mark.parametrize("security", ["", "ssl", "plain"])
def test_a_security_other_than_starttls_or_tls_is_refused(security: str) -> None:
    changes: dict[str, Any] = {"security": security}

    with pytest.raises(ValueError, match="security"):
        validate_target(replace(target_for(587), **changes))


def test_the_default_ports_are_587_for_starttls_and_465_for_tls() -> None:
    assert smtp.DEFAULT_PORTS == {"starttls": 587, "tls": 465}
