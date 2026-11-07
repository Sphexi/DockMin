"""A migrated database with a user, an environment and a rule, and events at chosen ages."""

import json
from datetime import timedelta
from typing import Any

from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.clocked import format_timestamp
from app.services.activity_retention import PRUNED_ACTION
from tests.auth_support import FakeClock
from tests.scheduler.support import START, Rig

NOW = START
DAYS = 30
UPDATER = "container-updater"
EXPORTER = "activity-log-exporter"
OLD_AGE = DAYS + 10
YOUNG_AGE = DAYS - 1
INSERT_BULK = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, target_type, target_id, "
    "action, result, detail, occurred_at) VALUES ('user_action', 'user', ?, 'container', ?, "
    "'container_started', 'success', ?, ?)"
)


class World:
    """The rows retention tests share: one administrator, one environment, one updater rule."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.user_id = rig.user("operator")
        self.environment_id = rig.environment("host")
        self.rule_id = 0

    async def with_rule(self) -> "World":
        self.rule_id = await self.rig.rule(
            "updates", owner=self.user_id, subsystem=UPDATER, environment_id=self.environment_id
        )
        return self

    async def exporter_rule(self, name: str = "export") -> int:
        return await self.rig.rule(
            name, owner=self.user_id, subsystem=EXPORTER, environment_id=None
        )

    async def event(
        self,
        label: str,
        age_days: float,
        *,
        action: str = "container_started",
        category: str = "user_action",
        subsystem: str | None = None,
        rule_id: int | None = None,
        environment: bool = True,
        target: tuple[str, str | None] = ("container", "c1"),
    ) -> None:
        """Record one event named by label in its detail, aged the given days before NOW."""
        clock = FakeClock(NOW - timedelta(days=age_days))
        actor: dict[str, Any] = (
            {"actor_type": "subsystem", "actor_user_id": None, "actor_subsystem": subsystem}
            if subsystem
            else {"actor_type": "user", "actor_user_id": self.user_id, "actor_subsystem": None}
        )
        async with self.rig.database.session() as db:
            await ActivityEventRepository(db, clock).insert(
                category=category,  # type: ignore[arg-type]
                rule_id=rule_id,
                job_run_id=None,
                request_id=None,
                environment_id=self.environment_id if environment else None,
                target_type=target[0],
                target_id=target[1],
                action=action,
                result="success",
                detail={"label": label},
                **actor,
            )

    def bulk(self, count: int, age_days: float, *, prefix: str = "bulk", first: int = 0) -> None:
        """Insert count plain user events, each with an action of its own, in one transaction."""
        stamp = format_timestamp(NOW - timedelta(days=age_days))
        connection = self.rig.database.connect()
        try:
            connection.execute("BEGIN")
            connection.executemany(
                INSERT_BULK,
                [
                    (
                        self.user_id,
                        f"c{first + n}",
                        json.dumps({"label": f"{prefix}{first + n}"}),
                        stamp,
                    )
                    for n in range(count)
                ],
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def labels(self) -> list[str]:
        """Return the action of every event but the pruner's, oldest id first."""
        return [
            json.loads(row["detail"])["label"]
            for row in self.rig.sql(
                "SELECT detail FROM activity_event WHERE action <> ? ORDER BY id", (PRUNED_ACTION,)
            )
        ]

    def count(self) -> int:
        return int(self.rig.sql("SELECT count(*) FROM activity_event")[0][0])

    def pruned_rows(self) -> list[dict[str, Any]]:
        """Return the pruner's rows with their detail decoded, oldest first."""
        return [
            {**dict(row), "detail": json.loads(row["detail"])}
            for row in self.rig.sql(
                "SELECT * FROM activity_event WHERE action = ? ORDER BY id", (PRUNED_ACTION,)
            )
        ]

    def move_cursor(self, rule_id: int, last_event_id: int) -> None:
        self.rig.sql(
            "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, ?) "
            "ON CONFLICT (rule_id) DO UPDATE SET last_event_id = excluded.last_event_id",
            (rule_id, last_event_id),
        )

    def id_of(self, label: str) -> int:
        return int(
            self.rig.sql(
                "SELECT id FROM activity_event WHERE json_extract(detail, '$.label') = ?", (label,)
            )[0]["id"]
        )
