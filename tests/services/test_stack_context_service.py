"""Build context writes on a Compose stack, audited, and the extraction the deploy step calls."""

import gzip
import hashlib
import tarfile
from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.db.repositories import stack_build_context as limits
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import DeployedState, StackRepository
from app.services.stack_contexts import extract_stored_context
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.archive_support import Member, build_tar
from tests.automation_support import event_rows
from tests.stack_support import Rig, seed_stack_row

SHOP = "shop"
CONTEXT = "app"
STALE = "0" * 64
SIMPLE = "services:\n  web:\n    image: nginx:1\n"
SHORT_BUILD = "services:\n  web:\n    build: ./app\n"
LONG_BUILD = "services:\n  web:\n    build:\n      context: APP\n      dockerfile: Dockerfile\n"
DOCKERFILE = b"FROM scratch\n"
FIRST = gzip.compress(build_tar(Member("Dockerfile", DOCKERFILE)), mtime=0)
SECOND = build_tar(Member("Dockerfile", DOCKERFILE), Member("run.sh", b"#!/bin/sh\n", mode=0o755))
LINKED = build_tar(
    Member("Dockerfile", DOCKERFILE), Member("l", type=tarfile.SYMTYPE, linkname="/")
)


async def stack_row(rig: Rig) -> tuple[int, str, str | None]:
    row = await rig.fresh().queries.require_stack(ADMIN, ENV_A, SHOP, "write")
    return row.id, row.inputs_digest or "", row.deployed_inputs_digest


async def write(rig: Rig, name: str, archive: bytes) -> None:
    await rig.fresh().contexts.write(ADMIN, ENV_A, SHOP, name, archive=archive)


