"""Notification channels, their subscriptions and the link base URL, checked against the role."""

import asyncio
import json
import re
import ssl
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, get_args

import httpx
from pydantic import SecretStr

from app import netcheck
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.registry import NOTIFICATION_CHANNEL_SECRETS
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.notification_channel import (
    MAX_CHANNELS,
    ChannelKind,
    NotificationChannel,
    NotificationChannelRepository,
)
from app.db.repositories.setting import SettingRepository
from app.netcheck import HIDDEN_CATEGORIES
from app.netpolicy import is_internal_host, literal_host_refusal
from app.notifications.catalogue import CATALOGUE
from app.notifications.content import NotificationEvent, channel_test_event, validate_address
from app.notifications.delivery import (
    MESSAGES,
    DeliveryError,
    DiscordTarget,
    GotifyTarget,
    HttpDeliverer,
    HttpTarget,
    NtfyTarget,
    SlackTarget,
    TeamsTarget,
    WebhookTarget,
    validate_endpoint_url,
    validate_secret_header,
    validate_token,
    validate_topic,
)
from app.notifications.smtp import (
    DEFAULT_PORTS,
    SmtpSecurity,
    SmtpTarget,
    send_email,
    validate_target,
)
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.guards import require_administrator
from app.services.secret_mask import mask

LINK_BASE_URL_KEY: Final = "notification_link_base_url"
NAME_MAX: Final = 100
LINK_BASE_MAX: Final = 512
TEXT_MAX: Final = 4096
SECRET_MAX: Final = 1024
ENVIRONMENT_CHOICES: Final = 500
SCOPES: Final = ("all", "listed")
SECRETS_KEY: Final = "secrets"

NAME_EXISTS: Final = "A channel with this name already exists."
TOO_MANY_CHANNELS: Final = f"At most {MAX_CHANNELS} channels can be configured."
CHANGED_SINCE_REVIEW: Final = "Changed since it was reviewed; nothing was saved."
DELETE_CHANGED: Final = "Changed since it was reviewed; nothing was deleted."
SECRET_UNREADABLE: Final = (
    "The stored secrets cannot be read with the current encryption key. Enter them again."
)
DESTINATION_MOVED: Final = "The destination changed, so enter this again."
INSECURE_WARNING: Final = (
    "This address uses plain http, so notifications cross the network unencrypted."
)
INTERNAL_WARNING: Final = (
    "This address is on this server or a link-local network. It is accepted as configured."
)

_SIGNING_KEY: Final = re.compile(r"[\x20-\x7e]{1,256}", re.ASCII)
_RECIPIENT_SEPARATOR: Final = re.compile(r"[,;\s]+")
_PROBE: Final = SmtpTarget(
    host="example.com",
    port=587,
    security="starttls",
    sender="dockmin@example.com",
    recipients=("dockmin@example.com",),
)

__all__ = [
    "KINDS",
    "LINK_BASE_URL_KEY",
    "ChannelInput",
    "ChannelView",
    "EnvironmentChoice",
    "EventTypeChoice",
    "FieldSpec",
    "KindSpec",
    "NotificationService",
    "NotificationSettings",
    "SecretsUnreadable",
    "SubscriptionInput",
    "SubscriptionView",
    "build_target",
    "form_names",
    "open_secrets",
    "send_message",
]


class SecretsUnreadable(Exception):
    """A channel's stored secrets cannot be decrypted."""


class _Refused(ValueError):
    """An address the outbound policy refuses; the text names the setting that permits it."""


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One input of a channel form. A secret is never shown back; blank keeps the stored one."""

    name: str
    label: str
    invalid: str
    input_type: str = "text"
    secret: bool = False
    required: bool = False
    clearable: bool = False
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def clear_name(self) -> str:
        """Return the name of the checkbox that removes this field's stored value."""
        return f"clear_{self.name}"


@dataclass(frozen=True, slots=True)
class KindSpec:
    """A kind of channel and the inputs its form shows."""

    kind: ChannelKind
    label: str
    description: str
    fields: tuple[FieldSpec, ...]

    @property
    def destination(self) -> tuple[str, ...]:
        """Return the fields that say where messages go; changing one clears stored secrets."""
        return _DESTINATIONS.get(self.kind, ())


_DESTINATIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "webhook": ("url", "header_name"),
    "ntfy": ("server_url",),
    "gotify": ("server_url",),
    "email": ("host",),
}

_URL_INVALID: Final = (
    "Enter an http or https address with a host name and no user name or fragment."
)
_SERVER_INVALID: Final = (
    "Enter an http or https address with a host name and no user name, query or fragment."
)

