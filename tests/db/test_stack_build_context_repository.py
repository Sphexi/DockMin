"""Tests for app/db/repositories/stack_build_context.py: stored archives, limits and the digest."""

import hashlib
import sqlite3

import pytest

from app.db.engine import Session
from app.db.repositories import stack_build_context as limits
from app.db.repositories.errors import MissingReferenceError
from app.db.repositories.stack import StackRepository, inputs_digest_of
from app.db.repositories.stack_build_context import (
    ContextWrite,
    StackContextLimitError,
    StackContextRepository,
)
from tests.crypto.helpers import FIRST_KEY, vault_with
from tests.db.helpers import CREATED_AT, CREATED_STAMP, UNKNOWN_ID, at

STACK_NAME = "shop"
COMPOSE = "services:\n  web:\n    image: x\n"
COMPOSE_DIGEST = "d" * 64
CONTEXT = "app"
FIRST = b"first-archive"
SECOND = b"second-archive"
MEMBERS = 3
EXTRACTED = 11


def write_of(content: bytes, *, compressed: bool = False) -> ContextWrite:
    return ContextWrite(
        content=content,
        content_digest=hashlib.sha256(content).hexdigest(),
        compressed=compressed,
        member_count=MEMBERS,
        extracted_size=EXTRACTED,
    )


@pytest.fixture
def stacks(session: Session) -> StackRepository:
    return StackRepository(session, vault_with(FIRST_KEY), at(CREATED_AT))


@pytest.fixture
async def stack_id(session: Session, stacks: StackRepository) -> int:
    environment = await session.execute(
        "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
        "VALUES ('one', 'unix_socket', '/run/one.sock', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    assert environment.last_row_id is not None
    return await stacks.insert(
        environment_id=environment.last_row_id,
        name=STACK_NAME,
        kind="compose",
        origin="created",
        project_directory=f"{1:032x}",
        compose_file=COMPOSE,
        compose_file_digest=COMPOSE_DIGEST,
        created_by_user_id=None,
    )


async def inputs_digest(session: Session, stack_id: int) -> str:
    row = await session.fetch_one("SELECT inputs_digest FROM stack WHERE id = ?", (stack_id,))
    assert row is not None
    return str(row["inputs_digest"])


def test_the_limits_are_the_documented_sizes() -> None:
    assert limits.MAX_CONTEXT_UPLOAD_BYTES == 16 * 1024 * 1024
    assert limits.MAX_STACK_CONTEXT_BYTES == 64 * 1024 * 1024
    assert limits.MAX_CONTEXT_EXTRACTED_BYTES == 256 * 1024 * 1024
    assert limits.MAX_CONTEXT_MEMBERS == 10_000


def test_a_stack_with_no_context_keeps_the_digest_it_had_before_contexts_existed() -> None:
    bare = inputs_digest_of((), ())
    with_context = inputs_digest_of((), (), [(CONTEXT, "a" * 64)])
    reordered = inputs_digest_of((), (), [("b", "b" * 64), ("A", "a" * 64)])

    assert with_context != bare
    assert reordered == inputs_digest_of((), (), [("a", "a" * 64), ("B", "b" * 64)])


async def test_a_context_is_stored_replaced_and_listed_and_each_write_moves_the_inputs_digest(
    session: Session, stacks: StackRepository, stack_id: int
) -> None:
    contexts = stacks.contexts
    empty = await inputs_digest(session, stack_id)

    added = await contexts.put(stack_id, CONTEXT, write_of(FIRST, compressed=True))
    after_add = await inputs_digest(session, stack_id)
    replaced = await contexts.put(stack_id, CONTEXT.upper(), write_of(SECOND))
    after_replace = await inputs_digest(session, stack_id)
    unchanged = await contexts.put(stack_id, CONTEXT, write_of(SECOND))
    archive = await contexts.archive(stack_id, CONTEXT.upper())
    listed = await contexts.contexts(stack_id)

    assert (added.name, added.compressed, added.size_bytes) == (CONTEXT, True, len(FIRST))
    assert (added.member_count, added.extracted_size) == (MEMBERS, EXTRACTED)
    assert replaced.name == CONTEXT and replaced.content_digest == write_of(SECOND).content_digest
    assert unchanged.content_digest == replaced.content_digest
    assert len({empty, after_add, after_replace}) == 3
    assert await inputs_digest(session, stack_id) == after_replace
    assert archive is not None and archive.content == SECOND and archive.compressed is False
    assert listed == [unchanged]
    assert await contexts.archive(stack_id, "missing") is None


async def test_a_write_past_the_total_or_the_count_is_refused_and_changes_nothing(
    session: Session,
    stacks: StackRepository,
    stack_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts = stacks.contexts
    monkeypatch.setattr(limits, "MAX_STACK_CONTEXT_BYTES", len(FIRST) + len(SECOND))
    monkeypatch.setattr(limits, "MAX_CONTEXTS", 2)
    await contexts.put(stack_id, "one", write_of(FIRST))
    await contexts.put(stack_id, "two", write_of(SECOND))
    held = await inputs_digest(session, stack_id)

    with pytest.raises(StackContextLimitError):
        await contexts.put(stack_id, "one", write_of(FIRST + b"x"))
    with pytest.raises(StackContextLimitError):
        await contexts.put(stack_id, "three", write_of(b"y"))

    assert [item.name for item in await contexts.contexts(stack_id)] == ["one", "two"]
    archive = await contexts.archive(stack_id, "one")
    assert archive is not None and archive.content == FIRST
    assert await inputs_digest(session, stack_id) == held


async def test_deleting_a_context_restores_the_digest_and_a_missing_one_is_reported(
    session: Session, stacks: StackRepository, stack_id: int
) -> None:
    contexts = stacks.contexts
    empty = await inputs_digest(session, stack_id)
    await contexts.put(stack_id, CONTEXT, write_of(FIRST))

    removed = await contexts.delete(stack_id, CONTEXT.upper())
    again = await contexts.delete(stack_id, CONTEXT)

    assert (removed, again) == (True, False)
    assert await inputs_digest(session, stack_id) == empty
    assert await contexts.contexts(stack_id) == []


async def test_a_context_for_a_missing_stack_is_refused_and_removing_a_stack_removes_its_contexts(
    session: Session, stacks: StackRepository, stack_id: int
) -> None:
    contexts = StackContextRepository(session, at(CREATED_AT), stacks=stacks)
    await contexts.put(stack_id, CONTEXT, write_of(FIRST))

    with pytest.raises(MissingReferenceError):
        await contexts.put(UNKNOWN_ID, CONTEXT, write_of(FIRST))
    await session.execute("DELETE FROM stack WHERE id = ?", (stack_id,))
    left = await session.fetch_one("SELECT count(*) AS total FROM stack_build_context")

    assert left is not None and left["total"] == 0


async def test_the_table_refuses_a_digest_that_is_not_lower_case_hex(
    session: Session, stack_id: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await session.execute(
            "INSERT INTO stack_build_context (stack_id, name, content, content_digest, "
            "compressed, member_count, extracted_size, created_at, updated_at) "
            "VALUES (?, 'x', x'00', ?, 0, 1, 1, ?, ?)",
            (stack_id, "G" * 64, CREATED_STAMP, CREATED_STAMP),
        )
