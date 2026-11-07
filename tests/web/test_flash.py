"""The one-shot notice cookie: signing, limits, attributes, binding, expiry and display."""

from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.responses import Response

from app.auth.sessions import session_cookie_name
from app.config import Settings
from app.web.flash import (
    FLASH_COOKIE,
    FLASH_MAX_AGE,
    INSECURE_FLASH_COOKIE,
    MAX_COOKIE_CHARS,
    MAX_FLASH_CHARS,
    MAX_FLASHES,
    Flash,
    clear_flashes,
    flash_cookie_name,
    pending_flashes,
)
from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_SESSION_TOKEN,
    ADMIN_USERNAME,
    STANDARD_SESSION_TOKEN,
    Browsers,
    cookie_set_by,
    csrf_form,
    csrf_headers,
)
from tests.web.flash_support import (
    NO_CONTENT,
    SEE_OTHER,
    Ticker,
    fake_app,
    flash_cookie_of,
    flashes_in,
    issue_flash,
    land,
    location,
    request_with,
)

KEY = b"k" * 32
OTHER_KEY = b"o" * 32
SESSION = "S" * 43
OTHER_SESSION = "T" * 43
HOSTILE = "<script>alert(1)</script>"
EXPIRED = "0"
TWO_BYTES = "é"
HTMX = {"HX-Request": "true"}
SERVER_DOMAIN = "testserver.local"

NOTICE = Flash("ok", "Saved.")
WARNING = Flash("warn", "Check this.")


@pytest.fixture
def clock() -> Ticker:
    return Ticker()


@pytest.fixture
def bench(settings: Settings, clock: Ticker) -> Any:
    return fake_app(settings, KEY, clock)


def read(app: Any, value: str, *, session_token: str = "") -> tuple[Flash, ...]:
    settings: Settings = app.state.settings
    cookies = {flash_cookie_name(settings): value}
    if session_token:
        cookies[session_cookie_name(settings)] = session_token
    return pending_flashes(request_with(app, cookies))


def flip_payload(value: str) -> str:
    payload, signature = value.split(".")
    swapped = "A" if payload[0] != "A" else "B"
    return f"{swapped}{payload[1:]}.{signature}"


def wrong_signature(value: str) -> str:
    return value.split(".")[0] + "." + "0" * 64


def without_separator(value: str) -> str:
    return value.replace(".", "")


def oversize(value: str) -> str:
    return "a" * (MAX_COOKIE_CHARS + 1)


def signed_by_another_key(value: str) -> str:
    return value.split(".")[0] + "." + "f" * 64


@pytest.mark.parametrize(
    "damage",
    [flip_payload, wrong_signature, without_separator, oversize, signed_by_another_key],
    ids=["payload-changed", "signature-wrong", "no-separator", "oversize", "other-key"],
)
def test_a_cookie_that_was_altered_or_oversize_yields_no_notices(bench: Any, damage: Any) -> None:
    _, value = issue_flash(bench, [NOTICE])

    assert read(bench, damage(value)) == ()


@pytest.mark.parametrize("value", ["", "!!!", "a.b", "é.é"], ids=repr)
def test_a_cookie_that_is_not_a_signed_payload_yields_no_notices(bench: Any, value: str) -> None:
    assert read(bench, value) == ()


def test_a_cookie_that_was_read_and_found_invalid_is_expired_on_the_response(bench: Any) -> None:
    settings: Settings = bench.state.settings
    request = request_with(bench, {flash_cookie_name(settings): "garbage"})
    response = Response()

    clear_flashes(request, response)

    assert "Max-Age=0" in response.headers["set-cookie"]


def test_a_response_to_a_request_without_the_cookie_sets_nothing(bench: Any) -> None:
    response = Response()

    clear_flashes(request_with(bench), response)

    assert "set-cookie" not in response.headers


def test_notices_survive_a_round_trip_in_order_with_their_levels(bench: Any) -> None:
    _, value = issue_flash(bench, [NOTICE, WARNING])

    assert read(bench, value) == (NOTICE, WARNING)


def test_only_the_newest_notices_up_to_the_cap_are_kept(bench: Any) -> None:
    flashes = [Flash("info", f"message {number}") for number in range(MAX_FLASHES + 2)]
    _, value = issue_flash(bench, flashes)

    assert read(bench, value) == tuple(flashes[-MAX_FLASHES:])


def test_a_message_is_cut_to_its_cap(bench: Any) -> None:
    _, value = issue_flash(bench, [Flash("ok", "x" * (MAX_FLASH_CHARS + 100))])

    [kept] = read(bench, value)
    assert len(kept.message) == MAX_FLASH_CHARS


def test_the_encoded_value_stays_under_the_cookie_limit_by_dropping_the_oldest_notice(
    bench: Any,
) -> None:
    long = TWO_BYTES * MAX_FLASH_CHARS
    flashes = [Flash("ok", long), Flash("warn", long), Flash("danger", long)]

    _, value = issue_flash(bench, flashes)

    assert len(value) <= MAX_COOKIE_CHARS
    assert read(bench, value) == tuple(flashes[-2:])


def test_the_cookie_carries_the_security_attributes_and_the_host_prefixed_name(
    bench: Any,
) -> None:
    response, _ = issue_flash(bench, [NOTICE])

    jar: SimpleCookie = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    cookie = jar[FLASH_COOKIE]
    assert bool(cookie["httponly"]) and bool(cookie["secure"])
    assert (cookie["samesite"].lower(), cookie["path"], cookie["max-age"]) == (
        "lax",
        "/",
        str(FLASH_MAX_AGE),
    )


