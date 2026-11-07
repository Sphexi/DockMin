"""Companion files through the Swarm stack services: config and secret objects built from files."""

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_file import StackFileRepository
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from tests.access_support import ADMIN, ENV_A
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.stack_files_support import plain, secret, use_files_compose_for_swarm
from tests.stack_support import Rig
from tests.swarm_stack_support import (
    NAME,
    PLAIN_IMAGE,
    StackDaemon,
    deploy_stack,
    redeploy_stack,
    reviewed_plan,
)

CONFIG_KEY = "cfg"
SECRET_KEY = "sec"
CONFIG_CONTENT = "listen $port;\n"
OTHER_CONFIG_CONTENT = "listen 8080;\n"
SECRET_CONTENT = "first-file-secret\n"
OTHER_SECRET_CONTENT = "second-file-secret\n"
DIGEST_LENGTH = 12
FILE_STACK = f"""
services:
  web:
    image: {PLAIN_IMAGE}
    deploy: {{replicas: 1}}
    configs: [{CONFIG_KEY}]
    secrets: [{SECRET_KEY}]
configs:
  {CONFIG_KEY}: {{file: app.conf}}
secrets:
  {SECRET_KEY}: {{file: key.txt}}
"""
SECRET_AS_CONFIG_STACK = FILE_STACK.replace("{file: app.conf}", "{file: key.txt}")
PLAIN_AS_SECRET_STACK = FILE_STACK.replace("{file: key.txt}", "{file: app.conf}")


@pytest.fixture
async def files_swarm(swarm_stack_rig: Rig, tmp_path: Path) -> Rig:
    await use_files_compose_for_swarm(swarm_stack_rig, tmp_path / "files-compose")
    return swarm_stack_rig


def stored_text(item: dict[str, Any]) -> str:
    return base64.b64decode(item["Spec"]["Data"]).decode()


def digest_suffix(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:DIGEST_LENGTH]


async def token_of(rig: Rig, name: str) -> str:
    stacks = StackRepository(rig.db, rig.registry.vault, rig.clock)
    stored = await stacks.get(ENV_A, NAME)
    assert stored is not None
    files = StackFileRepository(rig.db, rig.registry.vault, rig.clock, stacks=stacks)
    return next(item.value_token for item in await files.files(stored.id) if item.name == name)


async def create_file_stack(rig: Rig, text: str = FILE_STACK) -> None:
    await rig.fresh().swarm_actions.create(
        ADMIN,
        ENV_A,
        name=NAME,
        compose_text=text,
        files=(plain("app.conf", CONFIG_CONTENT), secret("key.txt", SECRET_CONTENT)),
    )


async def test_a_config_from_a_plain_file_is_named_by_its_content_and_a_secret_file_by_its_token(
    files_swarm: Rig, stack_daemon: StackDaemon
) -> None:
    rig = files_swarm
    await create_file_stack(rig)

    await deploy_stack(rig)

    assert stack_daemon.config_names() == [f"{NAME}_{CONFIG_KEY}_{digest_suffix(CONFIG_CONTENT)}"]
    assert stack_daemon.secret_names() == [f"{NAME}_{SECRET_KEY}_{await token_of(rig, 'key.txt')}"]
    assert [stored_text(item) for item in stack_daemon.configs.values()] == [CONFIG_CONTENT]
    assert [stored_text(item) for item in stack_daemon.secrets.values()] == [SECRET_CONTENT]


async def test_a_changed_secret_file_rotates_the_secret_and_a_changed_plain_file_the_config(
    files_swarm: Rig, stack_daemon: StackDaemon
) -> None:
    rig = files_swarm
    await create_file_stack(rig)
    await deploy_stack(rig)
    old_configs, old_secrets = stack_daemon.config_names(), stack_daemon.secret_names()
    await rig.fresh().swarm_actions.write_file(
        ADMIN, ENV_A, NAME, "key.txt", content=OTHER_SECRET_CONTENT, secret=True
    )
    await rig.fresh().swarm_actions.write_file(
        ADMIN, ENV_A, NAME, "app.conf", content=OTHER_CONFIG_CONTENT, secret=False
    )

    view = await redeploy_stack(rig)

    assert view.state == "succeeded", view.outcome
    assert stack_daemon.secret_names() == [f"{NAME}_{SECRET_KEY}_{await token_of(rig, 'key.txt')}"]
    assert stack_daemon.config_names() == [
        f"{NAME}_{CONFIG_KEY}_{digest_suffix(OTHER_CONFIG_CONTENT)}"
    ]
    assert set(stack_daemon.secret_names()).isdisjoint(old_secrets)
    assert set(stack_daemon.config_names()).isdisjoint(old_configs)


async def test_a_secret_rewritten_with_the_same_content_still_rotates_because_it_has_a_new_token(
    files_swarm: Rig, stack_daemon: StackDaemon
) -> None:
    rig = files_swarm
    await create_file_stack(rig)
    await deploy_stack(rig)
    old_secrets = stack_daemon.secret_names()
    await rig.fresh().swarm_actions.write_file(
        ADMIN, ENV_A, NAME, "key.txt", content=SECRET_CONTENT, secret=True
    )

    await redeploy_stack(rig)

    assert set(stack_daemon.secret_names()).isdisjoint(old_secrets)


async def test_a_file_changed_after_the_review_refuses_the_redeploy_and_sends_nothing(
    files_swarm: Rig, stack_daemon: StackDaemon
) -> None:
    rig = files_swarm
    await create_file_stack(rig)
    await deploy_stack(rig)
    row, plan = await reviewed_plan(rig)
    await rig.fresh().swarm_actions.write_file(
        ADMIN,
        ENV_A,
        NAME,
        "app.conf",
        content=OTHER_CONFIG_CONTENT,
        secret=False,
        expected_inputs_digest=row.inputs_digest or "",
    )
    before = list(stack_daemon.mutating())

    with pytest.raises(ChangedSinceReview):
        await rig.fresh().swarm_actions.redeploy(
            ADMIN,
            ENV_A,
            NAME,
            expected_file_digest=row.compose_file_digest,
            expected_inputs_digest=row.inputs_digest or "",
            expected_plan=plan.digest,
            remove_orphans=False,
        )

    assert stack_daemon.mutating() == before


@pytest.mark.parametrize(
    ("text", "path"),
    [
        pytest.param(SECRET_AS_CONFIG_STACK, "configs.cfg.file", id="config-from-a-secret-file"),
        pytest.param(PLAIN_AS_SECRET_STACK, "secrets.sec.file", id="secret-from-a-plain-file"),
    ],
)
async def test_a_config_from_a_secret_file_and_a_secret_from_a_plain_file_are_refused(
    files_swarm: Rig, stack_daemon: SwarmEngine, text: str, path: str
) -> None:
    rig = files_swarm

    with pytest.raises(StackRejected) as refused:
        await create_file_stack(rig, text)

    assert refused.value.fields["compose_file"].startswith(f"`{path}`: ")
    assert stack_daemon.mutating() == []
