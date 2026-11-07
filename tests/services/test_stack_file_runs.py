"""Companion files through the stack services: delivery to Compose, confinement and cleanup."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.auth.principal import Principal
from app.logging import REDACTED
from app.services.stack_deploy import ChangedSinceReview, StackOperationFailed, StackRejected
from app.services.stack_document import CompanionFile
from app.services.stacks import StackService
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.stack_files_support import (
    invocation,
    plain,
    read_file_records,
    secret,
    use_files_compose,
)
from tests.stack_support import (
    Rig,
    activity_dump,
    files_containing,
    logged_text,
    poll_until,
    project_box,
    read_records,
    recorded_call,
    removal_binding,
    seed_stack_row,
)
from tests.support import present

SHOP = "shop"
PLAIN_SHOP = "plainshop"
CONFIG_CONTENT = "server {\n  location / { return 200 '$host'; }\n}\n"
SECRET_FILE_CONTENT = "k$ey\n\n"
SENTINEL = "SECRET_SENTINEL_VALUE"
SECRET_ENV_KEY = "API_KEY"
SHORT_TIMEOUT = 1.0
OLD_VALUE = "A=old\n"
NEW_VALUE = "A=new\n"

EVERY_COMPANION = """
include: [inc.yaml]
services:
  web:
    image: nginx:1
    env_file: [app.env, ./secret.env, {{path: opt.env}}]
    label_file: labels.txt
    configs: [app_conf]
    secrets: [app_key]
  other:
    image: nginx:1
    extends: {{file: base.yaml, service: b}}
configs:
  app_conf: {{file: nginx.conf}}
secrets:
  app_key: {{file: ./key.txt}}
x-fake:
  record: {record}
"""
EVERY_COMPANION_FILES = (
    plain("app.env", "PLAIN=1\n"),
    secret("secret.env", "TOKEN=hidden-token-value\n"),
    plain("opt.env", "OPT=1\n"),
    plain("labels.txt", "com.example.kind=web\n"),
    plain("nginx.conf", CONFIG_CONTENT),
    secret("key.txt", SECRET_FILE_CONTENT),
    plain("inc.yaml", "services: {}\n"),
    plain("base.yaml", "services:\n  b:\n    image: nginx:1\n"),
)
SECRET_ENV_STACK = """
services:
  web:
    image: nginx:1
    env_file: s.env
x-fake:
  record: {record}
  up: {up}
"""
PLAIN_ENV_STACK = """
services:
  web:
    image: nginx:1
    env_file: p.env
x-fake:
  record: {record}
"""
FOREIGN_LABEL_STACK = """
services:
  web:
    image: nginx:1
    label_file: l.env
x-fake:
  record: {record}
  label_path: /etc/passwd
"""
REFUSED_STORED_STACK = """
services:
  web:
    image: nginx:1
    env_file: /etc/passwd
x-fake:
  record: {record}
