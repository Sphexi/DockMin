"""The access platform settings: complete or off, and how the startup check names a gap."""

from pathlib import Path

import pytest

from app.config import (
    JWT_AUDIENCE_VAR,
    JWT_HEADER_VAR,
    JWT_ISSUER_VAR,
    JWT_JWKS_URL_VAR,
    JWT_KEY_FILE_VAR,
    ConfigError,
    Settings,
    load_settings,
)
from app.selfcheck import JWT_REMEDY, from_config_error
from tests.jwt_support import (
    ec_signer,
    jwt_env,
    write_jwks,
    write_public_pem,
)

JWKS_URL = "https://tenant.example.test/api/v1/jwk/ec"
DEFAULT_MAX_AGE_SECONDS = 86400


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    return write_public_pem(tmp_path / "public.pem", ec_signer())


def _env(env: dict[str, str], **changes: str | None) -> dict[str, str]:
    merged = dict(env)
    for name, value in changes.items():
        variable = f"DOCKMIN_JWT_{name.upper()}"
        if value is None:
            merged.pop(variable, None)
        else:
            merged[variable] = value
    return merged


def test_the_feature_is_off_and_nothing_is_read_while_the_header_is_unset(
    settings: Settings,
) -> None:
    assert (settings.jwt_enabled, settings.jwt_file_keys) == (False, ())
    assert settings.jwt_algorithms == ["ES256"]
    assert settings.jwt_identity_claim == "sub"
    assert settings.jwt_max_age.total_seconds() == DEFAULT_MAX_AGE_SECONDS
    assert (settings.jwt_create_users, settings.jwt_password_sign_in) == (False, "all")


def test_a_complete_configuration_with_a_key_file_reads_the_key_at_start(
    env: dict[str, str], key_file: Path
) -> None:
    loaded = load_settings(env | jwt_env(key_file))

    assert loaded.jwt_enabled
    assert [key.kid for key in loaded.jwt_file_keys] == [None]


def test_a_complete_configuration_with_a_url_reads_no_file(
    env: dict[str, str], key_file: Path
) -> None:
    configuration = jwt_env(key_file, jwks_url=JWKS_URL)
    del configuration[JWT_KEY_FILE_VAR]

    loaded = load_settings(env | configuration)

    assert (loaded.jwt_enabled, loaded.jwt_file_keys, loaded.jwt_jwks_url) == (True, (), JWKS_URL)


def test_a_key_file_may_hold_a_jwks_document(env: dict[str, str], tmp_path: Path) -> None:
    path = write_jwks(tmp_path / "keys.json", ec_signer("a"), ec_signer("b"))

    loaded = load_settings(env | jwt_env(path))

    assert [key.kid for key in loaded.jwt_file_keys] == ["a", "b"]


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        pytest.param({"issuer": None}, JWT_ISSUER_VAR, id="no-issuer"),
        pytest.param({"audience": None}, JWT_AUDIENCE_VAR, id="no-audience"),
        pytest.param({"key_file": None}, JWT_JWKS_URL_VAR, id="no-key-source"),
        pytest.param({"jwks_url": JWKS_URL}, JWT_JWKS_URL_VAR, id="two-key-sources"),
        pytest.param({"header": None}, JWT_HEADER_VAR, id="settings-without-the-header"),
        pytest.param({"header": "bad header!"}, JWT_HEADER_VAR, id="bad-header-name"),
        pytest.param({"algorithms": "HS256"}, "DOCKMIN_JWT_ALGORITHMS", id="hmac-algorithm"),
        pytest.param({"algorithms": "ES256,none"}, "DOCKMIN_JWT_ALGORITHMS", id="none-algorithm"),
        pytest.param({"identity_claim": "a b"}, "DOCKMIN_JWT_IDENTITY_CLAIM", id="bad-claim"),
        pytest.param({"password_sign_in": "nobody"}, "DOCKMIN_JWT_PASSWORD_SIGN_IN", id="bad-mode"),
    ],
)
def test_a_partial_or_invalid_configuration_fails_naming_the_setting(
    env: dict[str, str], key_file: Path, changes: dict[str, str | None], named: str
) -> None:
    with pytest.raises(ConfigError) as refused:
        load_settings(_env(env | jwt_env(key_file), **changes))

    assert named in refused.value.variables
    assert named in str(refused.value)
    failure = from_config_error(refused.value)
    assert failure is not None
    assert (failure.check, failure.remedy) == ("jwt_settings", JWT_REMEDY)


@pytest.mark.parametrize("url", ["http://tenant.example.test/jwk", "https://u:p@host.test/jwk"])
def test_a_jwks_url_must_be_https_without_credentials(
    env: dict[str, str], key_file: Path, url: str
) -> None:
    configuration = jwt_env(key_file, jwks_url=url)
    del configuration[JWT_KEY_FILE_VAR]

    with pytest.raises(ConfigError) as refused:
        load_settings(env | configuration)

    assert JWT_JWKS_URL_VAR in refused.value.variables


def test_a_key_file_that_cannot_be_read_or_holds_no_key_fails_the_check(
    env: dict[str, str], tmp_path: Path
) -> None:
    unreadable = tmp_path / "missing.pem"
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a key", encoding="utf-8")

    failures = []
    for path in (unreadable, garbage):
        with pytest.raises(ConfigError) as refused:
            load_settings(env | jwt_env(path))
        failures.append(refused.value)

    assert all(JWT_KEY_FILE_VAR in failure.variables for failure in failures)
    assert all("not a key" not in str(failure) for failure in failures)


def test_the_algorithm_list_is_split_deduplicated_and_defaults_when_blank(
    env: dict[str, str], key_file: Path
) -> None:
    listed = load_settings(env | jwt_env(key_file, algorithms="ES256, RS256,ES256"))
    blank = load_settings(env | jwt_env(key_file, algorithms=" "))

    assert listed.jwt_algorithms == ["ES256", "RS256"]
    assert blank.jwt_algorithms == ["ES256"]
