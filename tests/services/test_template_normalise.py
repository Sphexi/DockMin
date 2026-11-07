"""Normalising recorded linuxserver.io and Portainer documents into one template model."""

import json
from typing import Any

import pytest

from app.services.app_template import (
    MAX_ENTRIES,
    TemplateRef,
    parse_template_ref,
    template_ref,
)
from app.services.template_fetch import CatalogueFetchError
from app.services.template_normalise import normalise, parse_catalogue
from tests.template_support import (
    container,
    digest_key,
    fixture_text,
    normalised,
    portainer_document,
)

SOCKET_PATH = "/var/run/docker.sock"


def by_key(name: str, kind: str) -> dict[str, Any]:
    return {t.key: t.model_dump(mode="json") for t in normalised(name, kind).templates}


def test_a_linuxserver_document_keeps_the_usable_images_and_counts_what_it_skips() -> None:
    result = normalised("linuxserver.json", "linuxserver")

    assert [t.key for t in result.templates] == ["plex", "swag", "gluetun"]
    assert result.skipped == {
        "deprecated": 1,
        "invalid_key": 1,
        "invalid_port": 1,
        "not_an_object": 1,
    }
    assert result.entry_count == 3


def test_a_linuxserver_image_maps_its_config_into_the_template() -> None:
    plex = by_key("linuxserver.json", "linuxserver")["plex"]

    assert plex["image"] == "lscr.io/linuxserver/plex:latest"
    assert plex["categories"] == ["Media Servers", "Other"]
    assert plex["architectures"] == ["amd64", "arm64"]
    assert [
        (p["container_port"], p["protocol"], p["host_port"], p["optional"]) for p in plex["ports"]
    ] == [
        (32400, "tcp", 32400, False),
        (1900, "udp", 1900, True),
    ]
    assert [(v["container_path"], v["optional"], v["bind"]) for v in plex["volumes"]] == [
        ("/config", False, None),
        ("/tv", True, None),
    ]
    assert [(e["name"], e["default"], e["optional"]) for e in plex["env"]] == [
        ("PUID", "1000", False),
        ("TZ", "Etc/UTC", False),
        ("VERSION", "docker", True),
    ]
    assert plex["capabilities"] == ["NET_ADMIN"]
    assert plex["sysctls"] == ["net.ipv4.ip_unprivileged_port_start=0"]
    assert plex["security_options"] == ["seccomp=unconfined"]
    assert (plex["shm_size"], plex["hostname"], plex["host_network"]) == ("1gb", "plex", True)
    assert plex["setup_url"].startswith("https://docs.linuxserver.io/")
    assert plex["restart_policy"] == "unless-stopped" and plex["administrator_only"] is False


def test_linuxserver_networking_hostname_and_privileged_are_read_in_their_observed_shapes() -> None:
    templates = by_key("linuxserver.json", "linuxserver")

    plex, gluetun = templates["plex"], templates["gluetun"]
    assert (plex["host_network"], plex["hostname"], plex["privileged"]) == (True, "plex", False)
    assert (gluetun["host_network"], gluetun["hostname"], gluetun["privileged"]) == (
        False,
        None,
        True,
    )


def test_linuxserver_custom_entries_map_shm_size_and_a_sysctl_list_and_ignore_stop_timeout() -> (
    None
):
    plex = by_key("linuxserver.json", "linuxserver")["plex"]

    assert plex["shm_size"] == "1gb"
    assert plex["sysctls"] == ["net.ipv4.ip_unprivileged_port_start=0"]


def test_optional_linuxserver_capabilities_security_options_sysctls_and_shm_size_are_dropped() -> (
    None
):
    gluetun = by_key("linuxserver.json", "linuxserver")["gluetun"]

    assert gluetun["capabilities"] == []
    assert gluetun["security_options"] == []
    assert gluetun["sysctls"] == []
    assert gluetun["shm_size"] is None


