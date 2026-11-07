"""The Encryption pages: overview, check, confirmation, re-encryption run and who may reach them."""

import asyncio
import re
from contextlib import asynccontextmanager
from html import unescape
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

import app.services.encryption as encryption_module
from app.auth.principal import Principal
from app.crypto.keys import Keyring
from app.crypto.registry import SECRET_FIELDS
from app.crypto.vault import Vault
from app.db.repositories.crypto_state import CryptoStateRepository
from app.services.encryption import CONFIRM_MESSAGE, STALE_COUNTS_MESSAGE
from app.services.restore_gate import REKEY_MESSAGE
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, csrf_form
from tests.crypto.helpers import SECOND_KEY, CredentialRepository, seed_row, tamper
from tests.encryption_support import Steps
from tests.job_support import rows
from tests.support import KEY_MATERIAL
from tests.web.flash_support import land, location
from tests.web.helpers import hidden_inputs
from tests.web.html_tree import parse

OVERVIEW = "/server/encryption"
CHECK = "/server/encryption/check"
CONFIRM = "/server/encryption/rekey"
RUN_PREFIX = "/server/encryption/runs/"
TOKENS = "/account/tokens"
UNKNOWN_RUN = "f" * 32
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
CONFLICT = 409
UNPROCESSABLE = 422
UNAVAILABLE = 503
OLD_VERSION = 1
CURRENT_VERSION = 2
BELOW_CURRENT = 2
SETTLE_POLLS = 1200
SETTLE_PAUSE = 0.01
UNPARSEABLE = "not-a-token"
ADMIN = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")
REGISTRY_COLUMN = "registry_credential.secret_ciphertext"
TLS_COLUMN = "environment_tls_material.client_key_ciphertext"
POLL_ATTRIBUTE = 'hx-trigger="every 2s"'
FINISHED_NOTICE = "After the re-encryption"
HTMX = {"HX-Request": "true"}


@pytest.fixture
async def rotated_app(started_app: FastAPI) -> FastAPI:
    """The started application after a rotation: two credentials are still at the old version."""
    async with started_app.state.database.session() as db:
        old = CredentialRepository(db, started_app.state.vault)
        await old.add("credential-one", "plain-one")
        await old.add("credential-two", "plain-two")
        state = await CryptoStateRepository(db).get()
    assert state is not None
    keyring = Keyring(CURRENT_VERSION, {OLD_VERSION: KEY_MATERIAL, CURRENT_VERSION: SECOND_KEY})
    started_app.state.vault = Vault(keyring, state.instance_id)
    return started_app


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> Steps:
    held = Steps()
    monkeypatch.setattr(encryption_module, "RekeyPass", held)
    return held


