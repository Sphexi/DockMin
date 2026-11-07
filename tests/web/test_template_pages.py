"""Browsing app templates and creating containers and stacks from them, by role."""

import json
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.db.engine import Database
from app.services.activity import ActivityRecorder
from app.services.template_catalogues import CatalogueInput, TemplateCatalogueService
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import ADMIN
from tests.stack_support import (  # noqa: F401
    job_id_of,
    settle_stack_job,
    stopped_stack_jobs,
)
from tests.template_support import (
    PORTAINER_URL,
    ScriptedFetcher,
    container,
    linuxserver_document,
    linuxserver_image,
    portainer_document,
)
from tests.web.environment_support import World
from tests.web.helpers import (
    checked_values,
    form_values,
    inline_code_in,
    inputs_named,
    links_named,
    value_of,
)

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND = 200, 303, 400, 403, 404
UNPROCESSABLE = 422
HTMX = {"HX-Request": "true"}
HEIMDALL_IMAGE = "lscr.io/linuxserver/heimdall:latest"
VPNBOX_IMAGE = "lscr.io/linuxserver/vpnbox:latest"
UNAVAILABLE = "That template is not available to you, so the form is blank."
SCRIPT = "<script>alert(1)</script>"
MARKUP_NOTE = "<img src=x onerror=alert(1)>"
RICH_DESCRIPTION = "<i>Fast</i> <em>proxy</em> &amp; more"
RICH_NOTE = (
    "<strong>Template created by</strong><br>"
    '<a href="https://example.test/docs" onclick="steal()">docs</a><script>alert(1)</script>'
)
RICH_NOTE_HTML = (
    "<strong>Template created by</strong><br>"
    '<a href="https://example.test/docs" target="_blank" rel="noopener noreferrer nofollow">'
    "docs</a>"
)
RICH_LIST_TEXT = "Fast proxy &amp; more"


def heimdall() -> dict[str, Any]:
    return linuxserver_image(
        "heimdall",
        env_vars=[
            {"name": "PUID", "value": "1000", "optional": False},
            {"name": "VERSION", "value": "docker", "optional": True},
        ],
        volumes=[
            {"path": "/config", "host_path": "/path/to/config", "optional": False},
            {"path": "/library", "host_path": "/path/to/library", "optional": True},
        ],
        ports=[
            {"external": "8081", "internal": "80", "optional": False},
            {"external": "8443", "internal": "443", "optional": True},
        ],
    )


def vpnbox() -> dict[str, Any]:
    return linuxserver_image(
        "vpnbox",
        env_vars=[{"name": "PUID", "value": "1000", "optional": False}],
        volumes=[{"path": "/config", "optional": False}],
        ports=[{"external": "51820", "internal": "51820/udp", "optional": False}],
        caps=[{"cap_add": "NET_ADMIN", "optional": False}],
        devices=[{"path": "/dev/net/tun", "host_path": "/dev/net/tun", "optional": False}],
        custom=[{"name": "sysctl", "value": ["net.ipv4.ip_forward=1"], "optional": False}],
        privileged=True,
    )


async def seed(app: FastAPI, name: str, kind: str, document: bytes, *, enabled: bool = True) -> int:
    clock = app.state.auth.clock
    database: Database = app.state.database
    async with database.session() as db:
        service = TemplateCatalogueService(
            db, clock, ActivityRecorder(db, clock), fetcher=ScriptedFetcher(document)
        )
        catalogue_id = await service.create(ADMIN, CatalogueInput(name, kind, PORTAINER_URL))
        await service.enable(ADMIN, catalogue_id)
        if not enabled:
            await service.disable(ADMIN, catalogue_id)
    return catalogue_id


async def audited_detail(app: FastAPI, action: str) -> dict[str, Any]:
    async with app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT detail FROM activity_event WHERE action = ?", (action,), limit=5
        )
    assert len(rows) == 1
    detail: dict[str, Any] = json.loads(rows[0]["detail"])
    return detail


@pytest.fixture
def daemon(container_host: ContainerEngine) -> ContainerEngine:
    container_host.add_image(HEIMDALL_IMAGE)
    container_host.add_image(VPNBOX_IMAGE, image_id="sha256:" + "b" * 64)
    return container_host


@pytest.fixture
async def catalogue(world: World, daemon: ContainerEngine) -> int:
    return await seed(
        world.app,
        "linuxserver",
        "linuxserver",
        linuxserver_document(heimdall(), vpnbox()),
    )


