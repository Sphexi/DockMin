"""The token pages: a confirmation dialog per row, and the notice after a revoke."""

import re

from httpx import AsyncClient

from tests.auth_support import STANDARD_ID, csrf_form, csrf_headers
from tests.web.environment_support import issue_token
from tests.web.flash_support import flash_texts, land, location

TOKENS_PATH = "/account/tokens"
REVOKED_NOTICE = "Token revoked."
SEE_OTHER = 303
FIRST_TOKEN, SECOND_TOKEN = 1, 2

DIALOG_ID = re.compile(r'<dialog id="(revoke-token-\d+)"')
OPENER = re.compile(r'<a [^>]*href="#(revoke-token-\d+)"[^>]*data-dialog-open="(revoke-token-\d+)"')
DIALOG_ACTION = re.compile(
    r'<dialog id="(revoke-token-\d+)".*?<form [^>]*action="([^"]+)"', re.DOTALL
)


async def two_tokens(client: AsyncClient) -> None:
    await issue_token(client)
    await client.post(TOKENS_PATH, data=csrf_form(client, label="second"))


async def test_each_token_row_has_its_own_confirmation_dialog_that_its_opener_targets(
    standard_client: AsyncClient,
) -> None:
    await two_tokens(standard_client)

    page = await standard_client.get(TOKENS_PATH)

    dialogs = DIALOG_ID.findall(page.text)
    assert sorted(dialogs) == [f"revoke-token-{FIRST_TOKEN}", f"revoke-token-{SECOND_TOKEN}"]
    assert [target for target, _ in OPENER.findall(page.text)] == dialogs
    assert [opened for _, opened in OPENER.findall(page.text)] == dialogs
    assert dict(DIALOG_ACTION.findall(page.text)) == {
        f"revoke-token-{FIRST_TOKEN}": f"{TOKENS_PATH}/{FIRST_TOKEN}/revoke",
        f"revoke-token-{SECOND_TOKEN}": f"{TOKENS_PATH}/{SECOND_TOKEN}/revoke",
    }


async def test_an_administrators_view_of_a_users_tokens_posts_each_dialog_to_that_users_route(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    await two_tokens(standard_client)

    page = await admin_client.get(f"/users/{STANDARD_ID}")

    assert dict(DIALOG_ACTION.findall(page.text)) == {
        f"revoke-token-{FIRST_TOKEN}": f"/users/{STANDARD_ID}/tokens/{FIRST_TOKEN}/revoke",
        f"revoke-token-{SECOND_TOKEN}": f"/users/{STANDARD_ID}/tokens/{SECOND_TOKEN}/revoke",
    }


async def test_a_revoked_token_offers_no_dialog_and_the_revoke_is_announced_on_the_next_page(
    standard_client: AsyncClient,
) -> None:
    await two_tokens(standard_client)

    revoked = await standard_client.post(
        f"{TOKENS_PATH}/{FIRST_TOKEN}/revoke", headers=csrf_headers(standard_client)
    )
    page = await land(standard_client, revoked)

    assert (revoked.status_code, location(revoked)) == (SEE_OTHER, TOKENS_PATH)
    assert flash_texts(page.text) == [REVOKED_NOTICE]
    assert DIALOG_ID.findall(page.text) == [f"revoke-token-{SECOND_TOKEN}"]
