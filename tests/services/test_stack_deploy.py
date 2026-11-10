"""The deploy sequence of the stack service: targets, pulls, bindings, objects, version."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli.binary import ComposeBinary
from app.compose_cli.parsers import FLOOR, ComposeVersion, ResolvedProject
from app.logging import REDACTED
from app.services.access import ResourceKey
from app.services.container_provisioning import NETWORK_UNAVAILABLE, VOLUME_UNAVAILABLE
from app.services.image_bindings import BindingPullCredentials
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ControlSpec
from app.services.stack_deploy import (
    ComposeNotReady,
    PreexistingObjects,
    StackRejected,
    delivered_sources,
    file_secret_values,
    hide_keys,
    secret_env_keys,
)
from app.services.stack_document import CompanionFile, precheck
from tests.access_support import ADMIN, ENV_A, ENV_B, REMOTE_URL, SOCKET_PATH, STANDARD, digest_of
from tests.image_support import (
    REFERENCE,
    binding_rows,
    decode_auth,
    digest,
    mark_missing,
    register,
    rows_for,
)
from tests.stack_support import (
    Rig,
    activity_dump,
    create_then_deploy,
    files_containing,
    form_variables,
    logged_text,
    read_records,
    recorded_call,
)
from tests.support import present

SECRET = "planted-secret-7731"
GREETING = "hello-value"
LEAKED = "leaked-from-the-server-environment"
LOCAL_STACK = "shopb"
REMOTE_STACK = "shopa"

BASE_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "DOCKER_CONFIG",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TZ",
        "NO_COLOR",
        "DOCKER_CLI_HINTS",
        "COMPOSE_MENU",
        "DOCKER_HOST",
    }
)
TLS_ENVIRONMENT = frozenset({"DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"})
STACK_VARIABLES = frozenset({"GREETING", "TOKEN"})
UP_TAIL = ["up", "--detach", "--pull", "never", "--no-build"]
CERTIFICATE_FILES = {"ca.pem": "0o600", "cert.pem": "0o600", "key.pem": "0o600"}


def recorded_stack(record: Path) -> str:
    return f"""
services:
  web:
    image: nginx:1
    environment:
      GREETING: ${{GREETING}}
      TOKEN: ${{TOKEN}}
    volumes:
      - data:/data
volumes:
  data: {{}}
x-fake:
  record: {record}
"""


async def test_one_path_deploys_to_a_local_socket_and_a_remote_tls_environment(
    stack_rig: Rig,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("HOST_ONLY_VARIABLE", LEAKED)
    monkeypatch.setenv("DOCKER_HOST", "tcp://leaked.invalid:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/leaked/ca.pem")
    caplog.set_level(logging.DEBUG)
    rig = stack_rig
    records = tmp_path_factory.mktemp("records")
    local_record, remote_record = records / "local.jsonl", records / "remote.jsonl"
    variables = form_variables(("GREETING", GREETING, False), ("TOKEN", SECRET, True))

    await create_then_deploy(
        rig.services.actions,
        ADMIN,
        ENV_B,
        name=LOCAL_STACK,
        compose_text=recorded_stack(local_record),
        variables=variables,
    )
    await create_then_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_A,
        name=REMOTE_STACK,
        compose_text=recorded_stack(remote_record),
        variables=variables,
    )

    local, remote = recorded_call(local_record, "up"), recorded_call(remote_record, "up")
    work_base = rig.compose.work_base
    assert set(local["env"]) == BASE_ENVIRONMENT | STACK_VARIABLES
    assert local["env"]["DOCKER_HOST"] == f"unix://{SOCKET_PATH}"
    assert local["cert"] == {}
    assert set(remote["env"]) == BASE_ENVIRONMENT | TLS_ENVIRONMENT | STACK_VARIABLES
    assert remote["env"]["DOCKER_HOST"] == REMOTE_URL
    assert remote["env"]["DOCKER_TLS_VERIFY"] == "1"
    assert remote["cert"] == CERTIFICATE_FILES
    cert_path = Path(remote["env"]["DOCKER_CERT_PATH"])
    assert cert_path.is_relative_to(work_base)
    for call in (local, remote):
        assert call["argv"][-5:] == UP_TAIL
        assert call["env"]["TOKEN"] == SECRET
        assert call["env"]["GREETING"] == GREETING
        assert Path(call["env"]["DOCKER_CONFIG"]).is_relative_to(work_base)
        assert Path(call["cwd"]).parent == rig.compose.files.base
        assert call["dir_files"] == ["compose.yaml"]
    assert list(work_base.iterdir()) == []
    assert files_containing(tmp_path, SECRET) == []
    assert SECRET not in await activity_dump(rig)
    assert SECRET not in logged_text(caplog)


PULLED_DIGEST = digest(7)
MIXED_STACK = f"""
services:
  web:
    image: nginx:1
  app:
    image: {REFERENCE}