"""


@pytest.fixture
async def files_rig(stack_rig: Rig) -> Rig:
    await use_files_compose(stack_rig)
    return stack_rig


@pytest.fixture
def record(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("records") / "calls.jsonl"


def project_files(rig: Rig, token: str) -> list[str]:
    return sorted(path.name for path in (rig.compose.files.base / token).iterdir())


async def project_token(rig: Rig) -> str:
    return str(
        present(await rig.db.fetch_one("SELECT project_directory FROM stack"))["project_directory"]
    )


async def create_and_deploy(
    actions: StackService,
    actor: Principal,
    environment_id: int,
    *,
    name: str,
    compose_text: str,
    files: Sequence[CompanionFile],
) -> None:
    created = await actions.create(
        actor, environment_id, name=name, compose_text=compose_text, files=files
    )
    await actions.deploy(actor, environment_id, created.name)


async def refusal(call: Any) -> StackRejected:
    with pytest.raises(StackRejected) as raised:
        await call
    return raised.value


async def test_every_companion_form_passes_and_compose_gets_the_override_the_files_and_the_sources(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig
    created = await rig.fresh().actions.create(
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=EVERY_COMPANION.format(record=record),
        files=EVERY_COMPANION_FILES,
    )

    await rig.fresh().actions.deploy(ADMIN, ENV_A, created.name)

    up = recorded_call(record, "up")
    seen = invocation(record, "up")
    stack_file = next(item for item in up["argv"] if item.endswith("compose.yaml"))
    first = up["argv"].index("--file")
    assert up["argv"][first + 1] == stack_file
    assert up["argv"][first + 2] == "--file"
    assert up["argv"][first + 3].endswith("sources.yaml")
    assert up["env"]["DOCKMIN_SOURCE_1"] == CONFIG_CONTENT
    assert up["env"]["DOCKMIN_SOURCE_2"] == SECRET_FILE_CONTENT
    assert "environment: DOCKMIN_SOURCE_1" in seen["override"]
    assert "environment: DOCKMIN_SOURCE_2" in seen["override"]
    assert sorted(seen["directory"]) == [
        "app.env",
        "base.yaml",
        "compose.yaml",
        "inc.yaml",
        "labels.txt",
        "opt.env",
        "secret.env",
    ]
    assert seen["directory"]["secret.env"]["mode"] == "0o600"


async def test_no_secret_file_stays_in_the_project_directory_after_a_run(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig

    await create_and_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=EVERY_COMPANION.format(record=record),
        files=EVERY_COMPANION_FILES,
    )

    assert project_files(rig, await project_token(rig)) == [
        "app.env",
        "base.yaml",
        "compose.yaml",
        "inc.yaml",
        "labels.txt",
        "opt.env",
    ]
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_refused_form_is_refused_at_its_path_and_compose_never_runs(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig

    refused = await refusal(
        rig.fresh().actions.create(
            ADMIN,
            ENV_A,
            name=SHOP,
            compose_text=REFUSED_STORED_STACK.format(record=record),
            files=(plain("p.env"),),
        )
    )

    assert refused.reason == "refused_construct"
    assert list(refused.fields) == ["compose_file"]
    assert refused.fields["compose_file"].startswith("`services.web.env_file`: ")
    assert read_records(record) == []
    assert read_file_records(record) == []


async def test_stored_text_that_fails_the_pre_check_refuses_every_run_with_a_failure_row(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig
    await seed_stack_row(
        rig, ENV_A, SHOP, REFUSED_STORED_STACK.format(record=record), owner_user_id=ADMIN.user_id
    )
    project_box(rig.host, 1, SHOP, "web", state="running")
    bound = await removal_binding(rig, ADMIN, ENV_A, SHOP)

    refusals = {
        "stack_started": await refusal(rig.fresh().actions.start(ADMIN, ENV_A, SHOP)),
        "stack_stopped": await refusal(rig.fresh().actions.stop(ADMIN, ENV_A, SHOP)),
        "stack_restarted": await refusal(rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)),
        "stack_removed": await refusal(rig.fresh().actions.remove(ADMIN, ENV_A, SHOP, **bound)),
        "stack_redeployed": await refusal(rig.fresh().actions.redeploy(ADMIN, ENV_A, SHOP)),
    }

    rows = {action: await rig.rows(action) for action in refusals}
    outcomes = {
        action: (
            error.reason,
            [row["result"] for row in rows[action]],
            [row["detail"]["reason"] for row in rows[action]],
        )
        for action, error in refusals.items()
    }
    assert outcomes == {
        action: ("refused_construct", ["failure"], ["refused_construct"]) for action in refusals
    }
    assert read_records(record) == []
    assert len(rig.host.boxes) == 1


async def test_a_resolved_file_path_that_names_no_stored_file_is_refused_before_anything_runs(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig

    refused = await refusal(
        rig.fresh().actions.create(
            ADMIN,
            ENV_A,
            name=SHOP,
            compose_text=FOREIGN_LABEL_STACK.format(record=record),
            files=(plain("l.env", "com.example.k=v\n"),),
        )
    )

    assert refused.reason == "refused_construct"
    assert list(refused.fields) == ["services.web.label_file[0]"]
    assert not any(call["cmd"] == "up" for call in read_records(record))


async def test_a_failed_run_shows_no_secret_file_content_and_leaves_no_file(
    files_rig: Rig,
    record: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = files_rig
    content = f"{SECRET_ENV_KEY}={SENTINEL}\n"
    await rig.fresh().actions.create(
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=SECRET_ENV_STACK.format(record=record, up="fail"),
        files=(secret("s.env", content),),
    )

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, SHOP)

    shown = failed.value.stderr + await activity_dump(rig) + logged_text(caplog)
    assert invocation(record, "up")["directory"]["s.env"]["content"] == content
    assert "port is already allocated" in failed.value.stderr
    assert REDACTED in failed.value.stderr
    assert SENTINEL not in shown
    assert project_files(rig, await project_token(rig)) == ["compose.yaml"]
    assert files_containing(tmp_path, SENTINEL) == []
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_cancelled_run_leaves_no_secret_file(files_rig: Rig, record: Path) -> None:
    rig = files_rig
    await rig.fresh().actions.create(
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=SECRET_ENV_STACK.format(record=record, up="hang"),
        files=(secret("s.env", f"{SECRET_ENV_KEY}={SENTINEL}\n"),),
    )
    rig.sim.operation_timeout = SHORT_TIMEOUT
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_A, SHOP))
    await poll_until(lambda: any(call["cmd"] == "up" for call in read_records(record)))

    deploy.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deploy

    assert "s.env" in invocation(record, "up")["directory"]
    assert project_files(rig, await project_token(rig)) == ["compose.yaml"]
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_secret_environment_value_is_masked_in_the_resolved_digest_and_a_plain_one_is_not(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig
    secret_text = SECRET_ENV_STACK.format(record=record, up="ok")
    await rig.fresh().actions.create(
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=secret_text,
        files=(secret("s.env", f"{SECRET_ENV_KEY}=first-hidden-value\n"),),
    )
    await rig.fresh().actions.create(
        ADMIN,
        ENV_A,
        name=PLAIN_SHOP,
        compose_text=PLAIN_ENV_STACK.format(record=record),
        files=(plain("p.env", OLD_VALUE),),
    )
    await rig.fresh().actions.deploy(ADMIN, ENV_A, SHOP)
    await rig.fresh().actions.deploy(ADMIN, ENV_A, PLAIN_SHOP)
    first = await resolved_digests(rig)

    await rig.fresh().actions.write_file(
        ADMIN, ENV_A, SHOP, "s.env", content=f"{SECRET_ENV_KEY}=second-hidden-value\n", secret=True
    )
    await rig.fresh().actions.write_file(
        ADMIN, ENV_A, PLAIN_SHOP, "p.env", content=NEW_VALUE, secret=False
    )
    await rig.fresh().actions.redeploy(ADMIN, ENV_A, SHOP)
    await rig.fresh().actions.redeploy(ADMIN, ENV_A, PLAIN_SHOP)
    second = await resolved_digests(rig)

    assert second[SHOP] == first[SHOP]
    assert second[PLAIN_SHOP] != first[PLAIN_SHOP]


async def resolved_digests(rig: Rig) -> dict[str, str]:
    rows = await rig.db.fetch_limited("SELECT name, resolved_digest FROM stack", (), limit=10)
    return {row["name"]: row["resolved_digest"] for row in rows}


async def test_a_file_written_in_the_second_of_the_deploy_marks_the_stack_changed(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig
    await create_and_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=PLAIN_ENV_STACK.format(record=record),
        files=(plain("p.env", OLD_VALUE),),
    )

    async def changed() -> bool:
        return (await rig.fresh().queries.detail(ADMIN, ENV_A, SHOP)).row.changed_since_deploy

    async def write(content: str, *, as_secret: bool = False) -> None:
        stored = await rig.fresh().queries.require_stack(ADMIN, ENV_A, SHOP, "write")
        await rig.fresh().actions.write_file(
            ADMIN,
            ENV_A,
            SHOP,
            "p.env",
            content=content,
            secret=as_secret,
            expected_inputs_digest=stored.inputs_digest or "",
        )

    after_deploy = await changed()
    await write(NEW_VALUE)
    after_edit = await changed()
    await write(OLD_VALUE)
    after_set_back = await changed()
    await write(OLD_VALUE, as_secret=True)
    after_made_secret = await changed()

    assert not after_deploy
    assert after_edit
    assert not after_set_back
    assert after_made_secret


async def test_a_redeploy_reviewed_before_a_file_changed_is_refused_and_nothing_runs(
    files_rig: Rig, record: Path
) -> None:
    rig = files_rig
    await create_and_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=PLAIN_ENV_STACK.format(record=record),
        files=(plain("p.env", OLD_VALUE),),
    )
    review = await rig.fresh().queries.redeploy_confirmation(ADMIN, ENV_A, SHOP)
    await rig.fresh().actions.write_file(
        ADMIN,
        ENV_A,
        SHOP,
        "p.env",
        content=NEW_VALUE,
        secret=False,
        expected_inputs_digest=review.expected_inputs_digest,
    )
    mutations = len(rig.host.mutations())
    calls = len(read_records(record))

    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.redeploy(
            ADMIN,
            ENV_A,
            SHOP,
            expected_file_digest=review.expected_file_digest,
            expected_inputs_digest=review.expected_inputs_digest,
        )

    assert len(rig.host.mutations()) == mutations
    assert len(read_records(record)) == calls


STANDARD_FILES_STACK = """
services:
  web:
    image: nginx:1
    env_file: p.env
    label_file: l.env