class SuspensionSpy:
    """Counts how often the application's scheduler was suspended and released."""

    def __init__(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = 0
        self.exited = 0
        scheduler = app.state.scheduler.scheduler
        original = scheduler.suspend

        @asynccontextmanager
        async def counted() -> Any:
            self.entered += 1
            async with original():
                yield
            self.exited += 1

        monkeypatch.setattr(scheduler, "suspend", counted)


async def finished(app: FastAPI, run_id: str) -> None:
    for _ in range(SETTLE_POLLS):
        if (await app.state.encryption.run(ADMIN, run_id)).phase == "finished":
            return
        await asyncio.sleep(SETTLE_PAUSE)
    raise TimeoutError("the run did not finish")


def run_id_of(response: Response) -> str:
    found = re.fullmatch(rf"{RUN_PREFIX}([0-9a-f]{{32}})", location(response))
    assert found is not None, location(response)
    return found.group(1)


def table_counts(html: str) -> dict[str, list[str]]:
    """Map each column row of the overview table to its per-version counts, in page order."""
    found: dict[str, list[str]] = {}
    for row in parse(html).walk():
        if row.tag != "tr":
            continue
        cells = [c for c in row.children if not isinstance(c, str)]
        if cells and cells[0].tag == "th" and cells[0].attrs.get("scope") == "row":
            found[cells[0].text()] = [c.text() for c in cells[1:]]
    return found


def column_headings(html: str) -> list[str]:
    return [n.text() for n in parse(html).walk() if n.tag == "th" and n.attrs.get("scope") == "col"]


async def stored(app: FastAPI, table_column: str = REGISTRY_COLUMN) -> list[str]:
    table, column = table_column.split(".")
    found = await rows(app, f"SELECT {column} AS value FROM {table} ORDER BY id")
    return [row["value"] for row in found]


async def confirmation_form(client: AsyncClient) -> dict[str, str]:
    page = await client.get(CONFIRM)
    return hidden_inputs(page.text)


async def send(client: AsyncClient, method: str, path: str) -> Response:
    form = csrf_form(client) if method == "POST" else None
    return await client.request(method, path, data=form)


async def rekey_post(client: AsyncClient, **fields: str) -> Response:
    return await client.post(CONFIRM, data=csrf_form(client, **fields))


async def test_the_overview_shows_the_key_and_the_count_of_values_in_each_column_per_version(
    rotated_app: FastAPI, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(OVERVIEW)

    counts = table_counts(response.text)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert rotated_app.state.vault.key_fingerprint() in response.text
    assert f"Version {CURRENT_VERSION} (current)" in column_headings(response.text)
    assert counts[REGISTRY_COLUMN] == ["2", "0"]
    assert [counts[f"{f.table}.{f.column}"] for f in SECRET_FIELDS[1:]] == [["0", "0"]] * (
        len(SECRET_FIELDS) - 1
    )
    assert "Review and re-encrypt" in response.text
    assert "Check stored secrets" in response.text


async def test_the_overview_offers_nothing_to_re_encrypt_while_every_value_is_current(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(OVERVIEW)

    assert "Nothing to re-encrypt." in response.text
    assert "Review and re-encrypt" not in response.text


async def test_the_backups_page_links_to_the_encryption_page(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/backups")

    assert f'href="{OVERVIEW}"' in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", OVERVIEW),
        ("GET", CONFIRM),
        ("GET", f"{RUN_PREFIX}{UNKNOWN_RUN}"),
        ("POST", CHECK),
        ("POST", CONFIRM),
    ],
)
async def test_a_standard_user_is_refused_every_encryption_route_and_an_anonymous_one_signs_in(
    standard_client: AsyncClient, anonymous_client: AsyncClient, method: str, path: str
) -> None:
    standard = await send(standard_client, method, path)
    anonymous = await send(anonymous_client, method, path)

    assert standard.status_code == FORBIDDEN
    assert (anonymous.status_code, location(anonymous).split("?")[0]) == (SEE_OTHER, "/login")


async def test_an_unknown_or_malformed_run_is_not_found(admin_client: AsyncClient) -> None:
    unknown = await admin_client.get(f"{RUN_PREFIX}{UNKNOWN_RUN}")
    malformed = await admin_client.get(f"{RUN_PREFIX}not-a-run")

    assert (unknown.status_code, malformed.status_code) == (NOT_FOUND, NOT_FOUND)


async def test_a_check_polls_while_it_runs_and_stops_polling_and_lists_each_failure_when_it_ends(
    rotated_app: FastAPI, admin_client: AsyncClient, steps: Steps
) -> None:
    async with rotated_app.state.database.session() as db:
        current = CredentialRepository(db, rotated_app.state.vault)
        sound = await current.add("credential-three", "plain-three")
        tls_row = await seed_row(db, "environment_tls_material")
        await db.execute(
            "UPDATE environment_tls_material SET client_key_ciphertext = ? WHERE id = ?",
            (UNPARSEABLE, tls_row),
        )
        corrupt = (await stored(rotated_app))[sound - 1]
        await db.execute(
            "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
            (tamper(corrupt), sound),
        )
    started = await admin_client.post(CHECK, data=csrf_form(admin_client))
    run_id = run_id_of(started)
    await steps.reached.wait()

    page = await admin_client.get(location(started))
    fragment = await admin_client.get(location(started), headers=HTMX)
    steps.proceed.set()
    await finished(rotated_app, run_id)
    done = await admin_client.get(location(started))

    registry_page = rotated_app.url_path_for("registry_detail", credential_id=sound)
    assert started.status_code == SEE_OTHER
    assert POLL_ATTRIBUTE in page.text and "Refresh status" in page.text
    assert 'aria-busy="true"' in page.text and "Checking stored secrets" in page.text
    assert "<html" not in fragment.text and POLL_ATTRIBUTE in fragment.text
    assert "hx-trigger" not in done.text and "Refresh status" not in done.text
    assert "Finished with values that did not open" in done.text
    assert f"{REGISTRY_COLUMN} row {sound}" in done.text
    assert f"{TLS_COLUMN} row {tls_row}" in done.text
    assert f'href="{registry_page}"' in done.text
    assert "Open the environments" in done.text
    assert corrupt not in done.text and UNPARSEABLE not in done.text


async def test_a_check_of_sound_values_ends_complete_and_the_overview_links_the_run(
    rotated_app: FastAPI, admin_client: AsyncClient
) -> None:
    started = await admin_client.post(CHECK, data=csrf_form(admin_client))
    await finished(rotated_app, run_id_of(started))

    run = await admin_client.get(location(started))
    overview = await admin_client.get(OVERVIEW)

    assert "Every stored secret opened." in run.text
    assert f'href="{location(started)}"' in overview.text
    assert "Check of stored secrets" in overview.text


async def test_a_second_check_while_one_runs_answers_conflict_on_the_overview(
    rotated_app: FastAPI, admin_client: AsyncClient, steps: Steps
) -> None:
    await admin_client.post(CHECK, data=csrf_form(admin_client))
    await steps.reached.wait()

    second = await admin_client.post(CHECK, data=csrf_form(admin_client))
    steps.proceed.set()

    assert second.status_code == CONFLICT
    assert "A check or re-encryption is already running." in second.text


async def test_the_confirmation_names_every_column_below_the_current_version_and_both_versions(
    rotated_app: FastAPI, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(CONFIRM)

    form = hidden_inputs(response.text)
    overview = await rotated_app.state.encryption.overview(ADMIN)
    assert response.status_code == 200
    assert REGISTRY_COLUMN in response.text and "2 at version 1" in response.text
    assert TLS_COLUMN not in response.text
    assert f"Re-encrypt {BELOW_CURRENT} stored secrets?" in response.text
    assert f"key version {OLD_VERSION} are rewritten under key version {CURRENT_VERSION}" in (
        unescape(response.text)
    )
    assert "status 503" in response.text
    assert form["expected_counts"] == overview.counts_digest
    assert 'name="confirm_rekey"' in response.text


async def test_the_confirmation_of_a_rekey_with_nothing_to_rewrite_answers_conflict(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(CONFIRM)

    assert response.status_code == CONFLICT
    assert "Every stored secret is already at key version" in response.text


async def test_a_rekey_with_counts_changed_since_the_review_answers_conflict_and_rewrites_nothing(
    rotated_app: FastAPI, admin_client: AsyncClient
) -> None:
    form = await confirmation_form(admin_client)
    async with rotated_app.state.database.session() as db:
        await CredentialRepository(db, rotated_app.state.vault).add("added-later", "plain-later")
    before = await stored(rotated_app)

    response = await rekey_post(admin_client, **form, confirm_rekey="yes")

    assert response.status_code == CONFLICT
    assert STALE_COUNTS_MESSAGE in response.text
    assert await stored(rotated_app) == before
    assert rotated_app.state.restore_gate.phase == "open"


async def test_a_rekey_without_the_confirmation_answers_unprocessable_and_rewrites_nothing(
    rotated_app: FastAPI, admin_client: AsyncClient
) -> None:
    form = await confirmation_form(admin_client)
    before = await stored(rotated_app)

    response = await rekey_post(admin_client, **form)

    assert response.status_code == UNPROCESSABLE
    assert CONFIRM_MESSAGE in response.text
    assert await stored(rotated_app) == before
    assert rotated_app.state.restore_gate.phase == "open"


async def test_a_confirmed_rekey_refuses_changes_while_it_runs_and_then_leaves_the_instance_open(
    rotated_app: FastAPI,
    admin_client: AsyncClient,
    steps: Steps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = SuspensionSpy(rotated_app, monkeypatch)
    form = await confirmation_form(admin_client)

    started = await rekey_post(admin_client, **form, confirm_rekey="yes")
    await steps.reached.wait()
    change = await admin_client.post(TOKENS, data=csrf_form(admin_client, label="held"))
    restore = await admin_client.post("/restore/begin", data=csrf_form(admin_client))
    download = await admin_client.post("/backups/download", data=csrf_form(admin_client))
    running = await admin_client.get(location(started))
    during = (spy.entered, spy.exited)
    steps.proceed.set()
    await finished(rotated_app, run_id_of(started))
    done = await admin_client.get(location(started))
    after = await admin_client.post(TOKENS, data=csrf_form(admin_client, label="released"))

    assert started.status_code == SEE_OTHER
    assert (change.status_code, restore.status_code, download.status_code) == (UNAVAILABLE,) * 3
    assert REKEY_MESSAGE in change.text + restore.text + download.text
    assert [REKEY_MESSAGE in r.text for r in (change, restore, download)] == [True] * 3
    assert running.status_code == 200 and POLL_ATTRIBUTE in running.text
    assert "The instance refuses changes until this finishes." in running.text
    assert during == (1, 0)
    assert (spy.entered, spy.exited) == (1, 1)
    assert after.status_code == 200 and "dkm_" in after.text
    assert "hx-trigger" not in done.text and "Refresh status" not in done.text
    assert "Every stored secret is now sealed under key version 2." in done.text
    assert "DOCKMIN_PREVIOUS_SECRET_KEY_FILE" in done.text and FINISHED_NOTICE in done.text


async def test_a_confirmed_rekey_leaves_no_value_below_the_current_version_and_records_both_rows(
    rotated_app: FastAPI, admin_client: AsyncClient
) -> None:
    form = await confirmation_form(admin_client)
    before = await stored(rotated_app)

    started = await rekey_post(admin_client, **form, confirm_rekey="yes")
    await finished(rotated_app, run_id_of(started))

    async with rotated_app.state.database.session() as db:
        versions = await CryptoStateRepository(db).count_by_version()
    actions = await rows(
        rotated_app,
        "SELECT action, result, actor_user_id FROM activity_event "
        "WHERE action LIKE 'encryption_rekey_%' ORDER BY id",
    )
    landed = await land(admin_client, started)
    assert set(versions) == {CURRENT_VERSION}
    after = await stored(rotated_app)
    assert [value.split(":")[0] for value in after] == [f"v{CURRENT_VERSION}"] * len(before)
    assert after != before
    assert [(a["action"], a["result"], a["actor_user_id"]) for a in actions] == [
        ("encryption_rekey_requested", "success", ADMIN_ID),
        ("encryption_rekey_completed", "success", ADMIN_ID),
    ]
    assert landed.status_code == 200 and "Complete" in landed.text


async def test_a_restore_cannot_begin_while_a_check_runs_and_the_page_says_why(
    rotated_app: FastAPI, admin_client: AsyncClient, steps: Steps
) -> None:
    await admin_client.post(CHECK, data=csrf_form(admin_client))
    await steps.reached.wait()

    response = await admin_client.post(
        "/restore/begin", data=csrf_form(admin_client, confirm_offline="yes")
    )
    steps.proceed.set()

    assert response.status_code == CONFLICT
    assert "A stored-secret check is running" in response.text
    assert rotated_app.state.restore_gate.phase == "open"