volumes:
  data: {{}}
"""
ABSENT_REFERENCE = "registry.example.com/absent:1"
NEVER_PULL_STACK = f"""
services:
  app:
    image: {ABSENT_REFERENCE}
    pull_policy: never
"""


async def sub_resources(rig: Rig, name: str) -> list[str]:
    row = await rig.db.fetch_one(
        "SELECT sub_resource_ids FROM resource_control "
        "WHERE resource_type = 'stack' AND resource_id = ?",
        (name,),
    )
    assert row is not None
    ids: list[str] = json.loads(row["sub_resource_ids"])
    return ids


def kinds_of(ids: list[str]) -> list[str]:
    return sorted(item.split(":")[0] for item in ids)


def pulls_made(rig: Rig) -> int:
    return len(rig.host.calls("POST", "/images/create"))


async def test_pulls_follow_the_policy_bind_the_credential_set_and_refresh_sub_resources(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    set_id = await register(rig, "only", environments=(ENV_A,))
    rig.host.pull_digest = PULLED_DIGEST
    original = BindingPullCredentials.resolve
    in_transaction: list[bool] = []

    async def watching(self: BindingPullCredentials, *args: Any, **kwargs: Any) -> Any:
        in_transaction.append(self._bindings.in_transaction)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(BindingPullCredentials, "resolve", watching)

    first = await create_then_deploy(
        rig.services.actions, ADMIN, ENV_A, name="shop", compose_text=MIXED_STACK
    )
    first_ids = await sub_resources(rig, "shop")
    [pull] = rig.host.calls("POST", "/images/create")
    deployed = (await rig.rows("stack_deployed"))[-1]["detail"]
    [binding] = await binding_rows(rig.db)

    assert first.pulled == (REFERENCE,)
    assert decode_auth(pull.headers["x-registry-auth"])["username"] == "user-only"
    assert (binding["image_reference"], binding["registry_credential_id"]) == (REFERENCE, set_id)
    assert binding["observed_digest"] == PULLED_DIGEST
    assert deployed["binding"] == "created"
    assert len(await rows_for(rig.db, "image_bound")) == 1
    assert kinds_of(first_ids) == ["container", "container", "network", "volume"]
    assert in_transaction
    assert not any(in_transaction)

    second = await rig.fresh().actions.redeploy(ADMIN, ENV_A, "shop")
    second_ids = await sub_resources(rig, "shop")

    assert second.pulled == ()
    assert pulls_made(rig) == 1
    assert second_ids != first_ids
    assert kinds_of(second_ids) == kinds_of(first_ids)
    assert not {i for i in first_ids if i.startswith("container:")} & set(second_ids)

    third = await rig.fresh().actions.redeploy(ADMIN, ENV_A, "shop", pull=True)
    redeployed = (await rig.rows("stack_redeployed"))[-1]["detail"]

    assert third.pulled == ("nginx:1", REFERENCE)
    assert pulls_made(rig) == 3
    assert redeployed["pull_requested"] is True
    assert redeployed["binding"] == "skipped,kept"
    assert [b["image_reference"] for b in await binding_rows(rig.db)] == [REFERENCE]

    with pytest.raises(StackRejected) as refused:
        await rig.fresh().queries.validate(
            ADMIN,
            ENV_A,
            name="never",
            compose_text=NEVER_PULL_STACK,
            variables=None,
            deploy=True,
        )
    assert refused.value.reason == "image_missing"
    assert list(refused.value.fields) == ["services.app.pull_policy"]
    assert pulls_made(rig) == 3


APP_STACK = f"""
services:
  app:
    image: {REFERENCE}
