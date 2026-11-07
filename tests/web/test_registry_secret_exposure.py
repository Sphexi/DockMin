"""No stored registry secret reaches a response, a log record, an activity row or the database."""

import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.config import ENV_PREFIX
from app.registry_client.auth_config import basic_token, encode_registry_auth
from app.registry_client.probe import RegistryProbeCoordinator, RegistryProbeTimeouts
from app.web import registry_routes
from tests.auth_support import STANDARD_USERNAME, read_events
from tests.registry_client.loopback import (
    LOOPBACK,
    Certificates,
    LoopbackServer,
    Request,
    build_certificates,
    http_reply,
    serve,
)
from tests.registry_support import ScriptedRegistryProbes, issued_token
from tests.support import present
from tests.web.environment_support import LOCAL_NAME, World
from tests.web.registry_support import (
    ACCESS_KEY,
    ADDRESS,
    DERIVED_PASSWORD,
    HTMX,
    OK,
    PROVIDER_KINDS,
    UNPROCESSABLE,
    USERNAME,
    cache_derived_token,
    create_set,
    custom_form,
    ecr_form,
    leaks,
    registry_form,
    rendered,
    script_probes,
)

STATIC_SECRET = "static-secret-marker-5127"
ECR_SECRET = "ecr-secret-marker-6638"
CERTIFICATE_SECRET = "bundle-set-secret-marker-7741"
MINIMUM_GET_ROUTES = 5
FAST = RegistryProbeTimeouts(registry_api=1.0, authentication=1.0, total=5.0)


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | {
        f"{ENV_PREFIX}LOG_LEVEL": "DEBUG",
        f"{ENV_PREFIX}PERMITTED_INTERNAL_NETWORKS": "127.0.0.0/8,::1/128",
    }


@pytest.fixture(scope="module")
def certificates() -> Certificates:
    return build_certificates()


@dataclass(slots=True)
class Sets:
    static: int
    ecr: int
    bundled: int
    plaintexts: list[str]
    ciphertexts: list[str]

    @property
    def markers(self) -> list[str]:
        return [*self.plaintexts, *self.ciphertexts]


def pem_body(pem: str) -> list[str]:
    return [line for line in pem.splitlines() if line and not line.startswith("-----")]


async def stored_ciphertexts(app: FastAPI) -> list[str]:
    async with app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT secret_ciphertext, derived_secret_ciphertext FROM registry_credential",
            (),
            limit=100,
        )
    return [value for row in rows for value in row if value]


@pytest.fixture
async def sets(
    admin_client: AsyncClient, world: World, certificates: Certificates
) -> AsyncIterator[Sets]:
    """A static set, an ECR set holding a cached token, and a set with a CA bundle."""
    app = world.app
    static = await create_set(
        admin_client, custom_form(admin_client, "static", secret=STATIC_SECRET)
    )
    ecr = await create_set(admin_client, ecr_form(admin_client, "ecr", secret=ECR_SECRET))
    bundled = await create_set(
        admin_client,
        custom_form(
            admin_client,
            "bundled",
            address="mirror.example.com",
            secret=CERTIFICATE_SECRET,
            ca_bundle=certificates.ca_pem,
        ),
    )
    await cache_derived_token(app, ecr)
    for credential_id in (static, ecr, bundled):
        await admin_client.post(
            f"/registries/{credential_id}/environments",
            data=registry_form(admin_client, environment_name=LOCAL_NAME),
        )
    await admin_client.post(
        f"/registries/{static}/access",
        data=registry_form(admin_client, grantee_kind="user", grantee_name=STANDARD_USERNAME),
    )
    derived_token = issued_token(password=DERIVED_PASSWORD).get_secret_value()
    auth_header = encode_registry_auth(
        username=USERNAME, password=SecretStr(STATIC_SECRET), server_address=ADDRESS
    ).get_secret_value()
    plaintexts = [
        STATIC_SECRET,
        ECR_SECRET,
        CERTIFICATE_SECRET,
        DERIVED_PASSWORD,
        derived_token,
        basic_token(USERNAME, SecretStr(STATIC_SECRET)).get_secret_value(),
        basic_token(ACCESS_KEY, SecretStr(ECR_SECRET)).get_secret_value(),
        auth_header,
        *pem_body(certificates.ca_pem),
    ]
    yield Sets(static, ecr, bundled, plaintexts, await stored_ciphertexts(app))


def get_paths(sets: Sets) -> list[str]:
    """Every GET path of the registry router, per set and per provider where it takes one."""
    templates = [
        route.path
        for route in registry_routes.router.routes
        if isinstance(route, APIRoute) and "GET" in present(route.methods)
    ]
    paths: list[str] = []
    for template in templates:
        if "{credential_id}" not in template:
            paths.append(template)
            paths.extend(f"{template}?provider={kind}" for kind in PROVIDER_KINDS)
            continue
        for credential_id in (sets.static, sets.ecr, sets.bundled):
            path = template.replace("{credential_id}", str(credential_id))
            paths.append(path)
            paths.extend(f"{path}?provider={kind}" for kind in PROVIDER_KINDS)
    return paths


async def sweep_gets(client: AsyncClient, sets: Sets) -> list[Response]:
    return [await client.get(path) for path in get_paths(sets)]


