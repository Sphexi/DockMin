"""Notification events and their renderings: a generic JSON body and one format per kind."""

import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any, Final, Literal

SCHEMA_VERSION: Final = 1
MAX_EVENTS_PER_MESSAGE: Final = 20
MAX_FIELD_CHARS: Final = 160
MAX_LINK_CHARS: Final = 600
MAX_ADDRESS_CHARS: Final = 254
SUBJECT_PREFIX: Final = "dockmin: "
DISCORD_DESCRIPTION_LIMIT: Final = 4000

type ActorType = Literal["user", "subsystem"]
type Rendering = dict[str, Any]
type Escape = Callable[[str], str]
type LinkMarkup = Callable[[str], str]

_COLOR_FAILURE: Final = 0xE74C3C
_COLOR_SUCCESS: Final = 0x2ECC71
_COLOR_OTHER: Final = 0x3498DB
_LINK: Final = re.compile(r"https?://[\x21-\x7e]+", re.ASCII)
_LINK_ENCODED: Final = str.maketrans({c: f"%{ord(c):02X}" for c in "()|<>\"'"})
_LOCAL_PART: Final = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}", re.ASCII)
_DOMAIN_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
_SPACING: Final = frozenset({"Cc", "Zl", "Zp"})
_DROPPED: Final = frozenset({"Cf", "Cs", "Co", "Cn"})
_DISCORD_SPECIALS: Final = frozenset("\\*_~`|>#[]()<:@!")
_TEAMS_SPECIALS: Final = frozenset("\\*_~`|>#[]()<")


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """The fixed set of fields a notification carries; an activity row's `detail` is not part."""

    id: int
    type: str
    label: str
    result: str
    actor_type: ActorType
    actor_name: str
    actor_rule: str | None
    target_type: str
    target_name: str
    environment_id: int | None
    environment_name: str | None
    time: datetime
    link: str | None = None


@dataclass(frozen=True, slots=True)
class NtfyMessage:
    """An ntfy publication: `title` and `tags` go in headers, `body` is the text."""

    title: str
    tags: str
    body: str


@dataclass(frozen=True, slots=True)
class _View:
    event: NotificationEvent
    label: str
    result: str
    actor: str
    actor_name: str
    actor_rule: str | None
    target_type: str
    target_name: str
    target: str
    environment_name: str | None
    time: str
    link: str | None