def container_new(environment_id: int, ref: str) -> str:
    return f"/environments/{environment_id}/containers/new?template={ref}"


def stack_new(environment_id: int, ref: str) -> str:
    return f"/environments/{environment_id}/stacks/-/new?template={ref}"


def compose_of(html: str) -> dict[str, Any]:
    found = re.search(r'<textarea[^>]*name="compose_file"[^>]*>(.*?)</textarea>', html, re.S)
    assert found is not None
    parsed: dict[str, Any] = yaml.safe_load(unescape(found.group(1)))
    return parsed


def row_values(html: str, prefix: str, anchor: str) -> list[dict[str, str]]:
    """Return the filled rows of a repeated group; a row is filled when its `anchor` field is."""
    values = form_values(html)
    indices = sorted({int(name.split("-")[1]) for name in values if name.startswith(f"{prefix}-")})
    rows = [
        {
            name.split("-", 2)[2]: value
            for name, value in values.items()
            if name.startswith(f"{prefix}-{index}-")
        }
        for index in indices
    ]
    return [row for row in rows if row.get(anchor)]


def targets_in(html: str, ref: str) -> dict[str, set[int]]:
    """Return the environments whose create links carry exactly this template, by kind."""
    links = re.findall(r'href="(/environments/(\d+)/(?:containers|stacks)/[^"]*)"', html)
    return {
        kind: {
            int(environment)
            for href, environment in links
            if f"/{kind}/" in href and parse_qs(urlsplit(href).query)["template"] == [ref]
        }
        for kind in ("containers", "stacks")
    }


def titles_in(response: Response) -> list[str]:
    return re.findall(r'<th scope="row">\s*<a href="/templates/[^"]+">([^<]+)</a>', response.text)


async def test_every_signed_in_role_sees_the_templates_and_the_sidebar_offers_the_page(
    admin_client: AsyncClient, standard_client: AsyncClient, catalogue: int
) -> None:
    pages = {
        "admin": await admin_client.get("/templates"),
        "standard": await standard_client.get("/templates"),
    }
    home = await standard_client.get("/")

    assert {r.status_code for r in pages.values()} == {OK}
    assert titles_in(pages["standard"]) == ["heimdall", "vpnbox"]
    assert titles_in(pages["admin"]) == ["heimdall", "vpnbox"]
    assert 'href="/templates"' in home.text
    assert all(r.headers["cache-control"] == "no-store" for r in pages.values())
    assert inline_code_in(pages["standard"].text) == []


async def test_search_category_catalogue_and_paging_narrow_and_carry_the_list(
    admin_client: AsyncClient, world: World
) -> None:
    document = portainer_document(
        container("Redis", id=1, description="In-memory store", categories=["Database"]),
        container("Postgres", id=2, description="Relational store", categories=["Database"]),
        container("Nginx", id=3, description="Web server", categories=["Web"]),
    )
    first = await seed(world.app, "first", "portainer", document)
    second = await seed(
        world.app, "second", "portainer", portainer_document(container("Caddy", id=1))
    )

    searched = await admin_client.get("/templates?q=store")
    by_category = await admin_client.get("/templates?category=web")
    by_catalogue = await admin_client.get(f"/templates?catalogue={second}")
    unfiltered_form = await admin_client.get("/templates?q=&category=&catalogue=")
    bad_catalogue = await admin_client.get("/templates?catalogue=abc")
    nothing = await admin_client.get("/templates?q=zzzz")
    paged = await admin_client.get("/templates?limit=2&q=e")
    later = await admin_client.get(
        links_named(paged.text, "pagination__links")[-1][1].replace("&amp;", "&")
    )
    table = await admin_client.get("/templates?limit=2", headers=HTMX)

    assert titles_in(searched) == ["Postgres", "Redis"]
    assert titles_in(by_category) == ["Nginx"]
    assert titles_in(by_catalogue) == ["Caddy"]
    assert f'<option value="{second}" selected>' in by_catalogue.text
    assert titles_in(unfiltered_form) == ["Caddy", "Nginx", "Postgres", "Redis"]
    assert bad_catalogue.status_code == UNPROCESSABLE
    assert "Nothing matches these filters" in nothing.text
    assert titles_in(paged) == ["Nginx", "Postgres"] and titles_in(later) == ["Redis"]
    assert "q=e" in links_named(paged.text, "pagination__links")[-1][1]
    assert first != second
    assert 'id="templates-table"' in table.text and "<html" not in table.text


