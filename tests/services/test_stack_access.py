"""Stack authorisation: restrictions through the service, container reach and environment gate."""

from collections.abc import Awaitable, Sequence
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.access import ResourceKey
from app.services.stack_deploy import StackRejected
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, control
from tests.stack_support import (
    Rig,
    compose_doc,
    project_box,
    removal_binding,
    seed_stack_row,
    validation_outcomes,
)

SHOP = "shop"
STALE_DIGEST = "0" * 64
RESTRICTED_DOCUMENTS = {
    "privileged": compose_doc(privileged=True),
    "bind": compose_doc(volumes=["/srv/data:/data"]),
    "host_network": compose_doc(network_mode="host"),
    "cap_add": compose_doc(cap_add=["SYS_ADMIN"]),
    "unknown": compose_doc(use_api_socket=True),
}
RESERVED_LABEL = compose_doc(labels={"com.docker.compose.project": "other"})
EXPECTED_PATHS = {
    "privileged": ("services.web.privileged", "privileged"),
    "bind": ("services.web.volumes[0].type", "bind_mount"),
    "host_network": ("services.web.network_mode", "network_host"),
    "cap_add": ("services.web.cap_add", "cap_add"),
    "unknown": ("services.web.use_api_socket", "unknown_option"),
}
FILE_READING_DOCUMENTS = {
    "env_file": compose_doc(env_file=["/run/secrets/x"]),
    "build": compose_doc(build="."),
    "extends": compose_doc(extends={"file": "/srv/other.yaml", "service": "x"}),
    "include": "include:\n  - /etc/other.yaml\nservices:\n  web:\n    image: nginx:1\n",
    "secret_file": (
        "services:\n  web:\n    image: nginx:1\n    secrets: [s]\n"
        "secrets:\n  s:\n    file: ../s.txt\n"
    ),
    "config_file": (
        "services:\n  web:\n    image: nginx:1\n    configs: [c]\n"
        "configs:\n  c:\n    file: /etc/c.txt\n"
    ),
}
VOLUME_FILE = """
services:
  web:
    image: nginx:1
    volumes:
      - data:/var/lib/data
volumes:
  data: {}
"""


def refusal_of(error: Any) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (
        error.reason,
        tuple(error.fields),
        tuple(finding.option for finding in error.findings),
    )


async def raised_by[E: BaseException](calls: Sequence[Awaitable[Any]], error: type[E]) -> list[E]:
    """Await each call and collect the error of the given type that it raises."""
    caught: list[E] = []
    for call in calls:
        with pytest.raises(error) as raised:
            await call
        caught.append(raised.value)
    return caught


async def assert_nothing_written(rig: Rig) -> None:
    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    assert list(rig.compose.files.base.iterdir()) == []
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_standard_user_is_refused_each_restricted_option_at_its_compose_path(
    stack_rig: Rig,
) -> None:
    rig = stack_rig

    outcomes = await validation_outcomes(
        rig.fresh().queries, STANDARD, ENV_A, {**RESTRICTED_DOCUMENTS, "reserved": RESERVED_LABEL}
    )

    assert {
        key: isinstance(error, StackRejected) for key, error in outcomes.items()
    } == dict.fromkeys([*RESTRICTED_DOCUMENTS, "reserved"], True)
    assert {key: refusal_of(outcomes[key]) for key in RESTRICTED_DOCUMENTS} == {
        key: ("restricted", (path,), (option,)) for key, (path, option) in EXPECTED_PATHS.items()
    }
    assert refusal_of(outcomes["reserved"]) == (
        "restricted",
        ("services.web.labels",),
        ("reserved_label",),
    )
    await assert_nothing_written(rig)


async def test_an_administrator_is_accepted_for_those_options_and_told_which_were_used(
    stack_rig: Rig,
) -> None:
    rig = stack_rig

    outcomes = await validation_outcomes(
        rig.fresh().queries, ADMIN, ENV_B, {**RESTRICTED_DOCUMENTS, "reserved": RESERVED_LABEL}
    )

    assert {key: outcomes[key].restricted for key in RESTRICTED_DOCUMENTS} == {
        key: (option,) for key, (_, option) in EXPECTED_PATHS.items()
    }
    assert refusal_of(outcomes["reserved"])[0] == "restricted"