def test_an_optional_device_is_left_out_and_a_setup_link_that_is_not_https_is_dropped() -> None:
    templates = by_key("linuxserver.json", "linuxserver")

    assert templates["plex"]["devices"] == []
    assert templates["swag"]["setup_url"] is None
    assert templates["swag"]["architectures"] == ["arm64"]


def test_a_portainer_v3_document_keeps_container_templates_and_skips_stacks_by_reason() -> None:
    result = normalised("portainer_v3.json", "portainer")

    assert [t.key for t in result.templates] == ["1", "2"]
    assert result.skipped == {
        "duplicate_key": 1,
        "invalid_image": 1,
        "invalid_port": 1,
        "missing_title": 1,
        "not_an_object": 1,
        "unsupported_stack_template": 2,
        "unsupported_type": 1,
    }


def test_a_portainer_container_template_maps_its_fields() -> None:
    nginx = by_key("portainer_v3.json", "portainer")["1"]

    assert (nginx["title"], nginx["image"], nginx["name"]) == ("Nginx", "nginx:latest", "web")
    assert [(p["container_port"], p["host_port"]) for p in nginx["ports"]] == [
        (80, 80),
        (443, None),
    ]
    assert nginx["restart_policy"] == "unless-stopped"
    assert nginx["categories"] == ["webserver", "Web"]
    assert nginx["labels"] == [{"name": "managed-by", "value": "template"}]
    mode, token, region = nginx["env"]
    assert (mode["default"], [o["value"] for o in mode["options"]]) == ("prod", ["prod", "dev"])
    assert (token["default"], token["preset"]) == ("", False)
    assert (region["default"], region["preset"]) == ("eu", True)


def test_markup_in_portainer_text_is_kept_as_text_for_the_page_to_escape() -> None:
    nginx = by_key("portainer_v3.json", "portainer")["1"]

    assert nginx["description"] == "<b>High performance</b> web server"
    assert nginx["note"].startswith("<script>alert(1)</script>")


def test_a_portainer_administrator_template_carries_its_privileged_settings() -> None:
    proxy = by_key("portainer_v3.json", "portainer")["2"]

    assert proxy["image"] == "quay.io/tecnativa/docker-socket-proxy:latest"
    assert (proxy["administrator_only"], proxy["privileged"], proxy["interactive"]) == (
        True,
        True,
        True,
    )
    assert (proxy["host_network"], proxy["hostname"], proxy["command"]) == (
        True,
        "proxy",
        "--verbose",
    )
    assert [(v["container_path"], v["bind"], v["read_only"]) for v in proxy["volumes"]] == [
        (SOCKET_PATH, SOCKET_PATH, True)
    ]


