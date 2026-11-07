"""Tests for app/services/container_spec.py: form parsing, validation and the Engine body."""

from functools import reduce
from typing import Any

import pytest

from app.services.container_spec import (
    ROW_CAPS,
    ContainerSpec,
    FormState,
    SpecInvalid,
    parse_container_name,
    parse_duration,
    parse_form,
    parse_image,
    parse_int,
    parse_memory,
    parse_port_range,
    row_slots,
    spec_from_form,
    to_engine_body,
)

DIGEST = "sha256:" + "a" * 64
HEX_64 = "ab" * 32
KIB = 1024
MIB = KIB * KIB
GIB = MIB * KIB
NS_PER_SECOND = 1_000_000_000
LABEL_LIMIT = 64 * KIB
SENTINEL = "SENTINEL 'x !"
STACK_LABEL = "com.docker.compose.project"
SWARM_LABEL = "com.docker.stack.namespace"
FRONT_ID, BACK_ID = "f" * 64, "b" * 64


ROW_ANCHOR = {
    "env": "key",
    "label": "key",
    "port": "container_port",
    "mount": "target",
    "network": "name",
    "device": "host_path",
    "host": "name",
    "logopt": "key",
}


def form(**fields: str) -> dict[str, str]:
    return {"image": "nginx"} | fields


def errors_of(**fields: str) -> dict[str, str]:
    return dict(parse_form(form(**fields)).state.errors)


def at(body: dict[str, Any], path: str) -> Any:
    return reduce(lambda node, key: node[key], path.split("."), body)


def numbered(prefix: str, count: int) -> dict[str, str]:
    return {f"{prefix}-{index}-key": f"k{index}" for index in range(count)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nginx", "nginx:latest"),
        ("nginx:1.27", "nginx:1.27"),
        ("  nginx:1.27  ", "nginx:1.27"),
        ("library/nginx", "library/nginx:latest"),
        ("ghcr.io/org/app:v1", "ghcr.io/org/app:v1"),
        ("localhost/app", "localhost/app:latest"),
        ("localhost:5000/team/app", "localhost:5000/team/app:latest"),
        ("registry.example.test:5000/team/app:v1", "registry.example.test:5000/team/app:v1"),
        (f"nginx@{DIGEST}", f"nginx@{DIGEST}"),
        (f"nginx:1@{DIGEST}", f"nginx:1@{DIGEST}"),
        ("my-app/web__1.v2", "my-app/web__1.v2:latest"),
        ("nginx:" + "t" * 128, "nginx:" + "t" * 128),
    ],
)
def test_parse_image_normalises_a_valid_reference(text: str, expected: str) -> None:
    assert parse_image(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "NGINX",
        "nginx:",
        "nginx:tag with space",
        "nginx:" + "t" * 129,
        "user:password@registry.example.test/app",
        "nginx@sha256:abc",
        "nginx@md5:" + "a" * 64,
        "has space",
        "-leading/app",
        "app//double",
        "x" * 256,
        "registry.example.test:port/app",
        "reg_istry.example.test/app",
    ],
)
def test_parse_image_refuses_an_invalid_reference_without_quoting_it(text: str) -> None:
    with pytest.raises(ValueError) as refused:
        parse_image(text)

    assert text.strip() not in str(refused.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("web", "web"),
        ("  web  ", "web"),
        ("web_1.a-b", "web_1.a-b"),
        ("", None),
        ("   ", None),
        ("a" * 63, "a" * 63),
        ("a" * 65, "a" * 65),
        ("a" * 128, "a" * 128),
    ],
)
def test_parse_container_name_accepts_valid_names_and_treats_blank_as_unset(
    text: str, expected: str | None
) -> None:
    assert parse_container_name(text) == expected


@pytest.mark.parametrize(
    "text", ["a", "-web", ".web", "web app", "web/app", "a" * 129, HEX_64, "ab" * 32]
)
def test_parse_container_name_refuses_invalid_names_and_a_64_hex_id(text: str) -> None:
    with pytest.raises(ValueError, match="Name"):
        parse_container_name(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1024", KIB),
        ("512m", 512 * MIB),
        ("512M", 512 * MIB),
        ("512 mb", 512 * MIB),
        ("512MiB", 512 * MIB),
        ("1.5g", 3 * GIB // 2),
        ("2G", 2 * GIB),
        ("1t", GIB * KIB),
        ("64k", 64 * KIB),
        ("3b", 3),
    ],
)
def test_parse_memory_reads_sizes_as_multiples_of_1024(text: str, expected: int) -> None:
    assert parse_memory(text, "Memory") == expected


