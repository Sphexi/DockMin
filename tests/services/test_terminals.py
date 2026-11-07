"""Tests for the terminal service: who may open a terminal, what is recorded, the re-check."""

import asyncio
import json
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from app.auth.principal import Principal
from app.db.repositories.clocked import format_timestamp
from app.services import terminals
from app.services.activity import UserActor
from app.services.assembly import app_container_services
from app.services.container_restrictions import option_label
from app.services.object_locks import LockHolder, ObjectKey
from app.services.terminals import TerminalBridge, TerminalSpec, build_spec
from tests.auth_support import ADMIN_SESSION_TOKEN, STANDARD_SESSION_TOKEN
from tests.container_support import ABSENT, CACHE, DB, JOB, WEB, plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.terminal_engine import Call, TerminalEngine
from tests.environment_support import activity_rows
from tests.terminal_support import (
    ADDED_CAPABILITY,
    ADMIN,
    ADMIN_ACCOUNT,
    BIND_MOUNT,
    CONTAINER,
    DEVICE,
    HOST_PID,
    INTERACTIVE_CONFIG,
    LOCAL_WRITE,
    LONG_AGO,
    PLAIN_CONTAINER,
    PLAIN_MEMBER,
    PRIVILEGED,
    SENTINEL,
    STANDARD,
    STDIN_ONCE,
    STOPPED,
    Account,
    ScriptedPeer,
    add_account,
    attempt,
    rows_of,
    sql,
)
from tests.web.environment_support import World

EXIT_CODE = 3


async def prepare(
    app: FastAPI,
    who: Principal,
    token: str,
    environment_id: int,
    cid: str,
    mode: str = "exec",
    form: dict[str, str] | None = None,
) -> object:
    async with app_container_services(app) as services:
        return await services.terminals.prepare(
            who,
            token,
            environment_id,
            cid,
            mode,  # type: ignore[arg-type]
            form or {},
        )


def standard_prepare(
    app: FastAPI,
    environment_id: int,
    cid: str,
    mode: str = "exec",
    form: dict[str, str] | None = None,
) -> Awaitable[object]:
    return prepare(app, STANDARD, STANDARD_SESSION_TOKEN, environment_id, cid, mode, form)


async def what_the_standard_user_sees(
    app: FastAPI, environment_id: int, ids: list[str]
) -> dict[str, tuple[str, str | None]]:
    """For each container, the outcome of a prepare and the refusal its form page would show."""
    seen: dict[str, tuple[str, str | None]] = {}
    for cid in ids:
        outcome = await attempt(standard_prepare(app, environment_id, cid))
        async with app_container_services(app) as services:
            form = await services.terminals.form_context(STANDARD, environment_id, cid, "exec")
        seen[cid] = (outcome, None if form.refusal is None else form.refusal.code)
    return seen


async def test_a_standard_user_is_refused_a_terminal_in_a_container_using_a_restricted_option(
    world: World, restricted_stack: list[str], shared_host: ContainerEngine, started_app: FastAPI
) -> None:
    seen = await what_the_standard_user_sees(started_app, world.remote_id, restricted_stack)

    assert seen == {
        PRIVILEGED: ("RestrictedContainer", "restricted_container"),
        BIND_MOUNT: ("RestrictedContainer", "restricted_container"),
        HOST_PID: ("RestrictedContainer", "restricted_container"),
        ADDED_CAPABILITY: ("RestrictedContainer", "restricted_container"),
        DEVICE: ("RestrictedContainer", "restricted_container"),
        PLAIN_MEMBER: ("issued", None),
    }
    assert [r for r in shared_host.requests if "/exec" in r.path or "/attach" in r.path] == []
    async with started_app.state.database.session() as db:
        assert await activity_rows(db) == []


async def test_the_refusal_names_each_option_that_caused_it(
    world: World, restricted_stack: list[str], started_app: FastAPI
) -> None:
    async with app_container_services(started_app) as services:
        forms = {
            cid: await services.terminals.form_context(STANDARD, world.remote_id, cid, "exec")
            for cid in (PRIVILEGED, HOST_PID, ADDED_CAPABILITY, DEVICE)
        }

    labels = {cid: form.refusal.options for cid, form in forms.items() if form.refusal}
    assert labels == {
        PRIVILEGED: (option_label("privileged"),),
        HOST_PID: (option_label("pid_namespace"),),
        ADDED_CAPABILITY: (option_label("cap_add"),),
        DEVICE: (option_label("devices"),),
    }


