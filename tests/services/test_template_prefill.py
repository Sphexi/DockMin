"""What a template sets for each role, as container form values and as a Compose file."""

import json
from typing import Any

import pytest
import yaml

from app.services.app_template import AppTemplate
from app.services.template_normalise import normalise
from app.services.template_prefill import (
    architecture_note,
    canonical_architecture,
    container_name,
    container_prefill,
    settings_for,
    stack_name,
    stack_prefill,
)
from tests.template_support import container, linuxserver_document, linuxserver_image

SOCKET = "/var/run/docker.sock"
ADMIN, STANDARD = True, False


def linuxserver(name: str, **config: Any) -> AppTemplate:
    document = json.loads(linuxserver_document(linuxserver_image(name, **config)))
    return normalise("linuxserver", document).templates[0]


def portainer(title: str, **changes: Any) -> AppTemplate:
    document = {"version": "3", "templates": [container(title, **changes)]}
    return normalise("portainer", document).templates[0]


def required(name: str, value: str = "") -> dict[str, Any]:
    return {"name": name, "value": value, "optional": False}


def heimdall() -> AppTemplate:
    return linuxserver(
        "heimdall",
        env_vars=[
            required("PUID", "1000"),
            required("TZ", "Etc/UTC"),
            {"name": "VERSION", "optional": True},
        ],
        volumes=[
            {"path": "/config", "host_path": "/path/to/config", "optional": False},
            {"path": "/library", "host_path": "/path/to/library", "optional": True},
        ],
        ports=[
            {"external": "8080", "internal": "80", "optional": False},
            {"external": "8443", "internal": "443", "optional": True},
        ],
    )


def plex() -> AppTemplate:
    return linuxserver(
        "plex",
        ports=[{"external": "32400", "internal": "32400", "optional": False}],
        caps=[{"cap_add": "NET_ADMIN", "optional": False}],
        custom=[
            {
                "name": "sysctl",
                "value": ["net.ipv4.ip_unprivileged_port_start=0"],
                "optional": False,
            },
            {"name": "shm-size", "value": "1gb", "optional": False},
        ],
        security_opt=[{"run_var": "seccomp=unconfined", "optional": False}],
        networking="host",
        hostname={"hostname": "plex", "optional": False},
    )


def test_a_linuxserver_container_is_prefilled_with_required_entries_only() -> None:
    prefill = container_prefill(heimdall(), is_administrator=STANDARD)

    assert prefill.values == {
        "image": "lscr.io/linuxserver/heimdall:latest",
        "name": "heimdall",
        "restart_policy": "unless-stopped",
        "env-0-key": "PUID",
        "env-0-value": "1000",
        "env-1-key": "TZ",
        "env-1-value": "Etc/UTC",
        "port-0-container_port": "80",
        "port-0-protocol": "tcp",
        "port-0-host_port": "8080",
        "mount-0-type": "volume",
        "mount-0-source": "heimdall-config",
        "mount-0-target": "/config",
    }
    assert prefill.withheld is None and prefill.not_carried is None


def test_an_administrator_gets_the_privileged_settings_that_have_a_field() -> None:
    prefill = container_prefill(plex(), is_administrator=ADMIN)

    assert prefill.values["cap_add"] == "NET_ADMIN"
    assert prefill.values["network_mode"] == "host"
    assert prefill.values["shm_size"] == "1gb"
    assert not any(name.startswith("port-") for name in prefill.values)
    assert prefill.withheld == (
        "This template sets elevated options: host networking, added capabilities (NET_ADMIN), "
        "security options (seccomp=unconfined). Review them before you create it."
    )
    assert prefill.not_carried == (
        "The create form has no field for the host name plex, "
        "the kernel parameters net.ipv4.ip_unprivileged_port_start=0, "
        "the security options seccomp=unconfined, so they were not carried over."
    )