@pytest.mark.parametrize(
    "text", ["", "abc", "-5", "1.5", "1.3b", "10ib", "5x", "1e3", "9" * 30 + "t", "1 2"]
)
def test_parse_memory_refuses_malformed_fractional_and_oversized_values(text: str) -> None:
    with pytest.raises(ValueError, match="Memory"):
        parse_memory(text, "Memory")


def test_parse_memory_reads_minus_one_only_where_unlimited_is_allowed() -> None:
    assert parse_memory("-1", "Swap", allow_unlimited=True) == -1
    with pytest.raises(ValueError, match="Swap"):
        parse_memory("-1", "Swap")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1ms", 1_000_000),
        ("500ms", 500_000_000),
        ("30s", 30 * NS_PER_SECOND),
        ("1m30s", 90 * NS_PER_SECOND),
        ("1h", 3600 * NS_PER_SECOND),
        ("24h", 24 * 3600 * NS_PER_SECOND),
    ],
)
def test_parse_duration_reads_units_into_nanoseconds(text: str, expected: int) -> None:
    assert parse_duration(text, "Interval") == expected


@pytest.mark.parametrize("text", ["", "30", "1d", "0s", "0ms", "25h", "s30", "1.5s", "-1s"])
def test_parse_duration_refuses_malformed_and_out_of_range_values(text: str) -> None:
    with pytest.raises(ValueError, match="Interval"):
        parse_duration(text, "Interval")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("80", (80, 80)), ("1", (1, 1)), ("65535", (65535, 65535)), ("8000-8010", (8000, 8010))],
)
def test_parse_port_range_reads_a_port_or_an_inclusive_range(
    text: str, expected: tuple[int, int]
) -> None:
    assert parse_port_range(text, "Port") == expected


@pytest.mark.parametrize("text", ["", "0", "65536", "9-8", "a", "80-", "-80", "80-90-100", "1 2"])
def test_parse_port_range_refuses_out_of_range_reversed_and_malformed_values(text: str) -> None:
    with pytest.raises(ValueError, match="Port"):
        parse_port_range(text, "Port")


@pytest.mark.parametrize(("text", "expected"), [("0", 0), ("-7", -7), (" 42 ", 42)])
def test_parse_int_reads_a_plain_decimal(text: str, expected: int) -> None:
    assert parse_int(text, "Count") == expected


@pytest.mark.parametrize("text", ["", "1.5", "0x10", "1e3", "9" * 19, "ten"])
def test_parse_int_refuses_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="Count"):
        parse_int(text, "Count")


VALID_BIND = {"mount-0-type": "bind", "mount-0-source": "/srv", "mount-0-target": "/x"}
NETWORKS_MODE = {"network_mode": "networks"}
COMMAND_CHECK = {"healthcheck_mode": "command", "healthcheck_test": "true"}

