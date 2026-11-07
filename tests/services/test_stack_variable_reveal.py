"""Tests for app/services/stack_variable_reveal.py and the variable views the Variables tab uses."""

import hashlib
import pickle
from collections.abc import Awaitable, Iterator

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.stack import StackKind, StackRepository, VariableWrite
from app.db.repositories.user_session import REVEAL_LIMIT, REVEAL_WINDOW, UserSessionRepository
from app.services import activity as activity_module
from app.services.activity import install_environment_change_hook
from app.services.secret_mask import MASK
from app.services.stack_variable_reveal import (
    CHANGED_SINCE_REVIEW,
    REVEAL_WINDOW_CLOSED,
    SECRETS_UNREADABLE,
    RevealedValue,
    RevealRefused,
    StackVariableReveal,
)
from app.services.stack_views import VariableView, variable_views
from tests.access_support import ENV_A, MEMBER_ID, MEMBER_USERNAME
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, FakeClock, read_events
from tests.crypto.helpers import SECOND_KEY, vault_with
from tests.reveal_support import (
    EXPOSED_SENTINEL,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_NAME,
    SENTINEL_VALUE,
    SHORT_SECRET,
    STACK_NAME,
    activity_text,
    add_stack,
    events_named,
    open_session,
    reveal_service,
)
from tests.support import present

ACTION = "stack_variable_revealed"
SHORT_NAME = "SHORT_KEY"
SWARM_STACK = "cluster"
SWARM_SECRET = "SWARM_SECRET"
RENAMED = "RENAMED_KEY"
UNKNOWN_ID = 9999
DETAIL_KEYS = {
    "name",
    "stack_kind",
    "variable",
    "value_version",
    "window_expires_at",
    "reveals_left",
}


@pytest.fixture(autouse=True)
def hook_restored() -> Iterator[None]:
    held = activity_module._environment_change_hook
    yield
    install_environment_change_hook(held)


@pytest.fixture
def stacks(db: Session, runtime: AuthRuntime, vault: Vault) -> StackRepository:
    return StackRepository(db, vault, runtime.clock)


@pytest.fixture
async def ids(stacks: StackRepository) -> dict[str, int]:
    _, compose = await add_stack(
        stacks,
        [
            VariableWrite(SECRET_NAME, True, SENTINEL_VALUE),
            VariableWrite(SHORT_NAME, True, SHORT_SECRET),
            VariableWrite(PLAIN_NAME, False, PLAIN_VALUE),
        ],
    )
    _, swarm = await add_stack(
        stacks,
        [VariableWrite(SWARM_SECRET, True, SENTINEL_VALUE)],
        kind="swarm",
        name=SWARM_STACK,
    )
    return compose | swarm


@pytest.fixture
async def admin(db: Session, runtime: AuthRuntime) -> Principal:
    return await open_session(
        db, runtime, ADMIN_ID, ADMIN_USERNAME, "administrator", token="admin-token"
    )


@pytest.fixture
async def standard(db: Session, runtime: AuthRuntime) -> Principal:
    return await open_session(
        db, runtime, MEMBER_ID, MEMBER_USERNAME, "standard", token="standard-token"
    )


@pytest.fixture
def reveal(db: Session, runtime: AuthRuntime, vault: Vault) -> StackVariableReveal:
    return reveal_service(db, runtime, vault)


async def _open_window(db: Session, runtime: AuthRuntime, actor: Principal) -> None:
    await UserSessionRepository(db, runtime.clock).open_reveal_window(
        present(actor.session_id), runtime.clock() + REVEAL_WINDOW
    )


async def _reveals_used(db: Session, actor: Principal) -> int:
    row = await db.fetch_one(
        "SELECT reveal_count FROM session WHERE id = ?", (present(actor.session_id),)
    )
    return int(present(row)["reveal_count"])


async def _stored_version(db: Session, variable_id: int) -> str:
    row = await db.fetch_one("SELECT value_token FROM stack_variable WHERE id = ?", (variable_id,))
    return str(present(row)["value_token"])