async def test_constructs_that_read_server_files_are_refused_for_everyone_and_nothing_is_written(
    stack_rig: Rig,
) -> None:
    rig = stack_rig

    as_admin = await validation_outcomes(rig.fresh().queries, ADMIN, ENV_B, FILE_READING_DOCUMENTS)
    as_standard = await validation_outcomes(
        rig.fresh().queries, STANDARD, ENV_A, FILE_READING_DOCUMENTS
    )

    assert {key: error.reason for key, error in as_admin.items()} == dict.fromkeys(
        FILE_READING_DOCUMENTS, "refused_construct"
    )
    assert {key: error.fields for key, error in as_standard.items()} == {
        key: error.fields for key, error in as_admin.items()
    }
    assert all(list(error.fields) == ["compose_file"] for error in as_standard.values())
    await assert_nothing_written(rig)


async def test_a_standard_user_cannot_change_a_stack_while_any_container_is_hidden_or_read_only(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_A, "hid", VOLUME_FILE, owner_user_id=STANDARD.user_id)
    await seed_stack_row(rig, ENV_A, "ro", VOLUME_FILE, owner_user_id=STANDARD.user_id)
    project_box(rig.host, 1, "hid")
    secret = project_box(rig.host, 2, "hid")
    project_box(rig.host, 3, "ro")
    read_only = project_box(rig.host, 4, "ro")
    await control(
        rig.access,
        ResourceKey(ENV_A, "container", secret.id),
        level="write",
        administrators_only=True,
    )
    await control(
        rig.access,
        ResourceKey(ENV_A, "container", read_only.id),
        level="read",
        users=(STANDARD.user_id,),
    )
    mutations = rig.host.mutations()

    removals = {name: await removal_binding(rig, STANDARD, ENV_A, name) for name in ("hid", "ro")}
    refusals = await raised_by(
        [
            getattr(rig.fresh().actions, operation)(
                STANDARD, ENV_A, name, **(removals[name] if operation == "remove" else {})
            )
            for name in ("hid", "ro")
            for operation in ("start", "stop", "redeploy", "remove")
        ],
        StackRejected,
    )
    hidden = await rig.fresh().queries.remove_confirmation(STANDARD, ENV_A, "hid")
    visible = await rig.fresh().queries.redeploy_confirmation(STANDARD, ENV_A, "ro")
    administrator = await rig.fresh().queries.remove_confirmation(ADMIN, ENV_A, "hid")

    assert {error.reason for error in refusals} == {"restricted"}
    assert rig.host.mutations() == mutations
    assert (hidden.unwritable_containers, hidden.hidden_containers) == (1, 1)
    assert (visible.unwritable_containers, visible.hidden_containers) == (1, 0)
    assert (administrator.unwritable_containers, administrator.hidden_containers) == (0, 0)
    assert len(rig.engine.locks) == 0


async def test_a_user_without_access_to_the_environment_is_told_nothing_in_it_exists(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_A, SHOP, VOLUME_FILE, owner_user_id=STANDARD.user_id)
    await EnvironmentAccessRepository(rig.db).remove_user("environment", ENV_A, STANDARD.user_id)
    queries = rig.fresh().queries

    await raised_by(
        [
            queries.page(STANDARD, ENV_A),
            queries.detail(STANDARD, ENV_A, SHOP),
            queries.create_context(STANDARD, ENV_A),
            queries.validate(
                STANDARD, ENV_A, name="fresh", compose_text=VOLUME_FILE, variables=None
            ),
            rig.fresh().actions.create(STANDARD, ENV_A, name="fresh", compose_text=VOLUME_FILE),
            rig.fresh().actions.stop(STANDARD, ENV_A, SHOP),
        ],
        ResourceNotFound,
    )

    assert await rig.rows() == []