VALIDATION_FAILURES: list[tuple[str, dict[str, str], str]] = [
    ("image-required", {"image": ""}, "image"),
    ("pull-policy", {"pull": "sometimes"}, "pull"),
    ("name-leading-hyphen", {"name": "-x"}, "name"),
    ("name-64-hex", {"name": HEX_64}, "name"),
    ("command-unclosed-quote", {"command": "echo 'x"}, "command"),
    ("entrypoint-trailing-backslash", {"entrypoint": "run \\"}, "entrypoint"),
    ("command-too-long", {"command": "x " * 2100}, "command"),
    ("working-dir-relative", {"working_dir": "rel"}, "working_dir"),
    ("working-dir-unnormalised", {"working_dir": "/a/../b"}, "working_dir"),
    ("user-with-space", {"user": "bad user"}, "user"),
    ("env-name-with-equals", {"env-0-key": "A=B", "env-0-value": "1"}, "env-0-key"),
    ("env-name-missing", {"env-0-key": "", "env-0-value": "x"}, "env-0-key"),
    ("env-duplicate-name", {"env-0-key": "A", "env-1-key": "A"}, "env-1-key"),
    ("env-value-nul", {"env-0-key": "A", "env-0-value": "x\0y"}, "env-0-value"),
    ("label-key-missing", {"label-0-key": "", "label-0-value": "x"}, "label-0-key"),
    ("label-duplicate-key", {"label-0-key": "a", "label-1-key": "a"}, "label-1-key"),
    ("label-control-character", {"label-0-key": "a\x07b"}, "label-0-key"),
    ("port-out-of-range", {"port-0-container_port": "99999"}, "port-0-container_port"),
    (
        "port-range-length-mismatch",
        {"port-0-container_port": "80", "port-0-host_port": "8000-8001"},
        "port-0-host_port",
    ),
    ("port-host-ip", {"port-0-container_port": "80", "port-0-host_ip": "nope"}, "port-0-host_ip"),
    (
        "port-protocol",
        {"port-0-container_port": "80", "port-0-protocol": "icmp"},
        "port-0-protocol",
    ),
    (
        "port-host-binding-twice",
        {
            "port-0-container_port": "80",
            "port-0-host_port": "8080",
            "port-1-container_port": "81",
            "port-1-host_port": "8080",
        },
        "port-1-host_port",
    ),
    (
        "port-same-binding-twice",
        {"port-0-container_port": "80", "port-1-container_port": "80"},
        "port-1-container_port",
    ),
    ("port-in-host-mode", {"network_mode": "host", "port-0-container_port": "80"}, "ports"),
    ("publish-all-in-none-mode", {"network_mode": "none", "publish_all": "on"}, "publish_all"),
    (
        "mount-relative-bind-source",
        {"mount-0-type": "bind", "mount-0-source": "rel", "mount-0-target": "/x"},
        "mount-0-source",
    ),
    (
        "mount-target-root",
        {"mount-0-type": "volume", "mount-0-source": "v1", "mount-0-target": "/"},
        "mount-0-target",
    ),
    (
        "mount-target-relative",
        {"mount-0-type": "volume", "mount-0-target": "data"},
        "mount-0-target",
    ),
    ("mount-duplicate-target", {"mount-0-target": "/x", "mount-1-target": "/x"}, "mount-1-target"),
    (
        "mount-volume-name",
        {"mount-0-type": "volume", "mount-0-source": "bad name", "mount-0-target": "/x"},
        "mount-0-source",
    ),
    (
        "mount-tmpfs-source",
        {"mount-0-type": "tmpfs", "mount-0-source": "s", "mount-0-target": "/x"},
        "mount-0-source",
    ),
    (
        "mount-tmpfs-size-on-volume",
        {"mount-0-type": "volume", "mount-0-target": "/x", "mount-0-tmpfs_size": "1m"},
        "mount-0-tmpfs_size",
    ),
    (
        "mount-tmpfs-size-malformed",
        {"mount-0-type": "tmpfs", "mount-0-target": "/x", "mount-0-tmpfs_size": "zz"},
        "mount-0-tmpfs_size",
    ),
    ("networks-mode-without-rows", NETWORKS_MODE, "networks"),
    ("network-rows-in-bridge-mode", {"network-0-name": "front"}, "networks"),
    (
        "network-duplicate",
        NETWORKS_MODE | {"network-0-name": "front", "network-1-name": "front"},
        "network-1-name",
    ),
    (
        "network-alias",
        NETWORKS_MODE | {"network-0-name": "front", "network-0-aliases": "bad_alias"},
        "network-0-aliases",
    ),
    (
        "network-ipv4",
        NETWORKS_MODE | {"network-0-name": "front", "network-0-ipv4": "::1"},
        "network-0-ipv4",
    ),
    ("network-name", NETWORKS_MODE | {"network-0-name": "bad name"}, "network-0-name"),
    ("network-mode", {"network_mode": "weird"}, "network_mode"),
    ("restart-policy", {"restart_policy": "sometimes"}, "restart_policy"),
    ("retries-without-on-failure", {"restart_max_retries": "3"}, "restart_max_retries"),
    (
        "retries-over-limit",
        {"restart_policy": "on-failure", "restart_max_retries": "1001"},
        "restart_max_retries",
    ),
    ("cpus-not-a-number", {"cpus": "abc"}, "cpus"),
    ("cpus-zero", {"cpus": "0"}, "cpus"),
    ("cpus-three-decimals", {"cpus": "0.001"}, "cpus"),
    ("cpu-shares-below-two", {"cpu_shares": "1"}, "cpu_shares"),
    ("memory-below-six-mib", {"memory": "5m"}, "memory"),
    ("memory-unit", {"memory": "5x"}, "memory"),
    ("swap-without-memory", {"memory_swap": "1g"}, "memory_swap"),
    ("swap-below-memory", {"memory": "2g", "memory_swap": "1g"}, "memory_swap"),
    ("pids-zero", {"pids_limit": "0"}, "pids_limit"),
    ("shm-zero", {"shm_size": "0"}, "shm_size"),
    ("capability-unknown", {"cap_add": "NOT_A_CAP"}, "cap_add"),
    ("capability-dropped-unknown", {"cap_drop": "NOT_A_CAP"}, "cap_drop"),
    ("device-relative", {"device-0-host_path": "dev/x"}, "device-0-host_path"),
    (
        "device-permissions",
        {"device-0-host_path": "/dev/x", "device-0-permissions": "rwx"},
        "device-0-permissions",
    ),
    ("dns-server", {"dns_servers": "nope"}, "dns_servers"),
    ("dns-search", {"dns_search": "bad_domain!"}, "dns_search"),
    ("dns-option", {"dns_options": "bad option!"}, "dns_options"),
    ("extra-host-name", {"host-0-name": "bad_name", "host-0-address": "1.2.3.4"}, "host-0-name"),
    ("extra-host-address", {"host-0-name": "ok", "host-0-address": "nope"}, "host-0-address"),
    ("health-mode", {"healthcheck_mode": "weird"}, "healthcheck_mode"),
    ("health-command-missing", {"healthcheck_mode": "command"}, "healthcheck_test"),
    ("health-command-in-image-mode", {"healthcheck_test": "true"}, "healthcheck_test"),
    (
        "health-interval-unit",
        COMMAND_CHECK | {"healthcheck_interval": "5x"},
        "healthcheck_interval",
    ),
    (
        "health-interval-over-24h",
        COMMAND_CHECK | {"healthcheck_interval": "25h"},
        "healthcheck_interval",
    ),
    ("health-timeout", COMMAND_CHECK | {"healthcheck_timeout": "0s"}, "healthcheck_timeout"),
    (
        "health-start-period",
        COMMAND_CHECK | {"healthcheck_start_period": "x"},
        "healthcheck_start_period",
    ),
    (
        "health-start-interval",
        COMMAND_CHECK | {"healthcheck_start_interval": "x"},
        "healthcheck_start_interval",
    ),
    ("health-retries", COMMAND_CHECK | {"healthcheck_retries": "101"}, "healthcheck_retries"),
    ("log-options-without-driver", {"logopt-0-key": "max-size"}, "log_driver"),
    ("log-driver-syntax", {"log_driver": "Json File"}, "log_driver"),
    (
        "log-option-duplicate",
        {"log_driver": "local", "logopt-0-key": "a", "logopt-1-key": "a"},
        "logopt-1-key",
    ),
    ("log-option-key-case", {"log_driver": "local", "logopt-0-key": "Max"}, "logopt-0-key"),
    ("runtime-syntax", {"runtime": "bad runtime"}, "runtime"),
]