async def test_an_administrator_may_prepare_a_terminal_in_every_one_of_them(
    world: World, restricted_stack: list[str], started_app: FastAPI
) -> None:
    outcomes = {
        cid: await attempt(prepare(started_app, ADMIN, ADMIN_SESSION_TOKEN, world.remote_id, cid))
        for cid in restricted_stack
    }

    assert set(outcomes.values()) == {"issued"}


async def test_a_terminal_is_refused_to_everyone_who_lacks_write_access_without_touching_docker(
    world: World, shared_host: ContainerEngine, started_app: FastAPI
) -> None:
    await plant_control(
        world.app, world.local_id, "container", LOCAL_WRITE, public=True, level="write"
    )
    shared_host.add(LOCAL_WRITE, "local-box")
    cases = {
        "member of the write stack": (world.remote_id, DB, "exec"),
        "read-only grant": (world.remote_id, JOB, "exec"),
        "administrators-only control": (world.remote_id, WEB, "exec"),
        "no grant at all": (world.remote_id, CACHE, "exec"),
        "container that does not exist": (world.remote_id, ABSENT, "exec"),
        "name instead of id": (world.remote_id, "web", "exec"),
        "write grant on the local socket": (world.local_id, LOCAL_WRITE, "exec"),
        "write grant on the local socket, attach": (world.local_id, LOCAL_WRITE, "attach"),
    }

    outcomes = {
        label: await attempt(standard_prepare(started_app, env, cid, mode))
        for label, (env, cid, mode) in cases.items()
    }

    assert outcomes == {
        "member of the write stack": "issued",
        "read-only grant": "PermissionDenied",
        "administrators-only control": "ResourceNotFound",
        "no grant at all": "ResourceNotFound",
        "container that does not exist": "ResourceNotFound",
        "name instead of id": "ResourceNotFound",
        "write grant on the local socket": "PermissionDenied",
        "write grant on the local socket, attach": "PermissionDenied",
    }
    assert [r for r in shared_host.requests if "/exec" in r.path or "/attach" in r.path] == []
    async with started_app.state.database.session() as db:
        assert await activity_rows(db) == []


async def test_only_an_administrator_may_choose_the_user_and_only_a_browser_session_may_ask(
    world: World, shared_host: ContainerEngine, started_app: FastAPI
) -> None:
    token_principal = Principal(ADMIN.user_id, ADMIN.username, "administrator", None)

    outcomes = {
        "standard chooses user": await attempt(
            standard_prepare(started_app, world.remote_id, DB, form={"user": "root"})
        ),
        "administrator chooses user": await attempt(
            prepare(
                started_app, ADMIN, ADMIN_SESSION_TOKEN, world.remote_id, DB, form={"user": "root"}
            )
        ),
        "token principal": await attempt(
            prepare(started_app, token_principal, ADMIN_SESSION_TOKEN, world.remote_id, DB)
        ),
        "no session cookie": await attempt(prepare(started_app, ADMIN, "", world.remote_id, DB)),
        "cookie value that is not ascii": await attempt(
            prepare(started_app, ADMIN, "\u00e9" * 43, world.remote_id, DB)
        ),
        "invalid command": await attempt(
            standard_prepare(started_app, world.remote_id, DB, form={"command": "ls 'x"})
        ),
    }

    assert outcomes == {
        "standard chooses user": "ValidationFailed",
        "administrator chooses user": "issued",
        "token principal": "PermissionDenied",
        "no session cookie": "PermissionDenied",
        "cookie value that is not ascii": "PermissionDenied",
        "invalid command": "ValidationFailed",
    }


@pytest.fixture
def states(terminal_daemon: TerminalEngine) -> TerminalEngine:
    terminal_daemon.script_container(STOPPED, "stopped", state="exited", config=INTERACTIVE_CONFIG)
    terminal_daemon.script_container(
        STDIN_ONCE, "once", config={**INTERACTIVE_CONFIG, "StdinOnce": True}
    )
    return terminal_daemon


