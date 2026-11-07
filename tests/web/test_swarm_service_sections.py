"""The service form's health check, process, host, security, limits and logging sections."""

import re
from html import unescape

from httpx import AsyncClient

from tests.auth_support import csrf_form
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.services.test_service_spec_task import EVERYTHING
from tests.swarm_support import form_action, hidden_fields, without_token
from tests.web.environment_support import World
from tests.web.helpers import form_values

OK, SEE_OTHER = 200, 303
SECTION_IDS = (
    "section-healthcheck",
    "section-process",
    "section-host",
    "section-security",
    "section-ulimits",
    "section-logging",
)
LOCKED_TEXT = "Set by an administrator"
CREATE = {"name": "web", "image": "busybox:latest", "mode": "replicated", "replicas": "1"}
STANDARD_CREATE = {
    "health_test": "shell",
    "health_command": "true",
    "user": "1000",
    "log_driver": "local",
    "logopt-0-key": "max-size",
    "logopt-0-value": "1m",
}


def base_of(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm"


def posted_by_the_page(html: str) -> dict[str, str]:
    """Return what a browser submits for the form: inputs, selects and text areas."""
    values = form_values(html)
    for name, content in re.findall(
        r'<textarea[^>]*\bname="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S
    ):
        values[name] = unescape(content.removeprefix("\n"))
    return without_token(values)


async def create(client: AsyncClient, world: World, **fields: str) -> str:
    response = await client.post(
        f"{base_of(world)}/services/new", data=csrf_form(client, **{**CREATE, **fields})
    )
    assert response.status_code == SEE_OTHER
    return response.headers["location"].split("/services/")[1].split("?")[0]


async def test_an_administrators_form_shows_every_section_and_an_unchanged_edit_changes_nothing(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create(admin_client, world, **EVERYTHING)
    base = f"{base_of(world)}/services/{service_id}"

    edit = await admin_client.get(f"{base}/edit")
    values = posted_by_the_page(edit.text)
    review = await admin_client.post(f"{base}/edit", data=csrf_form(admin_client, **values))

    assert edit.status_code == OK and all(f'id="{name}"' in edit.text for name in SECTION_IDS)
    assert 'name="cap_add"' in edit.text and 'name="sysctls"' in edit.text
    assert values["health_command"] == "curl -f 'http://localhost/a b'"
    assert values["cap_add"] == "CAP_NET_BIND_SERVICE CAP_SYS_TIME"
    assert values["ulimit-0-name"] == "nofile" and values["logopt-1-key"] == "max-file"
    assert review.status_code == OK
    assert "No setting differs from the stored configuration" in review.text


async def test_a_changed_section_is_named_on_the_review_and_replaces_tasks(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create(admin_client, world, **EVERYTHING)
    base = f"{base_of(world)}/services/{service_id}"
    values = posted_by_the_page((await admin_client.get(f"{base}/edit")).text)

    review = await admin_client.post(
        f"{base}/edit", data=csrf_form(admin_client, **{**values, "health_interval": "45s"})
    )
    confirm = await admin_client.post(
        form_action(review), data=csrf_form(admin_client, **without_token(hidden_fields(review)))
    )

    stored = swarm_host.services[service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]
    assert "<li>Health check</li>" in review.text and "<li>Process</li>" not in review.text
    assert "replaced as the update rolls out" in review.text
    assert "Running tasks are not replaced" not in review.text
    assert confirm.status_code == SEE_OTHER
    assert stored["Healthcheck"]["Interval"] == 45_000_000_000


async def test_a_standard_users_form_shows_refused_stored_values_read_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
) -> None:
    service_id = await create(standard_client, world, **STANDARD_CREATE)
    container = swarm_host.services[service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]
    container.update(CapabilityAdd=["CAP_NET_ADMIN"], Sysctls={"net.core.somaxconn": "9"})
    swarm_host.services[service_id]["Spec"]["TaskTemplate"]["LogDriver"] = {
        "Name": "fluentd",
        "Options": {"fluentd-address": "10.0.0.9:24224"},
    }
    swarm_host.services[service_id]["Version"]["Index"] += 1
    url = f"{base_of(world)}/services/{service_id}/edit"

    as_standard = await standard_client.get(url)
    as_administrator = await admin_client.get(url)

    assert as_standard.status_code == OK
    for name in ("cap_add", "sysctls", "log_driver"):
        assert f'name="{name}"' not in as_standard.text
        assert f'name="{name}"' in as_administrator.text
    assert as_standard.text.count(LOCKED_TEXT) == 4
    assert "CAP_NET_ADMIN" in as_standard.text and "net.core.somaxconn=9" in as_standard.text
    assert "fluentd" in as_standard.text and "10.0.0.9:24224" in as_standard.text
    assert LOCKED_TEXT not in as_administrator.text
    assert all(f'id="{name}"' in as_standard.text for name in SECTION_IDS)
    assert 'name="user"' in as_standard.text and 'name="health_command"' in as_standard.text


async def test_a_standard_users_form_offers_only_the_allowed_log_drivers_and_options(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create(standard_client, world, **STANDARD_CREATE)

    edit = await standard_client.get(f"{base_of(world)}/services/{service_id}/edit")

    options = re.findall(r'<option value="([^"]*)"', edit.text)
    assert {"json-file", "local", "none", "max-size", "max-file", "compress"} <= set(options)
    assert "syslog" not in options and "fluentd" not in options
    assert LOCKED_TEXT not in edit.text
    assert posted_by_the_page(edit.text)["log_driver"] == "local"