@pytest.mark.parametrize(
    ("fields", "key"),
    [pytest.param(fields, key, id=name) for name, fields, key in VALIDATION_FAILURES],
)
def test_each_validation_failure_is_keyed_to_the_field_it_belongs_beside(
    fields: dict[str, str], key: str
) -> None:
    parsed = parse_form(form(**fields))

    assert parsed.spec is None
    assert set(parsed.state.errors) == {key}
    assert parsed.state.errors[key]


def test_both_fields_of_a_capability_in_added_and_dropped_carry_the_error() -> None:
    assert set(errors_of(cap_add="NET_ADMIN", cap_drop="cap_net_admin")) == {"cap_add", "cap_drop"}


@pytest.mark.parametrize(
    "field_name",
    [
        "image",
        "name",
        "command",
        "working_dir",
        "user",
        "cpus",
        "cpu_shares",
        "memory",
        "pids_limit",
        "shm_size",
        "cap_add",
        "dns_servers",
        "dns_search",
        "healthcheck_interval",
        "log_driver",
        "runtime",
        "restart_policy",
        "network_mode",
        "pull",
    ],
)
def test_an_error_message_never_quotes_the_rejected_input(field_name: str) -> None:
    errors = errors_of(**{field_name: SENTINEL})

    assert field_name in errors
    assert "SENTINEL" not in " ".join(errors.values())


def test_a_failed_parse_keeps_every_submitted_value_beside_every_error() -> None:
    submitted = form(
        name="-x",
        memory="5m",
        command="serve --fast",
        **{
            "env-0-key": "A",
            "env-0-value": "kept",
            "port-0-container_port": "99999",
            "mount-0-type": "volume",
            "mount-0-source": "data",
            "mount-0-target": "/data",
        },
    )

    state = parse_form(submitted).state

    assert set(state.errors) == {"name", "memory", "port-0-container_port"}
    assert all(state.get(name) == value for name, value in submitted.items())


