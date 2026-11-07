"""Helpers for the registry page tests: form data, parsing and a scripted connection test."""

import re
from html import unescape
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.db.repositories.registry_credential import RegistryCredentialRepository
from tests.auth_support import csrf_form, read_events
from tests.registry_support import ISSUED_PASSWORD, ScriptedRegistryProbes, issued_token
from tests.web.helpers import parse_tags

OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
HTMX = {"HX-Request": "true"}
LOCATION = re.compile(r"^/registries/(\d+)$")
PROVIDER_KINDS = ("dockerhub", "ghcr", "acr", "gitlab", "ecr", "custom")
EXTRA_FIELDS = frozenset(
    {"organisation", "use_organisation", "instance_url", "project_id", "project_path", "region"}
)
USERNAME = "service-account"
SECRET = "web-secret-marker-3318"
SECRET_ROTATED = "web-rotated-marker-8820"
ADDRESS = "registry.example.com"
OTHER_ADDRESS = "mirror.example.com:5000"
ECR_ADDRESS = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
DERIVED_PASSWORD = f"{ISSUED_PASSWORD}-web"


def registry_form(client: AsyncClient, **fields: str) -> dict[str, str]:
    """Return form data for the registry form carrying the client's CSRF token."""
    return csrf_form(client, **fields)


def custom_form(client: AsyncClient, name: str = "registry", **overrides: str) -> dict[str, str]:
    """An authenticated custom registry, with the checkbox ticked as a browser would send it."""
    fields = {
        "name": name,
        "provider": "custom",
        "address": ADDRESS,
        "authentication_enabled": "on",
        "verify_tls": "on",
        "username": USERNAME,
        "secret": SECRET,
    }
    return registry_form(client, **{**fields, **overrides})


def anonymous_form(client: AsyncClient, name: str = "open", **overrides: str) -> dict[str, str]:
    fields = {"name": name, "provider": "custom", "address": ADDRESS, "verify_tls": "on"}
    return registry_form(client, **{**fields, **overrides})


def ecr_form(client: AsyncClient, name: str = "ecr", **overrides: str) -> dict[str, str]:
    fields = {
        "name": name,
        "provider": "ecr",
        "address": ECR_ADDRESS,
        "authentication_enabled": "on",
        "username": ACCESS_KEY,
        "secret": SECRET,
        "region": "eu-west-2",
    }
    return registry_form(client, **{**fields, **overrides})


def created_id(response: Response) -> int:
    assert response.status_code == SEE_OTHER, response.text
    found = LOCATION.fullmatch(response.headers["location"])
    assert found is not None, response.headers["location"]
    return int(found.group(1))


async def create_set(client: AsyncClient, form: dict[str, str]) -> int:
    return created_id(await client.post("/registries", data=form))


def input_names(html: str) -> set[str]:
    """Return the name of every form control on the page."""
    return {
        tag.attrs["name"]
        for tag in parse_tags(html)
        if tag.name in ("input", "select", "textarea") and "name" in tag.attrs
    }


def control(html: str, name: str) -> dict[str, str] | None:
    """Return the attributes of the first control with this name, None when there is none."""
    found = [t for t in parse_tags(html) if "name" in t.attrs and t.attrs["name"] == name]
    return found[0].attrs if found else None


def visible_text(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", html))


async def provider_forms(client: AsyncClient) -> dict[str, tuple[frozenset[str], bool, bool, bool]]:
    """Map each provider to (extras, address input, TLS options, can be anonymous)."""
    forms: dict[str, tuple[frozenset[str], bool, bool, bool]] = {}
    for kind in PROVIDER_KINDS:
        html = (await client.get("/registries/new", params={"provider": kind})).text
        names = input_names(html)
        auth = control(html, "authentication_enabled")
        forms[kind] = (
            frozenset(names & EXTRA_FIELDS),
            "address" in names,
            "verify_tls" in names,
            auth is not None and auth.get("type") == "checkbox",
        )
    return forms


def script_probes(app: FastAPI, probes: ScriptedRegistryProbes) -> ScriptedRegistryProbes:
    """Replace the application's connection test with a scripted one."""
    app.state.registries.probes = probes
    return probes


async def cache_derived_token(app: FastAPI, credential_id: int) -> None:
    """Store a derived token on a set, as a refresh would, and return nothing."""
    clock = app.state.auth.clock
    async with app.state.database.session() as db:
        repository = RegistryCredentialRepository(db, app.state.vault, clock)
        seen = await repository.get(credential_id)
        assert seen is not None
        stamped = clock()
        await repository.store_derived(
            seen,
            issued_token(password=DERIVED_PASSWORD),
            expires_at=stamped.replace(year=stamped.year + 1),
            refreshed_at=stamped,
        )


async def activity_rows(app: FastAPI) -> list[dict[str, Any]]:
    async with app.state.database.session() as db:
        return await read_events(db)


def leaks(texts: list[str], markers: list[str]) -> list[str]:
    """Return each marker that appears in any text."""
    joined = "\n".join(texts)
    return [marker for marker in markers if marker in joined]


def rendered(response: Response) -> str:
    """Everything a response shows a client: its address, headers and body."""
    headers = "\n".join(f"{name}: {value}" for name, value in response.headers.items())
    return f"{response.request.url}\n{headers}\n{response.text}"