async def _reveal(
    reveal: StackVariableReveal,
    actor: Principal,
    variable_id: int,
    *,
    name: str = SECRET_NAME,
    kind: StackKind = "compose",
    stack: str = STACK_NAME,
) -> object:
    return await reveal.reveal(actor, ENV_A, kind, stack, variable_id, name)


async def _raised(call: Awaitable[object]) -> type[BaseException] | None:
    try:
        await call
    except Exception as error:
        return type(error)
    return None


def _secret_states(views: tuple[VariableView, ...]) -> list[tuple[bool, str | None, bool]]:
    return [(v.readable, v.mask, v.can_reveal) for v in views if v.secret]


def test_a_revealed_value_gives_its_plaintext_only_through_reveal() -> None:
    value = RevealedValue(SENTINEL_VALUE)

    assert value.reveal() == SENTINEL_VALUE
    assert SENTINEL_VALUE not in repr(value)
    assert SENTINEL_VALUE not in f"{value} {value!s} {value!r} {[value]}"
    with pytest.raises(TypeError):
        pickle.dumps(value)


async def test_an_administrators_reveal_returns_that_value_and_records_a_row_without_it(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
    clock: FakeClock,
) -> None:
    await _open_window(db, runtime, admin)
    announced: list[int] = []
    install_environment_change_hook(announced.append)

    revealed = await reveal.reveal(
        admin, ENV_A, "compose", STACK_NAME, ids[SECRET_NAME], SECRET_NAME
    )

    ends = format_timestamp(clock() + REVEAL_WINDOW)
    [event] = await events_named(db, ACTION)
    [environment] = await db.fetch_limited(
        "SELECT environment_id FROM activity_event WHERE action = ?", (ACTION,), limit=2
    )
    recorded = await activity_text(db)
    assert revealed.value.reveal() == SENTINEL_VALUE
    assert (revealed.window_expires_at, revealed.reveals_left) == (ends, REVEAL_LIMIT - 1)
    assert revealed.variable == VariableView(
        ids[SECRET_NAME], SECRET_NAME, True, None, MASK, can_reveal=True
    )
    assert (event["actor_type"], event["actor_user_id"], event["result"]) == (
        "user",
        ADMIN_ID,
        "success",
    )
    assert (event["target_id"], environment["environment_id"]) == (STACK_NAME, ENV_A)
    assert set(event["detail"]) == DETAIL_KEYS
    assert event["detail"] | {"value_version": None} == {
        "name": STACK_NAME,
        "stack_kind": "compose",
        "variable": SECRET_NAME,
        "value_version": None,
        "window_expires_at": ends,
        "reveals_left": REVEAL_LIMIT - 1,
    }
    assert event["detail"]["value_version"] == await _stored_version(db, ids[SECRET_NAME])
    assert SENTINEL_VALUE not in recorded
    assert hashlib.sha256(SENTINEL_VALUE.encode()).hexdigest() not in recorded
    assert announced == []


async def test_each_reveal_of_either_stack_kind_spends_one_and_names_its_kind(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
) -> None:
    await _open_window(db, runtime, admin)

    compose = await reveal.reveal(
        admin, ENV_A, "compose", STACK_NAME, ids[SECRET_NAME], SECRET_NAME
    )
    swarm = await reveal.reveal(admin, ENV_A, "swarm", SWARM_STACK, ids[SWARM_SECRET], SWARM_SECRET)

    events = await events_named(db, ACTION)
    assert (compose.reveals_left, swarm.reveals_left) == (REVEAL_LIMIT - 1, REVEAL_LIMIT - 2)
    assert swarm.value.reveal() == SENTINEL_VALUE
    assert [(e["target_id"], e["detail"]["stack_kind"]) for e in events] == [
        (STACK_NAME, "compose"),
        (SWARM_STACK, "swarm"),
    ]
    assert await _reveals_used(db, admin) == 2