def test_a_portainer_v2_document_keys_templates_without_ids_by_position_and_title() -> None:
    result = normalised("portainer_v2.json", "portainer")

    assert [t.key for t in result.templates] == [
        digest_key(0, "Redis"),
        digest_key(1, "Admin tool"),
    ]
    assert [t.administrator_only for t in result.templates] == [False, True]
    assert result.skipped == {}


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        pytest.param("linuxserver", {"data": {}}, id="linuxserver-without-repositories"),
        pytest.param("linuxserver", [], id="linuxserver-array"),
        pytest.param("portainer", {"templates": []}, id="portainer-without-version"),
        pytest.param("portainer", {"version": "1", "templates": []}, id="portainer-version-one"),
        pytest.param("portainer", {"version": "3", "templates": {}}, id="portainer-not-a-list"),
        pytest.param("other", {}, id="unknown-kind"),
    ],
)
def test_a_document_of_the_wrong_shape_is_refused_as_invalid(kind: str, document: object) -> None:
    with pytest.raises(CatalogueFetchError) as refused:
        normalise(kind, document)

    assert refused.value.code == "invalid_document"


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1, NaN]", b'{"version": "3", "templates": [Infinity]}', b"[" * 100_000],
)
def test_a_body_that_is_not_json_is_refused_as_invalid(body: bytes) -> None:
    with pytest.raises(CatalogueFetchError) as refused:
        parse_catalogue("portainer", body)

    assert refused.value.code == "invalid_document"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param({"ports": "80"}, "invalid_port", id="ports-not-a-list"),
        pytest.param({"ports": ["70000"]}, "invalid_port", id="port-out-of-range"),
        pytest.param({"ports": ["80/xyz"]}, "invalid_port", id="unknown-protocol"),
        pytest.param(
            {"volumes": [{"container": "relative"}]}, "invalid_volume", id="relative-path"
        ),
        pytest.param({"volumes": [{"container": "/a:b"}]}, "invalid_volume", id="colon-in-path"),
        pytest.param({"volumes": ["/data"]}, "invalid_volume", id="volume-not-an-object"),
        pytest.param({"env": [{"name": "1BAD"}]}, "invalid_env", id="env-name-grammar"),
        pytest.param(
            {"env": [{"name": "A", "default": {"x": 1}}]}, "invalid_env", id="env-default"
        ),
        pytest.param({"labels": [{"value": "x"}]}, "invalid_label", id="label-without-name"),
        pytest.param({"command": 5}, "invalid_command", id="command-not-text"),
        pytest.param({"image": ""}, "invalid_image", id="empty-image"),
        pytest.param({"image": "UPPER/case"}, "invalid_image", id="uppercase-image"),
    ],
)
def test_a_malformed_portainer_entry_is_skipped_and_counted_by_reason(
    changes: dict[str, Any], reason: str
) -> None:
    good = container("Good")
    bad = container("Bad", **changes)

    result = normalise("portainer", json.loads(portainer_document(good, bad)))

    assert [t.title for t in result.templates] == ["Good"]
    assert result.skipped == {reason: 1}


def test_overlong_text_is_cut_to_its_limit_and_control_characters_are_removed() -> None:
    entry = container("T" * 500, description="a\x00b‮" + "d" * 5000, categories=["c" * 99] * 3)

    [template] = normalise("portainer", json.loads(portainer_document(entry))).templates

    assert len(template.title) == 128 and len(template.description) == 2000
    assert template.description.startswith("ab") and "‮" not in template.description
    assert template.categories == ("c" * 32,)


def test_a_catalogue_keeps_at_most_the_entry_limit_and_counts_the_rest() -> None:
    entries = [container(f"App {n}", id=n) for n in range(MAX_ENTRIES + 3)]

    result = normalise("portainer", json.loads(portainer_document(*entries)))

    assert result.entry_count == MAX_ENTRIES
    assert result.skipped == {"too_many_entries": 3}


def test_normalising_the_same_document_twice_gives_the_same_digest_and_a_change_gives_another() -> (
    None
):
    first = normalised("portainer_v3.json", "portainer")
    again = normalise("portainer", json.loads(fixture_text("portainer_v3.json")))
    changed = normalise("portainer", json.loads(portainer_document(container("Other"))))

    assert first.digest == again.digest and first.content == again.content
    assert changed.digest != first.digest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("3:plex", TemplateRef(3, "plex"), id="plain"),
        pytest.param("12:i0-ab12cd34", TemplateRef(12, "i0-ab12cd34"), id="digest-key"),
        pytest.param("0:plex", None, id="zero-id"),
        pytest.param("x:plex", None, id="non-numeric-id"),
        pytest.param("3:", None, id="empty-key"),
        pytest.param("3:../etc", None, id="path-key"),
        pytest.param("3", None, id="no-separator"),
        pytest.param("9" * 30 + ":plex", None, id="id-out-of-range"),
    ],
)
def test_a_template_reference_parses_only_in_its_exact_form(
    text: str, expected: TemplateRef | None
) -> None:
    assert parse_template_ref(text) == expected


def test_a_reference_round_trips() -> None:
    assert parse_template_ref(template_ref(7, "nginx")) == TemplateRef(7, "nginx")
