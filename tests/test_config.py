"""Tests for app/config.py."""

import base64
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import (
    ENV_PREFIX,
    KEY_BYTES,
    MANAGED_KEY_FILENAME,
    PREVIOUS_SECRET_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_DIR_VAR,
    SECRET_KEY_FILE_VAR,
    SECRET_KEY_VAR,
    TLS_SELF_SIGNED_VAR,
    ConfigError,
    load_settings,
)
from tests.conftest import DATA_DIR_VAR
from tests.support import KEY_MATERIAL, VALID_KEY

DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_SESSION_LIFETIME = timedelta(hours=12)
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_KEY_DIR = Path("/key")
MAX_PORT = 65535

SHORT_KEY = base64.b64encode(bytes(KEY_BYTES - 1)).decode("ascii")
LONG_KEY = base64.b64encode(bytes(KEY_BYTES + 1)).decode("ascii")
NOT_BASE64 = "%%%not-base64%%%"
PREVIOUS_KEY_MATERIAL = bytes(reversed(KEY_MATERIAL))
PREVIOUS_VALID_KEY = base64.b64encode(PREVIOUS_KEY_MATERIAL).decode("ascii")


def config_error_message(environ: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as caught:
        load_settings(environ)
    return str(caught.value)


def test_load_settings_with_only_a_key_applies_defaults() -> None:
    environ = {SECRET_KEY_VAR: VALID_KEY}

    settings = load_settings(environ)

    assert settings.port == DEFAULT_PORT
    assert settings.log_level == DEFAULT_LOG_LEVEL
    assert settings.session_lifetime == DEFAULT_SESSION_LIFETIME
    assert settings.data_dir == DEFAULT_DATA_DIR
    assert settings.encryption_key == KEY_MATERIAL


def test_load_settings_parses_and_normalises_provided_values(env: dict[str, str]) -> None:
    environ = env | {
        f"{ENV_PREFIX}PORT": "9000",
        f"{ENV_PREFIX}LOG_LEVEL": " debug ",
        f"{ENV_PREFIX}SESSION_LIFETIME": "PT1H",
    }

    settings = load_settings(environ)

    assert settings.port == 9000
    assert settings.log_level == "DEBUG"
    assert settings.session_lifetime == timedelta(hours=1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3600", timedelta(hours=1)), ("PT1H", timedelta(hours=1))],
)
def test_load_settings_reads_session_lifetime_as_seconds_or_iso_duration(
    env: dict[str, str], value: str, expected: timedelta
) -> None:
    settings = load_settings(env | {f"{ENV_PREFIX}SESSION_LIFETIME": value})

    assert settings.session_lifetime == expected


@pytest.mark.parametrize("port", ["1", str(MAX_PORT)])
def test_load_settings_accepts_ports_at_the_range_boundaries(
    env: dict[str, str], port: str
) -> None:
    settings = load_settings(env | {f"{ENV_PREFIX}PORT": port})

    assert settings.port == int(port)


def test_load_settings_reads_key_from_file_and_strips_trailing_newline(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(VALID_KEY + "\n", encoding="utf-8")

    settings = load_settings({SECRET_KEY_FILE_VAR: str(key_file)})

    assert settings.encryption_key == KEY_MATERIAL


def test_load_settings_treats_a_blank_key_variable_as_unset(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(VALID_KEY, encoding="utf-8")

    settings = load_settings({SECRET_KEY_VAR: "", SECRET_KEY_FILE_VAR: str(key_file)})

    assert settings.encryption_key == KEY_MATERIAL


def test_load_settings_ignores_the_process_environment(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "1234")

    settings = load_settings(env)

    assert settings.port == DEFAULT_PORT


def test_settings_repr_does_not_contain_the_key(env: dict[str, str]) -> None:
    settings = load_settings(env)

    assert VALID_KEY not in repr(settings)
    assert VALID_KEY not in str(settings)


def test_load_settings_without_any_key_generates_one_in_the_default_key_directory() -> None:
    settings = load_settings({})

    assert settings.key_source == "generated"
    assert settings.encryption_key is None
    assert settings.secret_key_dir == DEFAULT_KEY_DIR
    assert settings.managed_key_file == DEFAULT_KEY_DIR / MANAGED_KEY_FILENAME


def test_load_settings_with_both_key_sources_names_both_without_values(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(VALID_KEY, encoding="utf-8")

    message = config_error_message({SECRET_KEY_VAR: VALID_KEY, SECRET_KEY_FILE_VAR: str(key_file)})

    assert SECRET_KEY_VAR in message
    assert SECRET_KEY_FILE_VAR in message
    assert VALID_KEY not in message
    assert str(key_file) not in message


@pytest.mark.parametrize("bad_key", [SHORT_KEY, LONG_KEY, NOT_BASE64], ids=["short", "long", "b64"])
def test_load_settings_rejects_a_malformed_key_without_echoing_it(bad_key: str) -> None:
    message = config_error_message({SECRET_KEY_VAR: bad_key})

    assert SECRET_KEY_VAR in message
    assert bad_key not in message


def test_load_settings_rejects_a_malformed_key_file_naming_the_file_variable(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(SHORT_KEY, encoding="utf-8")

    message = config_error_message({SECRET_KEY_FILE_VAR: str(key_file)})

    assert SECRET_KEY_FILE_VAR in message
    assert SHORT_KEY not in message


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (f"{ENV_PREFIX}PORT", "0"),
        (f"{ENV_PREFIX}PORT", str(MAX_PORT + 1)),
        (f"{ENV_PREFIX}PORT", "not-a-port"),
        (f"{ENV_PREFIX}LOG_LEVEL", "verbose"),
        (f"{ENV_PREFIX}SESSION_LIFETIME", "PT0S"),
    ],
)
def test_load_settings_rejects_an_invalid_value_naming_the_variable(
    env: dict[str, str], variable: str, value: str
) -> None:
    message = config_error_message(env | {variable: value})

    assert variable in message
    assert value not in message


def test_load_settings_with_a_missing_key_file_names_the_file_variable(tmp_path: Path) -> None:
    missing = tmp_path / "absent"

    message = config_error_message({SECRET_KEY_FILE_VAR: str(missing)})

    assert SECRET_KEY_FILE_VAR in message
    assert str(missing) not in message


def test_load_settings_with_a_directory_as_key_file_names_the_file_variable(
    env: dict[str, str],
) -> None:
    message = config_error_message({SECRET_KEY_FILE_VAR: env[DATA_DIR_VAR]})

    assert SECRET_KEY_FILE_VAR in message
    assert env[DATA_DIR_VAR] not in message


def test_load_settings_with_a_non_text_key_file_names_the_file_variable(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_bytes(b"\xff\xfe\x00")

    message = config_error_message({SECRET_KEY_FILE_VAR: str(key_file)})

    assert SECRET_KEY_FILE_VAR in message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("true", True, id="true"),
        pytest.param("false", False, id="false"),
        pytest.param("", False, id="blank"),
        pytest.param("  ", False, id="spaces"),
    ],
)
def test_development_checks_follow_their_variable(value: str, expected: bool) -> None:
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY, f"{ENV_PREFIX}DEVELOPMENT_CHECKS": value})

    assert settings.development_checks is expected