@pytest.mark.parametrize(
    "key",
    [
        STACK_LABEL,
        SWARM_LABEL,
        STACK_LABEL.upper(),
        "Com.Docker.Compose.Project",
        f"  {SWARM_LABEL}  ",
        f"\t{STACK_LABEL}\n",
        SWARM_LABEL.title(),
    ],
)
def test_a_stack_label_is_refused_on_its_row_whatever_its_case_or_whitespace(key: str) -> None:
    parsed = parse_form(form(**{"label-2-key": key, "label-2-value": "shop"}))

    assert parsed.spec is None
    assert set(parsed.state.errors) == {"label-2-key"}


def test_a_look_alike_label_key_is_accepted() -> None:
    spec = spec_from_form(form(**{"label-0-key": f"{STACK_LABEL}.extra", "label-0-value": "x"}))

    assert [row.key for row in spec.labels] == [f"{STACK_LABEL}.extra"]


@pytest.mark.parametrize(
    ("prefix", "value_name", "group", "limit"),
    [("label", "label", "labels", LABEL_LIMIT), ("env", "env", "env", LABEL_LIMIT)],
)
def test_total_keys_and_values_of_a_group_are_capped_at_64_kib_inclusive(
    prefix: str, value_name: str, group: str, limit: int
) -> None:
    at_limit = {f"{prefix}-0-key": "k", f"{prefix}-0-value": "v" * (limit - 1)}
    over = {f"{prefix}-0-key": "k", f"{prefix}-0-value": "v" * limit}

    assert errors_of(**at_limit) == {}
    assert set(errors_of(**over)) == {group}


@pytest.mark.parametrize(
    ("prefix", "group"),
    [
        ("env", "env"),
        ("label", "labels"),
        ("port", "ports"),
        ("mount", "mounts"),
        ("network", "networks"),
        ("device", "devices"),
        ("host", "extra_hosts"),
        ("logopt", "log_options"),
    ],
)
def test_a_row_at_or_beyond_its_group_cap_is_a_group_error(prefix: str, group: str) -> None:
    cap = ROW_CAPS[group]
    beyond = {f"{prefix}-{cap}-{ROW_ANCHOR[prefix]}": "x"}

    assert set(errors_of(**beyond)) == {group}


def test_exactly_the_cap_of_rows_is_accepted() -> None:
    assert errors_of(**numbered("env", ROW_CAPS["env"])) == {}
    assert errors_of(**numbered("label", ROW_CAPS["labels"])) == {}


def test_more_rows_than_the_cap_is_a_group_error() -> None:
    assert set(errors_of(**numbered("env", ROW_CAPS["env"] + 1))) == {"env"}
    assert set(errors_of(**numbered("label", ROW_CAPS["labels"] + 1))) == {"labels"}


def test_fully_blank_rows_and_defaulted_selects_alone_are_ignored() -> None:
    spec = spec_from_form(
        form(
            **{
                "env-0-key": "",
                "env-0-value": "  ",
                "port-0-protocol": "tcp",
                "mount-0-type": "volume",
                "device-0-permissions": "rwm",
            }
        )
    )

    assert (spec.env, spec.ports, spec.mounts, spec.devices) == ((), (), (), ())


def test_row_indices_need_not_be_contiguous_and_errors_name_the_submitted_row() -> None:
    parsed = parse_form(form(**{"env-1-key": "A", "env-7-key": "B=C", "env-9-key": "D"}))

    assert set(parsed.state.errors) == {"env-7-key"}


def test_a_valid_sparse_form_keeps_its_rows_in_index_order() -> None:
    spec = spec_from_form(form(**{"env-9-key": "LAST", "env-2-key": "FIRST"}))

    assert [row.key for row in spec.env] == ["FIRST", "LAST"]


@pytest.mark.parametrize("text", ["on", "ON", "true", "1", "yes", " Yes "])
def test_a_checkbox_is_ticked_by_any_of_the_usual_truthy_values(text: str) -> None:
    assert spec_from_form(form(init=text)).init is True


@pytest.mark.parametrize("text", ["", "off", "0", "no", "false"])
def test_a_checkbox_is_unticked_by_anything_else(text: str) -> None:
    assert spec_from_form(form(init=text)).init is False