"""
UNKNOWN_SET = 9999


async def test_credential_choices_resolve_before_anything_runs_and_each_refusal_names_the_image(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await register(rig, "first", environments=(ENV_A,))
    second = await register(rig, "second", environments=(ENV_A,))

    with pytest.raises(StackRejected) as ambiguous:
        await create_then_deploy(
            rig.services.actions, ADMIN, ENV_A, name="shop", compose_text=APP_STACK
        )
    with pytest.raises(StackRejected) as unavailable:
        await create_then_deploy(
            rig.fresh().actions,
            ADMIN,
            ENV_A,
            name="shop",
            compose_text=APP_STACK,
            choices={REFERENCE: CredentialChoice(UNKNOWN_SET)},
        )

    assert ambiguous.value.reason == "credential_choice_required"
    assert list(ambiguous.value.fields) == [REFERENCE]
    assert unavailable.value.reason == "credential_unavailable"
    assert list(unavailable.value.fields) == [REFERENCE]
    assert rig.host.mutations() == []
    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    reasons = [r["detail"]["reason"] for r in await rig.rows("stack_created")]
    assert reasons == ["credential_choice_required", "credential_unavailable"]

    await create_then_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_A,
        name="shop",
        compose_text=APP_STACK,
        choices={REFERENCE: CredentialChoice(second)},
    )
    [pull] = rig.host.calls("POST", "/images/create")
    [binding] = await binding_rows(rig.db)

    assert decode_auth(pull.headers["x-registry-auth"])["username"] == "user-second"
    assert binding["registry_credential_id"] == second

    await mark_missing(rig.db, second)
    with pytest.raises(StackRejected) as missing:
        await rig.fresh().actions.redeploy(ADMIN, ENV_A, "shop", pull=True)

    assert missing.value.reason == "credential_choice_required"
    assert list(missing.value.fields) == [REFERENCE]
    assert pulls_made(rig) == 1
    [refused] = [r for r in await rig.rows("stack_redeployed") if r["result"] == "failure"]
    assert refused["detail"]["reason"] == "credential_choice_required"


async def test_an_administrators_explicit_choice_in_a_redeploy_rebinds_the_image(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    first = await register(rig, "first", environments=(ENV_A,))
    second = await register(rig, "second", environments=(ENV_A,))
    await create_then_deploy(
        rig.services.actions,
        ADMIN,
        ENV_A,
        name="shop",
        compose_text=APP_STACK,
        choices={REFERENCE: CredentialChoice(first)},
    )
    [created] = await binding_rows(rig.db)

    await rig.fresh().actions.redeploy(
        ADMIN, ENV_A, "shop", pull=True, choices={REFERENCE: CredentialChoice(second)}
    )

    [rebound] = await binding_rows(rig.db)
    assert (created["registry_credential_id"], rebound["registry_credential_id"]) == (first, second)
    [related] = await rows_for(rig.db, "image_rebound")
    assert related["detail"]["previous_credential_set_id"] == first


SHOP_STACK = """
services:
  web:
    image: nginx:1
    volumes:
      - data:/data
volumes:
  data: {}
"""
PROJECT_LABEL = "com.docker.compose.project"
FOREIGN_NETWORK = 0x99
EXTERNAL_STACK = """
services:
  web:
    image: nginx:1
    volumes:
      - legacy:/data
    networks:
      - backbone
volumes:
  legacy:
    external: true
networks:
  backbone:
    external: true