async def test_a_terminal_is_refused_for_a_container_in_the_wrong_state_or_with_the_wrong_shape(
    world: World, states: TerminalEngine, started_app: FastAPI
) -> None:
    engine = started_app.state.engine
    key = ObjectKey(world.local_id, "container", CONTAINER)
    held = LockHolder(UserActor(ADMIN.user_id), "someone", "stop")
    cases: dict[str, tuple[str, str, str]] = {
        "stopped": (STOPPED, "exec", "ContainerNotRunning"),
        "stopped, attach": (STOPPED, "attach", "ContainerNotRunning"),
        "plain container, attach": (PLAIN_CONTAINER, "attach", "AttachNotAvailable"),
        "stdin closes on detach, attach": (STDIN_ONCE, "attach", "AttachNotAvailable"),
        "plain container, exec": (PLAIN_CONTAINER, "exec", "issued"),
    }

    outcomes = {
        label: await attempt(
            prepare(started_app, ADMIN, ADMIN_SESSION_TOKEN, world.local_id, cid, mode)
        )
        for label, (cid, mode, _) in cases.items()
    }
    async with engine.locks.hold(key, held):
        busy = await attempt(
            prepare(started_app, ADMIN, ADMIN_SESSION_TOKEN, world.local_id, CONTAINER)
        )

    assert outcomes == {label: expected for label, (_, _, expected) in cases.items()}
    assert busy == "ObjectBusy"


async def test_the_terminal_form_says_what_is_possible_and_why_not(
    world: World, states: TerminalEngine, started_app: FastAPI
) -> None:
    async with app_container_services(started_app) as services:
        interactive = await services.terminals.form_context(
            ADMIN, world.local_id, CONTAINER, "exec"
        )
        plain = await services.terminals.form_context(
            ADMIN, world.local_id, PLAIN_CONTAINER, "attach"
        )
        stopped = await services.terminals.form_context(ADMIN, world.local_id, STOPPED, "exec")

    assert (interactive.refusal, interactive.attach_available, interactive.attach_note) == (
        None,
        True,
        None,
    )
    assert (interactive.name, interactive.state, interactive.is_local, interactive.offers_user) == (
        "web",
        "running",
        True,
        True,
    )
    assert plain.refusal is not None and plain.refusal.code == "attach_unavailable"
    assert not plain.attach_available and plain.attach_note
    assert stopped.refusal is not None and stopped.refusal.code == "not_running"


async def test_a_second_attach_is_refused_as_busy_without_naming_the_holder(
    world: World, states: TerminalEngine, started_app: FastAPI
) -> None:
    engine = started_app.state.engine
    key = ObjectKey(world.local_id, "container", CONTAINER)
    async with engine.attach_locks.hold(key, LockHolder(UserActor(2), "other-person", "attach")):
        with pytest.raises(Exception) as busy:
            await prepare(
                started_app, ADMIN, ADMIN_SESSION_TOKEN, world.local_id, CONTAINER, "attach"
            )
        async with app_container_services(started_app) as services:
            form = await services.terminals.form_context(ADMIN, world.local_id, CONTAINER, "attach")

    assert type(busy.value).__name__ == "AttachBusy"
    assert "other-person" not in str(busy.value)
    assert form.refusal is not None and form.refusal.code == "attach_busy"
    assert "other-person" not in (form.refusal.message + (form.attach_note or ""))


async def test_a_user_with_every_terminal_slot_taken_is_told_how_many_they_hold(
    world: World, states: TerminalEngine, started_app: FastAPI
) -> None:
    slots = started_app.state.engine.stream_slots
    held = [slots.acquire(ADMIN.user_id, kind="terminal") for _ in range(2)]

    with pytest.raises(Exception) as refused:
        await prepare(started_app, ADMIN, ADMIN_SESSION_TOKEN, world.local_id, CONTAINER)

    assert type(refused.value).__name__ == "TooManyStreams"
    assert str(refused.value) == "You have 2 terminals open. Close one, then try again."
    assert slots.open_count == len(held)


def exec_spec(**form: str) -> TerminalSpec:
    return build_spec(form, mode="exec", is_administrator=True)


async def open_exec(
    app: FastAPI, world: World, spec: TerminalSpec, cid: str = CONTAINER
) -> TerminalBridge:
    async with app_container_services(app) as services:
        return await services.terminals.open_exec(
            ADMIN, world.local_id, cid, spec, ADMIN_ACCOUNT.token_hash
        )


