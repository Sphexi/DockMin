"""Seeding stacks with companion files and reading their pages, shared by the file tests."""

from collections.abc import Sequence
from typing import Any, Literal

from fastapi import FastAPI
from httpx import Response

from app.db.repositories.stack import StackRepository
from app.services.stack_deploy import file_digest
from tests.web.environment_support import World
from tests.web.registry_support import visible_text

PLAIN_NAME = "app.conf"
PLAIN_BODY = "plain-visible-content\n"
SECRET_NAME = "app.key"
SECRET_BODY = "SECRET-FILE-SENTINEL-4417\n"
NOT_USED = "Not used"

type SeededFile = tuple[str, str, bool]

PLAIN_FILE: SeededFile = (PLAIN_NAME, PLAIN_BODY, False)
SECRET_FILE: SeededFile = (SECRET_NAME, SECRET_BODY, True)


def flat(response: Response) -> str:
    """Return the visible text of a page on one line."""
    return " ".join(visible_text(response.text).split())


def file_fields(*files: SeededFile) -> dict[str, str]:
    """Return the `file-N-*` form fields of rows given as (name, content, secret)."""
    fields: dict[str, str] = {}
    for index, (name, content, secret) in enumerate(files):
        fields[f"file-{index}-name"] = name
        fields[f"file-{index}-content"] = content
        if secret:
            fields[f"file-{index}-secret"] = "on"
    return fields


async def seed_stack(
    app: FastAPI,
    environment_id: int,
    name: str,
    text: str,
    *,
    kind: Literal["compose", "swarm"] = "compose",
    files: Sequence[SeededFile] = (),
) -> int:
    """Insert a stack row with files, as a finished create would leave it, and return its id."""
    async with app.state.database.session() as db:
        repository = StackRepository(db, app.state.vault, app.state.auth.clock)
        stack_id = await repository.insert(
            environment_id=environment_id,
            name=name,
            kind=kind,
            origin="created",
            project_directory=app.state.engine.compose.files.new_token(),
            compose_file=text,
            compose_file_digest=file_digest(text),
            created_by_user_id=None,
        )
        for file_name, content, secret in files:
            await repository.files.put_file(stack_id, file_name, content=content, secret=secret)
    return stack_id


async def stack_rows(world: World, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Return the rows a query reads from the application's database."""
    async with world.app.state.database.session() as db:
        found = await db.fetch_limited(sql, params, limit=1000)
    return [dict(row) for row in found]


UPDATED_ROWS = (
    "SELECT result, json_extract(detail, '$.reason') AS reason, "
    "json_extract(detail, '$.file_written') AS written, "
    "json_extract(detail, '$.file_removed') AS removed, "
    "json_extract(detail, '$.file_kind') AS kind, actor_user_id "
    "FROM activity_event WHERE action = 'stack_updated' AND target_id = ? ORDER BY id"
)