KINDS: Final[Mapping[str, KindSpec]] = {
    spec.kind: spec
    for spec in (
        KindSpec(
            "webhook",
            "Webhook",
            "Posts a JSON document to an address.",
            (
                FieldSpec("url", "Address", _URL_INVALID, input_type="url", required=True),
                FieldSpec(
                    "header_name",
                    "Secret header name",
                    "Use 1 to 64 letters, digits and hyphens, and not a header dockmin sets.",
                    help="Sent with the value below on every request. Leave blank for none.",
                ),
                FieldSpec(
                    "header_value",
                    "Secret header value",
                    "Use visible characters, up to 1024 of them.",
                    input_type="password",
                    secret=True,
                ),
                FieldSpec(
                    "hmac_key",
                    "Signing key",
                    "Use 1 to 256 visible characters.",
                    input_type="password",
                    secret=True,
                    clearable=True,
                    help="When set, each request carries an HMAC-SHA256 signature of its body.",
                ),
            ),
        ),
        KindSpec(
            "ntfy",
            "ntfy",
            "Publishes to an ntfy topic.",
            (
                FieldSpec(
                    "server_url", "Server address", _SERVER_INVALID, input_type="url", required=True
                ),
                FieldSpec(
                    "topic",
                    "Topic",
                    "Use 1 to 64 letters, digits, underscores and hyphens.",
                    required=True,
                ),
                FieldSpec(
                    "token",
                    "Access token",
                    "Use visible characters without spaces, up to 512 of them.",
                    input_type="password",
                    secret=True,
                    clearable=True,
                ),
            ),
        ),
        KindSpec(
            "gotify",
            "Gotify",
            "Sends a message to a Gotify server.",
            (
                FieldSpec(
                    "server_url", "Server address", _SERVER_INVALID, input_type="url", required=True
                ),
                FieldSpec(
                    "token",
                    "Application token",
                    "Use visible characters without spaces, up to 512 of them.",
                    input_type="password",
                    secret=True,
                    required=True,
                ),
            ),
        ),
        KindSpec(
            "discord",
            "Discord",
            "Posts to a Discord channel through a webhook.",
            (
                FieldSpec(
                    "webhook_url",
                    "Webhook address",
                    _URL_INVALID,
                    input_type="password",
                    secret=True,
                    required=True,
                ),
            ),
        ),
        KindSpec(
            "slack",
            "Slack",
            "Posts to a Slack channel through an incoming webhook.",
            (
                FieldSpec(
                    "webhook_url",
                    "Webhook address",
                    _URL_INVALID,
                    input_type="password",
                    secret=True,
                    required=True,
                ),
            ),
        ),
        KindSpec(
            "teams",
            "Microsoft Teams",
            "Posts to a Teams channel through an incoming webhook.",
            (
                FieldSpec(
                    "webhook_url",
                    "Webhook address",
                    _URL_INVALID,
                    input_type="password",
                    secret=True,
                    required=True,
                ),
            ),
        ),
        KindSpec(
            "email",
            "Email",
            "Sends a plain-text message over SMTP.",
            (
                FieldSpec(
                    "host", "Mail server", "Enter a host name or an IP address.", required=True
                ),
                FieldSpec(
                    "security",
                    "Connection security",
                    "Choose STARTTLS or TLS.",
                    input_type="select",
                    required=True,
                    choices=(("starttls", "STARTTLS"), ("tls", "TLS")),
                ),
                FieldSpec(
                    "port",
                    "Port",
                    "Enter a port from 1 to 65535.",
                    input_type="number",
                    help="Blank uses 587 for STARTTLS and 465 for TLS.",
                ),
                FieldSpec(
                    "sender", "From address", "Enter a plain mailbox address.", required=True
                ),
                FieldSpec(
                    "recipients",
                    "Recipients",
                    "Enter 1 to 10 plain mailbox addresses.",
                    input_type="textarea",
                    required=True,
                    help="Separate addresses with commas or new lines.",
                ),
                FieldSpec(
                    "username",
                    "User name",
                    "Use 1 to 256 printable ASCII characters.",
                    help="Leave blank when the server needs no sign-in.",
                ),
                FieldSpec(
                    "password",
                    "Password",
                    "Use 1 to 256 printable ASCII characters.",
                    input_type="password",
                    secret=True,
                ),
            ),
        ),
    )
}


@dataclass(frozen=True, slots=True)
class ChannelInput:
    """A create or update request as the form submits it.

    A blank secret on an update keeps the stored one; a name in `clear` removes an optional one.
    """

    name: str
    kind: str
    is_enabled: bool = True
    values: Mapping[str, str] = field(default_factory=dict)
    secrets: Mapping[str, SecretStr] = field(default_factory=dict, repr=False)
    clear: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SubscriptionInput:
    """The event types a channel subscribes to and the environments it is limited to."""

    event_types: frozenset[str]
    environment_scope: str
    environment_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class ChannelView:
    """A channel for its page; a secret is reported as set, with a mask where one was asked for."""

    id: int
    name: str
    kind: ChannelKind
    kind_label: str
    is_enabled: bool
    values: Mapping[str, str]
    secrets_set: frozenset[str]
    summary: str
    warnings: tuple[str, ...]
    environment_scope: str
    event_type_count: int
    environment_count: int
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    secret_masks: Mapping[str, str] = field(default_factory=dict, repr=False)
    secrets_unreadable: bool = False