def test_development_checks_are_off_unless_asked_for() -> None:
    assert load_settings({SECRET_KEY_VAR: VALID_KEY}).development_checks is False


RETENTION_VAR = f"{ENV_PREFIX}ACTIVITY_RETENTION_DAYS"
MIN_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 3650


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, None, id="absent"),
        pytest.param("", None, id="blank"),
        pytest.param("  ", None, id="spaces"),
        pytest.param(str(MIN_RETENTION_DAYS), MIN_RETENTION_DAYS, id="lower-bound"),
        pytest.param("30", 30, id="typical"),
        pytest.param(str(MAX_RETENTION_DAYS), MAX_RETENTION_DAYS, id="upper-bound"),
    ],
)
def test_activity_retention_is_off_unless_a_number_of_days_is_given(
    value: str | None, expected: int | None
) -> None:
    environ = {SECRET_KEY_VAR: VALID_KEY} | ({} if value is None else {RETENTION_VAR: value})

    assert load_settings(environ).activity_retention_days == expected


@pytest.mark.parametrize(
    "value", [str(MIN_RETENTION_DAYS - 1), str(MAX_RETENTION_DAYS + 1), "0", "-1", "a week", "1.5"]
)
def test_activity_retention_outside_7_to_3650_days_is_refused_naming_the_variable(
    env: dict[str, str], value: str
) -> None:
    message = config_error_message(env | {RETENTION_VAR: value})

    assert RETENTION_VAR in message
    assert value not in message


