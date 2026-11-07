"""Deploying a stack that builds its images: bases, buildx, the override, reuse and refusals."""

from pathlib import Path
from typing import Any

import pytest

import app.services.stack_runs as stack_runs
from app.services.stack_build_runs import check_bases
from app.services.stack_deploy import ChangedSinceReview, StackOperationFailed, StackRejected
from app.services.stack_views import BuildSummary, BuiltView
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.automation_support import authority_context
from tests.image_support import REFERENCE, binding_rows, decode_auth, register
from tests.stack_build_support import (
    BUILD_FILE,
    CONTEXT,
    DIGEST_LABEL,
    DOCKERFILE,
    PROJECT_LABEL,
    SERVICE_LABEL,
    STACK,
    TAG,
    add_context,
    runs_left_in,
    seed_build_stack,
    stub_argv,
)
from tests.stack_support import Rig, form_variables, read_records, recorded_call

WEBHOOK = "webhook-receiver"
UP_TAIL = ["up", "--detach", "--pull", "never", "--no-build"]
OTHER_PROJECT = "elsewhere"
STALE_DIGEST = "0" * 64
BASE_ID = "sha256:" + "ab" * 32
FOREIGN_ID = "sha256:" + "cd" * 32


def recording(path: Path) -> str:
    return f"{BUILD_FILE}x-fake:\n  record: {path}\n"


def pulls(rig: Rig) -> int:
    return len(rig.host.calls("POST", "/images/create"))