async def test_an_exec_session_records_its_start_and_end_with_the_program_never_an_argument(
    world: World, terminal_daemon: TerminalEngine, started_app: FastAPI
) -> None:
    spec = exec_spec(command=f"/bin/sh -c {SENTINEL}-argument", working_dir="/srv", user="root")
    bridge = await open_exec(started_app, world, spec)
    peer = ScriptedPeer()
    peer.say(f"{SENTINEL}-typed\rexit {EXIT_CODE}\r".encode())

    end = await bridge.run(peer)

    started, ended = (
        await rows_of(started_app, "container_exec_started"),
        await rows_of(started_app, "container_exec_ended"),
    )
    created = json.loads(terminal_daemon.calls("POST", f"/containers/{CONTAINER}/exec")[0].body)
    assert (end.reason, end.exit_code) == ("process_exited", EXIT_CODE)
    assert [
        (r["result"], r["target_type"], r["target_id"], r["actor_user_id"]) for r in started + ended
    ] == [
        ("success", "container", CONTAINER, ADMIN.user_id),
        ("success", "container", CONTAINER, ADMIN.user_id),
    ]
    assert started[0]["detail"] == {
        "name": "web",
        "session": bridge.session_id,
        "command": "/bin/sh",
        "argument_count": 2,
        "working_dir_set": True,
        "user": "root",
    }
    assert {k: v for k, v in ended[0]["detail"].items() if k != "duration_seconds"} == {
        "name": "web",
        "session": bridge.session_id,
        "end_reason": "process_exited",
        "exit_code": EXIT_CODE,
    }
    assert isinstance(ended[0]["detail"]["duration_seconds"], int)
    assert SENTINEL not in json.dumps([started, ended])
    assert created["Cmd"] == ["/bin/sh", "-c", f"{SENTINEL}-argument"]
    assert "Privileged" not in created and "Env" not in created
    assert started_app.state.engine.stream_slots.open_count == 0


async def failing_opens(
    app: FastAPI, world: World, daemon: TerminalEngine, script: list[tuple[Call, int | None]]
) -> list[str]:
    reasons: list[str] = []
    for call, status in script:
        daemon.clear_faults()
        if status is None:
            daemon.drop(call)
        else:
            daemon.refuse(call, status)
        reasons.append(await attempt(open_exec(app, world, exec_spec())))
    return reasons


async def test_a_failed_open_records_a_failure_row_with_its_reason_and_leaves_nothing_held(
    world: World, terminal_daemon: TerminalEngine, started_app: FastAPI
) -> None:
    script: list[tuple[Call, int | None]] = [
        (Call.CREATE_EXEC, 409),
        (Call.CREATE_EXEC, 404),
        (Call.START_EXEC, 500),
        (Call.START_EXEC, None),
    ]

    reasons = await failing_opens(started_app, world, terminal_daemon, script)

    failures = await rows_of(started_app, "container_exec_started")
    assert reasons == [
        "ContainerNotRunning",
        "ContainerGone",
        "EngineRefused",
        "EnvironmentUnavailable",
    ]
    assert [(r["result"], r["detail"]["reason"]) for r in failures] == [
        ("failure", "not_running"),
        ("failure", "not_found"),
        ("failure", "engine_error"),
        ("failure", "outcome_unknown"),
    ]
    assert await rows_of(started_app, "container_exec_ended") == []
    assert started_app.state.engine.stream_slots.open_count == 0
    await terminal_daemon.wait_for_no_connections()


async def test_an_open_that_a_standard_user_may_not_make_is_refused_again_with_no_row_and_no_call(
    world: World, shared_host: ContainerEngine, restricted_stack: list[str], started_app: FastAPI
) -> None:
    spec = build_spec({}, mode="exec", is_administrator=False)
    standard = Account(STANDARD, STANDARD_SESSION_TOKEN)

    async def opened(cid: str, with_spec: TerminalSpec) -> str:
        async with app_container_services(started_app) as services:
            return await attempt(
                services.terminals.open_exec(
                    STANDARD, world.remote_id, cid, with_spec, standard.token_hash
                )
            )

    chosen_user = TerminalSpec("exec", ("sh",), None, "root", 24, 80)
    outcomes = [
        await opened(PRIVILEGED, spec),
        await opened(JOB, spec),
        await opened(PLAIN_MEMBER, chosen_user),
    ]

    assert outcomes == ["RestrictedContainer", "PermissionDenied", "ValidationFailed"]
    assert [r for r in shared_host.requests if "/exec" in r.path] == []
    assert await rows_of(started_app, "container_exec_started") == []