TLS_FILES = {
    "DOCKMIN_TLS_CERT_FILE": "/run/secrets/cert",
    "DOCKMIN_TLS_KEY_FILE": "/run/secrets/key",
}
NO_TLS_VARIABLES = {
    "DOCKMIN_HTTP_MODE",
    "DOCKMIN_TLS_CERT_FILE",
    "DOCKMIN_TLS_KEY_FILE",
    TLS_SELF_SIGNED_VAR,
}


def test_the_serving_settings_default_to_a_self_signed_listener_with_the_documented_limits() -> (
    None
):
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY})

    assert settings.tls_enabled and settings.tls_source == "self_signed"
    assert (settings.https_port, settings.https_public_port, settings.http_mode) == (
        8443,
        None,
        None,
    )
    assert settings.effective_http_mode == "redirect"
    assert settings.max_concurrent_requests == 256
    assert settings.permitted_internal_networks == []


@pytest.mark.parametrize(
    ("extra", "mode", "redirect_port"),
    [
        pytest.param(TLS_FILES, "redirect", 8443, id="tls-redirects-by-default"),
        pytest.param(
            TLS_FILES | {"DOCKMIN_HTTP_MODE": " SERVE "}, "serve", 8443, id="mode-normalised"
        ),
        pytest.param(TLS_FILES | {"DOCKMIN_HTTP_MODE": "off"}, "off", 8443, id="off"),
        pytest.param(
            TLS_FILES | {"DOCKMIN_HTTPS_PUBLIC_PORT": "443"}, "redirect", 443, id="public-port"
        ),
        pytest.param(
            TLS_FILES | {"DOCKMIN_HTTPS_PORT": " "}, "redirect", 8443, id="blank-port-is-default"
        ),
        pytest.param({"DOCKMIN_HTTP_MODE": "serve"}, "serve", 8443, id="serve-with-self-signed"),
        pytest.param({}, "redirect", 8443, id="self-signed-redirects-by-default"),
        pytest.param({"DOCKMIN_HTTP_MODE": "off"}, "off", 8443, id="off-with-self-signed"),
        pytest.param(
            {"DOCKMIN_HTTP_MODE": "redirect", "DOCKMIN_HTTPS_PUBLIC_PORT": "443"},
            "redirect",
            443,
            id="redirect-with-self-signed-public-port",
        ),
        pytest.param(
            {TLS_SELF_SIGNED_VAR: "false"}, "serve", 8443, id="self-signed-off-serves-plain"
        ),
    ],
)
def test_the_plain_listener_mode_follows_the_setting_and_whether_tls_is_on(
    extra: dict[str, str], mode: str, redirect_port: int
) -> None:
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY} | extra)

    assert settings.effective_http_mode == mode
    assert settings.redirect_port == redirect_port