async def test_a_deploy_pulls_each_base_through_the_chosen_set_builds_and_runs_up_with_the_override(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    record = tmp_path / "up.jsonl"
    set_id = await register(rig, "only", environments=(ENV_A,))
    await seed_build_stack(rig, text=recording(record))

    outcome = await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    [pull] = rig.host.calls("POST", "/images/create")
    [binding] = await binding_rows(rig.db)
    argv = stub_argv(rig)
    up = recorded_call(record, "up")
    [box] = rig.host.boxes.values()
    detail = (await rig.rows("stack_deployed"))[-1]["detail"]
    assert decode_auth(pull.headers["x-registry-auth"])["username"] == "user-only"
    assert (binding["image_reference"], binding["registry_credential_id"]) == (REFERENCE, set_id)
    assert detail["binding"] == "created"
    assert argv[argv.index("--tag") + 1] == TAG
    assert {"--pull=false", "--provenance=false", "--sbom=false"} <= set(argv)
    assert argv[argv.index("--progress") + 1] == "rawjson"
    labels = {argv[i + 1] for i, part in enumerate(argv) if part == "--label"}
    assert {f"{PROJECT_LABEL}={STACK}", f"{SERVICE_LABEL}=web"} <= labels
    assert any(label.startswith(f"{DIGEST_LABEL}=") for label in labels)
    assert "docker_config={}" in rig.buildx.recorded("probe")  # type: ignore[union-attr]
    assert up["argv"][-5:] == UP_TAIL
    assert box.image == TAG
    assert outcome.built == (BuiltView("web", TAG, False),)
    assert detail["built"].startswith("web:") and detail["built"].endswith(":built")
    assert runs_left_in(rig.compose.work_base) == []


async def test_an_unchanged_digest_reuses_the_image_rebuild_builds_again_and_a_change_rebuilds(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    seeded = await seed_build_stack(rig)
    await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)
    assert rig.buildx.counter == 1 and pulls(rig) == 1

    reused = await rig.fresh().actions.redeploy(ADMIN, ENV_A, STACK)
    reused_detail = (await rig.rows("stack_redeployed"))[-1]["detail"]
    assert rig.buildx.counter == 1 and pulls(rig) == 1
    assert reused.built == (BuiltView("web", TAG, True),)
    assert reused_detail["built"].endswith(":reused")

    rebuilt = await rig.fresh().actions.redeploy(ADMIN, ENV_A, STACK, rebuild=True)
    assert rig.buildx.counter == 2
    assert rebuilt.built == (BuiltView("web", TAG, False),)

    await add_context(rig, seeded.stack_id, CONTEXT, {"Dockerfile": DOCKERFILE, "app.py": "x"})
    await rig.fresh().actions.redeploy(ADMIN, ENV_A, STACK)
    assert rig.buildx.counter == 3


async def test_an_image_at_the_tag_from_another_project_refuses_before_any_build(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    await seed_build_stack(rig)
    rig.host.add_image(TAG, FOREIGN_ID, {"Labels": {PROJECT_LABEL: OTHER_PROJECT}})

    with pytest.raises(StackRejected) as refused:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    assert refused.value.reason == "tag_in_use"
    assert list(refused.value.fields) == ["services.web.build"]
    assert OTHER_PROJECT in refused.value.fields["services.web.build"]
    assert rig.buildx.counter == 0 and pulls(rig) == 0
    [row] = await rig.rows("stack_deployed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "tag_in_use")


async def test_a_base_missing_when_the_build_starts_or_with_triggers_refuses_before_buildx_runs(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    await seed_build_stack(rig)
    rig.host.add_image(REFERENCE, BASE_ID, {"OnBuild": ["RUN touch /marker"]})

    with pytest.raises(StackOperationFailed) as triggers:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    assert triggers.value.reason == "base_onbuild"
    assert pulls(rig) == 0 and rig.buildx.counter == 0

    rig.host.add_image(REFERENCE, BASE_ID, {"OnBuild": []})
    checked = check_bases

    async def vanishing(client: Any, plans: Any) -> dict[str, str]:
        rig.host.remove_image(BASE_ID)
        return await checked(client, plans)

    monkeypatch.setattr(stack_runs, "check_bases", vanishing)

    with pytest.raises(StackOperationFailed) as missing:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    assert missing.value.reason == "base_missing"
    assert rig.buildx.counter == 0
    assert [row["result"] for row in await rig.rows("stack_deployed")] == ["failure", "failure"]


async def test_a_base_replaced_while_the_build_runs_fails_the_deploy_before_up(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    record = tmp_path / "up.jsonl"
    await seed_build_stack(rig, text=recording(record))
    rig.host.add_image(REFERENCE, BASE_ID)
    checked = check_bases

    async def replaced(client: Any, plans: Any) -> dict[str, str]:
        recorded = await checked(client, plans)
        rig.host.add_image(REFERENCE, FOREIGN_ID)
        return recorded

    monkeypatch.setattr(stack_runs, "check_bases", replaced)

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    assert failed.value.reason == "base_changed"
    assert rig.buildx.counter == 1
    assert "up" not in [call["cmd"] for call in read_records(record)]
    assert runs_left_in(rig.compose.work_base) == []


async def test_a_failed_build_fails_the_deploy_before_up_and_keeps_the_scrubbed_log(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    record = tmp_path / "up.jsonl"
    await seed_build_stack(rig, text=recording(record))
    rig.host.add_image(REFERENCE)
    (rig.buildx.directory / "stderr.txt").write_text("ERROR: step failed\n", encoding="utf-8")
    (rig.buildx.directory / "exit").write_text("1", encoding="utf-8")

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    assert failed.value.reason == "build_failed" and failed.value.exit_code == 1
    assert "ERROR: step failed" in failed.value.stderr
    assert "up" not in [call["cmd"] for call in read_records(record)]
    assert runs_left_in(rig.compose.work_base) == []
    assert [row["result"] for row in await rig.rows("stack_deployed")] == ["failure"]


async def test_a_standard_user_with_write_cannot_deploy_a_stack_that_builds(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    await seed_build_stack(rig, owner_user_id=STANDARD.user_id)
    rig.host.add_image(REFERENCE)

    with pytest.raises(StackRejected) as deploy:
        await rig.fresh().actions.deploy(STANDARD, ENV_A, STACK)
    with pytest.raises(StackRejected) as redeploy:
        await rig.fresh().actions.redeploy(STANDARD, ENV_A, STACK)
    with pytest.raises(StackRejected) as edited:
        await rig.fresh().queries.validate(
            STANDARD, ENV_A, name=STACK, compose_text=BUILD_FILE, variables=None, existing=True
        )

    assert deploy.value.reason == redeploy.value.reason == "build_administrator_only"
    assert edited.value.reason == "refused_construct"
    assert rig.buildx.counter == 0


async def test_the_json_interface_refuses_to_create_or_redeploy_a_stack_that_builds(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_build_stack(rig)

    with pytest.raises(StackRejected) as created:
        await rig.fresh().actions.create(
            ADMIN, ENV_A, name="made", compose_text=BUILD_FILE, allow_builds=False
        )
    with pytest.raises(StackRejected) as redeployed:
        await rig.fresh().actions.redeploy(ADMIN, ENV_A, STACK, allow_builds=False)

    assert created.value.reason == redeployed.value.reason == "build_not_available"
    assert pulls(rig) == 0


async def test_builds_are_refused_when_buildx_is_unavailable_and_nothing_else_changes(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_build_stack(rig)
    plain = "services:\n  web:\n    image: nginx:1\n"
    await rig.fresh().actions.create(ADMIN, ENV_A, name="plain", compose_text=plain)
    rig.compose.buildx.binary = None

    with pytest.raises(StackRejected) as refused:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)
    deployed = await rig.fresh().actions.deploy(ADMIN, ENV_A, "plain")

    assert refused.value.reason == "buildx_unavailable"
    assert deployed.status == "active"
    assert pulls(rig) == 0


async def test_a_secret_in_a_build_argument_is_refused(stack_rig: Rig) -> None:
    rig = stack_rig
    text = (
        f"services:\n  web:\n    build:\n      context: {CONTEXT}\n"
        "      args:\n        T: ${TOKEN}\n"
    )
    seeded = await seed_build_stack(rig, text=text)
    with pytest.raises(StackRejected) as refused:
        await rig.fresh().actions.redeploy(
            ADMIN, ENV_A, STACK, variables=form_variables(("TOKEN", "alpha-secret-value", True))
        )

    assert seeded.stack_id
    assert refused.value.reason == "secret_in_build_arg"
    assert list(refused.value.fields) == ["services.web.build.args.T"]


async def test_the_confirmation_names_the_builds_and_a_change_after_review_is_refused(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    seeded = await seed_build_stack(rig)

    review = await rig.fresh().queries.redeploy_confirmation(ADMIN, ENV_A, STACK)
    await add_context(rig, seeded.stack_id, CONTEXT, {"Dockerfile": DOCKERFILE, "app.py": "x"})

    assert review.builds == (BuildSummary("web", TAG, CONTEXT, (REFERENCE,), False),)
    assert len(review.expected_builds) == 64 and review.conflicts == ()
    current_inputs = (await rig.fresh().queries.require_stack(ADMIN, ENV_A, STACK)).inputs_digest
    for inputs_digest, builds_digest in (
        (review.expected_inputs_digest, None),
        (current_inputs or "", STALE_DIGEST),
    ):
        with pytest.raises(ChangedSinceReview):
            await rig.fresh().actions.redeploy(
                ADMIN,
                ENV_A,
                STACK,
                expected_file_digest=review.expected_file_digest,
                expected_inputs_digest=inputs_digest,
                expected_builds=builds_digest,
            )
    assert rig.buildx is not None and rig.buildx.counter == 0 and pulls(rig) == 0


async def test_an_automated_redeploy_reuses_the_tag_and_refuses_when_it_is_absent(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    await seed_build_stack(rig)
    await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)
    context = await authority_context(
        rig.db, rig.clock, rig.engine.locks, environment_id=ENV_A, subsystem=WEBHOOK
    )
    authority = context.authority()

    reused = await rig.fresh().actions.redeploy(authority, ENV_A, STACK)
    rig.host.remove_image(rig.host.images[TAG]["Id"])
    with pytest.raises(StackRejected) as absent:
        await rig.fresh().actions.redeploy(authority, ENV_A, STACK)

    assert reused.built == (BuiltView("web", TAG, True),)
    assert absent.value.reason == "built_image_missing"
    assert rig.buildx.counter == 1


async def test_an_administrator_saves_a_building_file_and_a_refused_dockerfile_is_named_at_deploy(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    assert rig.buildx is not None
    syntax = "# syntax=docker/dockerfile:1\nFROM nginx:1\n"
    seeded = await seed_build_stack(rig, dockerfile=syntax)
    stored = await rig.fresh().queries.require_stack(ADMIN, ENV_A, STACK, "write")

    report = await rig.fresh().queries.validate(
        ADMIN, ENV_A, name=STACK, compose_text=BUILD_FILE, variables=None, existing=True
    )
    saved = await rig.fresh().actions.update(
        ADMIN,
        ENV_A,
        STACK,
        expected_file_digest=stored.compose_file_digest,
        compose_text=BUILD_FILE,
    )
    with pytest.raises(StackRejected) as syntax_refused:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)
    await add_context(rig, seeded.stack_id, CONTEXT, {"Other": "FROM nginx:1\n"})
    with pytest.raises(StackRejected) as file_missing:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, STACK)

    assert [service.name for service in report.services] == ["web"] and saved is not None
    assert syntax_refused.value.reason == "refused_construct"
    assert "Dockerfile line 1" in syntax_refused.value.fields["services.web.build.dockerfile"]
    assert (
        "not a file in the build context"
        in file_missing.value.fields["services.web.build.dockerfile"]
    )
    assert rig.buildx.counter == 0 and pulls(rig) == 0