async def test_the_last_reveal_of_a_window_reports_none_left_and_the_next_is_refused_unrecorded(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
) -> None:
    await _open_window(db, runtime, admin)
    await db.execute(
        "UPDATE session SET reveal_count = ? WHERE id = ?",
        (REVEAL_LIMIT - 1, present(admin.session_id)),
    )

    last = await reveal.reveal(admin, ENV_A, "compose", STACK_NAME, ids[SECRET_NAME], SECRET_NAME)
    with pytest.raises(RevealRefused) as refused:
        await _reveal(reveal, admin, ids[SECRET_NAME])

    assert last.reveals_left == 0
    assert refused.value.reason == REVEAL_WINDOW_CLOSED
    assert len(await events_named(db, ACTION)) == 1
    assert await _reveals_used(db, admin) == REVEAL_LIMIT


async def test_a_session_whose_window_never_opened_or_has_ended_is_refused_and_leaves_no_row(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
    clock: FakeClock,
) -> None:
    unopened = await _raised(_reveal(reveal, admin, ids[SECRET_NAME]))
    await _open_window(db, runtime, admin)
    clock.advance(REVEAL_WINDOW)
    expired = await _raised(_reveal(reveal, admin, ids[SECRET_NAME]))

    assert (unopened, expired) == (RevealRefused, RevealRefused)
    assert await read_events(db) == []
    assert await _reveals_used(db, admin) == 0


async def test_a_renamed_variable_is_refused_with_a_failure_row_and_spends_nothing(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
) -> None:
    await _open_window(db, runtime, admin)

    with pytest.raises(RevealRefused) as refused:
        await _reveal(reveal, admin, ids[SECRET_NAME], name=RENAMED)

    [event] = await events_named(db, ACTION)
    assert refused.value.reason == CHANGED_SINCE_REVIEW
    assert (event["result"], event["detail"]["reason"]) == ("failure", CHANGED_SINCE_REVIEW)
    assert event["detail"]["variable"] == SECRET_NAME
    assert SENTINEL_VALUE not in await activity_text(db)
    assert await _reveals_used(db, admin) == 0


async def test_a_variable_that_is_gone_plain_or_in_another_stack_is_not_found_and_spends_nothing(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
) -> None:
    await _open_window(db, runtime, admin)

    outcomes = [
        await _raised(_reveal(reveal, admin, UNKNOWN_ID)),
        await _raised(_reveal(reveal, admin, ids[PLAIN_NAME], name=PLAIN_NAME)),
        await _raised(_reveal(reveal, admin, ids[SWARM_SECRET], name=SWARM_SECRET)),
        await _raised(_reveal(reveal, admin, ids[SECRET_NAME], stack="missing")),
        await _raised(_reveal(reveal, admin, ids[SECRET_NAME], kind="swarm")),
    ]

    assert outcomes == [ResourceNotFound] * len(outcomes)
    assert await read_events(db) == []
    assert await _reveals_used(db, admin) == 0


async def test_a_value_the_key_cannot_open_is_refused_with_a_failure_row_and_spends_nothing(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    ids: dict[str, int],
) -> None:
    await _open_window(db, runtime, admin)
    wrong_key = reveal_service(db, runtime, vault_with(SECOND_KEY))

    with pytest.raises(RevealRefused) as refused:
        await _reveal(wrong_key, admin, ids[SECRET_NAME])
    after = await reveal.reveal(admin, ENV_A, "compose", STACK_NAME, ids[SECRET_NAME], SECRET_NAME)

    failure = (await events_named(db, ACTION))[0]
    assert refused.value.reason == SECRETS_UNREADABLE
    assert (failure["result"], failure["detail"]["reason"]) == ("failure", SECRETS_UNREADABLE)
    assert (failure["detail"]["name"], failure["detail"]["variable"]) == (STACK_NAME, SECRET_NAME)
    assert after.reveals_left == REVEAL_LIMIT - 1