@pytest.mark.parametrize(
    ("extra", "variables"),
    [
        pytest.param(
            {"DOCKMIN_TLS_CERT_FILE": "/c"},
            {"DOCKMIN_TLS_CERT_FILE", "DOCKMIN_TLS_KEY_FILE"},
            id="certificate-without-key",
        ),
        pytest.param(
            {"DOCKMIN_TLS_KEY_FILE": "/k"},
            {"DOCKMIN_TLS_CERT_FILE", "DOCKMIN_TLS_KEY_FILE"},
            id="key-without-certificate",
        ),
        pytest.param(
            TLS_FILES | {"DOCKMIN_HTTPS_PORT": "8000"},
            {"DOCKMIN_HTTPS_PORT", "DOCKMIN_PORT"},
            id="https-port-equals-http-port",
        ),
        pytest.param(
            {"DOCKMIN_HTTP_MODE": "redirect", TLS_SELF_SIGNED_VAR: "false"},
            NO_TLS_VARIABLES,
            id="redirect-without-tls",
        ),
        pytest.param(
            {"DOCKMIN_HTTP_MODE": "off", TLS_SELF_SIGNED_VAR: "false"},
            NO_TLS_VARIABLES,
            id="off-without-tls",
        ),
        pytest.param(
            {"DOCKMIN_HTTPS_PORT": "8000"},
            {"DOCKMIN_HTTPS_PORT", "DOCKMIN_PORT"},
            id="https-port-equals-http-port-with-self-signed",
        ),
        pytest.param(TLS_FILES | {"DOCKMIN_HTTP_MODE": "both"}, {"DOCKMIN_HTTP_MODE"}, id="mode"),
        pytest.param({"DOCKMIN_HTTPS_PORT": "0"}, {"DOCKMIN_HTTPS_PORT"}, id="port-zero"),
        pytest.param(
            {"DOCKMIN_HTTPS_PUBLIC_PORT": "70000"}, {"DOCKMIN_HTTPS_PUBLIC_PORT"}, id="public-port"
        ),
        pytest.param(
            {"DOCKMIN_MAX_CONCURRENT_REQUESTS": "7"},
            {"DOCKMIN_MAX_CONCURRENT_REQUESTS"},
            id="limit-low",
        ),
        pytest.param(
            {"DOCKMIN_MAX_CONCURRENT_REQUESTS": "4097"},
            {"DOCKMIN_MAX_CONCURRENT_REQUESTS"},
            id="limit-high",
        ),
        pytest.param(
            {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS": "10.0.0.0/33"},
            {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS"},
            id="permit",
        ),
    ],
)
def test_an_inconsistent_serving_setting_names_each_variable_at_fault(
    extra: dict[str, str], variables: set[str]
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_VAR: VALID_KEY} | extra)

    assert set(raised.value.variables) == variables
    assert VALID_KEY not in str(raised.value)


@pytest.mark.parametrize("limit", ["8", "4096"])
def test_the_concurrency_limit_is_accepted_at_both_ends(limit: str) -> None:
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY, "DOCKMIN_MAX_CONCURRENT_REQUESTS": limit})

    assert settings.max_concurrent_requests == int(limit)


def test_blank_concurrency_limit_is_the_default() -> None:
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY, "DOCKMIN_MAX_CONCURRENT_REQUESTS": ""})

    assert settings.max_concurrent_requests == 256


def test_permitted_networks_are_normalised_one_per_comma_separated_entry() -> None:
    settings = load_settings(
        {
            SECRET_KEY_VAR: VALID_KEY,
            "DOCKMIN_PERMITTED_INTERNAL_NETWORKS": " 127.0.0.1 , 10.1.2.3/8 ,,fe80::1 ",
        }
    )

    assert settings.permitted_internal_networks == ["127.0.0.1/32", "10.0.0.0/8", "fe80::1/128"]


@pytest.mark.parametrize(
    ("origin", "stored"),
    [
        pytest.param(
            "https://Dockmin.Example.test", "https://dockmin.example.test", id="lower-cased"
        ),
        pytest.param(
            "https://dockmin.example.test:8443/",
            "https://dockmin.example.test:8443",
            id="trailing-slash",
        ),
        pytest.param("http://localhost:8000", "http://localhost:8000", id="http"),
    ],
)
def test_trusted_origins_are_stored_as_scheme_and_authority(origin: str, stored: str) -> None:
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY, "DOCKMIN_TRUSTED_ORIGINS": origin})

    assert settings.trusted_origins == [stored]


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://user:pw@dockmin.example.test", id="user-and-password"),
        pytest.param("https://:pw@dockmin.example.test", id="password-only"),
        pytest.param("https://user@dockmin.example.test", id="user-only"),
        pytest.param("https://@dockmin.example.test", id="empty-userinfo"),
        pytest.param("https://dockmin.example.test/path", id="path"),
        pytest.param("https://dockmin.example.test?q=1", id="query"),
        pytest.param("ftp://dockmin.example.test", id="scheme"),
        pytest.param("dockmin.example.test", id="no-scheme"),
    ],
)
def test_a_trusted_origin_that_is_not_a_bare_origin_is_refused_without_echoing_it(
    origin: str,
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_VAR: VALID_KEY, "DOCKMIN_TRUSTED_ORIGINS": origin})

    assert raised.value.variables == ("DOCKMIN_TRUSTED_ORIGINS",)
    assert "pw" not in str(raised.value)


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "supplied.key"
    path.write_text(VALID_KEY, encoding="utf-8")
    return path