"""
EXTENDED_PRIVILEGE_STACK = """
services:
  web:
    image: nginx:1
    extends: {file: base.yaml, service: base}
"""
PRIVILEGED_BASE = "services:\n  base:\n    image: nginx:1\n    privileged: true\n"
RESERVED_LABEL_FILE = "com.docker.compose.project=other\n"
RESERVED_LABEL_MESSAGE = "The stack membership labels are set by the product."


async def test_a_standard_user_may_use_env_and_label_files(files_rig: Rig) -> None:
    rig = files_rig

    created = await rig.fresh().actions.create(
        STANDARD,
        ENV_A,
        name=SHOP,
        compose_text=STANDARD_FILES_STACK,
        files=(plain("p.env"), plain("l.env", "com.example.kind=web\n")),
    )

    assert created.name == SHOP


async def test_a_label_file_that_sets_a_reserved_label_is_refused(files_rig: Rig) -> None:
    rig = files_rig

    refused = await refusal(
        rig.fresh().actions.create(
            ADMIN,
            ENV_A,
            name=SHOP,
            compose_text=STANDARD_FILES_STACK,
            files=(plain("p.env"), plain("l.env", RESERVED_LABEL_FILE)),
        )
    )

    assert refused.reason == "restricted"
    assert refused.fields == {"services.web.labels": RESERVED_LABEL_MESSAGE}


async def test_a_privilege_reached_through_extends_file_is_refused_for_a_standard_user_only(
    files_rig: Rig,
) -> None:
    rig = files_rig
    files = (plain("base.yaml", PRIVILEGED_BASE),)

    refused = await refusal(
        rig.fresh().actions.create(
            STANDARD, ENV_A, name=SHOP, compose_text=EXTENDED_PRIVILEGE_STACK, files=files
        )
    )
    created = await rig.fresh().actions.create(
        ADMIN, ENV_A, name=PLAIN_SHOP, compose_text=EXTENDED_PRIVILEGE_STACK, files=files
    )

    assert refused.reason == "restricted"
    assert any(finding.option == "privileged" for finding in refused.findings)
    assert created.name == PLAIN_SHOP
