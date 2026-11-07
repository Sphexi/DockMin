"""The reveal routes of both stack kinds, the Variables tab around them, and the JSON API."""

import io
from dataclasses import dataclass

import pytest
from httpx import AsyncClient, Response

from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackKind, StackRepository, VariableWrite
from app.services.secret_mask import MASK
from app.services.stack_variable_reveal import StackVariableReveal
from app.web.identifiers import MAX_ID
from app.web.templating import COUNTS_CHANGED_EVENT, TEMPLATES_DIR
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    STANDARD_PASSWORD,
    auth_events,
    csrf_form,
    fetch_scalar,
)
from tests.reveal_support import (
    EXPOSED_SENTINEL,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_NAME,
    SENTINEL_VALUE,
    SHORT_SECRET,
    activity_text,
    add_stack,
)
from tests.web.environment_support import World, bearer, issue_token
from tests.web.flash_support import location
from tests.web.helpers import hx_triggers

OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
METHOD_NOT_ALLOWED = 405
CONFLICT = 409
UNPROCESSABLE = 422
HTMX = {"HX-Request": "true"}
NO_STORE = "no-store"
SHORT_NAME = "SHORT_KEY"
RENAMED = "RENAMED_KEY"
UNKNOWN_ID = 999_999
WRONG_PASSWORD = "not-the-password-at-all"
WINDOW_ACTIONS = ("reveal_window_opened", "stack_variable_revealed")
WINDOW_CLOSED_TEXT = "The reveal window closed. Enter your password again."
INCORRECT = "The current password is incorrect."
UNOPENABLE = "Cannot be opened with the current key"
KINDS = [pytest.param("compose", id="compose"), pytest.param("swarm", id="swarm")]
SHORT_MASK_CELL = f'<code class="mono secret-mask">{MASK}</code>'


@dataclass(frozen=True, slots=True)
class Target:
    kind: StackKind
    environment_id: int
    name: str
    secret_id: int
    short_id: int

    @property
    def base(self) -> str:
        if self.kind == "compose":
            return f"/environments/{self.environment_id}/stacks/{self.name}"
        return f"/environments/{self.environment_id}/swarm/stacks/{self.name}"

    @property
    def variables_tab(self) -> str:
        return f"{self.base}?tab=variables"

    @property
    def row_id(self) -> str:
        return f"variable-row-{self.secret_id}"

    def reveal(self, variable_id: int) -> str:
        return f"{self.base}/variables/{variable_id}/reveal"

    def row(self, variable_id: int) -> str:
        return f"{self.base}/variables/{variable_id}"


type Targets = dict[str, Target]


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | {"DOCKMIN_LOG_LEVEL": "DEBUG"}


@pytest.fixture
async def targets(swarm_stacks_world: World) -> Targets:
    app = swarm_stacks_world.app
    placed: tuple[tuple[StackKind, int, str], ...] = (
        ("compose", swarm_stacks_world.local_id, "blog"),
        ("swarm", swarm_stacks_world.remote_id, "Shop"),
    )
    seeded: Targets = {}
    async with app.state.database.session() as db:
        stacks = StackRepository(db, app.state.engine.vault, app.state.auth.clock)
        controls = ResourceControlRepository(db, app.state.auth.clock)
        for kind, environment_id, name in placed:
            _, ids = await add_stack(
                stacks,
                [
                    VariableWrite(SECRET_NAME, True, SENTINEL_VALUE),
                    VariableWrite(SHORT_NAME, True, SHORT_SECRET),
                    VariableWrite(PLAIN_NAME, False, PLAIN_VALUE),
                ],
                kind=kind,
                name=name,
                environment_id=environment_id,
            )
            await controls.create(
                environment_id,
                "stack",
                name,
                is_public=True,
                is_administrators_only=False,
                access_level="read",
                owner_user_id=None,
            )
            seeded[kind] = Target(kind, environment_id, name, ids[SECRET_NAME], ids[SHORT_NAME])
    return seeded


async def _post(
    client: AsyncClient,
    path: str,
    *,
    name: str = SECRET_NAME,
    password: str | None = None,
    htmx: bool = True,
) -> Response:
    fields = {"expected_name": name} | ({} if password is None else {"password": password})
    return await client.post(path, data=csrf_form(client, **fields), headers=HTMX if htmx else {})


async def _window_rows(world: World) -> list[tuple[str, str]]:
    return [
        (str(e["action"]), str(e["result"]))
        for e in await auth_events(world.app)
        if e["action"] in WINDOW_ACTIONS
    ]