def test_the_key_source_is_the_variable_then_the_file_then_a_generated_key(
    key_file: Path,
) -> None:
    variable = load_settings({SECRET_KEY_VAR: VALID_KEY})
    from_file = load_settings({SECRET_KEY_FILE_VAR: str(key_file)})
    neither = load_settings({})

    assert (variable.key_source, from_file.key_source, neither.key_source) == (
        "variable",
        "file",
        "generated",
    )
    assert (variable.managed_key_file, from_file.managed_key_file) == (None, None)


def test_two_key_sources_are_refused_naming_exactly_those_two_variables(key_file: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_VAR: VALID_KEY, SECRET_KEY_FILE_VAR: str(key_file)})

    assert set(raised.value.variables) == {SECRET_KEY_VAR, SECRET_KEY_FILE_VAR}
    assert "at most one of" in str(raised.value)


def test_a_key_description_names_where_each_key_source_is(key_file: Path) -> None:
    variable = load_settings({SECRET_KEY_VAR: VALID_KEY})
    from_file = load_settings({SECRET_KEY_FILE_VAR: str(key_file)})
    generated = load_settings({SECRET_KEY_DIR_VAR: "/volume"})

    assert variable.key_description == f"the configured {SECRET_KEY_VAR}"
    assert from_file.key_description == f"the key in {key_file} ({SECRET_KEY_FILE_VAR})"
    assert generated.key_description == f"the key in /volume/{MANAGED_KEY_FILENAME}"


def test_a_previous_key_equal_to_a_supplied_key_is_refused_naming_both_variables() -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_VAR: VALID_KEY})

    assert set(raised.value.variables) == {PREVIOUS_SECRET_KEY_VAR, SECRET_KEY_VAR}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_the_key_directory_defaults_to_the_key_volume(value: str | None) -> None:
    environ = {} if value is None else {SECRET_KEY_DIR_VAR: value}

    assert load_settings(environ).secret_key_dir == DEFAULT_KEY_DIR


def test_a_relative_key_directory_is_refused_naming_its_variable() -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_DIR_VAR: "key"})

    assert raised.value.variables == (SECRET_KEY_DIR_VAR,)


@pytest.mark.parametrize(
    "key_dir",
    [
        pytest.param("data", id="equal"),
        pytest.param("data/key", id="inside"),
        pytest.param("data/../data", id="equal-through-a-parent-reference"),
        pytest.param(".", id="containing"),
    ],
)
def test_a_generated_key_directory_that_overlaps_the_data_directory_is_refused(
    tmp_path: Path, key_dir: str
) -> None:
    data_dir = tmp_path / "data"
    environ = {DATA_DIR_VAR: str(data_dir), SECRET_KEY_DIR_VAR: str(tmp_path / key_dir)}

    with pytest.raises(ConfigError) as raised:
        load_settings(environ)

    assert set(raised.value.variables) == {SECRET_KEY_DIR_VAR, DATA_DIR_VAR}


@pytest.mark.parametrize(
    "key_dir",
    [
        pytest.param("key", id="sibling"),
        pytest.param("data-key", id="shares-a-name-prefix"),
    ],
)
def test_a_generated_key_directory_beside_the_data_directory_is_accepted(
    tmp_path: Path, key_dir: str
) -> None:
    environ = {DATA_DIR_VAR: str(tmp_path / "data"), SECRET_KEY_DIR_VAR: str(tmp_path / key_dir)}

    assert load_settings(environ).secret_key_dir == tmp_path / key_dir