@dataclass(frozen=True, slots=True)
class EventTypeChoice:
    """One catalogue row with whether the channel subscribes to it."""

    key: str
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class EnvironmentChoice:
    """One environment with whether the channel lists it."""

    id: int
    name: str
    selected: bool


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    """What the subscriptions page shows."""

    channel_id: int
    channel_name: str
    event_types: tuple[EventTypeChoice, ...]
    environment_scope: str
    environments: tuple[EnvironmentChoice, ...]
    more_environments: bool


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    """Settings that apply to every channel."""

    link_base_url: str


@dataclass(frozen=True, slots=True)
class _Plan:
    name: str
    kind: ChannelKind
    is_enabled: bool
    settings: dict[str, Any]
    secrets: dict[str, str] = field(repr=False)
    changed: tuple[str, ...]
    replaced: tuple[str, ...]


def form_names(spec: KindSpec) -> frozenset[str]:
    """Return every posted field name a form of this kind may carry."""
    names = {"name", "kind", "is_enabled", "expected_updated_at"}
    for item in spec.fields:
        names.add(item.name)
        if item.clearable:
            names.add(item.clear_name)
    return frozenset(names)


def open_secrets(vault: Vault, channel: NotificationChannel) -> dict[str, str]:
    """Return the channel's secret fields, decrypted; raises SecretsUnreadable."""
    if channel.secrets_ciphertext is None:
        return {}
    location = SecretLocation(
        NOTIFICATION_CHANNEL_SECRETS.table, NOTIFICATION_CHANNEL_SECRETS.column, channel.id
    )
    try:
        opened = json.loads(vault.open(channel.secrets_ciphertext, location))
    except (DecryptionError, ValueError):
        raise SecretsUnreadable from None
    if not isinstance(opened, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in opened.items()
    ):
        raise SecretsUnreadable
    return dict(opened)


def build_target(
    channel: NotificationChannel, secrets: Mapping[str, str]
) -> HttpTarget | SmtpTarget:
    """Build the delivery target of a channel; raises KeyError, ValueError or TypeError."""
    stored = channel.settings
    match channel.kind:
        case "webhook":
            return WebhookTarget(
                url=stored["url"],
                hmac_key=_secret_of(secrets, "hmac_key"),
                header_name=stored.get("header_name") or None,
                header_value=_secret_of(secrets, "header_value"),
            )
        case "ntfy":
            return NtfyTarget(stored["server_url"], stored["topic"], _secret_of(secrets, "token"))
        case "gotify":
            return GotifyTarget(stored["server_url"], SecretStr(secrets["token"]))
        case "discord":
            return DiscordTarget(SecretStr(secrets["webhook_url"]))
        case "slack":
            return SlackTarget(SecretStr(secrets["webhook_url"]))
        case "teams":
            return TeamsTarget(SecretStr(secrets["webhook_url"]))
        case "email":
            return SmtpTarget(
                host=stored["host"],
                port=int(stored["port"]),
                security=stored["security"],
                sender=stored["sender"],
                recipients=tuple(stored["recipients"]),
                username=stored.get("username") or None,
                password=_secret_of(secrets, "password"),
            )


async def send_message(
    target: HttpTarget | SmtpTarget,
    events: Sequence[NotificationEvent],
    *,
    channel_name: str,
    http: HttpDeliverer,
    now: datetime,
    resolver: netcheck.Resolver | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> None:
    """Send the events as one message; raises DeliveryError."""
    if isinstance(target, SmtpTarget):
        await send_email(target, events, sent_at=now, resolver=resolver, ssl_context=ssl_context)
    else:
        await http.deliver(target, events, channel_name=channel_name)


def _secret_of(secrets: Mapping[str, str], name: str) -> SecretStr | None:
    value = secrets.get(name)
    return None if value is None else SecretStr(value)


def _valid_smtp(**changes: Any) -> bool:
    try:
        validate_target(replace(_PROBE, **changes))
    except ValueError:
        return False
    return True


def _check_url(url: str, *, allow_query: bool = True) -> None:
    validate_endpoint_url(url, allow_query=allow_query)
    refusal = literal_host_refusal(httpx.URL(url).host)
    if refusal is not None:
        raise _Refused(refusal)


def _check_header_name(name: str) -> None:
    validate_secret_header(name, "x")


def _check_signing_key(key: str) -> None:
    if _SIGNING_KEY.fullmatch(key) is None:
        raise ValueError("signing key not allowed")


def _name(text: str, problems: dict[str, str]) -> str:
    name = text.strip()
    if not name:
        problems["name"] = "Enter a name."
    elif len(name) > NAME_MAX:
        problems["name"] = f"The name is at most {NAME_MAX} characters."
    elif any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in name):
        problems["name"] = "The name may not contain control characters."
    return name