async def sweep_posts(client: AsyncClient, sets: Sets) -> list[Response]:
    """Post every kind of form with the secrets in the request, refused and accepted alike."""
    responses: list[Response] = []
    for kind in PROVIDER_KINDS:
        data = registry_form(
            client, name="typed", provider=kind, username=USERNAME, secret=STATIC_SECRET
        )
        responses.append(await client.post("/registries/form-fields", data=data))
        responses.append(await client.post("/registries/form-fields", data=data, headers=HTMX))
    refused = custom_form(client, name="", secret=ECR_SECRET)
    responses.append(await client.post("/registries", data=refused))
    for credential_id in (sets.static, sets.ecr, sets.bundled):
        unnamed = custom_form(client, name="", secret=STATIC_SECRET)
        redirected = custom_form(client, address="other.example.com", secret="")
        for edit in (unnamed, redirected):
            responses.append(await client.post(f"/registries/{credential_id}/edit", data=edit))
        responses.append(
            await client.post(
                f"/registries/{credential_id}/delete",
                data=registry_form(client, expected_name="not-its-name"),
            )
        )
    return responses


async def sweep_tests(client: AsyncClient, sets: Sets) -> list[Response]:
    responses: list[Response] = []
    for credential_id in (sets.static, sets.ecr, sets.bundled):
        path = f"/registries/{credential_id}/test"
        responses.append(await client.post(path, data=registry_form(client)))
        responses.append(await client.post(path, data=registry_form(client), headers=HTMX))
    return responses


def echoing_registry(request: Request) -> bytes:
    """Challenge for Basic, then answer every credential with a body that echoes it back."""
    headers = {"Docker-Distribution-API-Version": "registry/2.0"}
    sent = request.header("authorization")
    if sent is None:
        return http_reply(401, b"{}", {**headers, "WWW-Authenticate": 'Basic realm="r"'})
    echo = f"you sent {sent} and {STATIC_SECRET}".encode()
    return http_reply(403, echo, {**headers, "X-Echo": sent})


async def test_no_stored_secret_is_returned_by_any_registry_page_or_post_response(
    admin_client: AsyncClient, sets: Sets
) -> None:
    gets = await sweep_gets(admin_client, sets)
    posts = await sweep_posts(admin_client, sets)

    assert len(gets) >= MINIMUM_GET_ROUTES * len(PROVIDER_KINDS)
    assert {response.status_code for response in gets} == {OK}
    assert {response.status_code for response in posts} == {OK, UNPROCESSABLE}
    assert any(USERNAME in response.text and "Stored." in response.text for response in gets)
    assert leaks([rendered(response) for response in gets + posts], sets.markers) == []


async def test_the_test_action_never_echoes_what_a_hostile_registry_sends_back(
    admin_client: AsyncClient,
    sets: Sets,
    world: World,
    certificates: Certificates,
) -> None:
    server: LoopbackServer = await serve(echoing_registry, identity=certificates.server)
    try:
        async with world.app.state.database.session() as db:
            await db.execute(
                "UPDATE registry_credential SET url = ?, ca_bundle = ? WHERE id = ?",
                (f"{LOOPBACK}:{server.port}", certificates.ca_pem, sets.static),
            )
        probes = RegistryProbeCoordinator(derivers={}, timeouts=FAST)
        world.app.state.registries.probes = probes
        path = f"/registries/{sets.static}/test"

        fragment = await admin_client.post(path, data=registry_form(admin_client), headers=HTMX)
        page = await admin_client.post(path, data=registry_form(admin_client))
        await probes.aclose()
    finally:
        await server.close()

    assert "rejected the credentials" in fragment.text + page.text
    assert leaks([rendered(fragment), rendered(page)], [*sets.markers, "you sent"]) == []
    assert len(server.requests) >= 2


async def test_no_stored_secret_reaches_a_log_record_at_debug_an_activity_row_or_the_database_file(
    admin_client: AsyncClient,
    sets: Sets,
    world: World,
    log_stream: io.StringIO,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    script_probes(world.app, ScriptedRegistryProbes())
    await sweep_gets(admin_client, sets)
    await sweep_posts(admin_client, sets)
    await sweep_tests(admin_client, sets)
    async with world.app.state.database.session() as db:
        events = await read_events(db)
    raw_records = [
        f"{record.getMessage()} {json.dumps(record.__dict__, default=str)}"
        for record in caplog.records
    ]
    path: Path = world.app.state.settings.db_path
    database_files = [
        candidate.read_bytes().decode("latin-1")
        for candidate in (path, path.with_name(path.name + "-wal"))
        if candidate.exists()
    ]

    assert leaks(raw_records, sets.markers) == []
    assert leaks([log_stream.getvalue()], sets.markers) == []
    assert leaks([json.dumps(events)], sets.markers) == []
    assert (
        leaks(database_files, [STATIC_SECRET, ECR_SECRET, CERTIFICATE_SECRET, DERIVED_PASSWORD])
        == []
    )
    assert len(events) >= 3
    assert log_stream.getvalue() != ""
    assert leaks(database_files, sets.ciphertexts) == sets.ciphertexts