def clean(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """Strip control, format, surrogate and unassigned characters, collapse space, cut to limit."""
    kept: list[str] = []
    for char in text[: limit * 4]:
        category = unicodedata.category(char)
        if category in _SPACING:
            kept.append(" ")
        elif category not in _DROPPED:
            kept.append(char)
    collapsed = " ".join("".join(kept).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def iso_utc(moment: datetime) -> str:
    """Format an instant as ISO 8601 UTC with a Z suffix; a naive value is taken as UTC."""
    return _aware(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_json(value: Rendering) -> bytes:
    """Encode a rendering as compact UTF-8 JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def validate_address(text: str) -> str:
    """Return a plain mailbox (local part, `@`, domain) unchanged, or raise ValueError."""
    local, separator, domain = text.partition("@")
    labels = domain.split(".")
    if (
        not separator
        or len(text) > MAX_ADDRESS_CHARS
        or _LOCAL_PART.fullmatch(local) is None
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("not a plain mailbox address")
    return text


def channel_test_event(*, channel_name: str, actor_name: str, time: datetime) -> NotificationEvent:
    """Build the fixed event a channel test sends."""
    return NotificationEvent(
        id=0,
        type="test",
        label="Test notification",
        result="success",
        actor_type="user",
        actor_name=actor_name,
        actor_rule=None,
        target_type="notification_channel",
        target_name=channel_name,
        environment_id=None,
        environment_name=None,
        time=time,
    )


def webhook_body(
    channel_name: str, events: Sequence[NotificationEvent], sent_at: datetime
) -> bytes:
    """Render the generic webhook body: the exact bytes to send and to sign."""
    views = _views(events)
    return encode_json(
        {
            "schema_version": SCHEMA_VERSION,
            "sent_at": iso_utc(sent_at),
            "channel": clean(channel_name),
            "events": [_webhook_event(view) for view in views],
        }
    )


def ntfy_message(events: Sequence[NotificationEvent]) -> NtfyMessage:
    """Render an ntfy publication in plain text."""
    views = _views(events)
    tag = {"failure": "x", "success": "white_check_mark"}.get(_overall(views), "information_source")
    return NtfyMessage(title=_prefixed(views, str), tags=tag, body=_plain_text(views))


def gotify_payload(events: Sequence[NotificationEvent]) -> Rendering:
    """Render a Gotify message body in plain text."""
    views = _views(events)
    return {"title": _prefixed(views, str), "message": _plain_text(views), "priority": 5}


def discord_payload(events: Sequence[NotificationEvent]) -> Rendering:
    """Render a Discord webhook body: one embed, no mentions, markdown escaped."""
    views = _views(events)
    color = {"failure": _COLOR_FAILURE, "success": _COLOR_SUCCESS}.get(
        _overall(views), _COLOR_OTHER
    )
    blocks = [_markdown_block(view, escape_discord, _markdown_link) for view in views]
    return {
        "content": _prefixed(views, escape_discord),
        "embeds": [
            {
                "description": _fit(blocks, DISCORD_DESCRIPTION_LIMIT),
                "color": color,
                "timestamp": max(view.time for view in views),
            }
        ],
        "allowed_mentions": {"parse": []},
    }


def slack_payload(events: Sequence[NotificationEvent]) -> Rendering:
    """Render a Slack webhook body; `&`, `<` and `>` are escaped in every user-supplied value."""
    views = _views(events)
    blocks = [_markdown_block(view, escape_slack, _slack_link, bold="*") for view in views]
    return {"text": f"*{_prefixed(views, escape_slack)}*\n\n" + "\n\n".join(blocks)}


def teams_payload(events: Sequence[NotificationEvent]) -> Rendering:
    """Render a Teams incoming-webhook message: one Adaptive Card, markdown escaped."""
    views = _views(events)
    body: list[Rendering] = [
        {
            "type": "TextBlock",
            "text": _prefixed(views, escape_teams),
            "weight": "Bolder",
            "wrap": True,
        }
    ]
    for view in views:
        body.append(
            {
                "type": "TextBlock",
                "text": f"**{escape_teams(view.label)}** ({escape_teams(view.result)})",
                "wrap": True,
                "spacing": "Medium",
            }
        )
        body.append({"type": "FactSet", "facts": _facts(view, escape_teams, _markdown_link)})
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


def email_message(
    *,
    sender: str,
    recipients: Sequence[str],
    events: Sequence[NotificationEvent],
    sent_at: datetime,
) -> EmailMessage:
    """Build a plain-text message; every value placed in a header has CR and LF removed."""
    views = _views(events)
    sender = validate_address(sender)
    mailboxes = [validate_address(recipient) for recipient in recipients]
    if not mailboxes:
        raise ValueError("a message needs a recipient")
    message = EmailMessage()
    message["From"] = _header_value(sender)
    message["To"] = ", ".join(_header_value(mailbox) for mailbox in mailboxes)
    message["Subject"] = _header_value(_prefixed(views, str))
    message["Date"] = format_datetime(_aware(sent_at))
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    message.set_content(_plain_text(views) + "\n")
    return message


def escape_slack(text: str) -> str:
    """Escape the three characters Slack treats as control syntax."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_discord(text: str) -> str:
    """Backslash-escape every character Discord markdown, emoji or mention syntax reads."""
    return "".join(f"\\{char}" if char in _DISCORD_SPECIALS else char for char in text)


def escape_teams(text: str) -> str:
    """Backslash-escape every character the Adaptive Card markdown subset reads."""
    return "".join(f"\\{char}" if char in _TEAMS_SPECIALS else char for char in text)


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _views(events: Sequence[NotificationEvent]) -> list[_View]:
    if not 1 <= len(events) <= MAX_EVENTS_PER_MESSAGE:
        raise ValueError("a message carries between 1 and MAX_EVENTS_PER_MESSAGE events")
    return [_view(event) for event in events]


def _view(event: NotificationEvent) -> _View:
    rule = clean(event.actor_rule) if event.actor_rule is not None else None
    actor_name = clean(event.actor_name)
    target_type = clean(event.target_type)
    target_name = clean(event.target_name)
    return _View(
        event=event,
        label=clean(event.label),
        result=clean(event.result),
        actor=f"{actor_name} (rule: {rule})" if rule else actor_name,
        actor_name=actor_name,
        actor_rule=rule or None,
        target_type=target_type,
        target_name=target_name,
        target=f"{target_type} {target_name}",
        environment_name=clean(event.environment_name) if event.environment_name else None,
        time=iso_utc(event.time),
        link=_safe_link(event.link),
    )


def _safe_link(link: str | None) -> str | None:
    if link is None or len(link) > MAX_LINK_CHARS or _LINK.fullmatch(link) is None:
        return None
    return link.translate(_LINK_ENCODED)


def _webhook_event(view: _View) -> Rendering:
    event = view.event
    environment: Rendering | None = None
    if event.environment_id is not None:
        environment = {"id": event.environment_id, "name": view.environment_name}
    return {
        "id": event.id,
        "type": event.type,
        "label": view.label,
        "result": view.result,
        "actor": {"type": event.actor_type, "name": view.actor_name, "rule": view.actor_rule},
        "target": {"type": view.target_type, "name": view.target_name},
        "environment": environment,
        "time": view.time,
        "link": view.link,
    }


def _overall(views: Sequence[_View]) -> str:
    results = {view.result for view in views}
    if "failure" in results:
        return "failure"
    return "success" if results == {"success"} else "mixed"


def _title(views: Sequence[_View]) -> str:
    return views[0].label if len(views) == 1 else f"{len(views)} events"


def _prefixed(views: Sequence[_View], escape: Escape) -> str:
    return SUBJECT_PREFIX + escape(_title(views))


def _plain_text(views: Sequence[_View]) -> str:
    return "\n\n".join("\n".join(_plain_lines(view)) for view in views)


def _plain_lines(view: _View) -> list[str]:
    lines = [f"{view.label} ({view.result})", f"  Target: {view.target}"]
    if view.environment_name:
        lines.append(f"  Environment: {view.environment_name}")
    lines.extend([f"  Actor: {view.actor}", f"  Time: {view.time}"])
    if view.link:
        lines.append(f"  Link: {view.link}")
    return lines


def _header_value(text: str) -> str:
    return "".join(char for char in text if char not in "\r\n")


def _facts(view: _View, escape: Escape, link: LinkMarkup) -> list[Rendering]:
    facts: list[Rendering] = [{"title": "Target", "value": escape(view.target)}]
    if view.environment_name:
        facts.append({"title": "Environment", "value": escape(view.environment_name)})
    facts.append({"title": "Actor", "value": escape(view.actor)})
    facts.append({"title": "Time", "value": view.time})
    if view.link:
        facts.append({"title": "Link", "value": link(view.link)})
    return facts


def _markdown_block(view: _View, escape: Escape, link: LinkMarkup, *, bold: str = "**") -> str:
    lines = [f"{bold}{escape(view.label)}{bold} ({escape(view.result)})"]
    lines.extend(f"{fact['title']}: {fact['value']}" for fact in _facts(view, escape, link))
    return "\n".join(lines)


def _markdown_link(url: str) -> str:
    return f"[Open in dockmin]({url})"


def _slack_link(url: str) -> str:
    return f"<{escape_slack(url).replace('|', '%7C')}|Open in dockmin>"


def _fit(blocks: Sequence[str], limit: int) -> str:
    kept: list[str] = []
    used = 0
    for block in blocks:
        cost = len(block) + (2 if kept else 0)
        if used + cost > limit:
            break
        kept.append(block)
        used += cost
    omitted = len(blocks) - len(kept)
    if omitted:
        kept.append(f"and {omitted} more")
    return "\n\n".join(kept)