def _plain(
    item: FieldSpec,
    form: ChannelInput,
    problems: dict[str, str],
    check: Callable[[str], object] | None = None,
    *,
    required: bool | None = None,
) -> str:
    value = form.values.get(item.name, "").strip()
    if not value:
        if item.required if required is None else required:
            problems[item.name] = item.invalid
        return ""
    if len(value) > TEXT_MAX:
        problems[item.name] = item.invalid
        return ""
    if check is not None:
        try:
            check(value)
        except _Refused as refusal:
            problems[item.name] = str(refusal)
        except ValueError:
            problems[item.name] = item.invalid
    return value


def _secret(
    item: FieldSpec,
    form: ChannelInput,
    existing: Mapping[str, str],
    problems: dict[str, str],
    check: Callable[[str], None],
    *,
    required: bool | None = None,
) -> str | None:
    text = _supplied(form, item.name)
    if text:
        if len(text) > SECRET_MAX:
            problems[item.name] = item.invalid
            return None
        try:
            check(text)
        except _Refused as refusal:
            problems[item.name] = str(refusal)
            return None
        except ValueError:
            problems[item.name] = item.invalid
            return None
        return text
    if item.clearable and item.name in form.clear:
        return None
    kept = existing.get(item.name)
    if kept is None and (item.required if required is None else required):
        problems[item.name] = item.invalid
    return kept


def _supplied(form: ChannelInput, name: str) -> str:
    secret = form.secrets.get(name)
    return "" if secret is None else secret.get_secret_value()


def _fields(spec: KindSpec) -> dict[str, FieldSpec]:
    return {item.name: item for item in spec.fields}