def test_a_standard_user_gets_none_of_them_and_is_told_what_the_template_asks_for() -> None:
    prefill = container_prefill(plex(), is_administrator=STANDARD)

    assert {"cap_add", "network_mode", "privileged"}.isdisjoint(prefill.values)
    assert prefill.values["port-0-container_port"] == "32400"
    assert prefill.withheld == (
        "This template asks for added capabilities (NET_ADMIN), "
        "kernel parameters (net.ipv4.ip_unprivileged_port_start=0), "
        "security options (seccomp=unconfined), host networking, "
        "which only an administrator can set."
    )


def test_privileged_mode_and_host_binds_are_left_for_an_administrator_to_set() -> None:
    template = portainer(
        "Proxy",
        privileged=True,
        volumes=[{"container": SOCKET, "bind": SOCKET, "readonly": True}, {"container": "/data"}],
    )

    admin = container_prefill(template, is_administrator=ADMIN)
    standard = container_prefill(template, is_administrator=STANDARD)

    assert admin.values["mount-0-source"] == "proxy-data" and "mount-1-type" not in admin.values
    assert "privileged" not in admin.values
    assert admin.withheld == (
        f"This template asks for privileged mode, host bind mounts ({SOCKET}), "
        "which are not filled in. Set them yourself if you want them."
    )
    assert (
        standard.values["mount-0-source"] == "proxy-data" and "mount-1-type" not in standard.values
    )
    assert standard.withheld == (
        f"This template asks for privileged mode, host bind mounts ({SOCKET}), "
        "which only an administrator can set."
    )


@pytest.mark.parametrize(
    ("paths", "sources"),
    [
        pytest.param(["/config"], ["app-config"], id="last-segment"),
        pytest.param(["/var/lib/data/"], ["app-data"], id="trailing-slash"),
        pytest.param(["/"], ["app-data"], id="root"),
        pytest.param(["/a/data", "/b/data"], ["app-data", "app-data-2"], id="collision"),
    ],
)
def test_named_volumes_are_the_container_name_and_the_last_path_segment(
    paths: list[str], sources: list[str]
) -> None:
    template = portainer("App", name="app", volumes=[{"container": path} for path in paths])

    mounts = settings_for(template, is_administrator=STANDARD).mounts

    assert [mount.source for mount in mounts] == sources


def test_a_generated_stack_file_holds_one_service_with_the_required_entries() -> None:
    prefill = stack_prefill(heimdall(), is_administrator=STANDARD)

    assert prefill.name == "heimdall"
    assert yaml.safe_load(prefill.compose_text) == {
        "services": {
            "heimdall": {
                "image": "lscr.io/linuxserver/heimdall:latest",
                "restart": "unless-stopped",
                "ports": ["8080:80/tcp"],
                "volumes": ["heimdall-config:/config"],
                "environment": {"PUID": "1000", "TZ": "Etc/UTC"},
            }
        },
        "volumes": {"heimdall-config": {}},
    }