def test_a_missing_start_checkbox_parses_as_not_started() -> None:
    assert spec_from_form(form()).start is False
    assert spec_from_form(form(start="on")).start is True


def test_fields_the_form_does_not_define_are_ignored() -> None:
    fields = form(
        intent="review",
        rows="env",
        csrf_token="t",
        **{"from": "abc", "unknown": "x", "env-0-bogus": "x", "privilege": "on"},
    )

    assert spec_from_form(fields).model_dump() == spec_from_form(form()).model_dump()


def test_capabilities_and_dns_lists_accept_commas_and_spaces_and_normalise_names() -> None:
    spec = spec_from_form(
        form(cap_add="net_admin, CAP_SYS_TIME  net_admin", dns_servers="1.1.1.1,8.8.8.8 9.9.9.9")
    )

    assert spec.cap_add == ("NET_ADMIN", "SYS_TIME")
    assert spec.dns.servers == ("1.1.1.1", "8.8.8.8", "9.9.9.9")


def test_an_environment_value_keeps_its_surrounding_whitespace() -> None:
    spec = spec_from_form(form(**{"env-0-key": "A", "env-0-value": "  padded  "}))

    assert spec.env[0].value == "  padded  "


def test_spec_from_form_raises_with_the_state_the_form_re_renders_from() -> None:
    with pytest.raises(SpecInvalid) as refused:
        spec_from_form(form(memory="5m"))

    assert refused.value.fields == {"memory": refused.value.state.errors["memory"]}
    assert refused.value.state.get("memory") == "5m"


def test_the_row_slots_name_the_form_rows_the_validated_rows_came_from() -> None:
    spec = spec_from_form(
        form(**{"mount-3-target": "/a", "mount-5-target": "/b", "network_mode": "bridge"})
    )

    assert row_slots(spec)["mounts"] == (3, 5)


FORMS: dict[str, dict[str, str]] = {
    "image": {"image": "ghcr.io/org/app:1.2", "name": "web", "pull": "always"},
    "command": {
        "command": "serve --port 80 'a b'",
        "entrypoint": "/bin/tini --",
        "working_dir": "/srv",
        "user": "1000:1000",
    },
    "env": {"env-0-key": "A", "env-0-value": "1", "env-1-key": "B", "env-1-value": "x=y"},
    "labels": {"label-0-key": "tier", "label-0-value": "front"},
    "ports": {
        "port-0-container_port": "80-81",
        "port-0-host_port": "8080-8081",
        "port-0-host_ip": "127.0.0.1",
        "port-1-container_port": "53",
        "port-1-protocol": "udp",
        "publish_all": "on",
    },
    "mounts": {
        "mount-0-type": "volume",
        "mount-0-source": "data",
        "mount-0-target": "/data",
        "mount-1-type": "volume",
        "mount-1-target": "/anon",
        "mount-2-type": "tmpfs",
        "mount-2-target": "/tmp",
        "mount-2-tmpfs_size": "64m",
        "mount-2-read_only": "on",
        "mount-3-type": "bind",
        "mount-3-source": "/srv/x",
        "mount-3-target": "/x",
    },
    "networks": {
        "network_mode": "networks",
        "network-0-name": "front",
        "network-0-aliases": "web, www",
        "network-0-ipv4": "172.20.0.5",
        "network-1-name": "back",
    },
    "restart": {"restart_policy": "on-failure", "restart_max_retries": "3"},
    "resources": {
        "cpus": "1.5",
        "cpu_shares": "512",
        "memory": "512m",
        "memory_swap": "1g",
        "pids_limit": "100",
        "shm_size": "64m",
    },
    "capabilities": {"cap_add": "net_admin CAP_SYS_TIME", "cap_drop": "ALL"},
    "devices": {
        "device-0-host_path": "/dev/fuse",
        "device-0-container_path": "/dev/fuse2",
        "device-0-permissions": "rw",
    },
    "dns": {
        "dns_servers": "1.1.1.1, 8.8.8.8",
        "dns_search": "example.test",
        "dns_options": "ndots:2",
        "host-0-name": "db",
        "host-0-address": "10.0.0.5",
        "host-1-name": "gw",
        "host-1-address": "host-gateway",
    },
    "healthcheck": {
        "healthcheck_mode": "command",
        "healthcheck_test": "curl -f localhost",
        "healthcheck_interval": "30s",
        "healthcheck_timeout": "5s",
        "healthcheck_start_period": "1m",
        "healthcheck_start_interval": "2s",
        "healthcheck_retries": "3",
    },
    "health-disabled": {"healthcheck_mode": "none"},
    "logging": {"log_driver": "json-file", "logopt-0-key": "max-size", "logopt-0-value": "10m"},
    "runtime": {
        "runtime": "nvidia",
        "init": "on",
        "privileged": "on",
        "pid_host": "on",
        "ipc_host": "on",
    },
}