async def test_a_standard_user_and_a_token_cannot_reveal_open_a_view_or_ask_about_a_window(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    standard: Principal,
    ids: dict[str, int],
) -> None:
    token = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")
    await _open_window(db, runtime, standard)

    outcomes = [
        await _raised(_reveal(reveal, standard, ids[SECRET_NAME])),
        await _raised(_reveal(reveal, token, ids[SECRET_NAME])),
        await _raised(reveal.window_open(standard)),
        await _raised(
            reveal.variable_view(standard, ENV_A, "compose", STACK_NAME, ids[SECRET_NAME])
        ),
    ]

    assert outcomes == [PermissionDenied] * len(outcomes)
    assert await reveal.window_open(token) is False
    assert await read_events(db) == []


async def test_a_window_is_reported_open_without_being_spent_and_closed_once_it_ends(
    db: Session,
    runtime: AuthRuntime,
    reveal: StackVariableReveal,
    admin: Principal,
    clock: FakeClock,
) -> None:
    before = await reveal.window_open(admin)
    await _open_window(db, runtime, admin)
    during = [await reveal.window_open(admin), await reveal.window_open(admin)]
    used = await _reveals_used(db, admin)
    clock.advance(REVEAL_WINDOW)

    assert (before, during, used) == (False, [True, True], 0)
    assert await reveal.window_open(admin) is False


async def test_the_single_variable_view_carries_the_partial_mask_and_refuses_a_plain_variable(
    reveal: StackVariableReveal, admin: Principal, ids: dict[str, int]
) -> None:
    view = await reveal.variable_view(admin, ENV_A, "compose", STACK_NAME, ids[SECRET_NAME])

    assert view == VariableView(
        ids[SECRET_NAME], SECRET_NAME, True, None, EXPOSED_SENTINEL, can_reveal=True
    )
    with pytest.raises(ResourceNotFound):
        await reveal.variable_view(admin, ENV_A, "compose", STACK_NAME, ids[PLAIN_NAME])


async def test_each_viewer_sees_the_mask_their_role_allows_and_only_a_session_can_reveal(
    stacks: StackRepository, admin: Principal, standard: Principal, ids: dict[str, int]
) -> None:
    stack = present(await stacks.get(ENV_A, STACK_NAME))
    token = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")

    def shown(views: tuple[VariableView, ...]) -> dict[str, tuple[str | None, bool]]:
        return {v.name: (v.mask or v.value, v.can_reveal) for v in views}

    opened = await variable_views(stacks, stack.id, admin, open_masks=True)
    closed = await variable_views(stacks, stack.id, admin, open_masks=False)
    by_token = await variable_views(stacks, stack.id, token, open_masks=True)
    by_standard = await variable_views(stacks, stack.id, standard, open_masks=True)

    assert shown(opened) == {
        SECRET_NAME: (EXPOSED_SENTINEL, True),
        SHORT_NAME: (MASK, True),
        PLAIN_NAME: (PLAIN_VALUE, False),
    }
    assert shown(closed) == shown(opened) | {SECRET_NAME: (MASK, True)}
    assert shown(by_token) == {
        SECRET_NAME: (EXPOSED_SENTINEL, False),
        SHORT_NAME: (MASK, False),
        PLAIN_NAME: (PLAIN_VALUE, False),
    }
    assert shown(by_standard) == {
        SECRET_NAME: (MASK, False),
        SHORT_NAME: (MASK, False),
        PLAIN_NAME: (PLAIN_VALUE, False),
    }
    assert all(v.value is None for v in (*opened, *by_standard) if v.secret)


async def test_a_secret_the_key_cannot_open_is_unreadable_to_an_administrator_only(
    db: Session,
    runtime: AuthRuntime,
    admin: Principal,
    standard: Principal,
    ids: dict[str, int],
) -> None:
    wrong_key = StackRepository(db, vault_with(SECOND_KEY), runtime.clock)
    stack = present(await wrong_key.get(ENV_A, STACK_NAME))

    by_admin = await variable_views(wrong_key, stack.id, admin, open_masks=True)
    by_standard = await variable_views(wrong_key, stack.id, standard, open_masks=True)

    assert _secret_states(by_admin) == [(False, MASK, False)] * 2
    assert _secret_states(by_standard) == [(True, MASK, False)] * 2