def test_an_administrators_stack_file_carries_every_elevated_setting_but_privileged() -> None:
    template = linuxserver(
        "tool",
        caps=[{"cap_add": "NET_ADMIN", "optional": False}],
        devices=[{"path": "/dev/net/tun", "optional": False}],
        custom=[
            {"name": "sysctl", "value": ["net.ipv6.conf.all.disable_ipv6=1"], "optional": False}
        ],
        security_opt=[{"run_var": "no-new-privileges=true", "optional": False}],
        hostname={"hostname": "tool", "optional": False},
        privileged=True,
    )

    service = yaml.safe_load(stack_prefill(template, is_administrator=ADMIN).compose_text)[
        "services"
    ]["tool"]

    assert service["cap_add"] == ["NET_ADMIN"]
    assert service["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert service["sysctls"] == ["net.ipv6.conf.all.disable_ipv6=1"]
    assert service["security_opt"] == ["no-new-privileges=true"]
    assert service["hostname"] == "tool"
    assert "privileged" not in service
    standard = stack_prefill(template, is_administrator=STANDARD)
    assert yaml.safe_load(standard.compose_text)["services"]["tool"] == {
        "image": "lscr.io/linuxserver/tool:latest",
        "restart": "unless-stopped",
        "hostname": "tool",
    }
    assert standard.withheld is not None and "privileged mode" in standard.withheld


def test_a_dollar_sign_in_catalogue_text_is_escaped_so_compose_does_not_interpolate_it() -> None:
    template = portainer(
        "Dollar", env=[{"name": "TOKEN", "default": "a$b${C}"}], command="echo $HOME"
    )

    service = yaml.safe_load(stack_prefill(template, is_administrator=STANDARD).compose_text)[
        "services"
    ]["dollar"]

    assert service["environment"]["TOKEN"] == "a$$b$${C}"
    assert service["command"] == "echo $$HOME"


def test_host_networking_leaves_out_the_published_ports_it_would_refuse() -> None:
    admin = yaml.safe_load(stack_prefill(plex(), is_administrator=ADMIN).compose_text)
    standard = yaml.safe_load(stack_prefill(plex(), is_administrator=STANDARD).compose_text)

    assert admin["services"]["plex"]["network_mode"] == "host"
    assert "ports" not in admin["services"]["plex"]
    assert standard["services"]["plex"]["ports"] == ["32400:32400/tcp"]


def test_more_rows_than_the_form_holds_are_counted_in_a_notice() -> None:
    template = portainer("Many", env=[{"name": f"V{n}", "default": "x"} for n in range(105)])

    prefill = container_prefill(template, is_administrator=STANDARD)

    assert "env-99-key" in prefill.values and "env-100-key" not in prefill.values
    assert (
        prefill.not_carried == "5 environment variables were not carried over: the form holds 100."
    )


def test_environment_variables_that_offer_choices_are_listed_as_hints() -> None:
    template = portainer(
        "Choosy",
        env=[
            {
                "name": "MODE",
                "select": [{"text": "Prod", "value": "prod"}, {"text": "Dev", "value": "dev"}],
            }
        ],
    )

    assert container_prefill(template, is_administrator=STANDARD).hints == ("MODE: prod, dev",)


@pytest.mark.parametrize(
    ("title", "name", "container_suggestion", "stack_suggestion"),
    [
        pytest.param("My App", "web", "web", "web", id="name"),
        pytest.param("My App", None, "my-app", "my-app", id="title"),
        pytest.param("!!", None, None, "i0-", id="no-usable-name"),
    ],
)
def test_the_suggested_names_follow_each_grammar(
    title: str, name: str | None, container_suggestion: str | None, stack_suggestion: str
) -> None:
    template = portainer(title, **({} if name is None else {"name": name}))

    assert container_name(template) == container_suggestion
    assert stack_name(template).startswith(stack_suggestion)


@pytest.mark.parametrize(
    ("published", "host", "note"),
    [
        pytest.param(["amd64", "arm64"], "x86_64", None, id="published"),
        pytest.param(["amd64"], "aarch64", "not published for arm64", id="missing"),
        pytest.param(["amd64"], "arm64", "not published for arm64", id="docker-name"),
        pytest.param(["amd64"], None, None, id="host-unknown"),
        pytest.param([], "aarch64", None, id="template-silent"),
    ],
)
def test_the_architecture_note_names_a_host_the_template_is_not_published_for(
    published: list[str], host: str | None, note: str | None
) -> None:
    assert architecture_note(published, host) == note


@pytest.mark.parametrize(
    ("reported", "docker"),
    [
        ("x86_64", "amd64"),
        ("AMD64", "amd64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("riscv64", "riscv64"),
    ],
)
def test_a_host_architecture_maps_to_the_name_docker_uses(reported: str, docker: str) -> None:
    assert canonical_architecture(reported) == docker