async def test_catalogue_text_is_reduced_to_safe_formatting_and_never_runs_as_markup(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    catalogue_id = await seed(
        world.app,
        "hostile",
        "portainer",
        portainer_document(
            container(
                "<b>Bold</b>",
                id=1,
                description=SCRIPT,
                note=MARKUP_NOTE,
                categories=["<i>cat</i>"],
                env=[{"name": "MODE", "description": "<u>x</u>", "default": SCRIPT}],
            ),
            container("Rich", id=2, description=RICH_DESCRIPTION, note=RICH_NOTE),
        ),
    )
    ref = f"{catalogue_id}:1"

    pages = [
        await admin_client.get("/templates"),
        await admin_client.get(f"/templates/{catalogue_id}/1"),
        await standard_client.get(container_new(world.remote_id, ref)),
        await standard_client.get(stack_new(world.remote_id, ref)),
    ]
    rich = await admin_client.get(f"/templates/{catalogue_id}/2")

    raw = (SCRIPT, MARKUP_NOTE, "<b>Bold</b>", "<i>cat</i>", "<img")
    assert [page.status_code for page in pages] == [OK] * len(pages)
    assert [(n, text) for n, page in enumerate(pages) for text in raw if text in page.text] == []
    assert "&lt;b&gt;Bold&lt;/b&gt;" in pages[2].text
    assert RICH_NOTE_HTML in rich.text
    assert "<em>proxy</em>" in rich.text
    assert "<script>alert" not in rich.text
    assert "onclick" not in rich.text
    assert "alert(1)" not in rich.text
    assert "Note</h2>" not in pages[1].text
    assert "Note</h2>" in rich.text
    assert RICH_LIST_TEXT in pages[0].text and "<em>" not in pages[0].text


async def test_a_template_page_lists_what_it_sets_and_no_logo_is_loaded(
    admin_client: AsyncClient, catalogue: int
) -> None:
    page = await admin_client.get(f"/templates/{catalogue}/vpnbox")

    text = unescape(re.sub(r"<[^>]+>", " ", page.text))
    assert page.status_code == OK
    assert "lscr.io/linuxserver/vpnbox:latest" in text
    assert "51820/udp" in text and "/config" in text and "PUID" in text
    assert "NET_ADMIN" in text and "/dev/net/tun" in text and "net.ipv4.ip_forward=1" in text
    assert "Privileged mode" in text
    assert (
        "These are filled in on the create form, except privileged mode and host bind mounts, "
        "which you set yourself."
    ) in text
    assert "<img" not in page.text and "example.invalid" not in page.text


async def test_a_template_page_offers_creating_only_where_the_viewer_may_and_never_hides_it(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, catalogue: int
) -> None:
    path = f"/templates/{catalogue}/heimdall"

    admin = await admin_client.get(path)
    standard = await standard_client.get(path)

    assert admin.status_code == standard.status_code == OK
    assert targets_in(admin.text, f"{catalogue}:heimdall") == {
        "containers": {world.local_id, world.remote_id, world.down_id},
        "stacks": {world.local_id, world.remote_id, world.down_id},
    }
    assert targets_in(standard.text, f"{catalogue}:heimdall") == {
        "containers": {world.remote_id},
        "stacks": {world.remote_id},
    }
    assert "Create container" in standard.text and "Create stack" in standard.text


async def test_an_administrator_only_template_is_invisible_and_unusable_to_a_standard_user(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    catalogue_id = await seed(
        world.app,
        "mixed",
        "portainer",
        portainer_document(
            container("Open tool", id=1),
            container("Secret tool", id=2, **{"administrator-only": True}),
        ),
    )
    ref = f"{catalogue_id}:2"

    listed = await standard_client.get("/templates")
    detail = await standard_client.get(f"/templates/{catalogue_id}/2")
    container_form = await standard_client.get(container_new(world.remote_id, ref))
    stack_form = await standard_client.get(stack_new(world.remote_id, ref))
    admin_listed = await admin_client.get("/templates")
    admin_detail = await admin_client.get(f"/templates/{catalogue_id}/2")

    assert titles_in(listed) == ["Open tool"] and titles_in(admin_listed) == [
        "Open tool",
        "Secret tool",
    ]
    assert detail.status_code == NOT_FOUND and admin_detail.status_code == OK
    forms = (container_form, stack_form)
    assert [form.status_code for form in forms] == [OK, OK]
    assert all(UNAVAILABLE in form.text for form in forms)
    assert not any("busybox" in form.text or "Secret tool" in form.text for form in forms)
    assert value_of(container_form.text, "image") == ""
    assert inputs_named(container_form.text, "template_ref") == []


async def test_a_missing_disabled_or_malformed_template_gives_the_blank_form_with_a_notice(
    admin_client: AsyncClient, world: World, catalogue: int
) -> None:
    disabled = await seed(
        world.app, "off", "portainer", portainer_document(container("Off", id=1)), enabled=False
    )

    refs = [f"{catalogue}:missing", f"{disabled}:1", "garbage", "99:heimdall"]

    forms = [await admin_client.get(container_new(world.remote_id, ref)) for ref in refs]
    stacks = [await admin_client.get(stack_new(world.remote_id, ref)) for ref in refs]

    pages = [*forms, *stacks]
    assert {page.status_code for page in pages} == {OK}
    assert all(UNAVAILABLE in page.text for page in pages)
    assert {value_of(form.text, "image") for form in forms} == {""}
    assert [inputs_named(form.text, "template_ref") for form in forms] == [[]] * len(refs)


async def test_a_container_form_is_prefilled_from_a_linuxserver_template_on_the_server_only(
    admin_client: AsyncClient, world: World, catalogue: int
) -> None:
    ref = f"{catalogue}:heimdall"

    page = await admin_client.get(container_new(world.remote_id, ref))

    assert page.status_code == OK
    assert value_of(page.text, "image") == HEIMDALL_IMAGE
    assert value_of(page.text, "name") == "heimdall"
    assert checked_values(page.text, "restart_policy") == ["unless-stopped"]
    assert row_values(page.text, "env", "key") == [{"key": "PUID", "value": "1000"}]
    assert [
        (r["container_port"], r["host_port"], r["protocol"])
        for r in row_values(page.text, "port", "container_port")
    ] == [("80", "8081", "tcp")]
    assert [
        (r["type"], r["source"], r["target"]) for r in row_values(page.text, "mount", "target")
    ] == [("volume", "heimdall-config", "/config")]
    assert "/library" not in page.text and "VERSION" not in page.text and "8443" not in page.text
    assert value_of(page.text, "template_ref") == ref
    assert "From the template" in page.text and "heimdall" in page.text
    assert page.headers["cache-control"] == "no-store"


async def test_a_standard_user_gets_no_administrator_settings_and_is_told_what_was_left_out(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    catalogue: int,
    daemon: ContainerEngine,
) -> None:
    ref = f"{catalogue}:vpnbox"

    standard = await standard_client.get(container_new(world.remote_id, ref))
    administrator = await admin_client.get(container_new(world.remote_id, ref))

    assert standard.status_code == administrator.status_code == OK
    assert inputs_named(standard.text, "cap_add") == []
    assert inputs_named(standard.text, "privileged") == []
    assert inputs_named(standard.text, "device-0-host_path") == []
    assert (
        unescape(standard.text).count(
            "This template asks for added capabilities (NET_ADMIN), "
            "device mappings (/dev/net/tun), "
            "kernel parameters (net.ipv4.ip_forward=1), privileged mode, "
            "which only an administrator can set."
        )
        == 1
    )
    assert row_values(standard.text, "port", "container_port")[0]["container_port"] == "51820"
    assert value_of(administrator.text, "cap_add") == "NET_ADMIN"
    assert checked_values(administrator.text, "privileged") == []
    assert row_values(administrator.text, "device", "host_path")[0]["host_path"] == "/dev/net/tun"
    assert "only an administrator can set" not in administrator.text
    assert (
        unescape(administrator.text).count(
            "This template sets elevated options: added capabilities (NET_ADMIN), "
            "device mappings (/dev/net/tun). Review them before you create it. "
            "It also asks for privileged mode, which are not filled in. "
            "Set them yourself if you want them."
        )
        == 1
    )


async def test_submitting_the_prefilled_form_creates_the_container_and_records_the_template(
    standard_client: AsyncClient, world: World, catalogue: int, daemon: ContainerEngine
) -> None:
    ref = f"{catalogue}:vpnbox"
    page = await standard_client.get(container_new(world.remote_id, ref))
    data = form_values(page.text)

    reviewed = await standard_client.post(
        f"/environments/{world.remote_id}/containers/new", data=data | {"intent": "review"}
    )
    created = await standard_client.post(f"/environments/{world.remote_id}/containers", data=data)

    assert reviewed.status_code == OK and value_of(reviewed.text, "template_ref") == ref
    assert created.status_code == SEE_OTHER
    assert daemon.verbs() == ["create", "start"]
    assert (await audited_detail(world.app, "container_created"))["template"] == ref


async def test_the_restriction_set_still_refuses_what_a_prefilled_form_is_edited_to_ask_for(
    standard_client: AsyncClient, world: World, catalogue: int, daemon: ContainerEngine
) -> None:
    ref = f"{catalogue}:vpnbox"
    data = form_values((await standard_client.get(container_new(world.remote_id, ref))).text)

    refused = await standard_client.post(
        f"/environments/{world.remote_id}/containers",
        data=data | {"privileged": "on", "cap_add": "NET_ADMIN"},
    )

    assert refused.status_code == BAD_REQUEST
    assert "Only administrators may use" in refused.text
    assert value_of(refused.text, "template_ref") == ref
    assert daemon.mutations() == []


async def test_a_reference_that_no_longer_names_a_template_is_dropped_not_recorded(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    data = form_values(
        (await admin_client.get(f"/environments/{world.remote_id}/containers/new")).text
    )
    daemon.add_image("nginx:1")

    created = await admin_client.post(
        f"/environments/{world.remote_id}/containers",
        data=data | {"image": "nginx:1", "template_ref": "99:gone"},
    )

    assert created.status_code == SEE_OTHER
    assert "template" not in await audited_detail(world.app, "container_created")


@pytest.mark.usefixtures("stopped_stack_jobs")
async def test_a_stack_form_is_filled_with_a_generated_file_that_creates_a_stack_and_records_it(
    standard_client: AsyncClient, world: World, catalogue: int, stack_host: ContainerEngine
) -> None:
    ref = f"{catalogue}:heimdall"
    base = f"/environments/{world.remote_id}/stacks/-"
    page = await standard_client.get(stack_new(world.remote_id, ref))
    compose = compose_of(page.text)
    data = form_values(page.text) | {"compose_file": yaml.safe_dump(compose)}

    checked = await standard_client.post(f"{base}/validate", data=data)
    created = await standard_client.post(f"{base}/new", data=data)
    await settle_stack_job(world.app, job_id_of(created))

    assert page.status_code == OK and data["name"] == "heimdall" and data["template_ref"] == ref
    assert compose["services"]["heimdall"] == {
        "image": HEIMDALL_IMAGE,
        "restart": "unless-stopped",
        "ports": ["8081:80/tcp"],
        "volumes": ["heimdall-config:/config"],
        "environment": {"PUID": "1000"},
    }
    assert checked.status_code == OK and "Docker Compose rejected" not in checked.text
    assert created.status_code == SEE_OTHER
    assert (await audited_detail(world.app, "stack_created"))["template"] == ref


@pytest.mark.usefixtures("stopped_stack_jobs")
async def test_the_stack_form_withholds_administrator_settings_and_the_pre_check_still_refuses_them(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    catalogue: int,
    stack_host: ContainerEngine,
) -> None:
    ref = f"{catalogue}:vpnbox"
    standard = await standard_client.get(stack_new(world.remote_id, ref))
    administrator = await admin_client.get(stack_new(world.remote_id, ref))
    handed = compose_of(administrator.text)

    refused = await standard_client.post(
        f"/environments/{world.remote_id}/stacks/-/validate",
        data=csrf_form(standard_client, name="vpnbox", compose_file=yaml.safe_dump(handed)),
    )

    service = compose_of(standard.text)["services"]["vpnbox"]
    assert {"cap_add", "devices", "sysctls", "privileged"}.isdisjoint(service)
    assert handed["services"]["vpnbox"]["cap_add"] == ["NET_ADMIN"]
    assert "privileged" not in handed["services"]["vpnbox"]
    assert "only an administrator can set" in standard.text
    assert refused.status_code == FORBIDDEN
    assert "options your role cannot deploy" in refused.text
