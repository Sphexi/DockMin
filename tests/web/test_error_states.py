"""The copy each error status shows, and that a refusal reveals nothing about what exists."""

import re
from html import unescape
from http import HTTPStatus

import pytest
from httpx import AsyncClient, Response

from app.services.object_inspector import EnvironmentUnavailable
from app.web.problems import UNREACHABLE_NEXT, UNREACHABLE_TITLE, error_copy, unreachable_copy
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse
from tests.web.environment_support import World
from tests.web.error_support import without_reference

HTMX = {"HX-Request": "true"}
UNAVAILABLE = 503
FORBIDDEN = 403
NOT_FOUND = 404
ABSENT_ID = 9999
TITLE = re.compile(r'<h2 class="state__title">([^<]*)</h2>')
PAGE_TITLE = re.compile(r"<h1[^>]*>([^<]*)</h1>")

COPY = [
    (400, "The request could not be read", "Reload the page and try again."),
    (403, "Your role does not allow this", "Ask an administrator if you need it."),
    (
        404,
        "Not found",
        "It does not exist, or you do not have access to it. "
        "Check the address, or go back to the list.",
    ),
    (405, "Not available here", "Use the page's own controls."),
    (408, "The request took too long", "Try again."),
    (409, "Something changed", "Reload the page to see the current state, then try again."),
    (413, "Too large", "Send a smaller file or form."),
    (422, "Check the form", "Correct the fields shown, then submit again."),
    (429, "Too many requests", "Wait a few seconds, then try again."),
    (
        500,
        "Something went wrong",
        "Try again. If it keeps failing, give an administrator the reference below.",
    ),
    (
        502,
        "Docker answered with an error",
        "Try again. If it keeps failing, check the Docker host.",
    ),
    (503, "Not available right now", "Try again shortly."),
]
FALLBACK_MESSAGE = error_copy(500).message


@pytest.mark.parametrize(("status", "title", "message"), COPY, ids=[row[0] for row in COPY])
def test_each_listed_status_has_its_title_and_message(
    status: int, title: str, message: str
) -> None:
    assert tuple(error_copy(status)) == (title, message)


@pytest.mark.parametrize(
    ("status", "title"),
    [(HTTPStatus.IM_A_TEAPOT, "I'm a Teapot"), (599, "Something went wrong")],
    ids=["named-status", "unnamed-status"],
)
def test_an_unlisted_status_keeps_its_phrase_over_the_generic_message(
    status: int, title: str
) -> None:
    assert tuple(error_copy(status)) == (title, FALLBACK_MESSAGE)


@pytest.mark.parametrize("kind", ["unreachable", "unsupported", "credentials", "engine"])
def test_an_unavailable_environment_names_what_is_wrong_and_what_to_check(kind: str) -> None:
    error = EnvironmentUnavailable(kind)  # type: ignore[arg-type]

    copy = unreachable_copy(error)

    assert copy.title == UNREACHABLE_TITLE
    assert copy.message == f"{error} {UNREACHABLE_NEXT}"


def test_any_other_error_is_reported_under_the_unavailable_title_with_its_own_text() -> None:
    copy = unreachable_copy(ValueError("The data could not be read."))

    assert tuple(copy) == (error_copy(UNAVAILABLE).title, "The data could not be read.")


def shows_copy(responses: dict[int, Response]) -> dict[int, tuple[bool, bool]]:
    return {
        status: (
            error_copy(status).title in unescape(response.text),
            error_copy(status).message in unescape(response.text),
        )
        for status, response in responses.items()
    }


async def test_a_page_and_a_fragment_each_carry_the_copy_for_the_status(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    requests = {
        400: (admin_client, "GET", "/registries?cursor=zzz"),
        403: (standard_client, "GET", "/users"),
        404: (admin_client, "GET", f"/environments/{ABSENT_ID}"),
        405: (admin_client, "PUT", "/tags"),
        422: (admin_client, "GET", "/groups/0"),
    }

    pages = {
        status: await client.request(method, path)
        for status, (client, method, path) in requests.items()
    }
    fragments = {
        status: await client.request(method, path, headers=HTMX)
        for status, (client, method, path) in requests.items()
    }

    assert {s: r.status_code for s, r in pages.items()} == {s: s for s in requests}
    assert {s: r.status_code for s, r in fragments.items()} == {s: s for s in requests}
    assert shows_copy(pages) == dict.fromkeys(requests, (True, True))
    assert shows_copy(fragments) == dict.fromkeys(requests, (True, True))
    assert {s: TITLE.search(r.text) is not None for s, r in fragments.items()} == dict.fromkeys(
        requests, True
    )
    assert {s: "<html" in r.text for s, r in fragments.items()} == dict.fromkeys(requests, False)


async def test_a_standard_user_refused_a_role_page_is_told_the_role_is_the_reason(
    standard_client: AsyncClient,
) -> None:
    page = await standard_client.get("/users")

    title, message = error_copy(FORBIDDEN)
    assert page.status_code == FORBIDDEN
    assert title in page.text and message in page.text


async def test_an_environment_the_caller_may_not_see_is_the_same_404_as_one_that_is_missing(
    standard_client: AsyncClient, world: World
) -> None:
    denied = f"/environments/{world.remote_id}"
    missing = f"/environments/{ABSENT_ID}"

    page_denied = await standard_client.get(denied)
    page_missing = await standard_client.get(missing)
    fragment_denied = await standard_client.get(denied, headers=HTMX)
    fragment_missing = await standard_client.get(missing, headers=HTMX)

    assert {r.status_code for r in (page_denied, page_missing)} == {NOT_FOUND}
    assert without_reference(page_denied.text) == without_reference(page_missing.text)
    assert without_reference(fragment_denied.text) == without_reference(fragment_missing.text)
    assert fragment_denied.status_code == fragment_missing.status_code == NOT_FOUND


async def test_a_fragment_request_to_an_unreachable_environment_answers_503_with_the_fragment(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.route("GET", "/containers/json", Refuse())

    response = await admin_client.get(f"/environments/{world.remote_id}/containers", headers=HTMX)

    assert response.status_code == UNAVAILABLE
    assert "<html" not in response.text
    assert TITLE.search(response.text) is not None
    assert "The Docker host could not be reached." in response.text