def _plan_webhook(
    spec: KindSpec, form: ChannelInput, existing: Mapping[str, str], problems: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    items = _fields(spec)
    url = _plain(items["url"], form, problems, _check_url)
    header_name = _plain(items["header_name"], form, problems, _check_header_name)
    secrets: dict[str, str] = {}
    value_item = items["header_value"]

    def check_value(value: str) -> None:
        validate_secret_header(header_name or "x", value)

    header_value = _secret(
        value_item, form, existing, problems, check_value, required=bool(header_name)
    )
    if header_name and header_value is not None:
        secrets["header_value"] = header_value
    elif not header_name and _supplied(form, "header_value"):
        problems["header_name"] = items["header_name"].invalid
    key = _secret(items["hmac_key"], form, existing, problems, _check_signing_key)
    if key is not None:
        secrets["hmac_key"] = key
    return {"url": url, "header_name": header_name}, secrets


def _plan_ntfy(
    spec: KindSpec, form: ChannelInput, existing: Mapping[str, str], problems: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    items = _fields(spec)
    server = _plain(items["server_url"], form, problems, lambda v: _check_url(v, allow_query=False))
    topic = _plain(items["topic"], form, problems, validate_topic)
    secrets: dict[str, str] = {}
    token = _secret(items["token"], form, existing, problems, validate_token)
    if token is not None:
        secrets["token"] = token
    return {"server_url": server, "topic": topic}, secrets


def _plan_gotify(
    spec: KindSpec, form: ChannelInput, existing: Mapping[str, str], problems: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    items = _fields(spec)
    server = _plain(items["server_url"], form, problems, lambda v: _check_url(v, allow_query=False))
    secrets: dict[str, str] = {}
    token = _secret(items["token"], form, existing, problems, validate_token)
    if token is not None:
        secrets["token"] = token
    return {"server_url": server}, secrets


def _plan_chat(
    spec: KindSpec, form: ChannelInput, existing: Mapping[str, str], problems: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    secrets: dict[str, str] = {}
    address = _secret(_fields(spec)["webhook_url"], form, existing, problems, _check_url)
    if address is not None:
        secrets["webhook_url"] = address
    return {}, secrets


def _port(text: str, security: SmtpSecurity) -> int | None:
    if not text:
        return DEFAULT_PORTS[security]
    if not text.isascii() or not text.isdigit() or len(text) > 5 or not 1 <= int(text) <= 65535:
        return None
    return int(text)


def _security(text: str) -> SmtpSecurity | None:
    match text:
        case "starttls" | "tls":
            return text
        case _:
            return None


def _plan_email(
    spec: KindSpec, form: ChannelInput, existing: Mapping[str, str], problems: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    items = _fields(spec)

    def host_ok(value: str) -> None:
        if not _valid_smtp(host=value):
            raise ValueError("host not allowed")
        refusal = literal_host_refusal(value)
        if refusal is not None:
            raise _Refused(refusal)

    host = _plain(items["host"], form, problems, host_ok)
    security = _security(form.values.get("security", "").strip() or "starttls")
    port: int | None = None
    if security is None:
        problems["security"] = items["security"].invalid
    else:
        port = _port(form.values.get("port", "").strip(), security)
        if port is None:
            problems["port"] = items["port"].invalid
    sender = _plain(items["sender"], form, problems, validate_address)
    recipients = _recipients(items["recipients"], form, problems)
    username = _plain(items["username"], form, problems)
    secrets: dict[str, str] = {}
    if username:
        if not _valid_smtp(username=username, password=SecretStr("x")):
            problems["username"] = items["username"].invalid

        def password_ok(value: str) -> None:
            if not _valid_smtp(username="x", password=SecretStr(value)):
                raise ValueError("password not allowed")

        password = _secret(items["password"], form, existing, problems, password_ok, required=True)
        if password is not None:
            secrets["password"] = password
    elif _supplied(form, "password"):
        problems["username"] = items["username"].invalid
    return {
        "host": host,
        "port": port,
        "security": security,
        "sender": sender,
        "recipients": recipients,
        "username": username,
    }, secrets


def _recipients(item: FieldSpec, form: ChannelInput, problems: dict[str, str]) -> list[str]:
    text = form.values.get(item.name, "").strip()
    if len(text) > TEXT_MAX:
        problems[item.name] = item.invalid
        return []
    found = list(dict.fromkeys(part for part in _RECIPIENT_SEPARATOR.split(text) if part))
    if not _valid_smtp(recipients=tuple(found)):
        problems[item.name] = item.invalid
    return found


def _planner(kind: ChannelKind) -> Callable[..., tuple[dict[str, Any], dict[str, str]]]:
    match kind:
        case "webhook":
            return _plan_webhook
        case "ntfy":
            return _plan_ntfy
        case "gotify":
            return _plan_gotify
        case "discord" | "slack" | "teams":
            return _plan_chat
        case "email":
            return _plan_email


def _plan(
    form: ChannelInput, current: NotificationChannel | None, existing: Mapping[str, str]
) -> _Plan:
    spec = KINDS.get(form.kind)
    if spec is None:
        raise ValidationFailed({"kind": "Choose a kind of channel."})
    if current is not None and current.kind != spec.kind:
        raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
    problems: dict[str, str] = {}
    name = _name(form.name, problems)
    moved = current is not None and any(
        form.values.get(field, "").strip() != str(current.settings.get(field) or "")
        for field in spec.destination
    )
    kept = {} if moved else existing
    settings, secrets = _planner(spec.kind)(spec, form, kept, problems)
    if moved:
        for item in spec.fields:
            if item.secret and item.name in problems and not _supplied(form, item.name):
                problems[item.name] = DESTINATION_MOVED
    if problems:
        raise ValidationFailed(problems)
    settings[SECRETS_KEY] = sorted(secrets)
    replaced = tuple(
        item.name
        for item in spec.fields
        if item.secret
        and (
            bool(_supplied(form, item.name))
            or (item.clearable and item.name in form.clear and item.name in existing)
            or (item.name in existing and item.name not in secrets and moved)
        )
    )
    changed: tuple[str, ...] = ()
    if current is not None:
        changed = _differences(current, name, form.is_enabled, settings, replaced)
    return _Plan(name, spec.kind, form.is_enabled, settings, secrets, changed, replaced)


def _differences(
    current: NotificationChannel,
    name: str,
    is_enabled: bool,
    settings: Mapping[str, Any],
    replaced: tuple[str, ...],
) -> tuple[str, ...]:
    changed = [
        label
        for label, before, after in (
            ("name", current.name, name),
            ("is_enabled", current.is_enabled, is_enabled),
        )
        if before != after
    ]
    changed.extend(
        key
        for key in sorted(set(current.settings) | set(settings))
        if key != SECRETS_KEY and current.settings.get(key) != settings.get(key)
    )
    changed.extend(replaced)
    return tuple(dict.fromkeys(changed))


def _require_reentered(current: NotificationChannel, plan: _Plan, form: ChannelInput) -> None:
    """Refuse a save that would silently drop a secret the key can no longer open."""
    stored = current.settings.get(SECRETS_KEY)
    unwanted = {
        "header_value": not plan.settings.get("header_name"),
        "password": not plan.settings.get("username"),
    }
    problems = {
        name: SECRET_UNREADABLE
        for name in (stored if isinstance(stored, list) else [])
        if name not in plan.secrets and name not in form.clear and not unwanted.get(name, False)
    }
    if problems:
        raise ValidationFailed(problems)


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _summary(channel: NotificationChannel) -> str:
    stored = channel.settings
    match channel.kind:
        case "webhook":
            return str(stored.get("url", ""))
        case "ntfy":
            return f"{stored.get('server_url', '')}/{stored.get('topic', '')}"
        case "gotify":
            return str(stored.get("server_url", ""))
        case "discord" | "slack" | "teams":
            return "Webhook address stored"
        case "email":
            count = len(stored.get("recipients") or [])
            plural = "recipient" if count == 1 else "recipients"
            return f"{stored.get('host', '')}:{stored.get('port', '')}, {count} {plural}"


def _warnings(channel: NotificationChannel) -> tuple[str, ...]:
    found: list[str] = []
    for key in ("url", "server_url"):
        address = channel.settings.get(key)
        if not isinstance(address, str):
            continue
        try:
            parsed = httpx.URL(address)
        except httpx.InvalidURL:
            continue
        if parsed.scheme == "http":
            found.append(INSECURE_WARNING)
        if is_internal_host(parsed.host):
            found.append(INTERNAL_WARNING)
    host = channel.settings.get("host")
    if isinstance(host, str) and is_internal_host(host):
        found.append(INTERNAL_WARNING)
    return tuple(dict.fromkeys(found))


def _view_of(
    channel: NotificationChannel, event_types: frozenset[str], environments: frozenset[int]
) -> ChannelView:
    spec = KINDS[channel.kind]
    secrets_set = channel.settings.get(SECRETS_KEY)
    return ChannelView(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        kind_label=spec.label,
        is_enabled=channel.is_enabled,
        values={
            item.name: _value_text(channel.settings.get(item.name))
            for item in spec.fields
            if not item.secret
        },
        secrets_set=frozenset(secrets_set) if isinstance(secrets_set, list) else frozenset(),
        summary=_summary(channel),
        warnings=_warnings(channel),
        environment_scope=channel.environment_scope,
        event_type_count=len(event_types),
        environment_count=len(environments),
        last_success_at=channel.last_success_at,
        last_error_at=channel.last_error_at,
        last_error=channel.last_error,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _link_base(text: str) -> tuple[str, str | None]:
    value = text.strip()
    if not value:
        return "", None
    problem = (
        "Enter an absolute http or https address with no query or fragment, "
        f"up to {LINK_BASE_MAX} characters."
    )
    if len(value) > LINK_BASE_MAX or "?" in value or "#" in value:
        return "", problem
    try:
        validate_endpoint_url(value, allow_query=False)
    except ValueError:
        return "", problem
    return value.rstrip("/"), None


class NotificationService:
    """Applies channel rules; every method takes the acting principal and decides itself."""

    def __init__(
        self,
        db: Session,
        vault: Vault,
        clock: Clock,
        activity: ActivityRecorder,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: netcheck.Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._db = db
        self._vault = vault
        self._clock = clock
        self._activity = activity
        self._transport = transport
        self._resolver = resolver
        self._ssl_context = ssl_context
        self._channels = NotificationChannelRepository(db, clock)
        self._settings = SettingRepository(db, clock)

    async def list_channels(self, actor: Principal) -> list[ChannelView]:
        """Return every channel by name; administrators only."""
        require_administrator(actor)
        channels = await self._channels.list_channels()
        ids = [channel.id for channel in channels]
        subscribed = await self._channels.subscriptions_of(ids)
        listed = await self._channels.environment_ids_of(ids)
        return [
            _view_of(
                channel,
                subscribed.get(channel.id, frozenset()),
                listed.get(channel.id, frozenset()),
            )
            for channel in channels
        ]

    async def get(
        self, actor: Principal, channel_id: int, *, open_masks: bool = False
    ) -> ChannelView:
        """Return one channel; administrators only.

        With `open_masks` the stored secrets are opened once to build each set secret's mask;
        a sealed object the key cannot open sets `secrets_unreadable` instead.
        """
        require_administrator(actor)
        channel = await self._require(channel_id)
        view = _view_of(
            channel,
            await self._channels.subscriptions(channel_id),
            await self._channels.environment_ids(channel_id),
        )
        if not open_masks or not view.secrets_set:
            return view
        try:
            opened = await asyncio.to_thread(open_secrets, self._vault, channel)
        except SecretsUnreadable:
            return replace(view, secrets_unreadable=True)
        masks = {name: mask(opened.get(name, ""), expose=True) for name in sorted(view.secrets_set)}
        return replace(view, secret_masks=masks)

    async def settings(self, actor: Principal) -> NotificationSettings:
        """Return the settings that apply to every channel; administrators only."""
        require_administrator(actor)
        stored = await self._settings.get(LINK_BASE_URL_KEY)
        return NotificationSettings(link_base_url="" if stored is None else stored.value)

    async def subscription_view(self, actor: Principal, channel_id: int) -> SubscriptionView:
        """Return the catalogue and environments with the channel's choices marked."""
        require_administrator(actor)
        channel = await self._require(channel_id)
        selected_types = await self._channels.subscriptions(channel_id)
        selected_environments = await self._channels.environment_ids(channel_id)
        environments = EnvironmentRepository(self._db, self._clock)
        first = await environments.list_ids_page(limit=ENVIRONMENT_CHOICES + 1)
        more = len(first) > ENVIRONMENT_CHOICES
        ids = sorted(set(first[:ENVIRONMENT_CHOICES]) | selected_environments)
        names = await environments.names(ids)
        return SubscriptionView(
            channel_id=channel.id,
            channel_name=channel.name,
            event_types=tuple(
                EventTypeChoice(row.key, row.label, row.key in selected_types) for row in CATALOGUE
            ),
            environment_scope=channel.environment_scope,
            environments=tuple(
                EnvironmentChoice(item, names[item], item in selected_environments)
                for item in sorted(names, key=lambda item: (names[item].lower(), item))
            ),
            more_environments=more,
        )

    @audited(action="notification_channel_created", target_type="notification_channel")
    async def create(self, actor: Principal, form: ChannelInput) -> int:
        """Create a channel and seal its secrets; administrators only."""
        require_administrator(actor)
        plan = _plan(form, None, {})
        audit = current_audit()
        async with self._db.transaction():
            if await self._channels.count() >= MAX_CHANNELS:
                raise ValidationFailed({"name": TOO_MANY_CHANNELS})
            try:
                channel = await self._channels.create(
                    name=plan.name,
                    kind=plan.kind,
                    settings=plan.settings,
                    is_enabled=plan.is_enabled,
                    environment_scope="all",
                )
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            await self._store_secrets(channel.id, plan.secrets)
            audit.target_id = str(channel.id)
            audit.detail.update(
                name=plan.name,
                kind=plan.kind,
                is_enabled=plan.is_enabled,
                secrets=", ".join(sorted(plan.secrets)),
            )
        return channel.id

    @audited(
        action="notification_channel_updated",
        target_type="notification_channel",
        target_id=lambda a: str(a["channel_id"]),
    )
    async def update(
        self, actor: Principal, channel_id: int, form: ChannelInput, *, expected_updated_at: str
    ) -> None:
        """Change a channel if it is unchanged since the form was loaded; administrators only.

        A blank secret keeps the stored one.
        """
        require_administrator(actor)
        audit = current_audit()
        async with self._db.transaction():
            current = await self._require(channel_id)
            if current.updated_at != expected_updated_at:
                raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
            unreadable = False
            try:
                existing = open_secrets(self._vault, current)
            except SecretsUnreadable:
                existing, unreadable = {}, True
            plan = _plan(replace(form, kind=current.kind), current, existing)
            if unreadable:
                _require_reentered(current, plan, form)
            if not plan.changed:
                audit.unchanged()
                return
            try:
                await self._channels.update(
                    channel_id,
                    name=plan.name,
                    settings=plan.settings,
                    is_enabled=plan.is_enabled,
                    environment_scope=current.environment_scope,
                )
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            if plan.replaced or plan.secrets.keys() != existing.keys():
                await self._store_secrets(channel_id, plan.secrets)
            audit.detail.update(
                name=plan.name,
                kind=plan.kind,
                changed=", ".join(plan.changed),
                secrets_replaced=", ".join(plan.replaced),
            )

    @audited(
        action="notification_channel_deleted",
        target_type="notification_channel",
        target_id=lambda a: str(a["channel_id"]),
    )
    async def delete(self, actor: Principal, channel_id: int, *, expected_name: str) -> None:
        """Delete a channel with its subscriptions and queued deliveries; administrators only."""
        require_administrator(actor)
        audit = current_audit()
        async with self._db.transaction():
            current = await self._require(channel_id)
            if current.name != expected_name:
                raise ValidationFailed({"name": DELETE_CHANGED})
            await self._channels.delete(channel_id)
            audit.detail.update(name=current.name, kind=current.kind)

    @audited(
        action="notification_channel_tested",
        target_type="notification_channel",
        target_id=lambda a: str(a["channel_id"]),
    )
    async def test(self, actor: Principal, channel_id: int) -> None:
        """Send one fixed message through the saved channel; administrators only.

        Raises ValidationFailed under the key `test` with the product's wording when the message
        is not accepted. Only the saved channel is used, so no secret goes to an unsaved address.
        """
        require_administrator(actor)
        channel = await self._require(channel_id)
        audit = current_audit()
        audit.detail.update(name=channel.name, kind=channel.kind)
        try:
            target = build_target(channel, open_secrets(self._vault, channel))
        except SecretsUnreadable:
            audit.reason = "secrets_unreadable"
            raise ValidationFailed({"test": SECRET_UNREADABLE}) from None
        except (KeyError, ValueError, TypeError):
            audit.reason = "invalid_target"
            raise ValidationFailed({"test": MESSAGES["invalid_target"]}) from None
        started = self._clock()
        event = channel_test_event(
            channel_name=channel.name, actor_name=actor.username, time=started
        )
        try:
            async with HttpDeliverer(transport=self._transport, clock=self._clock) as http:
                await send_message(
                    target,
                    [event],
                    channel_name=channel.name,
                    http=http,
                    now=started,
                    resolver=self._resolver,
                    ssl_context=self._ssl_context,
                )
        except DeliveryError as failure:
            audit.reason = failure.code
            audit.detail["duration_ms"] = self._elapsed_ms(started)
            raise ValidationFailed({"test": failure.message}) from None
        audit.detail["duration_ms"] = self._elapsed_ms(started)

    @audited(
        action="notification_subscriptions_changed",
        target_type="notification_channel",
        target_id=lambda a: str(a["channel_id"]),
    )
    async def set_subscriptions(
        self, actor: Principal, channel_id: int, form: SubscriptionInput
    ) -> None:
        """Replace the channel's event types and environment scope; administrators only."""
        require_administrator(actor)
        audit = current_audit()
        problems = await self._subscription_problems(form)
        if problems:
            raise ValidationFailed(problems)
        channel = await self._require(channel_id)
        before_types = await self._channels.subscriptions(channel_id)
        before_environments = await self._channels.environment_ids(channel_id)
        if (
            before_types == form.event_types
            and before_environments == form.environment_ids
            and channel.environment_scope == form.environment_scope
        ):
            audit.unchanged()
            return
        added = sorted(form.event_types - before_types)
        removed = sorted(before_types - form.event_types)
        async with self._db.transaction():
            current = await self._require(channel_id)
            if current.environment_scope != form.environment_scope:
                await self._channels.update(
                    channel_id,
                    name=current.name,
                    settings=current.settings,
                    is_enabled=current.is_enabled,
                    environment_scope=form.environment_scope,  # type: ignore[arg-type]
                )
            await self._channels.replace_subscriptions(channel_id, form.event_types)
            try:
                await self._channels.replace_environments(channel_id, form.environment_ids)
            except MissingReferenceError:
                raise ValidationFailed(
                    {"environments": "An environment was deleted. Review the list again."}
                ) from None
            audit.detail.update(
                name=current.name,
                scope=form.environment_scope,
                environments=len(form.environment_ids),
                added=", ".join(added),
                removed=", ".join(removed),
            )

    @audited(action="notification_settings_updated", target_type="notification_settings")
    async def set_link_base_url(self, actor: Principal, value: str) -> None:
        """Set the address activity links are built on, or clear it; administrators only."""
        require_administrator(actor)
        address, problem = _link_base(value)
        if problem is not None:
            raise ValidationFailed({"link_base_url": problem})
        audit = current_audit()
        stored = await self._settings.get(LINK_BASE_URL_KEY)
        if (stored.value if stored is not None else "") == address:
            audit.unchanged()
            return
        async with self._db.transaction():
            if address:
                await self._settings.put(LINK_BASE_URL_KEY, "string", address)
            else:
                await self._settings.delete(LINK_BASE_URL_KEY)
            audit.detail.update(link_base_url_set=bool(address))

    async def _subscription_problems(self, form: SubscriptionInput) -> dict[str, str]:
        problems: dict[str, str] = {}
        known = {row.key for row in CATALOGUE}
        if not form.event_types <= known:
            problems["event_types"] = "Choose event types from the list."
        if form.environment_scope not in SCOPES:
            problems["environment_scope"] = "Choose all environments or the listed ones."
        names = await EnvironmentRepository(self._db, self._clock).names(form.environment_ids)
        if names.keys() != form.environment_ids:
            problems["environments"] = "An environment was deleted. Review the list again."
        return problems

    async def _store_secrets(self, channel_id: int, secrets: Mapping[str, str]) -> None:
        sealed: str | None = None
        if secrets:
            location = SecretLocation(
                NOTIFICATION_CHANNEL_SECRETS.table,
                NOTIFICATION_CHANNEL_SECRETS.column,
                channel_id,
            )
            sealed = self._vault.seal(json.dumps(dict(sorted(secrets.items()))), location)
        await self._channels.set_secrets(channel_id, sealed)

    async def _require(self, channel_id: int) -> NotificationChannel:
        channel = await self._channels.get(channel_id)
        if channel is None:
            raise ResourceNotFound("not found")
        return channel

    def _elapsed_ms(self, started: datetime) -> int:
        return int((self._clock() - started).total_seconds() * 1000)


assert set(get_args(ChannelKind)) == set(KINDS)