"""
BACKBONE_NUMBER = 0xB0


def forget_volume(rig: Rig, name: str) -> None:
    rig.host.volumes.pop(name)
    rig.host.volume_labels.pop(name)
    rig.host.volume_docs.pop(name)


async def test_a_deploy_refuses_project_objects_that_belong_to_something_else(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    volume_key = ResourceKey(ENV_B, "volume", "shop_data")
    await rig.services.actions.create(ADMIN, ENV_B, name="shop", compose_text=SHOP_STACK)
    rig.host.add_volume("shop_data")
    network_id = rig.host.add_network("shop_default", FOREIGN_NETWORK)

    with pytest.raises(PreexistingObjects) as foreign:
        await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    assert foreign.value.objects == ("Volume shop_data", "Network shop_default")
    assert "Volume shop_data already exists and does not belong to this stack." in str(
        foreign.value
    )
    [first] = await rig.rows("stack_deployed")
    assert first["detail"]["reason"] == "preexisting_objects"

    rig.host.networks.pop(network_id)
    rig.host.add_volume("shop_data", labels={PROJECT_LABEL: "shop"})
    await rig.access.controls.create(ADMIN, volume_key, ControlSpec("read"), expected_digest="")
    with pytest.raises(PreexistingObjects) as controlled:
        await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    assert controlled.value.objects == ("Volume shop_data",)
    assert rig.host.boxes == {}

    forget_volume(rig, "shop_data")
    await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    deployed = (await rig.rows("stack_deployed"))[-1]
    assert deployed["result"] == "success"
    assert deployed["detail"]["stale_controls_released"] == 1
    assert "preexisting_after_deploy" not in deployed["detail"]
    assert (
        await rig.db.fetch_one("SELECT 1 FROM resource_control WHERE resource_type = 'volume'")
        is None
    )


async def test_a_deploy_checks_the_reach_of_external_objects_again(stack_rig: Rig) -> None:
    rig = stack_rig
    rig.host.add_volume("legacy")
    network_id = rig.host.add_network("backbone", BACKBONE_NUMBER)
    volume_key = ResourceKey(ENV_A, "volume", "legacy")
    network_key = ResourceKey(ENV_A, "network", network_id)
    for key in (volume_key, network_key):
        await rig.access.controls.create(
            ADMIN, key, ControlSpec("write", owner_user_id=STANDARD.user_id), expected_digest=""
        )
    await create_then_deploy(
        rig.services.actions, STANDARD, ENV_A, name="shop", compose_text=EXTERNAL_STACK
    )
    containers = set(rig.host.boxes)
    for key in (volume_key, network_key):
        await rig.access.controls.delete(
            ADMIN, key, expected_digest=await digest_of(rig.access, key)
        )

    with pytest.raises(StackRejected) as refused:
        await rig.fresh().actions.redeploy(STANDARD, ENV_A, "shop")

    assert refused.value.reason == "restricted"
    assert refused.value.fields == {
        "services.web.volumes[0].source": VOLUME_UNAVAILABLE,
        "services.web.networks.backbone": NETWORK_UNAVAILABLE,
    }
    assert set(rig.host.boxes) == containers
    [failure] = [r for r in await rig.rows("stack_redeployed") if r["result"] == "failure"]
    assert failure["detail"]["reason"] == "restricted"


OLD_COMPOSE = ComposeVersion("v2.20.2", 2, 20, 2)
STALE_VERSION = "v2.0.0"
MISSING_BINARY = ("/nonexistent/docker-compose",)


async def test_the_compose_floor_is_enforced_at_deploy_and_the_installed_version_is_recorded(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await rig.services.actions.create(
        ADMIN, ENV_B, name="shop", compose_text=recorded_stack(record)
    )
    await rig.db.execute(
        "UPDATE environment SET compose_cli_version = ? WHERE id = ?", (STALE_VERSION, ENV_B)
    )
    installed = rig.compose.version
    rig.compose.version = OLD_COMPOSE

    with pytest.raises(ComposeNotReady) as too_old:
        await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    stack = present(await rig.db.fetch_one("SELECT last_operation FROM stack"))
    assert (too_old.value.installed, too_old.value.required) == ("v2.20.2", FLOOR.text)
    assert "v2.20.2" in str(too_old.value)
    assert FLOOR.text in str(too_old.value)
    assert [call["cmd"] for call in read_records(record)] == ["config"]
    assert stack["last_operation"] is None
    assert await environment_version(rig) == STALE_VERSION

    rig.compose.version = installed
    await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    assert await environment_version(rig) == present(installed).text

    rig.compose.use_binary(ComposeBinary(MISSING_BINARY))
    await rig.compose.detect()
    with pytest.raises(ComposeNotReady) as missing:
        await rig.fresh().actions.start(ADMIN, ENV_B, "shop")

    assert missing.value.installed is None
    reasons = [r["detail"]["reason"] for r in await rig.rows() if r["result"] == "failure"]
    assert reasons == ["compose_version_unsupported", "compose_unavailable"]


async def environment_version(rig: Rig) -> str | None:
    row = await rig.db.fetch_one(
        "SELECT compose_cli_version FROM environment WHERE id = ?", (ENV_B,)
    )
    assert row is not None
    version: str | None = row["compose_cli_version"]
    return version


SOURCE_TEXT = (
    "services:\n  web:\n    image: x\n    env_file: [s.env]\n"
    "configs:\n  app:\n    file: app.conf\n"
    "secrets:\n  token:\n    file: token.txt\n"
)
SOURCE_FILES = (
    CompanionFile("app.conf", "plain\n"),
    CompanionFile("token.txt", "tok-line-1\ntok-line-2\n", secret=True),
    CompanionFile("s.env", 'KEY = "quoted value"\n# note\nOTHER=bare\n', secret=True),
)


def test_each_file_source_is_delivered_with_the_content_of_its_file() -> None:
    uses = precheck(SOURCE_TEXT, files=SOURCE_FILES)

    sources = {(item.section, item.key): item for item in delivered_sources(uses, SOURCE_FILES)}

    assert set(sources) == {("configs", "app"), ("secrets", "token")}
    assert sources["configs", "app"].content == "plain\n"
    assert not sources["configs", "app"].secret
    assert sources["secrets", "token"].content == "tok-line-1\ntok-line-2\n"
    assert sources["secrets", "token"].secret


def test_a_secret_file_adds_its_content_lines_and_environment_values_to_the_secret_set() -> None:
    uses = precheck(SOURCE_TEXT, files=SOURCE_FILES)

    values = file_secret_values(SOURCE_FILES, uses)

    assert {"tok-line-1\ntok-line-2\n", "tok-line-1", "tok-line-2"} <= values
    assert {"quoted value", "bare"} <= values
    assert "plain" not in values


def test_a_plain_file_adds_nothing_to_the_secret_set() -> None:
    files = (CompanionFile("app.conf", "plain\n"),)

    assert file_secret_values(files, precheck(SOURCE_TEXT, files=SOURCE_FILES)) == frozenset()


def test_a_secret_file_line_shorter_than_eight_characters_is_not_a_secret_term() -> None:
    files = (CompanionFile("s.env", "A=1\nLONGER_KEY=longer-value\n", secret=True),)
    uses = precheck("services:\n  web:\n    image: x\n    env_file: [s.env]\n", files=files)

    values = file_secret_values(files, uses)

    assert "A=1" not in values
    assert {"A=1\nLONGER_KEY=longer-value\n", "LONGER_KEY=longer-value", "longer-value"} <= values


def test_the_keys_a_secret_env_or_label_file_defines_are_listed() -> None:
    content = "# note\nA=1\nexport B=2\nexport\tC: x\n  D = 4\nE\n"
    files = (
        CompanionFile("s.env", content, secret=True),
        CompanionFile("p.env", "PLAIN=1\n"),
    )
    uses = precheck("services:\n  web:\n    image: x\n    env_file: [s.env, p.env]\n", files=files)

    assert secret_env_keys(files, uses) == {"A", "B", "C", "D", "E"}


def test_hiding_keys_masks_them_in_environment_and_labels_only() -> None:
    services = {
        "web": {
            "environment": {"TOKEN": "t", "KEEP": "k"},
            "labels": ["TOKEN=t", "other=o"],
            "image": "TOKEN",
        }
    }
    project = ResolvedProject("p", services, {}, {}, {}, {}, {"services": services})

    hidden = hide_keys(project, {"TOKEN"})

    expected = {
        "web": {
            "environment": {"TOKEN": REDACTED, "KEEP": "k"},
            "labels": [f"TOKEN={REDACTED}", "other=o"],
            "image": "TOKEN",
        }
    }
    assert hidden.services == expected
    assert hidden.document["services"] == expected
    assert project.services["web"]["environment"]["TOKEN"] == "t"
