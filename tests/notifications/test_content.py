"""Notification content: the fixed field set and each kind's rendering of it."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.notifications import content
from app.notifications.content import (
    MAX_EVENTS_PER_MESSAGE,
    MAX_FIELD_CHARS,
    NotificationEvent,
    channel_test_event,
    clean,
    discord_payload,
    email_message,
    escape_discord,
    escape_slack,
    escape_teams,
    gotify_payload,
    iso_utc,
    ntfy_message,
    slack_payload,
    teams_payload,
    validate_address,
    webhook_body,
)
from tests.notification_support import EVENT_TIME
from tests.notification_support import sample_event as event

SENT_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
LINK = "https://dock.example.test/activity/7"
PLUS_TWO = timezone(timedelta(hours=2))
SENDER = "dockmin@example.test"
RECIPIENT = "ops@example.test"


def failing() -> NotificationEvent:
    return event(id=8, result="failure", label="Backup or object storage sync failed")


def test_the_webhook_body_carries_the_documented_fields_and_nothing_more() -> None:
    body = json.loads(webhook_body("chat", [event(link=LINK)], SENT_AT))

    assert body == {
        "schema_version": 1,
        "sent_at": "2030-01-02T03:04:05Z",
        "channel": "chat",
        "events": [
            {
                "id": 7,
                "type": "update_applied",
                "label": "Update applied",
                "result": "success",
                "actor": {"type": "subsystem", "name": "container-updater", "rule": "nightly"},
                "target": {"type": "container", "name": "web"},
                "environment": {"id": 3, "name": "prod"},
                "time": "2030-01-02T03:00:00Z",
                "link": LINK,
            }
        ],
    }


def test_an_event_with_no_environment_rule_or_link_renders_each_as_null() -> None:
    bare = event(environment_id=None, environment_name=None, actor_rule=None)

    [rendered] = json.loads(webhook_body("chat", [bare], SENT_AT))["events"]

    assert (rendered["environment"], rendered["actor"]["rule"], rendered["link"]) == (
        None,
        None,
        None,
    )


def test_the_webhook_body_is_compact_utf8_so_a_signature_covers_exactly_these_bytes() -> None:
    raw = webhook_body("caf\u00e9", [event(target_name="naïve")], SENT_AT)

    assert b": " not in raw and b", " not in raw
    assert "caf\u00e9".encode() in raw and "naïve".encode() in raw


def test_markup_in_a_user_typed_name_stays_data_in_the_body() -> None:
    hostile = '"}],"events":[{"x":"<script>alert(1)</script>'

    [rendered] = json.loads(webhook_body("chat", [event(target_name=hostile)], SENT_AT))["events"]

    assert rendered["target"]["name"] == hostile


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("javascript:alert(1)", id="script-scheme"),
        pytest.param("https://dock.example.test/a b", id="space"),
        pytest.param("https://dock.example.test/\u00e9", id="non-ascii"),
        pytest.param("https://dock.example.test/" + "a" * content.MAX_LINK_CHARS, id="too-long"),
    ],
)
def test_a_link_that_is_not_a_plain_http_address_is_dropped(link: str) -> None:
    [rendered] = json.loads(webhook_body("chat", [event(link=link)], SENT_AT))["events"]

    assert rendered["link"] is None


def test_characters_that_end_markdown_and_slack_links_are_percent_encoded_in_a_link() -> None:
    [rendered] = json.loads(
        webhook_body("chat", [event(link="https://dock.example.test/a(b)|<c>")], SENT_AT)
    )["events"]

    assert rendered["link"] == "https://dock.example.test/a%28b%29%7C%3Cc%3E"


def test_a_field_is_stripped_of_control_characters_and_cut_to_the_limit() -> None:
    noisy = "web\x00\x1b[31m\u202e" + "x" * (MAX_FIELD_CHARS * 2)

    [rendered] = json.loads(webhook_body("chat", [event(target_name=noisy)], SENT_AT))["events"]

    name = rendered["target"]["name"]
    assert len(name) == MAX_FIELD_CHARS and name.endswith("…")
    assert "\x00" not in name and "\x1b" not in name and "\u202e" not in name


@pytest.mark.parametrize("count", [0, MAX_EVENTS_PER_MESSAGE + 1])
def test_a_message_of_no_events_or_too_many_is_refused(count: int) -> None:
    with pytest.raises(ValueError, match="between 1 and"):
        webhook_body("chat", [event()] * count, SENT_AT)


def test_a_message_of_the_most_events_is_accepted() -> None:
    body = json.loads(webhook_body("chat", [event()] * MAX_EVENTS_PER_MESSAGE, SENT_AT))

    assert len(body["events"]) == MAX_EVENTS_PER_MESSAGE


def test_a_test_notification_is_a_fixed_event_naming_the_channel_and_the_administrator() -> None:
    test = channel_test_event(channel_name="chat", actor_name="ana", time=EVENT_TIME)

    assert (test.id, test.type, test.label) == (0, "test", "Test notification")
    assert test.result == "success"
    assert (test.actor_type, test.actor_name, test.target_name) == ("user", "ana", "chat")
    assert test.environment_id is None


def test_ntfy_puts_the_title_and_tag_in_headers_and_the_fields_in_the_text() -> None:
    message = ntfy_message([event(link=LINK)])

    assert message.title == "dockmin: Update applied"
    assert message.tags == "white_check_mark"
    assert message.body.splitlines() == [
        "Update applied (success)",
        "  Target: container web",
        "  Environment: prod",
        "  Actor: container-updater (rule: nightly)",
        "  Time: 2030-01-02T03:00:00Z",
        f"  Link: {LINK}",
    ]


@pytest.mark.parametrize(
    ("events", "tag"),
    [
        ([failing()], "x"),
        ([event(), failing()], "x"),
        ([event()], "white_check_mark"),
        ([event(), event(result="partial")], "information_source"),
    ],
)
def test_ntfy_tags_a_message_by_its_worst_result(events: list[NotificationEvent], tag: str) -> None:
    assert ntfy_message(events).tags == tag


def test_several_events_share_a_title_that_counts_them() -> None:
    assert ntfy_message([event(), event(id=9)]).title == "dockmin: 2 events"


def test_gotify_sends_title_message_and_priority_five() -> None:
    payload = gotify_payload([event()])

    assert payload["title"] == "dockmin: Update applied"
    assert payload["priority"] == 5
    assert "Target: container web" in payload["message"]


def test_discord_sends_one_embed_and_allows_no_mentions() -> None:
    payload = discord_payload([event(link=LINK)])

    [embed] = payload["embeds"]
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["content"] == "dockmin: Update applied"
    assert embed["color"] == 0x2ECC71 and embed["timestamp"] == "2030-01-02T03:00:00Z"
    assert f"[Open in dockmin]({LINK})" in embed["description"]


def test_a_failure_colours_the_discord_embed_red() -> None:
    assert discord_payload([failing()])["embeds"][0]["color"] == 0xE74C3C


def test_discord_markdown_emoji_and_mentions_in_a_name_are_escaped() -> None:
    payload = discord_payload([event(target_name="@everyone *bold* :tada: <@123>")])

    description = payload["embeds"][0]["description"]
    assert "\\@everyone \\*bold\\* \\:tada\\: \\<\\@123\\>" in description


def test_a_discord_message_too_long_for_an_embed_ends_with_a_count_of_the_rest() -> None:
    long_name = "n" * MAX_FIELD_CHARS
    events = [event(id=number, target_name=long_name) for number in range(MAX_EVENTS_PER_MESSAGE)]

    description = discord_payload(events)["embeds"][0]["description"]

    assert len(description) <= content.DISCORD_DESCRIPTION_LIMIT + 32
    assert description.rsplit("\n\n", 1)[1].startswith("and ") and description.endswith(" more")


def test_slack_escapes_the_three_control_characters_in_every_user_supplied_value() -> None:
    payload = slack_payload([event(target_name="<!channel> & <http://x|y>", actor_name="a<b")])

    text = payload["text"]
    assert "<!channel>" not in text and "&lt;!channel&gt; &amp; &lt;http://x|y&gt;" in text
    assert "a&lt;b" in text


def test_slack_writes_a_link_in_its_own_syntax() -> None:
    assert f"<{LINK}|Open in dockmin>" in slack_payload([event(link=LINK)])["text"]


def test_teams_sends_one_adaptive_card_with_a_fact_set_per_event() -> None:
    payload = teams_payload([event(link=LINK), failing()])

    [attachment] = payload["attachments"]
    card = attachment["content"]
    facts = [block for block in card["body"] if block["type"] == "FactSet"]
    assert payload["type"] == "message"
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert (card["type"], card["version"]) == ("AdaptiveCard", "1.4")
    assert card["body"][0]["text"] == "dockmin: 2 events"
    assert [fact["title"] for fact in facts[0]["facts"]] == [
        "Target",
        "Environment",
        "Actor",
        "Time",
        "Link",
    ]
    assert len(facts) == 2


def test_teams_escapes_markdown_in_text_blocks() -> None:
    payload = teams_payload([event(target_name="**bold** [x](y)")])

    facts = payload["attachments"][0]["content"]["body"][2]["facts"]
    assert facts[0]["value"] == "container \\*\\*bold\\*\\* \\[x\\]\\(y\\)"


@pytest.mark.parametrize(
    ("escape", "text", "escaped"),
    [
        (escape_slack, "a&b<c>d", "a&amp;b&lt;c&gt;d"),
        (escape_discord, "_x_ `y` ~z~", "\\_x\\_ \\`y\\` \\~z\\~"),
        (escape_teams, "# *x* |y|", "\\# \\*x\\* \\|y\\|"),
    ],
)
def test_each_escape_covers_the_characters_its_format_reads(
    escape: Any, text: str, escaped: str
) -> None:
    assert escape(text) == escaped


def test_an_email_has_a_plain_text_body_and_the_envelope_headers() -> None:
    message = email_message(
        sender=SENDER, recipients=[RECIPIENT, "dev@example.test"], events=[event()], sent_at=SENT_AT
    )

    assert message["From"] == SENDER
    assert message["To"] == f"{RECIPIENT}, dev@example.test"
    assert message["Subject"] == "dockmin: Update applied"
    assert message["Message-ID"].endswith("@example.test>")
    assert message.get_content_type() == "text/plain"
    assert "Target: container web" in message.get_content()


def test_a_line_break_smuggled_into_a_field_cannot_add_a_header() -> None:
    hostile = event(label="Update applied\r\nBcc: attacker@example.test")

    message = email_message(
        sender=SENDER, recipients=[RECIPIENT], events=[hostile], sent_at=SENT_AT
    )

    assert message["Bcc"] is None
    assert "\r" not in message["Subject"] and "\n" not in message["Subject"]


def test_an_email_with_no_recipient_is_refused() -> None:
    with pytest.raises(ValueError, match="recipient"):
        email_message(sender=SENDER, recipients=[], events=[event()], sent_at=SENT_AT)


@pytest.mark.parametrize(
    "address",
    ["ops@example.test", "first.last+tag@sub.example.test", "a@b", "x_y@host-1.example"],
)
def test_a_plain_mailbox_is_accepted(address: str) -> None:
    assert validate_address(address) == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "no-at-sign",
        "@example.test",
        "ops@",
        ".ops@example.test",
        "ops.@example.test",
        "o..ps@example.test",
        "Ops <ops@example.test>",
        "ops@example.test\r\nBcc: x@y",
        "ops@-example.test",
        "ops@exa mple.test",
        "ops@example..test",
        "ü@example.test",
        "a" * 250 + "@example.test",
    ],
)
def test_anything_but_a_plain_mailbox_is_refused(address: str) -> None:
    with pytest.raises(ValueError, match="mailbox"):
        validate_address(address)


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("a\tb\nc", "a b c"),
        ("  padded   words  ", "padded words"),
        ("zero\u200bwidth", "zerowidth"),
        ("bell\x07", "bell"),
        ("line\u2028separator", "line separator"),
    ],
)
def test_clean_collapses_spacing_and_drops_invisible_characters(raw: str, cleaned: str) -> None:
    assert clean(raw) == cleaned


@pytest.mark.parametrize(
    ("moment", "text"),
    [
        (datetime(2030, 1, 2, 3, 4, 5), "2030-01-02T03:04:05Z"),
        (datetime(2030, 1, 2, 5, 4, 5, tzinfo=PLUS_TWO), "2030-01-02T03:04:05Z"),
        (SENT_AT.replace(microsecond=999_999), "2030-01-02T03:04:05Z"),
    ],
)
def test_a_time_is_written_as_utc_to_the_second(moment: datetime, text: str) -> None:
    assert iso_utc(moment) == text