async def _failure_reasons(world: World) -> list[str]:
    return [
        str(e["detail"]["reason"])
        for e in await auth_events(world.app)
        if e["action"] in WINDOW_ACTIONS and e["result"] == "failure"
    ]


async def _reveals_used(world: World) -> int:
    used = await fetch_scalar(
        world.app, "SELECT reveal_count FROM session WHERE user_id = ?", (ADMIN_ID,)
    )
    return int(used)


async def _leaks(world: World, log_stream: io.StringIO, *responses: Response) -> list[str]:
    async with world.app.state.database.session() as db:
        places = {"activity": await activity_text(db), "log": log_stream.getvalue()}
    for number, response in enumerate(responses):
        places[f"headers of response {number}"] = str(sorted(response.headers.items()))
        places[f"url of response {number}"] = str(response.request.url)
    return [place for place, text in places.items() if SENTINEL_VALUE in text]


async def _run(world: World, sql: str, *params: object) -> None:
    async with world.app.state.database.session() as db:
        await db.execute(sql, params)


async def _corrupt(world: World, variable_id: int) -> None:
    await _run(
        world,
        "UPDATE stack_variable SET value_ciphertext = 'corrupt' WHERE id = ?",
        variable_id,
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_the_password_opens_a_window_and_each_reveal_answers_one_row_with_the_value(
    admin_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
    log_stream: io.StringIO,
    kind: str,
) -> None:
    target = targets[kind]

    first = await _post(admin_client, target.reveal(target.secret_id), password=ADMIN_PASSWORD)
    second = await _post(admin_client, target.reveal(target.secret_id))
    hidden = await admin_client.get(target.row(target.secret_id), headers=HTMX)
    tab = await admin_client.get(target.variables_tab)

    assert (first.status_code, second.status_code) == (OK, OK)
    assert first.text.count(SENTINEL_VALUE) == 1 and second.text.count(SENTINEL_VALUE) == 1
    assert f'<tr id="{target.row_id}">' in first.text and "<html" not in first.text
    assert first.text.count("<tr") == 1 and SHORT_NAME not in first.text
    assert "19 reveals left in this window" in first.text
    assert "18 reveals left in this window" in second.text
    assert (first.headers["cache-control"], second.headers["cache-control"]) == (NO_STORE, NO_STORE)
    assert COUNTS_CHANGED_EVENT not in hx_triggers(first) | hx_triggers(second)
    assert EXPOSED_SENTINEL in hidden.text and "data-revealed" not in hidden.text
    assert SENTINEL_VALUE not in hidden.text + tab.text
    assert await _window_rows(swarm_stacks_world) == [
        ("reveal_window_opened", "success"),
        ("stack_variable_revealed", "success"),
        ("stack_variable_revealed", "success"),
    ]
    assert await _leaks(swarm_stacks_world, log_stream, first, second, hidden, tab) == []
    assert log_stream.getvalue()


async def test_without_a_window_the_row_asks_for_the_password_and_a_wrong_one_is_refused(
    admin_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = targets["compose"]
    path = target.reveal(target.secret_id)

    asked = await _post(admin_client, path)
    after_asking = await _window_rows(swarm_stacks_world)
    wrong = await _post(admin_client, path, password=WRONG_PASSWORD)
    after_wrong = await _window_rows(swarm_stacks_world)

    async def always_open(self: StackVariableReveal, actor: object) -> bool:
        return True

    monkeypatch.setattr(StackVariableReveal, "window_open", always_open)
    raced = await _post(admin_client, path)

    assert asked.status_code == OK and f'<tr id="{target.row_id}">' in asked.text
    assert 'name="password"' in asked.text and 'type="password"' in asked.text
    assert "data-revealed" not in asked.text and INCORRECT not in asked.text
    assert after_asking == []
    assert wrong.status_code == UNPROCESSABLE and INCORRECT in wrong.text
    assert after_wrong == [("reveal_window_opened", "failure")]
    assert await _failure_reasons(swarm_stacks_world) == ["bad_current_password"]
    assert raced.status_code == UNPROCESSABLE and WINDOW_CLOSED_TEXT in raced.text
    assert 'name="password"' in raced.text and SENTINEL_VALUE not in raced.text
    assert await _window_rows(swarm_stacks_world) == after_wrong
    assert await _reveals_used(swarm_stacks_world) == 0


async def test_a_refusal_over_htmx_answers_one_row_at_the_refusal_status(
    admin_client: AsyncClient, swarm_stacks_world: World, targets: Targets
) -> None:
    target = targets["compose"]
    await _run(swarm_stacks_world, "DELETE FROM stack_variable WHERE id = ?", target.short_id)

    renamed = await _post(
        admin_client, target.reveal(target.secret_id), name=RENAMED, password=ADMIN_PASSWORD
    )
    removed = await _post(admin_client, target.reveal(target.short_id), name=SHORT_NAME)
    await _corrupt(swarm_stacks_world, target.secret_id)
    unreadable = await _post(admin_client, target.reveal(target.secret_id))

    assert (renamed.status_code, removed.status_code, unreadable.status_code) == (
        CONFLICT,
        NOT_FOUND,
        CONFLICT,
    )
    assert (
        f'<tr id="{target.row_id}">' in renamed.text and "The variable was renamed" in renamed.text
    )
    assert f'<tr id="variable-row-{target.short_id}">' in removed.text
    assert "The variable no longer exists." in removed.text
    assert f'<tr id="{target.row_id}">' in unreadable.text and UNOPENABLE in unreadable.text
    assert "cannot be read with this server" in unreadable.text
    assert not any("<html" in r.text for r in (renamed, removed, unreadable))
    assert await _window_rows(swarm_stacks_world) == [
        ("reveal_window_opened", "success"),
        ("stack_variable_revealed", "failure"),
        ("stack_variable_revealed", "failure"),
    ]
    assert await _failure_reasons(swarm_stacks_world) == [
        "changed_since_review",
        "secrets_unreadable",
    ]
    assert await _reveals_used(swarm_stacks_world) == 0


async def test_without_script_the_answer_is_a_full_page_holding_that_one_variable(
    admin_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
    log_stream: io.StringIO,
) -> None:
    target = targets["compose"]
    path = target.reveal(target.secret_id)

    asked = await _post(admin_client, path, htmx=False)
    revealed = await _post(admin_client, path, password=ADMIN_PASSWORD, htmx=False)
    renamed = await _post(admin_client, path, name=RENAMED, htmx=False)
    row = await admin_client.get(target.row(target.secret_id))
    removed = await _post(admin_client, target.reveal(UNKNOWN_ID), htmx=False)

    assert asked.status_code == OK and "<html" in asked.text and 'name="password"' in asked.text
    assert revealed.status_code == OK and "<html" in revealed.text
    assert revealed.text.count(SENTINEL_VALUE) == 1
    assert SECRET_NAME in revealed.text
    assert SHORT_NAME not in revealed.text and PLAIN_NAME not in revealed.text
    assert f'href="{target.variables_tab}"' in revealed.text
    assert revealed.headers["cache-control"] == NO_STORE
    assert renamed.status_code == CONFLICT and "<html" in renamed.text
    assert "The variable changed" in renamed.text and "<tr" not in renamed.text
    assert (row.status_code, location(row)) == (SEE_OTHER, target.variables_tab)
    assert removed.status_code == NOT_FOUND and "<html" in removed.text
    assert await _leaks(swarm_stacks_world, log_stream, revealed, renamed, row, removed) == []


async def _gate_statuses(
    admin: AsyncClient, standard: AsyncClient, target: Target
) -> dict[str, int]:
    secret = target.secret_id
    unsigned = {"expected_name": SECRET_NAME, "password": ADMIN_PASSWORD}
    return {
        "reveal without a token": (
            await admin.post(target.reveal(secret), data=unsigned)
        ).status_code,
        "reveal by GET": (await admin.get(target.reveal(secret))).status_code,
        "reveal as a standard user": (
            await _post(standard, target.reveal(secret), password=STANDARD_PASSWORD)
        ).status_code,
        "row as a standard user": (await standard.get(target.row(secret))).status_code,
        "reveal of id zero": (await _post(admin, target.reveal(0))).status_code,
        "reveal of id past the range": (await _post(admin, target.reveal(MAX_ID + 1))).status_code,
        "row of id zero": (await admin.get(target.row(0))).status_code,
        "row of id past the range": (await admin.get(target.row(MAX_ID + 1))).status_code,
    }


@pytest.mark.parametrize("kind", KINDS)
async def test_every_route_refuses_a_missing_token_a_get_a_standard_user_and_a_bad_id(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
    kind: str,
) -> None:
    refusals = await _gate_statuses(admin_client, standard_client, targets[kind])

    assert refusals == {
        "reveal without a token": FORBIDDEN,
        "reveal by GET": METHOD_NOT_ALLOWED,
        "reveal as a standard user": NOT_FOUND,
        "row as a standard user": NOT_FOUND,
        "reveal of id zero": UNPROCESSABLE,
        "reveal of id past the range": UNPROCESSABLE,
        "row of id zero": UNPROCESSABLE,
        "row of id past the range": UNPROCESSABLE,
    }
    assert await _window_rows(swarm_stacks_world) == []


async def test_an_api_token_cannot_reach_the_reveal_route(
    admin_client: AsyncClient,
    anonymous_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
) -> None:
    target = targets["compose"]
    token = await issue_token(admin_client)

    refused = await anonymous_client.post(
        target.reveal(target.secret_id),
        data=csrf_form(anonymous_client, expected_name=SECRET_NAME, password=ADMIN_PASSWORD),
        headers=bearer(token),
    )

    assert (refused.status_code, location(refused).startswith("/login")) == (SEE_OTHER, True)
    assert SENTINEL_VALUE not in refused.text
    assert await _window_rows(swarm_stacks_world) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_the_variables_tab_shows_an_administrator_the_partial_mask_and_a_standard_user_none(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    targets: Targets,
    kind: str,
) -> None:
    target = targets[kind]

    admin_page = await admin_client.get(target.variables_tab)
    admin_fragment = await admin_client.get(target.variables_tab, headers=HTMX)
    standard_page = await standard_client.get(target.variables_tab)

    for admin in (admin_page, admin_fragment):
        assert EXPOSED_SENTINEL in admin.text and SHORT_MASK_CELL in admin.text
        assert target.reveal(target.secret_id) in admin.text
        assert target.reveal(target.short_id) in admin.text
        assert PLAIN_VALUE in admin.text
        assert SENTINEL_VALUE not in admin.text and SHORT_SECRET not in admin.text
        assert admin.headers["cache-control"] == NO_STORE
    assert "<html" not in admin_fragment.text
    assert standard_page.text.count(SHORT_MASK_CELL) == 2
    assert EXPOSED_SENTINEL not in standard_page.text and "/reveal" not in standard_page.text
    assert PLAIN_VALUE in standard_page.text
    assert SENTINEL_VALUE not in standard_page.text and SHORT_SECRET not in standard_page.text


@pytest.mark.parametrize("kind", KINDS)
async def test_a_secret_the_key_cannot_open_is_marked_on_the_tab_only_for_an_administrator(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
    kind: str,
) -> None:
    target = targets[kind]
    await _corrupt(swarm_stacks_world, target.secret_id)

    overview = await admin_client.get(target.base)
    admin_tab = await admin_client.get(target.variables_tab)
    row = await admin_client.get(target.row(target.secret_id), headers=HTMX)
    standard_tab = await standard_client.get(target.variables_tab)

    assert overview.status_code == OK and UNOPENABLE not in overview.text
    assert admin_tab.text.count(UNOPENABLE) == 1
    assert target.reveal(target.secret_id) not in admin_tab.text
    assert target.reveal(target.short_id) in admin_tab.text
    assert UNOPENABLE in row.text and "<form" not in row.text
    assert UNOPENABLE not in standard_tab.text
    assert standard_tab.text.count(SHORT_MASK_CELL) == 2


async def test_the_json_api_returns_no_value_for_a_secret_even_when_it_cannot_be_opened(
    admin_client: AsyncClient,
    anonymous_client: AsyncClient,
    swarm_stacks_world: World,
    targets: Targets,
) -> None:
    target = targets["compose"]
    path = f"/api/v1/environments/{target.environment_id}/stacks/{target.name}"
    headers = bearer(await issue_token(admin_client))

    healthy = await anonymous_client.get(path, headers=headers)
    await _corrupt(swarm_stacks_world, target.secret_id)
    unreadable = await anonymous_client.get(path, headers=headers)

    expected = [
        {"name": SECRET_NAME, "secret": True, "value": None},
        {"name": PLAIN_NAME, "secret": False, "value": PLAIN_VALUE},
        {"name": SHORT_NAME, "secret": True, "value": None},
    ]
    assert (healthy.status_code, unreadable.status_code) == (OK, OK)
    assert healthy.json()["variables"] == expected == unreadable.json()["variables"]
    assert SENTINEL_VALUE not in healthy.text and EXPOSED_SENTINEL not in healthy.text


def test_the_base_template_keeps_the_history_cache_disabled() -> None:
    template = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

    assert '"historyCacheSize":0' in template
