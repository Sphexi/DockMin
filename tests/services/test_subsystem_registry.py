"""The subsystem registry agrees with the published list and with the schema."""

import re
from pathlib import Path

import pytest

from app.db.engine import Database, Session
from app.db.repositories.clocked import format_timestamp, utc_now
from app.services.activity import SUBSYSTEMS, ActivityRecorder, SubsystemActor

SUBSYSTEMS_DOCUMENT = Path(__file__).resolve().parents[2] / "docs" / "Reference-Subsystems.md"
NAME_FORMAT = re.compile(r"[a-z]+(-[a-z]+)+")
DOCUMENTED_NAME = re.compile(r"^\|\s*`([^`|]+)`\s*\|", re.MULTILINE)

TWO_TABLES = """
| Name | Responsibility |
|---|---|
| `first-worker` | Does one thing |
| `second-worker` | Does another |

| Change | Location |
|---|---|
| Registry entry in `SUBSYSTEMS` | `app/services/activity.py` |
"""


def documented_names(text: str) -> set[str]:
    """Return the names in the first column of every table row that opens with a code span."""
    return set(DOCUMENTED_NAME.findall(text))


async def record_one_event_per_subsystem(db: Session) -> list[str]:
    """Write one event for each registered subsystem and return the stored subsystem names."""
    now = format_timestamp(utc_now())
    rule_ids: dict[str, int | None] = {}
    for name, needs_rule in SUBSYSTEMS.items():
        if needs_rule:
            written = await db.execute(
                "INSERT INTO automation_rule (name, subsystem, target_selector, created_at, "
                "updated_at) VALUES (?, ?, '{}', ?, ?)",
                (f"rule for {name}", name, now, now),
            )
            rule_ids[name] = written.last_row_id
        else:
            rule_ids[name] = None
    recorder = ActivityRecorder(db)
    for name, rule_id in rule_ids.items():
        await recorder.record_event(
            SubsystemActor(name, rule_id),
            action="probe",
            target_type="probe",
            result="success",
        )
    rows = await db.fetch_limited(
        "SELECT actor_subsystem FROM activity_event ORDER BY id", (), limit=len(SUBSYSTEMS) + 1
    )
    return [row["actor_subsystem"] for row in rows]


def test_the_registry_holds_exactly_the_subsystems_in_the_published_list() -> None:
    published = documented_names(SUBSYSTEMS_DOCUMENT.read_text(encoding="utf-8"))

    assert set(SUBSYSTEMS) == published


def test_the_published_list_parser_reads_only_rows_that_open_with_a_code_span() -> None:
    assert documented_names(TWO_TABLES) == {"first-worker", "second-worker"}


@pytest.mark.parametrize("name", sorted(SUBSYSTEMS))
def test_a_registered_name_is_lowercase_ascii_kebab_case(name: str) -> None:
    assert NAME_FORMAT.fullmatch(name)


async def test_the_schema_accepts_an_event_from_every_registered_subsystem(
    database: Database,
) -> None:
    async with database.session() as db:
        stored = await record_one_event_per_subsystem(db)

    assert sorted(stored) == sorted(SUBSYSTEMS)