async def test_an_attach_records_its_rows_and_a_second_attach_is_busy_until_the_first_ends(
    world: World, terminal_daemon: TerminalEngine, started_app: FastAPI
) -> None:
    spec = build_spec({"rows": "30", "cols": "100"}, mode="attach", is_administrator=True)

    async def attach() -> TerminalBridge:
        async with app_container_services(started_app) as services:
            return await services.terminals.open_attach(
                ADMIN, world.local_id, CONTAINER, spec, ADMIN_ACCOUNT.token_hash
            )

    first = await attach()
    second = await attempt(attach())
    peer = ScriptedPeer()
    peer.hang_up()
    await first.run(peer)
    third = await attach()
    await third.abandon()

    started = await rows_of(started_app, "container_attach_started")
    ended = await rows_of(started_app, "container_attach_ended")
    assert second == "AttachBusy"
    assert [(r["result"], r["detail"].get("reason")) for r in started] == [
        ("success", None),
        ("failure", "attach_busy"),
        ("success", None),
    ]
    assert started[0]["detail"] == {"name": "web", "session": first.session_id}
    assert ended[0]["detail"]["end_reason"] == "client_closed"
    assert "exit_code" not in ended[0]["detail"]
    assert len(started_app.state.engine.attach_locks) == 0
    assert started_app.state.engine.stream_slots.open_count == 0


async def test_a_ticket_opens_exactly_one_session(
    world: World,
    terminal_daemon: TerminalEngine,
    started_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminals, "PROCESS_SETTLE_SECONDS", 0.05)
    async with app_container_services(started_app) as services:
        issued = await services.terminals.prepare(
            ADMIN, ADMIN_SESSION_TOKEN, world.local_id, CONTAINER, "exec", {}
        )
        bridge = await services.terminals.open(
            ADMIN, ADMIN_SESSION_TOKEN, world.local_id, CONTAINER, issued.ticket
        )
        replay = await attempt(
            services.terminals.open(
                ADMIN, ADMIN_SESSION_TOKEN, world.local_id, CONTAINER, issued.ticket
            )
        )
    await bridge.abandon()

    assert replay == "InvalidTicket"
    assert len(terminal_daemon.sessions) == 1


async def cancel_open_at(
    app: FastAPI,
    world: World,
    daemon: TerminalEngine,
    call: Call,
    *,
    attach: bool,
    answered_in_time: bool,
) -> None:
    gate = daemon.hold(call)
    spec = build_spec({}, mode="attach" if attach else "exec", is_administrator=True)

    async def start() -> TerminalBridge:
        async with app_container_services(app) as services:
            opener = services.terminals.open_attach if attach else services.terminals.open_exec
            return await opener(ADMIN, world.local_id, CONTAINER, spec, ADMIN_ACCOUNT.token_hash)

    opening = asyncio.create_task(start())
    await asyncio.wait_for(gate.entered.wait(), 5)
    opening.cancel()
    await asyncio.sleep(0)
    if answered_in_time:
        gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    gate.release.set()


@pytest.mark.parametrize(
    ("call", "attach", "answered", "action", "result", "reason"),
    [
        (Call.CREATE_EXEC, False, True, "container_exec_started", "failure", "interrupted"),
        (Call.CREATE_EXEC, False, False, "container_exec_started", "failure", "outcome_unknown"),
        (Call.START_EXEC, False, True, "container_exec_started", "success", None),
        (Call.START_EXEC, False, False, "container_exec_started", "failure", "outcome_unknown"),
        (Call.ATTACH, True, True, "container_attach_started", "success", None),
    ],
    ids=[
        "create-answered",
        "create-unanswered",
        "start-answered",
        "start-unanswered",
        "attach-answered",
    ],
)
async def test_a_cancelled_open_closes_what_opened_and_leaves_an_interrupted_row_and_no_hold(
    world: World,
    terminal_daemon: TerminalEngine,
    started_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    call: Call,
    attach: bool,
    answered: bool,
    action: str,
    result: str,
    reason: str | None,
) -> None:
    monkeypatch.setattr(terminals, "SETTLE_SECONDS", 0.1)

    await cancel_open_at(
        started_app, world, terminal_daemon, call, attach=attach, answered_in_time=answered
    )

    await terminal_daemon.wait_for_no_connections()
    rows = await rows_of(started_app, action)
    assert [
        (r["result"], r["detail"].get("reason"), r["detail"].get("interrupted")) for r in rows
    ] == [(result, reason, True)]
    assert started_app.state.engine.stream_slots.open_count == 0
    assert len(started_app.state.engine.attach_locks) == 0