@pytest.mark.parametrize("supplied", ["variable", "file"])
def test_the_key_directory_is_not_checked_against_the_data_directory_for_a_supplied_key(
    tmp_path: Path, key_file: Path, supplied: str
) -> None:
    key = {"variable": {SECRET_KEY_VAR: VALID_KEY}, "file": {SECRET_KEY_FILE_VAR: str(key_file)}}
    environ = key[supplied] | {DATA_DIR_VAR: str(tmp_path), SECRET_KEY_DIR_VAR: str(tmp_path)}

    assert load_settings(environ).secret_key_dir == tmp_path


@pytest.mark.parametrize(
    "value",
    ["true", "TRUE", "True", "1", "t", "yes", "y", "on", "On", "", "   "],
)
def test_the_self_signed_certificate_is_on_for_every_true_spelling_and_for_a_blank_value(
    value: str,
) -> None:
    settings = load_settings({TLS_SELF_SIGNED_VAR: value})

    assert settings.tls_self_signed is True
    assert settings.tls_source == "self_signed"


@pytest.mark.parametrize(
    "value", ["false", "FALSE", "False", "0", "f", "no", "n", "off", "Off", "OFF"]
)
def test_the_self_signed_certificate_is_off_for_every_false_spelling(value: str) -> None:
    settings = load_settings({TLS_SELF_SIGNED_VAR: value})

    assert settings.tls_self_signed is False
    assert (settings.tls_source, settings.tls_enabled) == (None, False)


def test_an_unrecognised_self_signed_value_is_refused_naming_only_its_variable() -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({TLS_SELF_SIGNED_VAR: "maybe"})

    assert raised.value.variables == (TLS_SELF_SIGNED_VAR,)
    assert "maybe" not in str(raised.value)


@pytest.mark.parametrize(
    ("extra", "source"),
    [
        pytest.param({}, "self_signed", id="default"),
        pytest.param({TLS_SELF_SIGNED_VAR: "false"}, None, id="switched-off"),
        pytest.param(TLS_FILES, "supplied", id="supplied"),
        pytest.param(TLS_FILES | {TLS_SELF_SIGNED_VAR: "false"}, "supplied", id="supplied-and-off"),
    ],
)
def test_the_certificate_source_is_the_supplied_pair_else_the_generated_one_else_none(
    extra: dict[str, str], source: str | None
) -> None:
    settings = load_settings(extra)

    assert settings.tls_source == source
    assert settings.tls_enabled is (source is not None)


def test_the_served_files_are_the_supplied_pair_or_the_generated_pair_in_the_data_directory(
    tmp_path: Path,
) -> None:
    generated = load_settings({DATA_DIR_VAR: str(tmp_path)})
    supplied = load_settings(TLS_FILES)
    plain = load_settings({TLS_SELF_SIGNED_VAR: "false"})

    assert generated.served_cert_file == tmp_path / "server-tls" / "cert.pem"
    assert generated.served_key_file == tmp_path / "server-tls" / "key.pem"
    assert (supplied.served_cert_file, supplied.served_key_file) == (
        Path(TLS_FILES["DOCKMIN_TLS_CERT_FILE"]),
        Path(TLS_FILES["DOCKMIN_TLS_KEY_FILE"]),
    )
    assert (plain.served_cert_file, plain.served_key_file) == (None, None)


@pytest.fixture
def previous_key_file(tmp_path: Path) -> Path:
    path = tmp_path / "previous.key"
    path.write_text(PREVIOUS_VALID_KEY, encoding="utf-8")
    return path