MIB64 = 64 * MIB

BODY_CASES: list[tuple[str, str, Any]] = [
    ("image", "Image", "ghcr.io/org/app:1.2"),
    ("command", "Cmd", ["serve", "--port", "80", "a b"]),
    ("command", "Entrypoint", ["/bin/tini", "--"]),
    ("command", "WorkingDir", "/srv"),
    ("command", "User", "1000:1000"),
    ("env", "Env", ["A=1", "B=x=y"]),
    ("labels", "Labels", {"tier": "front"}),
    ("ports", "ExposedPorts", {"80/tcp": {}, "81/tcp": {}, "53/udp": {}}),
    (
        "ports",
        "HostConfig.PortBindings",
        {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}],
            "81/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8081"}],
            "53/udp": [{"HostIp": "", "HostPort": ""}],
        },
    ),
    ("ports", "HostConfig.PublishAllPorts", True),
    (
        "mounts",
        "HostConfig.Mounts",
        [
            {"Type": "volume", "Target": "/data", "Source": "data"},
            {"Type": "volume", "Target": "/anon"},
            {
                "Type": "tmpfs",
                "Target": "/tmp",
                "ReadOnly": True,
                "TmpfsOptions": {"SizeBytes": MIB64},
            },
            {"Type": "bind", "Target": "/x", "Source": "/srv/x"},
        ],
    ),
    ("restart", "HostConfig.RestartPolicy", {"Name": "on-failure", "MaximumRetryCount": 3}),
    ("resources", "HostConfig.NanoCpus", 1_500_000_000),
    ("resources", "HostConfig.CpuShares", 512),
    ("resources", "HostConfig.Memory", 512 * MIB),
    ("resources", "HostConfig.MemorySwap", GIB),
    ("resources", "HostConfig.PidsLimit", 100),
    ("resources", "HostConfig.ShmSize", MIB64),
    ("capabilities", "HostConfig.CapAdd", ["NET_ADMIN", "SYS_TIME"]),
    ("capabilities", "HostConfig.CapDrop", ["ALL"]),
    (
        "devices",
        "HostConfig.Devices",
        [
            {
                "PathOnHost": "/dev/fuse",
                "PathInContainer": "/dev/fuse2",
                "CgroupPermissions": "rw",
            }
        ],
    ),
    ("dns", "HostConfig.Dns", ["1.1.1.1", "8.8.8.8"]),
    ("dns", "HostConfig.DnsSearch", ["example.test"]),
    ("dns", "HostConfig.DnsOptions", ["ndots:2"]),
    ("dns", "HostConfig.ExtraHosts", ["db:10.0.0.5", "gw:host-gateway"]),
    (
        "healthcheck",
        "Healthcheck",
        {
            "Test": ["CMD-SHELL", "curl -f localhost"],
            "Interval": 30 * NS_PER_SECOND,
            "Timeout": 5 * NS_PER_SECOND,
            "StartPeriod": 60 * NS_PER_SECOND,
            "StartInterval": 2 * NS_PER_SECOND,
            "Retries": 3,
        },
    ),
    ("health-disabled", "Healthcheck", {"Test": ["NONE"]}),
    (
        "logging",
        "HostConfig.LogConfig",
        {"Type": "json-file", "Config": {"max-size": "10m"}},
    ),
    ("runtime", "HostConfig.Runtime", "nvidia"),
    ("runtime", "HostConfig.Init", True),
    ("runtime", "HostConfig.Privileged", True),
    ("runtime", "HostConfig.PidMode", "host"),
    ("runtime", "HostConfig.IpcMode", "host"),
]


@pytest.mark.parametrize(
    ("group", "path", "expected"),
    [
        pytest.param(group, path, expected, id=f"{group}-{path}")
        for group, path, expected in BODY_CASES
    ],
)
def test_the_engine_body_carries_each_option_group_at_its_engine_path(
    group: str, path: str, expected: Any
) -> None:
    body = to_engine_body(spec_from_form(form(**FORMS[group])))

    assert at(body, path) == expected