def test_without_secure_cookies_the_name_has_no_prefix_and_the_flag_is_absent(
    settings: Settings, clock: Ticker
) -> None:
    insecure = fake_app(settings.model_copy(update={"cookie_secure": False}), KEY, clock)

    response, _ = issue_flash(insecure, [NOTICE])

    jar: SimpleCookie = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    cookie = jar[INSECURE_FLASH_COOKIE]
    assert not cookie["secure"]
    assert bool(cookie["httponly"])
    assert FLASH_COOKIE not in jar


def test_a_notice_is_shown_only_with_the_session_it_was_written_under(bench: Any) -> None:
    _, value = issue_flash(bench, [NOTICE], session_token=SESSION)

    assert read(bench, value, session_token=SESSION) == (NOTICE,)
    assert read(bench, value, session_token=OTHER_SESSION) == ()
    assert read(bench, value) == ()


@pytest.mark.parametrize(
    ("elapsed", "shown"),
    [(0, 1), (FLASH_MAX_AGE, 1), (FLASH_MAX_AGE + 1, 0)],
    ids=["new", "at-the-limit", "past-the-limit"],
)
def test_a_notice_older_than_the_maximum_age_is_ignored(
    bench: Any, clock: Ticker, elapsed: int, shown: int
) -> None:
    _, value = issue_flash(bench, [NOTICE])
    clock.advance(elapsed)

    assert len(read(bench, value)) == shown


def forged(app: FastAPI, client: AsyncClient, flashes: list[Flash], session_token: str) -> None:
    _, value = issue_flash(app, flashes, session_token=session_token)
    client.cookies.set(flash_cookie_name(app.state.settings), value, domain=SERVER_DOMAIN, path="/")


async def test_a_page_shows_a_notice_escaped_and_expires_its_cookie_so_the_next_page_has_none(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    forged(started_app, admin_client, [Flash("warn", HOSTILE)], ADMIN_SESSION_TOKEN)

    first = await admin_client.get("/account")
    second = await admin_client.get("/account")

    assert flashes_in(first.text) == [("warn", HOSTILE)]
    assert HOSTILE not in first.text
    assert "Max-Age=0" in first.headers["set-cookie"]
    assert flashes_in(second.text) == []


async def test_a_notice_written_for_another_session_is_not_shown_to_this_one(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    forged(started_app, admin_client, [NOTICE], STANDARD_SESSION_TOKEN)

    page = await admin_client.get("/account")

    assert flashes_in(page.text) == []


async def test_a_notice_is_ignored_once_the_injected_clock_passes_its_age(
    started_app: FastAPI, admin_client: AsyncClient, flash_clock: Ticker
) -> None:
    forged(started_app, admin_client, [NOTICE], ADMIN_SESSION_TOKEN)
    flash_clock.advance(FLASH_MAX_AGE + 1)

    page = await admin_client.get("/account")

    assert flashes_in(page.text) == []


async def test_only_a_full_page_consumes_a_notice_not_a_fragment_json_or_a_redirect(
    admin_client: AsyncClient,
) -> None:
    created = await admin_client.post("/tags", data=csrf_form(admin_client, name="red"))
    kept_through = {
        "redirect": await admin_client.get("/restore/review"),
        "json": await admin_client.get("/api/health"),
        "fragment": await admin_client.get("/tags", headers=HTMX),
    }
    held_after = flash_cookie_of(admin_client)

    page = await land(admin_client, created)

    assert [r.status_code for r in kept_through.values()] == [SEE_OTHER, 200, 200]
    assert held_after is not None
    assert flashes_in(page.text) == [("ok", "Created the tag red.")]
    assert flash_cookie_of(admin_client) is None


async def test_an_htmx_post_answers_204_with_the_redirect_header_and_the_notice_cookie(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.post(
        "/tags", data={"name": "green"}, headers=HTMX | csrf_headers(admin_client)
    )

    page = await admin_client.get(response.headers["HX-Redirect"])
    assert (response.status_code, response.headers["HX-Redirect"]) == (NO_CONTENT, "/tags")
    assert FLASH_COOKIE in response.headers["set-cookie"]
    assert flashes_in(page.text) == [("ok", "Created the tag green.")]


async def test_signing_in_clears_a_pending_notice_and_so_does_signing_out(
    started_app: FastAPI, browsers: Browsers, admin_client: AsyncClient
) -> None:
    visitor = browsers.open()
    forged(started_app, visitor, [NOTICE], "")
    forged(started_app, admin_client, [NOTICE], ADMIN_SESSION_TOKEN)

    signed_in = await visitor.post(
        "/login", data=csrf_form(visitor, username=ADMIN_USERNAME, password=ADMIN_PASSWORD)
    )
    signed_out = await admin_client.post("/logout", headers=csrf_headers(admin_client))

    assert signed_in.status_code == signed_out.status_code == SEE_OTHER
    assert location(signed_out) == "/login"
    assert cookie_set_by(signed_in, FLASH_COOKIE)["max-age"] == EXPIRED
    assert cookie_set_by(signed_out, FLASH_COOKIE)["max-age"] == EXPIRED