async def verdicts(
    app: FastAPI,
    world: World,
    scenarios: dict[str, tuple[Account, bool]],
) -> dict[str, str]:
    results: dict[str, str] = {}
    async with app_container_services(app) as services:
        for label, (account, was_administrator) in scenarios.items():
            try:
                results[label] = await services.terminals.recheck_access(
                    account.token_hash,
                    account.principal.session_id or 0,
                    world.local_id,
                    CONTAINER,
                    was_administrator=was_administrator,
                    touch=False,
                )
            except Exception as error:
                results[label] = type(error).__name__
    return results


async def test_the_recheck_reports_what_became_of_the_session_and_the_users_standing(
    world: World, terminal_daemon: TerminalEngine, started_app: FastAPI
) -> None:
    still_valid = await add_account(started_app, "still-valid")
    signed_out = await add_account(started_app, "signed-out")
    disabled = await add_account(started_app, "disabled")
    demoted = await add_account(started_app, "demoted")
    expired = await add_account(started_app, "expired")
    idle = await add_account(started_app, "idle")
    standard = Account(STANDARD, STANDARD_SESSION_TOKEN)
    await sql(started_app, "DELETE FROM session WHERE id = ?", signed_out.principal.session_id)
    await sql(
        started_app, "UPDATE user SET is_enabled = 0 WHERE id = ?", disabled.principal.user_id
    )
    await sql(
        started_app, "UPDATE user SET role = 'standard' WHERE id = ?", demoted.principal.user_id
    )
    await sql(
        started_app,
        "UPDATE session SET expires_at = ? WHERE id = ?",
        LONG_AGO,
        expired.principal.session_id,
    )
    await sql(
        started_app,
        "UPDATE session SET last_seen_at = ? WHERE id = ?",
        LONG_AGO,
        idle.principal.session_id,
    )

    outcomes = await verdicts(
        started_app,
        world,
        {
            "valid": (still_valid, True),
            "signed out": (signed_out, True),
            "user disabled": (disabled, True),
            "demoted from administrator": (demoted, True),
            "session lifetime over": (expired, True),
            "idle too long": (idle, True),
            "standard user on the local socket": (standard, False),
        },
    )

    assert outcomes == {
        "valid": "permitted",
        "signed out": "access_revoked",
        "user disabled": "access_revoked",
        "demoted from administrator": "access_revoked",
        "session lifetime over": "session_expired",
        "idle too long": "session_expired",
        "standard user on the local socket": "access_revoked",
    }


async def last_seen_after_rechecks(
    app: FastAPI, world: World, account: Account, touches: tuple[bool, ...]
) -> list[str]:
    seen: list[str] = []
    async with app_container_services(app) as services:
        for touch in touches:
            await services.terminals.recheck_access(
                account.token_hash,
                account.principal.session_id or 0,
                world.local_id,
                CONTAINER,
                was_administrator=True,
                touch=touch,
            )
            async with app.state.database.session() as db:
                row = await db.fetch_one(
                    "SELECT last_seen_at FROM session WHERE id = ?", (account.principal.session_id,)
                )
            assert row is not None
            seen.append(str(row[0]))
    return seen


async def test_a_recheck_extends_the_session_only_when_asked_to(
    world: World, terminal_daemon: TerminalEngine, started_app: FastAPI
) -> None:
    account = await add_account(started_app, "watcher")
    stale = format_timestamp(datetime.now(UTC) - timedelta(minutes=10))
    await sql(
        started_app,
        "UPDATE session SET last_seen_at = ? WHERE id = ?",
        stale,
        account.principal.session_id,
    )

    untouched, touched = await last_seen_after_rechecks(started_app, world, account, (False, True))

    assert untouched == stale
    assert touched > stale