def test_the_engine_body_of_a_bare_image_sets_only_the_image_and_the_default_network_mode() -> None:
    body = to_engine_body(spec_from_form(form()))

    assert body == {"Image": "nginx:latest", "HostConfig": {"NetworkMode": "bridge"}}


def test_the_engine_body_never_carries_the_name_or_the_create_time_choices() -> None:
    body = to_engine_body(spec_from_form(form(name="web", pull="always", start="on")))

    assert "web" not in str(body)
    assert set(body) == {"Image", "HostConfig"}


def test_a_bind_mount_is_expressed_as_a_mounts_entry_and_binds_is_never_emitted() -> None:
    body = to_engine_body(spec_from_form(form(**FORMS["mounts"])))

    assert "Binds" not in body["HostConfig"]


def test_networks_are_keyed_by_the_resolved_id_and_the_first_is_the_network_mode() -> None:
    spec = spec_from_form(form(**FORMS["networks"]))

    body = to_engine_body(spec, network_ids={"front": FRONT_ID, "back": BACK_ID})

    assert body["HostConfig"]["NetworkMode"] == FRONT_ID
    assert body["NetworkingConfig"]["EndpointsConfig"] == {
        FRONT_ID: {"Aliases": ["web", "www"], "IPAMConfig": {"IPv4Address": "172.20.0.5"}},
        BACK_ID: {},
    }


def test_an_unresolved_network_is_keyed_by_the_name_given() -> None:
    body = to_engine_body(spec_from_form(form(**FORMS["networks"])))

    assert list(body["NetworkingConfig"]["EndpointsConfig"]) == ["front", "back"]


@pytest.mark.parametrize("mode", ["bridge", "none", "host"])
def test_a_plain_network_mode_is_passed_through_with_no_endpoints(mode: str) -> None:
    body = to_engine_body(spec_from_form(form(network_mode=mode)))

    assert body["HostConfig"]["NetworkMode"] == mode
    assert "NetworkingConfig" not in body


def test_a_labels_row_reaches_the_body_through_the_submitted_labels_guard() -> None:
    body = to_engine_body(spec_from_form(form(**{"label-0-key": "a", "label-0-value": "b"})))

    assert body["Labels"] == {"a": "b"}


@pytest.mark.parametrize("group", sorted(FORMS))
def test_a_spec_survives_prefill_and_reparse_unchanged(group: str) -> None:
    spec = spec_from_form(form(**FORMS[group]))

    again = spec_from_form(dict(FormState.from_spec(spec).values))

    assert again.model_dump() == spec.model_dump()


def test_a_blank_prefill_ticks_start_and_defaults_the_pull_policy() -> None:
    state = FormState.from_spec(ContainerSpec(image="nginx"))

    assert state.checked("start")
    assert state.get("pull") == "missing"
    assert state.get("network_mode") == "bridge"


def test_add_rows_gives_three_more_slots_and_one_more_for_the_short_groups() -> None:
    state = FormState.from_form({})

    grown = state.add_rows("env").add_rows("networks")

    assert grown.row_indices("env") == range(6)
    assert grown.row_indices("networks") == range(2)


def test_add_rows_stops_at_the_group_cap() -> None:
    state = reduce(
        lambda grown, _: grown.add_rows("devices"),
        range(ROW_CAPS["devices"]),
        FormState.from_form({}),
    )

    assert state.row_indices("devices") == range(ROW_CAPS["devices"])


def test_a_form_with_values_in_later_rows_renders_those_rows_and_three_spare() -> None:
    state = FormState.from_form({"env-4-key": "A", "env-4-value": "1"})

    assert state.rows["env"] == 5
    assert state.row_indices("env") == range(8)


def test_attaching_errors_keeps_the_rows_that_carry_them_visible() -> None:
    state = FormState.from_form({}).with_errors({"label-11-key": "bad", "memory": "bad"})

    assert state.row_indices("labels").stop > 11
    assert state.errors == {"label-11-key": "bad", "memory": "bad"}
    assert state.without_errors().errors == {}


def test_the_hidden_fields_carry_every_non_empty_value_and_no_empty_one() -> None:
    state = FormState.from_form({"image": "nginx", "name": "", "init": "on", "env-0-key": "A"})

    assert state.hidden_fields() == [("image", "nginx"), ("init", "on"), ("env-0-key", "A")]


def test_from_form_does_not_validate_what_it_collects() -> None:
    state = FormState.from_form({"memory": "not a size", "init": "yes"})

    assert state.get("memory") == "not a size"
    assert state.checked("init")
    assert state.errors == {}