@pytest.mark.parametrize("suffix", ["", "\n", "  \r\n"], ids=["bare", "newline", "padded"])
def test_a_previous_key_file_gives_the_key_the_variable_gives(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "previous.key"
    path.write_text(PREVIOUS_VALID_KEY + suffix, encoding="utf-8")

    from_file = load_settings({SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_FILE_VAR: str(path)})
    from_variable = load_settings(
        {SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_VAR: PREVIOUS_VALID_KEY}
    )

    assert from_file.previous_encryption_key == PREVIOUS_KEY_MATERIAL
    assert from_file.previous_encryption_key == from_variable.previous_encryption_key


def test_a_previous_key_is_described_by_the_source_that_supplies_it(
    previous_key_file: Path,
) -> None:
    from_file = load_settings(
        {SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_FILE_VAR: str(previous_key_file)}
    )
    from_variable = load_settings(
        {SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_VAR: PREVIOUS_VALID_KEY}
    )

    assert from_file.previous_key_variable == PREVIOUS_SECRET_KEY_FILE_VAR
    assert from_file.previous_key_description == (
        f"the key in {previous_key_file} ({PREVIOUS_SECRET_KEY_FILE_VAR})"
    )
    assert from_variable.previous_key_variable == PREVIOUS_SECRET_KEY_VAR
    assert from_variable.previous_key_description == f"the configured {PREVIOUS_SECRET_KEY_VAR}"


def test_both_previous_key_sources_are_refused_naming_exactly_those_two_variables(
    previous_key_file: Path,
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings(
            {
                SECRET_KEY_VAR: VALID_KEY,
                PREVIOUS_SECRET_KEY_VAR: PREVIOUS_VALID_KEY,
                PREVIOUS_SECRET_KEY_FILE_VAR: str(previous_key_file),
            }
        )

    assert set(raised.value.variables) == {PREVIOUS_SECRET_KEY_VAR, PREVIOUS_SECRET_KEY_FILE_VAR}
    assert "at most one of" in str(raised.value)
    assert PREVIOUS_VALID_KEY not in str(raised.value)


def test_a_blank_previous_key_file_is_unset(previous_key_file: Path) -> None:
    settings = load_settings(
        {
            SECRET_KEY_VAR: VALID_KEY,
            PREVIOUS_SECRET_KEY_VAR: PREVIOUS_VALID_KEY,
            PREVIOUS_SECRET_KEY_FILE_VAR: "  ",
        }
    )

    assert settings.previous_secret_key_file is None
    assert settings.previous_encryption_key == PREVIOUS_KEY_MATERIAL


@pytest.mark.parametrize(
    "content",
    [
        SHORT_KEY.encode(),
        LONG_KEY.encode(),
        NOT_BASE64.encode(),
        b"\xff\xfe\x00",
        b"",
    ],
    ids=["short", "long", "not-base64", "not-text", "empty"],
)
def test_a_previous_key_file_that_does_not_hold_a_key_is_refused_naming_its_variable(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "previous.key"
    path.write_bytes(content)

    message = config_error_message(
        {SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_FILE_VAR: str(path)}
    )

    assert PREVIOUS_SECRET_KEY_FILE_VAR in message
    assert SECRET_KEY_FILE_VAR not in message
    assert str(path) not in message


@pytest.mark.parametrize("name", ["absent", "."], ids=["missing", "directory"])
def test_a_previous_key_file_that_cannot_be_read_is_refused_naming_its_variable(
    tmp_path: Path, name: str
) -> None:
    unreadable = tmp_path / name

    message = config_error_message(
        {SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_FILE_VAR: str(unreadable)}
    )

    assert PREVIOUS_SECRET_KEY_FILE_VAR in message
    assert SECRET_KEY_FILE_VAR not in message


def test_a_previous_key_file_equal_to_the_current_key_is_refused_naming_both_variables(
    tmp_path: Path,
) -> None:
    same = tmp_path / "same.key"
    same.write_text(VALID_KEY, encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_FILE_VAR: str(same)})

    assert set(raised.value.variables) == {PREVIOUS_SECRET_KEY_FILE_VAR, SECRET_KEY_VAR}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", False), ("", False)],
)
def test_the_access_log_follows_its_variable_and_is_off_when_unset(
    value: str, expected: bool
) -> None:
    settings = load_settings({SECRET_KEY_VAR: VALID_KEY, f"{ENV_PREFIX}ACCESS_LOG": value})

    assert settings.access_log is expected
    assert load_settings({SECRET_KEY_VAR: VALID_KEY}).access_log is False
