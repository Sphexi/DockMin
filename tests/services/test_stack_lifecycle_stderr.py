"""Compose stderr on a failed start, stop, restart, remove or volume plan is for administrators."""

import dataclasses

import pytest

from app.services.stack_deploy import StackOperationFailed, StackRejected
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.automation_support import STACK_RESTARTER, authority_context
from tests.stack_support import Rig, create_then_deploy, removal_binding, seed_stack_row

ADMIN_STACK = "admin-shop"
STANDARD_STACK = "user-shop"
EXIT_CODE = 1
DOWN_EXIT_CODE = 4
COMPOSE_FAILED = "compose_failed"
INVALID_COMPOSE = "invalid_compose"
START_TEXT = "dependency failed to start"
STOP_TEXT = "stop failed"
DOWN_TEXT = "down failed"
CONFIG_TEXT = "services.web.image is invalid"


def stack_file(script: str) -> str:
    return f"services:\n  web:\n    image: nginx:1\nx-fake:\n  {script}\n"


async def deploy_both(rig: Rig, script: str) -> None:
    text = stack_file(script)
    await create_then_deploy(
        rig.fresh().actions, STANDARD, ENV_A, name=STANDARD_STACK, compose_text=text
    )
    await create_then_deploy(rig.fresh().actions, ADMIN, ENV_A, name=ADMIN_STACK, compose_text=text)


async def test_a_failed_start_shows_compose_stderr_to_an_administrator_only(
    stack_rig: Rig,
) -> None:
    await deploy_both(stack_rig, "start: fail")
    await stack_rig.fresh().actions.stop(STANDARD, ENV_A, STANDARD_STACK)
    await stack_rig.fresh().actions.stop(ADMIN, ENV_A, ADMIN_STACK)

    with pytest.raises(StackOperationFailed) as by_standard:
        await stack_rig.fresh().actions.start(STANDARD, ENV_A, STANDARD_STACK)
    with pytest.raises(StackOperationFailed) as by_admin:
        await stack_rig.fresh().actions.start(ADMIN, ENV_A, ADMIN_STACK)

    assert (by_standard.value.reason, by_standard.value.exit_code) == (COMPOSE_FAILED, EXIT_CODE)
    assert by_standard.value.stderr == ""
    assert START_TEXT in by_admin.value.stderr


async def test_a_failed_stop_shows_compose_stderr_to_an_administrator_only(
    stack_rig: Rig,
) -> None:
    await deploy_both(stack_rig, "stop: fail")

    with pytest.raises(StackOperationFailed) as by_standard:
        await stack_rig.fresh().actions.stop(STANDARD, ENV_A, STANDARD_STACK)
    with pytest.raises(StackOperationFailed) as by_admin:
        await stack_rig.fresh().actions.stop(ADMIN, ENV_A, ADMIN_STACK)

    assert (by_standard.value.reason, by_standard.value.exit_code) == (COMPOSE_FAILED, EXIT_CODE)
    assert by_standard.value.stderr == ""
    assert STOP_TEXT in by_admin.value.stderr


async def test_a_failed_remove_shows_compose_stderr_to_an_administrator_only(
    stack_rig: Rig,
) -> None:
    await deploy_both(stack_rig, "down: fail")
    standard_binding = await removal_binding(stack_rig, STANDARD, ENV_A, STANDARD_STACK)
    admin_binding = await removal_binding(stack_rig, ADMIN, ENV_A, ADMIN_STACK)

    with pytest.raises(StackOperationFailed) as by_standard:
        await stack_rig.fresh().actions.remove(STANDARD, ENV_A, STANDARD_STACK, **standard_binding)
    with pytest.raises(StackOperationFailed) as by_admin:
        await stack_rig.fresh().actions.remove(ADMIN, ENV_A, ADMIN_STACK, **admin_binding)

    assert (by_standard.value.reason, by_standard.value.exit_code) == (
        COMPOSE_FAILED,
        DOWN_EXIT_CODE,
    )
    assert by_standard.value.stderr == ""
    assert DOWN_TEXT in by_admin.value.stderr


async def test_a_failed_restart_shows_compose_stderr_when_the_rule_owner_is_an_administrator(
    stack_rig: Rig,
) -> None:
    await deploy_both(stack_rig, "start: fail")
    context = await authority_context(
        stack_rig.db,
        stack_rig.clock,
        stack_rig.engine.locks,
        environment_id=ENV_A,
        subsystem=STACK_RESTARTER,
    )
    administrator = context.authority()
    standard = dataclasses.replace(administrator, owner_is_administrator=False)

    with pytest.raises(StackOperationFailed) as by_standard:
        await stack_rig.fresh().actions.restart(standard, ENV_A, STANDARD_STACK)
    with pytest.raises(StackOperationFailed) as by_admin:
        await stack_rig.fresh().actions.restart(administrator, ENV_A, ADMIN_STACK)

    assert (by_standard.value.reason, by_standard.value.exit_code) == (COMPOSE_FAILED, EXIT_CODE)
    assert by_standard.value.stderr == ""
    assert START_TEXT in by_admin.value.stderr


async def test_a_volume_plan_whose_config_fails_shows_compose_stderr_to_an_administrator_only(
    stack_rig: Rig,
) -> None:
    text = stack_file("config: fail")
    await seed_stack_row(stack_rig, ENV_A, STANDARD_STACK, text, owner_user_id=STANDARD.user_id)
    await seed_stack_row(stack_rig, ENV_A, ADMIN_STACK, text, owner_user_id=ADMIN.user_id)

    with pytest.raises(StackRejected) as by_standard:
        await stack_rig.fresh().queries.remove_confirmation(
            STANDARD, ENV_A, STANDARD_STACK, volumes=True
        )
    with pytest.raises(StackRejected) as by_admin:
        await stack_rig.fresh().queries.remove_confirmation(ADMIN, ENV_A, ADMIN_STACK, volumes=True)

    assert by_standard.value.reason == INVALID_COMPOSE
    assert by_standard.value.stderr == ""
    assert CONFIG_TEXT in by_admin.value.stderr
