"""Swarm secrets: the value is write-only and appears nowhere the product keeps or shows."""

import asyncio
import base64
import copy
import json
import logging
from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.services.access import ResourceKey
from app.services.swarm_configs import (
    SecretValue,
    SwarmObjectChangedSinceReview,
    SwarmObjectInUse,
    redisplay,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, PEER, STANDARD, control
from tests.object_support import drain_background, rows_of
from tests.services.conftest import SwarmRig
from tests.swarm_support import SENTINEL

MAX_SECRET_BYTES = 64 * 1024


def form(name: str, **fields: str) -> dict[str, str]:
    return {"name": name, **fields}


def encodings(value: str) -> tuple[str, ...]:
    raw = value.encode()
    return (value, base64.b64encode(raw).decode(), base64.b64encode(raw)[:20].decode())


def holds_a_value(text: str) -> bool:
    return any(encoding in text for encoding in encodings(SENTINEL))


async def test_the_value_reaches_the_engine_once_and_the_row_carries_only_counts(
    swarm: SwarmRig,
) -> None:
    created = await swarm.sw.secret_actions.create(
        ADMIN,
        ENV_A,
        form("db-password", value=SENTINEL, **{"label-0-key": "role", "label-0-value": "db"}),
    )

    [row] = await rows_of(swarm.db, "secret_created")
    [(collection, body)] = swarm.host.create_bodies
    assert collection == "secrets" and base64.b64decode(body["Data"]) == SENTINEL.encode()
    assert row["detail"] == {"name": "db-password", "labels": 1, "control_created": True}
    assert (row["target_type"], row["target_id"]) == ("secret", created.id)


async def test_the_value_is_in_no_row_log_view_database_file_exception_or_repr(
    swarm: SwarmRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("positive-control").info("marker", extra={"probe": SENTINEL})
    assert SENTINEL in "".join(repr(r.__dict__) for r in caplog.records)
    caplog.clear()
    refused: list[BaseException] = []
    request_form = form("db-password", value=SENTINEL)

    created = await swarm.sw.secret_actions.create(ADMIN, ENV_A, request_form)
    attempts = [
        (form("db-password", value=SENTINEL), None),
        (form("x", value=""), None),
        (form("bad name", value=SENTINEL), None),
        (form("y", value=SENTINEL + "z" * (MAX_SECRET_BYTES + 100)), None),
        (form("w", value=SENTINEL), b"also-upload"),
    ]
    for attempt, upload in attempts:
        with pytest.raises(Exception) as raised:
            await swarm.sw.secret_actions.create(ADMIN, ENV_A, attempt, upload=upload)
        refused.append(raised.value)
    swarm.host.create_status = 400
    with pytest.raises(Exception) as engine_refused:
        await swarm.sw.secret_actions.create(ADMIN, ENV_A, form("v", value=SENTINEL))
    refused.append(engine_refused.value)

    detail = await swarm.sw.secrets.detail(ADMIN, ENV_A, created.id)
    page = await swarm.sw.secrets.page(ADMIN, ENV_A)
    every_row = json.dumps(await rows_of(swarm.db), default=str)
    logged = "".join(f"{r.getMessage()}{r.__dict__!r}" for r in caplog.records)
    stored = b"".join(
        p.read_bytes() for p in Path(str(swarm.database.path)).parent.glob("dockmin.db*")
    )
    assert len(refused) == 6
    for error in refused:
        text = repr(error) + str(error) + repr(getattr(error, "fields", "")) + repr(error.__dict__)
        assert not holds_a_value(text)
    assert not holds_a_value(repr(detail) + repr(page) + every_row + logged)
    assert not any(e.encode() in stored for e in encodings(SENTINEL))
    assert str(len(SENTINEL)) not in json.dumps(
        [r["detail"] for r in await rows_of(swarm.db, "secret_created")]
    )


def test_a_refused_form_is_redisplayed_without_the_value() -> None:
    echoed = redisplay("secret", form("db-password", value=SENTINEL))

    assert "value" not in echoed and echoed["name"] == "db-password"


def test_a_held_value_prints_redacted_and_refuses_to_be_copied_or_measured() -> None:
    held = SecretValue(SENTINEL.encode())

    assert SENTINEL not in repr(held) + str(held) + f"{held}"
    assert str(SecretValue(b"x")) == "SecretValue(<redacted>)"
    with pytest.raises(TypeError):
        copy.copy(held)
    with pytest.raises(TypeError):
        len(held)  # type: ignore[arg-type]


async def test_a_daemon_that_returns_secret_data_on_every_read_has_it_dropped(
    swarm: SwarmRig,
) -> None:
    swarm.host.secret_data_visible = True
    secret_id = swarm.host.add_secret("hostile", data=SENTINEL.encode())

    page = await swarm.sw.secrets.page(ADMIN, ENV_A)
    detail = await swarm.sw.secrets.detail(ADMIN, ENV_A, secret_id)

    assert not holds_a_value(repr(page) + repr(detail))


async def test_a_secret_in_use_is_refused_naming_the_service_with_no_force(
    swarm: SwarmRig,
) -> None:
    secret_id = swarm.host.add_secret("sec")
    swarm.host.named_service("web", secrets=[(secret_id, "sec")])

    impact = await swarm.sw.secrets.remove_impact(ADMIN, ENV_A, secret_id)
    with pytest.raises(SwarmObjectChangedSinceReview):
        await swarm.sw.secret_actions.remove(
            ADMIN, ENV_A, secret_id, expected_name="sec", expected_in_use=0
        )
    with pytest.raises(SwarmObjectInUse) as raised:
        await swarm.sw.secret_actions.remove(
            ADMIN, ENV_A, secret_id, expected_name="sec", expected_in_use=1
        )

    reasons = [r["detail"]["reason"] for r in await rows_of(swarm.db, "secret_removed")]
    assert impact.in_use == 1 and [u.name for u in raised.value.users] == ["web"]
    assert reasons == ["changed_since_review", "secret_in_use"]
    assert secret_id in swarm.host.secrets


async def test_removing_a_free_secret_deletes_it_and_releases_the_control(
    swarm: SwarmRig,
) -> None:
    created = await swarm.sw.secret_actions.create(ADMIN, ENV_A, form("sec", value="v"))
    swarm.fresh()

    removed = await swarm.sw.secret_actions.remove(
        ADMIN, ENV_A, created.id, expected_name="sec", expected_in_use=0
    )

    [row] = await rows_of(swarm.db, "secret_removed")
    assert removed.control_released and not swarm.host.secrets
    assert row["detail"] == {"name": "sec", "control_released": True}


async def test_a_standard_user_sees_only_their_secret_and_others_read_as_missing(
    swarm: SwarmRig,
) -> None:
    mine = await swarm.sw.secret_actions.create(STANDARD, ENV_A, form("mine", value="v"))
    other = swarm.host.add_secret("theirs")
    swarm.fresh()

    visible = await swarm.sw.secrets.page(STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound) as hidden:
        await swarm.sw.secrets.detail(STANDARD, ENV_A, other)
    with pytest.raises(ResourceNotFound):
        await swarm.sw.secrets.detail(PEER, ENV_A, mine.id)

    assert [r.name for r in visible.page.items] == ["mine"]
    assert str(hidden.value) == "not found"


async def test_on_a_local_socket_creating_and_removing_a_secret_is_forbidden_to_a_standard_user(
    swarm: SwarmRig,
) -> None:
    secret_id = swarm.host.add_secret("s1")
    await control(swarm.access, ResourceKey(ENV_B, "secret", secret_id), level="write", public=True)
    swarm.fresh()

    with pytest.raises(PermissionDenied):
        await swarm.sw.secret_actions.create(STANDARD, ENV_B, form("s2", value="v"))
    with pytest.raises(PermissionDenied):
        await swarm.sw.secret_actions.remove(
            STANDARD, ENV_B, secret_id, expected_name="s1", expected_in_use=0
        )

    assert swarm.host.create_bodies == []


async def test_a_secret_create_cancelled_after_dispatch_still_writes_one_row_without_the_value(
    swarm: SwarmRig,
) -> None:
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(
        swarm.sw.secret_actions.create(ADMIN, ENV_A, form("late", value=SENTINEL))
    )
    await asyncio.wait_for(swarm.host.arrived.wait(), 2)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    [row] = await rows_of(swarm.db, "secret_created")
    assert row["result"] == "success" and row["detail"]["interrupted"] is True
    assert row["detail"]["control_created"] is True and not holds_a_value(json.dumps(row))


async def test_a_secret_remove_cancelled_after_dispatch_still_releases_the_control(
    swarm: SwarmRig,
) -> None:
    created = await swarm.sw.secret_actions.create(ADMIN, ENV_A, form("late", value="v"))
    swarm.fresh()
    swarm.host.gate = asyncio.Event()
    swarm.host.arrived.clear()
    task = asyncio.create_task(
        swarm.sw.secret_actions.remove(
            ADMIN, ENV_A, created.id, expected_name="late", expected_in_use=0
        )
    )
    await asyncio.wait_for(swarm.host.arrived.wait(), 2)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    [gone] = await rows_of(swarm.db, "secret_removed")
    assert gone["result"] == "success" and gone["detail"]["interrupted"] is True
    assert gone["detail"]["control_released"] is True and not swarm.host.secrets
