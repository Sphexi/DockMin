"""Tests for app/compose_cli/environment.py."""

from pathlib import Path

import pytest

from app.compose_cli.environment import (
    DISCLOSING_VARIABLES,
    TARGET_VARIABLES,
    build_environment,
    is_reserved,
)

WORKDIR = Path("/work/run-1")
CONSTRUCTED_KEYS = {
    "PATH",
    "HOME",
    "DOCKER_CONFIG",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
    "NO_COLOR",
    "DOCKER_CLI_HINTS",
    "COMPOSE_MENU",
}
TLS_TARGET = {
    "DOCKER_HOST": "tcp://203.0.113.9:2376",
    "DOCKER_TLS_VERIFY": "1",
    "DOCKER_CERT_PATH": "/work/run-1/tls",
}
RESERVED_NAMES = [
    "PATH",
    "path",
    "Path",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LANGUAGE",
    "TZ",
    "NO_COLOR",
    "PWD",
    "SHELL",
    "USER",
    "LOGNAME",
    "DOCKER_HOST",
    "docker_host",
    "Docker_Host",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "COMPOSE_PROJECT_NAME",
    "compose_file",
    "BUILDKIT_PROGRESS",
    "BUILDX_BUILDER",
    "DOCKMIN_SECRET_KEY",
    "LC_ALL",
    "lc_messages",
    "LD_PRELOAD",
    "ld_library_path",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "SSL_CERT_FILE",
    "ssl_cert_dir",
    "XDG_CONFIG_HOME",
    "GODEBUG",
    "godebug",
    "GOGC",
    "GOMAXPROCS",
    "GOMEMLIMIT",
    "GOTRACEBACK",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "ALL_PROXY",
    "FTP_Proxy",
    "MY_PROXY",
]
PERMITTED_NAMES = [
    "APP_PORT",
    "lower_case",
    "_leading_underscore",
    "GOOD",
    "DOCKERFILE_PATH",
    "COMPOSER_HOME",
    "LCD",
    "LDAP_URL",
    "PROXY_HOST",
    "USERNAME",
    "A" * 128,
]
MALFORMED_NAMES = ["", "1ABC", "A B", "A-B", "A=B", "A.B", "A\n", "\xe4bc", "A" * 129]
SERVER_SENTINEL = "must-not-reach-compose"


def test_the_environment_holds_exactly_the_constructed_keys() -> None:
    environment = build_environment(WORKDIR, {}, {})

    assert set(environment) == CONSTRUCTED_KEYS


def test_the_environment_points_home_config_and_temp_into_the_work_directory() -> None:
    environment = build_environment(WORKDIR, {}, {})

    assert environment["HOME"] == str(WORKDIR / "home")
    assert environment["DOCKER_CONFIG"] == str(WORKDIR / "config")
    assert environment["TMPDIR"] == str(WORKDIR / "tmp")
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_the_environment_keeps_compose_non_interactive_and_unstyled() -> None:
    environment = build_environment(WORKDIR, {}, {})

    assert environment["NO_COLOR"] == "1"
    assert environment["COMPOSE_MENU"] == "false"
    assert environment["DOCKER_CLI_HINTS"] == "false"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert environment["TZ"] == "UTC"


def test_the_environment_takes_only_the_connection_variables_from_the_target() -> None:
    target_env = TLS_TARGET | {"SSL_CERT_FILE": "/etc/ssl/ca.pem", "DOCKER_CONTEXT": "other"}

    environment = build_environment(WORKDIR, target_env, {})

    assert {k: environment[k] for k in TARGET_VARIABLES} == TLS_TARGET
    assert set(environment) == CONSTRUCTED_KEYS | TARGET_VARIABLES
    assert "SSL_CERT_FILE" not in environment
    assert "SSL_CERT_DIR" not in environment


def test_the_server_process_environment_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SERVER_SENTINEL", "HTTPS_PROXY", "AWS_SECRET_ACCESS_KEY", "SSL_CERT_FILE"):
        monkeypatch.setenv(name, SERVER_SENTINEL)

    environment = build_environment(WORKDIR, {}, {"STACK_VALUE": "kept"})

    assert SERVER_SENTINEL not in environment.values()
    assert set(environment) == CONSTRUCTED_KEYS | {"STACK_VALUE"}


def test_stack_variables_are_passed_verbatim() -> None:
    awkward = "a;touch x;$(id)`id`|&'\"#\n\xe4 ☃"

    environment = build_environment(WORKDIR, {}, {"DB_PASSWORD": awkward, "EMPTY": ""})

    assert environment["DB_PASSWORD"] == awkward
    assert environment["EMPTY"] == ""


@pytest.mark.parametrize("name", RESERVED_NAMES)
def test_a_reserved_name_is_reported_and_refused_in_any_case(name: str) -> None:
    assert is_reserved(name) is True
    with pytest.raises(ValueError, match="reserved or malformed"):
        build_environment(WORKDIR, {}, {name: "v"})


@pytest.mark.parametrize("name", PERMITTED_NAMES)
def test_a_name_that_only_resembles_a_reserved_one_is_permitted(name: str) -> None:
    assert is_reserved(name) is False
    assert build_environment(WORKDIR, {}, {name: "v"})[name] == "v"


@pytest.mark.parametrize("name", MALFORMED_NAMES)
def test_a_malformed_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="reserved or malformed"):
        build_environment(WORKDIR, {}, {name: "v"})


def test_a_value_with_a_nul_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="NUL") as refusal:
        build_environment(WORKDIR, {}, {"TOKEN": "hunter2\0tail"})

    assert "hunter2" not in str(refusal.value)
    assert "TOKEN" not in str(refusal.value)


def test_a_target_variable_cannot_be_overridden_by_a_stack_variable() -> None:
    with pytest.raises(ValueError, match="reserved or malformed"):
        build_environment(WORKDIR, TLS_TARGET, {"DOCKER_HOST": "tcp://203.0.113.66:2375"})


def test_the_disclosing_variables_are_exactly_those_set_from_the_target_and_the_run_directory() -> (
    None
):
    environment = build_environment(WORKDIR, TLS_TARGET, {})

    holding_a_path_or_address = {
        name for name, value in environment.items() if name in TLS_TARGET or str(WORKDIR) in value
    }
    assert holding_a_path_or_address == DISCLOSING_VARIABLES


def test_generated_sources_are_passed_verbatim_beside_the_variables() -> None:
    content = "a=$HOME ${X} $$\n"

    environment = build_environment(
        WORKDIR, {}, {"STACK_VALUE": "kept"}, {"DOCKMIN_SOURCE_1": content}
    )

    assert environment["DOCKMIN_SOURCE_1"] == content
    assert environment["STACK_VALUE"] == "kept"


@pytest.mark.parametrize("name", ["DOCKMIN_SOURCE_", "DOCKMIN_SOURCE_x", "DOCKMIN_OTHER", "PATH"])
def test_a_source_outside_the_generated_names_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="generated"):
        build_environment(WORKDIR, {}, {}, {name: "v"})


def test_a_source_with_a_nul_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="NUL") as refusal:
        build_environment(WORKDIR, {}, {}, {"DOCKMIN_SOURCE_1": "hunter2\0tail"})

    assert "hunter2" not in str(refusal.value)


def test_a_stack_variable_cannot_take_a_generated_source_name() -> None:
    with pytest.raises(ValueError, match="reserved or malformed"):
        build_environment(WORKDIR, {}, {"DOCKMIN_SOURCE_1": "v"})