async def test_writing_a_context_stores_it_audits_it_and_moves_the_inputs_digest_past_a_deploy(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    stack_id, before, _ = await stack_row(stack_rig)
    stacks = StackRepository(stack_rig.db, stack_rig.registry.vault, stack_rig.clock)
    await stacks.record_operation(
        stack_id, operation="deploy", outcome="success", deployed=DeployedState("a" * 64, "b" * 64)
    )

    added = await stack_rig.fresh().contexts.write(ADMIN, ENV_A, SHOP, CONTEXT, archive=FIRST)
    _, after_add, deployed = await stack_row(stack_rig)
    replaced = await stack_rig.fresh().contexts.write(
        ADMIN, ENV_A, SHOP, CONTEXT.upper(), archive=SECOND
    )
    _, after_replace, _ = await stack_row(stack_rig)
    shown = await stack_rig.fresh().contexts.views(ADMIN, ENV_A, SHOP)

    assert (added.name, added.size, added.compressed, added.uses) == (
        CONTEXT,
        len(FIRST),
        True,
        (),
    )
    assert added.digest == hashlib.sha256(FIRST).hexdigest()
    assert (replaced.name, replaced.digest) == (CONTEXT, hashlib.sha256(SECOND).hexdigest())
    assert deployed == before and after_add != deployed
    assert after_replace not in {before, after_add}
    assert shown == (replaced,)
    rows = await event_rows(stack_rig.db, "stack_updated")
    assert [(r["result"], r["target_id"], r["detail"]["context_written"]) for r in rows] == [
        ("success", SHOP, CONTEXT),
        ("success", SHOP, CONTEXT),
    ]
    assert [(r["detail"]["context_size"], r["detail"]["context_digest"]) for r in rows] == [
        (len(FIRST), hashlib.sha256(FIRST).hexdigest()),
        (len(SECOND), hashlib.sha256(SECOND).hexdigest()),
    ]


async def test_a_refused_archive_or_name_stores_nothing_and_is_audited_as_a_failure(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    _, before, _ = await stack_row(stack_rig)
    refusals: list[tuple[str, str]] = []

    for name, archive in ((CONTEXT, LINKED), ("bad name", FIRST), ("compose.yaml", FIRST)):
        with pytest.raises(StackRejected) as caught:
            await write(stack_rig, name, archive)
        refusals.append((caught.value.reason, ",".join(caught.value.fields)))
    monkeypatch.setattr(limits, "MAX_CONTEXT_UPLOAD_BYTES", len(FIRST) - 1)
    with pytest.raises(StackRejected) as oversized:
        await write(stack_rig, CONTEXT, FIRST)

    assert refusals == [("member_type", "archive"), ("invalid", "name"), ("invalid", "name")]
    assert oversized.value.reason == "upload_too_large"
    assert (await stack_row(stack_rig))[1] == before
    assert await stack_rig.fresh().contexts.views(ADMIN, ENV_A, SHOP) == ()
    rows = await event_rows(stack_rig.db, "stack_updated")
    assert [r["result"] for r in rows] == ["failure"] * 4


async def test_a_stack_holds_contexts_only_up_to_the_total_and_a_refused_write_changes_nothing(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    monkeypatch.setattr(limits, "MAX_STACK_CONTEXT_BYTES", len(FIRST) + len(SECOND))
    await write(stack_rig, "one", FIRST)
    await write(stack_rig, "two", SECOND)
    _, held, _ = await stack_row(stack_rig)

    with pytest.raises(StackRejected) as over:
        await write(stack_rig, "three", FIRST)
    with pytest.raises(StackRejected):
        await write(stack_rig, "one", FIRST + SECOND)

    assert over.value.reason == "too_large"
    assert (await stack_row(stack_rig))[1] == held
    views = await stack_rig.fresh().contexts.views(ADMIN, ENV_A, SHOP)
    assert [(v.name, v.size) for v in views] == [("one", len(FIRST)), ("two", len(SECOND))]


async def test_a_context_the_compose_file_builds_from_is_not_removed_until_it_is_unreferenced(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SHORT_BUILD)
    await write(stack_rig, CONTEXT, FIRST)
    await write(stack_rig, "spare", SECOND)
    stack_id, digest, _ = await stack_row(stack_rig)
    contexts = stack_rig.fresh().contexts

    confirmation = await contexts.remove_confirmation(ADMIN, ENV_A, SHOP, CONTEXT)
    with pytest.raises(StackRejected) as used:
        await contexts.remove(ADMIN, ENV_A, SHOP, CONTEXT, expected_inputs_digest=digest)
    with pytest.raises(ChangedSinceReview):
        await contexts.remove(ADMIN, ENV_A, SHOP, "spare", expected_inputs_digest=STALE)
    with pytest.raises(ResourceNotFound):
        await contexts.remove(ADMIN, ENV_A, SHOP, "missing")
    await contexts.remove(ADMIN, ENV_A, SHOP, "SPARE", expected_inputs_digest=digest)
    stored = StackRepository(stack_rig.db, stack_rig.registry.vault, stack_rig.clock)
    await stored.update_file(stack_id, compose_file=LONG_BUILD, compose_file_digest=STALE)
    long_form = await stack_rig.fresh().contexts.remove_confirmation(ADMIN, ENV_A, SHOP, CONTEXT)

    assert confirmation.in_use and confirmation.context.uses == ("services.web.build",)
    assert (used.value.reason, ",".join(used.value.fields)) == ("context_in_use", "context")
    assert long_form.context.uses == ("services.web.build.context",)
    await stored.update_file(stack_id, compose_file=SIMPLE, compose_file_digest=STALE)
    await stack_rig.fresh().contexts.remove(ADMIN, ENV_A, SHOP, CONTEXT)
    assert await stack_rig.fresh().contexts.views(ADMIN, ENV_A, SHOP) == ()
    removed = [
        (r["result"], r["detail"].get("context_removed"))
        for r in await event_rows(stack_rig.db, "stack_updated")
        if "context_removed" in r["detail"]
    ]
    assert removed == [("failure", CONTEXT), ("success", "spare"), ("success", CONTEXT)]


async def test_a_standard_user_cannot_read_write_or_remove_contexts(stack_rig: Rig) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    await write(stack_rig, CONTEXT, FIRST)
    await ResourceControlRepository(stack_rig.db, stack_rig.clock).create(
        ENV_A,
        "stack",
        SHOP,
        is_public=True,
        is_administrators_only=False,
        access_level="read",
        owner_user_id=None,
    )
    contexts = stack_rig.fresh().contexts

    with pytest.raises(PermissionDenied):
        await contexts.views(STANDARD, ENV_A, SHOP)
    with pytest.raises(PermissionDenied):
        await contexts.remove_confirmation(STANDARD, ENV_A, SHOP, CONTEXT)
    with pytest.raises(PermissionDenied):
        await contexts.write(STANDARD, ENV_A, SHOP, "other", archive=SECOND)
    with pytest.raises(PermissionDenied):
        await contexts.remove(STANDARD, ENV_A, SHOP, CONTEXT)

    views = await stack_rig.fresh().contexts.views(ADMIN, ENV_A, SHOP)
    reader = await stack_rig.fresh().queries.detail(STANDARD, ENV_A, SHOP)
    admin = await stack_rig.fresh().queries.detail(ADMIN, ENV_A, SHOP)
    assert [v.name for v in views] == [CONTEXT]
    assert reader.contexts == () and reader.can_manage is False
    assert admin.contexts == views


async def test_a_stored_context_is_extracted_into_a_directory_and_a_missing_one_is_not_found(
    stack_rig: Rig, tmp_path: Path
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    await write(stack_rig, CONTEXT, SECOND)
    stack_id, _, _ = await stack_row(stack_rig)
    stacks = StackRepository(stack_rig.db, stack_rig.registry.vault, stack_rig.clock)
    work = tmp_path / "work"
    work.mkdir()

    await extract_stored_context(stacks, stack_id, CONTEXT.upper(), work)
    with pytest.raises(ResourceNotFound):
        await extract_stored_context(stacks, stack_id, "missing", tmp_path)

    assert sorted(path.name for path in work.iterdir()) == ["Dockerfile", "run.sh"]
    assert (work / "run.sh").stat().st_mode & 0o777 == 0o755
