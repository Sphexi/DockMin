"""Companion file writes on a managed Swarm stack and the queries behind its Files tab."""

from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackRepository
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.automation_support import event_rows
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.stack_support import Rig, activity_dump
from tests.swarm_stack_support import (
    NAME,
    compose_commands,
    seed_swarm_stack,
    simple_file,
    swarm_services,
)
from tests.web.stack_file_support import PLAIN_BODY, PLAIN_NAME, SECRET_BODY, SECRET_NAME

STALE = "0" * 64
EDITED_BODY = "edited-visible-content\n"
INCLUDED = "inc.yaml"
INCLUDED_BODY = "services:\n  db:\n    image: busybox:latest\n"
BUILD_BODY = INCLUDED_BODY + "    build: .\n"
INCLUDING = f"include:\n  - {INCLUDED}\n" + simple_file()


async def put(rig: Rig, name: str, content: str, *, secret: bool = False) -> None:
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    stored = await repository.get(ENV_A, NAME)
    assert stored is not None
    await repository.files.put_file(stored.id, name, content=content, secret=secret)


async def test_a_swarm_file_write_and_remove_check_the_stack_and_never_touch_the_cluster(
    swarm_stack_rig: Rig, stack_daemon: SwarmEngine, tmp_path: Path
) -> None:
    rig = swarm_stack_rig
    await seed_swarm_stack(rig, simple_file())
    mutations = stack_daemon.mutating()
    calls = compose_commands(tmp_path / "compose")

    plain = await rig.fresh().swarm_actions.write_file(
        ADMIN, ENV_A, NAME, PLAIN_NAME, content=PLAIN_BODY, secret=False
    )
    secret = await rig.fresh().swarm_actions.write_file(
        ADMIN, ENV_A, NAME, SECRET_NAME, content=SECRET_BODY, secret=True
    )
    kept = await rig.fresh().swarm_actions.write_file(
        ADMIN, ENV_A, NAME, SECRET_NAME, content=None, secret=True
    )
    await rig.fresh().swarm_actions.remove_file(ADMIN, ENV_A, NAME, PLAIN_NAME)

    assert (plain.content, plain.size, plain.uses) == (PLAIN_BODY, len(PLAIN_BODY), ())
    assert (secret.content, secret.size, secret.digest) == (None, None, None)
    assert kept.secret is True
    assert stack_daemon.mutating() == mutations
    assert compose_commands(tmp_path / "compose")[len(calls) :] == ["config"] * 4
    rows = await event_rows(rig.db, "stack_updated")
    assert [
        (r["result"], r["detail"].get("file_written"), r["detail"].get("file_removed"))
        for r in rows
    ] == [
        ("success", PLAIN_NAME, None),
        ("success", SECRET_NAME, None),
        ("success", SECRET_NAME, None),
        ("success", None, PLAIN_NAME),
    ]
    dump = await activity_dump(rig)
    assert SECRET_BODY.strip() not in dump and PLAIN_BODY.strip() not in dump


async def test_a_swarm_file_change_the_stack_would_not_survive_or_a_stale_one_is_refused(
    swarm_stack_rig: Rig,
) -> None:
    rig = swarm_stack_rig
    await seed_swarm_stack(rig, INCLUDING)
    await put(rig, INCLUDED, INCLUDED_BODY)

    with pytest.raises(StackRejected) as broken:
        await rig.fresh().swarm_actions.write_file(
            ADMIN, ENV_A, NAME, INCLUDED, content=BUILD_BODY, secret=False
        )
    with pytest.raises(StackRejected) as in_use:
        await rig.fresh().swarm_actions.remove_file(ADMIN, ENV_A, NAME, INCLUDED)
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().swarm_actions.write_file(
            ADMIN,
            ENV_A,
            NAME,
            PLAIN_NAME,
            content=PLAIN_BODY,
            secret=False,
            expected_inputs_digest=STALE,
        )
    with pytest.raises(ResourceNotFound):
        await rig.fresh().swarm_actions.remove_file(ADMIN, ENV_A, NAME, "missing.env")

    assert broken.value.reason == "refused_construct"
    assert in_use.value.reason == "file_in_use"
    panel = await swarm_services(rig).stacks.files_panel(ADMIN, ENV_A, NAME)
    assert [f.name for f in panel.files] == [INCLUDED] and panel.files[0].content == INCLUDED_BODY
    rows = await event_rows(rig.db, "stack_updated")
    assert [r["detail"].get("reason") for r in rows] == [
        "refused_construct",
        "file_in_use",
        "changed_since_review",
    ]


async def test_the_swarm_files_queries_serve_a_reader_the_tab_and_a_writer_the_pages(
    swarm_stack_rig: Rig,
) -> None:
    rig = swarm_stack_rig
    await seed_swarm_stack(rig, simple_file())
    await put(rig, PLAIN_NAME, PLAIN_BODY)
    await put(rig, SECRET_NAME, SECRET_BODY, secret=True)
    async with rig.database.session() as db:
        await ResourceControlRepository(db, rig.clock).create(
            ENV_A,
            "stack",
            NAME,
            is_public=True,
            is_administrators_only=False,
            access_level="read",
            owner_user_id=None,
        )
    queries = swarm_services(rig).stacks

    reader = await queries.files_panel(STANDARD, ENV_A, NAME)
    writer = await queries.files_panel(ADMIN, ENV_A, NAME)
    add = await queries.file_form(ADMIN, ENV_A, NAME, None)
    edit = await queries.file_form(ADMIN, ENV_A, NAME, SECRET_NAME)
    confirmation = await queries.file_remove_confirmation(ADMIN, ENV_A, NAME, PLAIN_NAME)

    assert [f.name for f in reader.files] == [PLAIN_NAME, SECRET_NAME]
    assert reader.files[0].content == PLAIN_BODY and reader.files[1].content is None
    assert (reader.can_write, writer.can_write) == (False, True)
    assert SECRET_BODY.strip() not in repr(reader)
    assert add.file is None and edit.file is not None and edit.file.content is None
    assert (confirmation.in_use, confirmation.file.name) == (False, PLAIN_NAME)
    assert confirmation.expected_inputs_digest == writer.inputs_digest == add.inputs_digest
    for name in (None, PLAIN_NAME):
        with pytest.raises(PermissionDenied):
            await queries.file_form(STANDARD, ENV_A, NAME, name)
    for name in ("missing.env", ".env", "a b"):
        with pytest.raises(ResourceNotFound):
            await queries.file_form(ADMIN, ENV_A, NAME, name)
        with pytest.raises(ResourceNotFound):
            await queries.file_remove_confirmation(ADMIN, ENV_A, NAME, name)
