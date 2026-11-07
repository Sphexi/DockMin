"""Validation of the credential set form: what is stored, kept and must be entered again."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.auth.errors import ValidationFailed
from app.db.repositories.registry_credential import StoredRegistryCredential
from app.netpolicy import REFUSAL_TEXT
from app.registry_client.providers import ProviderKind, parse_extras
from app.services.registry_input import (
    PROVIDER_ORDER,
    SECRET_MAX,
    USERNAME_MAX,
    PlannedRegistry,
    RegistrySpec,
    check_ca_bundle,
    extras_defaults,
    extras_for_form,
    plan_registry,
    provider_shape,
    summarise_ca_bundle,
)
from tests.registry_client.loopback import Certificates, build_certificates

STAMP = "2026-01-02T03:04:05Z"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
ECR_ADDRESS = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET = "plain-secret-value"
HOSTILE = "hostile-marker-9f3a"


def stored(**overrides: object) -> StoredRegistryCredential:
    kind: ProviderKind = overrides.get("provider", "custom")  # type: ignore[assignment]
    extras = overrides.pop("extras", None) or parse_extras(kind, {})
    values: dict[str, object] = {
        "id": 1,
        "name": "registry",
        "provider": "custom",
        "url": "registry.example.com",
        "authentication_enabled": True,
        "username": "service",
        "secret_kind": "static",
        "secret_ciphertext": "sealed",
        "derived_secret_ciphertext": None,
        "derived_secret_expires_at": None,
        "derived_secret_refreshed_at": None,
        "extras": extras,
        "extras_json": "{}",
        "verify_tls": True,
        "ca_bundle": None,
        "is_default_for_host": False,
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    return StoredRegistryCredential(**{**values, **overrides})  # type: ignore[arg-type]


def spec(**overrides: object) -> RegistrySpec:
    values: dict[str, object] = {
        "name": "registry",
        "provider": "custom",
        "address": "registry.example.com",
        "authentication_enabled": True,
        "username": "service",
        "secret": SecretStr(SECRET),
    }
    return RegistrySpec(**{**values, **overrides})  # type: ignore[arg-type]


def problems_of(
    request: RegistrySpec, current: StoredRegistryCredential | None = None
) -> dict[str, str]:
    with pytest.raises(ValidationFailed) as refused:
        plan_registry(request, current, None)
    return refused.value.fields


def plan(request: RegistrySpec, current: StoredRegistryCredential | None = None) -> PlannedRegistry:
    return plan_registry(request, current, None)


@pytest.fixture(scope="module")
def certificates() -> Certificates:
    return build_certificates()


def test_an_anonymous_set_needs_no_user_name_and_no_secret_and_stores_neither() -> None:
    planned = plan(
        spec(authentication_enabled=False, username="ignored", secret=SecretStr("ignored"))
    )

    assert planned.fields.authentication_enabled is False
    assert planned.fields.username is None
    assert planned.secret is None
    assert (planned.keep_secret, planned.secret_replaced) == (False, False)


@pytest.mark.parametrize("kind", ["dockerhub", "ghcr", "acr", "gitlab", "custom"])
def test_every_provider_but_ecr_can_be_anonymous(kind: ProviderKind) -> None:
    address = "registry.example.com" if kind in ("acr", "gitlab", "custom") else ""

    planned = plan(spec(provider=kind, address=address, authentication_enabled=False))

    assert planned.fields.authentication_enabled is False


def test_an_authenticated_set_keeps_its_user_name_and_carries_the_new_secret() -> None:
    planned = plan(spec())

    assert planned.fields.username == "service"
    assert planned.secret is not None
    assert planned.secret.get_secret_value() == SECRET
    assert planned.secret_replaced is True


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"username": ""}, "username"),
        ({"username": "   "}, "username"),
        ({"secret": SecretStr("")}, "secret"),
        ({"secret": SecretStr("  \t ")}, "secret"),
        ({"name": ""}, "name"),
        ({"address": ""}, "address"),
        ({"provider": "nonsense"}, "provider"),
        ({"provider": ""}, "provider"),
    ],
)
def test_a_required_field_left_blank_is_named_in_the_problems(
    overrides: Mapping[str, object], field: str
) -> None:
    assert field in problems_of(spec(**overrides))


def test_ecr_without_a_region_is_refused_on_the_region() -> None:
    request = spec(provider="ecr", address=ECR_ADDRESS, username=ACCESS_KEY)

    assert problems_of(request) == {"region": "The region is required."}


def test_an_ecr_address_that_is_not_an_ecr_address_is_refused_on_the_address() -> None:
    request = spec(
        provider="ecr",
        address="registry.example.com",
        username=ACCESS_KEY,
        extras={"region": "eu-west-2"},
    )

    assert set(problems_of(request)) == {"address"}


def test_an_ecr_user_name_that_is_not_an_access_key_id_is_refused() -> None:
    request = spec(
        provider="ecr", address=ECR_ADDRESS, username="not-a-key", extras={"region": "eu-west-2"}
    )

    assert "access key ID" in problems_of(request)["username"]


def test_ecr_can_not_be_saved_without_authentication() -> None:
    request = spec(
        provider="ecr",
        address=ECR_ADDRESS,
        authentication_enabled=False,
        extras={"region": "eu-west-2"},
    )

    assert "always needs credentials" in problems_of(request)["authentication_enabled"]


def test_an_ecr_set_is_planned_with_its_region_as_the_only_extra() -> None:
    planned = plan(
        spec(
            provider="ecr",
            address=ECR_ADDRESS,
            username=ACCESS_KEY,
            extras={"region": "eu-west-2"},
        )
    )

    assert planned.fields.extras == {"region": "eu-west-2"}
    assert planned.fields.url == ECR_ADDRESS


def test_an_extra_that_belongs_to_another_provider_is_refused() -> None:
    request = spec(extras={"region": "eu-west-2"})

    assert problems_of(request) == {"extras": "These settings are not valid for this provider."}


def test_a_fixed_address_provider_ignores_a_blank_address_and_refuses_another() -> None:
    blank = plan(spec(provider="dockerhub", address=""))
    other = problems_of(spec(provider="dockerhub", address="registry.example.com"))

    assert blank.fields.url == "docker.io"
    assert "always docker.io" in other["address"]


@pytest.mark.parametrize(
    ("secret", "fragment"),
    [
        ("x" * (SECRET_MAX + 1), f"at most {SECRET_MAX}"),
        ("bad\x01secret", "control characters"),
        ("bad\x7fsecret", "control characters"),
    ],
)
def test_a_secret_that_is_too_long_or_holds_control_characters_is_refused(
    secret: str, fragment: str
) -> None:
    assert fragment in problems_of(spec(secret=SecretStr(secret)))["secret"]


def test_a_secret_may_hold_the_whitespace_a_json_key_file_carries() -> None:
    key_file = SecretStr('{\r\n\t"type": "service_account"\r\n}')

    planned = plan(spec(secret=key_file))

    assert planned.secret is not None
    assert planned.secret.get_secret_value() == key_file.get_secret_value()


@pytest.mark.parametrize(
    ("username", "fragment"),
    [
        ("a:b", "colon"),
        ("a\x00b", "control characters"),
        ("a​b".replace("​", chr(0x200B)), "control characters"),
        ("u" * (USERNAME_MAX + 1), f"at most {USERNAME_MAX}"),
    ],
)
def test_a_user_name_with_a_colon_a_hidden_character_or_too_long_is_refused(
    username: str, fragment: str
) -> None:
    assert fragment in problems_of(spec(username=username))["username"]


@pytest.mark.parametrize(
    ("name", "fragment"),
    [("", "Enter a name"), ("n" * 65, "at most 64"), ("a\x07b", "control characters")],
)
def test_a_name_that_is_blank_long_or_hidden_is_refused(name: str, fragment: str) -> None:
    assert fragment in problems_of(spec(name=name))["name"]


def test_a_name_is_trimmed_before_it_is_stored() -> None:
    assert plan(spec(name="  padded  ")).fields.name == "padded"


def test_every_problem_is_reported_in_one_refusal() -> None:
    request = spec(name="", username="", secret=SecretStr(""), address="http://x")

    assert set(problems_of(request)) == {"name", "username", "secret", "address"}


def test_no_message_quotes_the_input_it_refused() -> None:
    request = spec(
        name="x" * 65,
        address=f"{HOSTILE}/path",
        username=f"{HOSTILE}:colon",
        secret=SecretStr(f"{HOSTILE}\x01"),
        extras={"region": HOSTILE},
    )

    problems = problems_of(replace(request, provider="ecr"))

    assert [text for text in problems.values() if HOSTILE in text] == []


def test_a_blank_secret_on_an_update_keeps_the_stored_one_when_nothing_it_protects_changed() -> (
    None
):
    planned = plan(spec(secret=SecretStr(""), name="renamed", username="other"), stored())

    assert (planned.keep_secret, planned.secret, planned.secret_replaced) == (True, None, False)
    assert planned.changed == ("name", "username")


def test_a_blank_secret_on_an_update_to_an_anonymous_set_demands_one() -> None:
    current = stored(authentication_enabled=False, username=None, secret_ciphertext=None)

    assert "Enter the secret" in problems_of(spec(secret=SecretStr("")), current)["secret"]


@pytest.mark.parametrize(
    "change", [{"provider": "acr"}, {"address": "other.example.com"}], ids=["provider", "address"]
)
def test_changing_where_the_secret_goes_requires_entering_it_again(
    change: Mapping[str, object],
) -> None:
    request = spec(secret=SecretStr(""), **change)

    assert "Enter the secret again" in problems_of(request, stored())["secret"]


def test_turning_certificate_verification_off_is_refused_while_authentication_is_on() -> None:
    assert "authentication" in problems_of(spec(verify_tls=False))["verify_tls"]


def test_certificate_verification_may_be_off_on_an_anonymous_set() -> None:
    planned = plan(spec(authentication_enabled=False, verify_tls=False))

    assert planned.fields.verify_tls is False


def test_a_provider_without_tls_options_is_always_verified() -> None:
    planned = plan(spec(provider="dockerhub", address="", verify_tls=False))

    assert planned.fields.verify_tls is True


def test_changing_the_ca_bundle_requires_entering_the_secret_again(
    certificates: Certificates,
) -> None:
    request = spec(secret=SecretStr(""))
    current = stored(ca_bundle=certificates.ca_pem)

    problems = problems_of(replace(request, remove_ca_bundle=True), current)

    assert "Enter the secret again" in problems["secret"]


def test_changing_the_ecr_region_requires_entering_the_secret_again() -> None:
    current = stored(
        provider="ecr",
        url=ECR_ADDRESS,
        username=ACCESS_KEY,
        secret_kind="derived",
        extras=parse_extras("ecr", {"region": "eu-west-2"}),
    )
    request = spec(
        provider="ecr",
        address="123456789012.dkr.ecr.eu-west-1.amazonaws.com",
        username=ACCESS_KEY,
        secret=SecretStr(""),
        extras={"region": "eu-west-1"},
    )

    assert "Enter the secret again" in problems_of(request, current)["secret"]


def test_changing_the_gitlab_instance_requires_entering_the_secret_again() -> None:
    current = stored(
        provider="gitlab",
        url="registry.gitlab.com",
        extras=parse_extras("gitlab", {"instance_url": "https://gitlab.com"}),
    )
    request = spec(
        provider="gitlab",
        address="registry.gitlab.com",
        secret=SecretStr(""),
        extras={"instance_url": "https://gitlab.internal.example"},
    )

    assert "Enter the secret again" in problems_of(request, current)["secret"]


def test_changing_an_extra_that_names_no_destination_keeps_the_secret() -> None:
    current = stored(
        provider="gitlab",
        url="registry.gitlab.com",
        extras=parse_extras("gitlab", {"instance_url": "https://gitlab.com"}),
    )
    request = spec(
        provider="gitlab",
        address="registry.gitlab.com",
        secret=SecretStr(""),
        extras={"instance_url": "https://gitlab.com", "project_id": "42"},
    )

    planned = plan(request, current)

    assert planned.keep_secret is True
    assert planned.changed == ("extras",)


def test_an_update_that_changes_nothing_reports_no_change_and_no_replaced_secret() -> None:
    planned = plan(spec(secret=SecretStr("")), stored())

    assert (planned.changed, planned.secret_replaced) == ((), False)


@pytest.mark.parametrize(
    ("extras", "field"),
    [
        ({"project_id": "abc"}, "project_id"),
        ({"project_id": "1" * 40}, "project_id"),
        ({"project_id": "-5"}, "project_id"),
        ({"project_id": "0"}, "project_id"),
        ({"instance_url": "http://gitlab.example.com"}, "instance_url"),
        ({"project_path": "single"}, "project_path"),
    ],
)
def test_gitlab_extras_are_coerced_from_form_text_and_refused_when_invalid(
    extras: Mapping[str, str], field: str
) -> None:
    request = spec(provider="gitlab", address="registry.gitlab.com", extras=extras)

    assert field in problems_of(request)


def test_form_text_for_extras_is_coerced_to_the_types_the_model_takes() -> None:
    request = spec(
        provider="gitlab",
        address="registry.gitlab.com",
        extras={"instance_url": "", "project_id": " 42 ", "project_path": "grp/proj"},
    )

    planned = plan(request)

    assert planned.fields.extras == {
        "instance_url": "https://gitlab.com",
        "project_id": 42,
        "project_path": "grp/proj",
    }


def test_the_ghcr_organisation_flag_needs_an_organisation() -> None:
    request = spec(
        provider="ghcr", address="", extras={"organisation": "", "use_organisation": True}
    )

    assert "use_organisation" in problems_of(request)


def test_each_providers_shape_lists_exactly_its_own_extras_fields() -> None:
    shapes = {kind: provider_shape(kind).extras_fields for kind in PROVIDER_ORDER}

    assert shapes == {
        "dockerhub": (),
        "ghcr": ("organisation", "use_organisation"),
        "acr": (),
        "gitlab": ("instance_url", "project_id", "project_path"),
        "ecr": ("region",),
        "custom": (),
    }


def test_blank_form_defaults_and_stored_extras_render_as_strings_and_flags() -> None:
    stored_extras = parse_extras("ghcr", {"organisation": "acme", "use_organisation": True})

    assert extras_defaults("ghcr") == {"organisation": "", "use_organisation": False}
    assert extras_for_form(stored_extras) == {"organisation": "acme", "use_organisation": True}
    assert extras_defaults("gitlab")["instance_url"] == "https://gitlab.com"


def test_a_valid_ca_bundle_is_checked_and_described(certificates: Certificates) -> None:
    checked = check_ca_bundle(certificates.ca_pem, NOW)

    assert checked.pem.strip() == certificates.ca_pem.strip()
    assert [c.leaf_subject for c in checked.certificates] == ["registry test CA"]
    assert [c.leaf_subject for c in summarise_ca_bundle(checked.pem, NOW)] == ["registry test CA"]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("just text", "not in PEM format"),
        ("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", "private key"),
        ("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n", "only CERTIFICATE"),
        ("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", "could not be read"),
        ("-----BEGIN CERTIFICATE-----\n" + "A" * (64 * 1024) + "\n", "larger than 64 KiB"),
    ],
    ids=["not-pem", "private-key", "public-key", "garbage-certificate", "oversized"],
)
def test_a_ca_bundle_that_is_not_certificates_is_refused_without_quoting_it(
    text: str, fragment: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        check_ca_bundle(text, NOW)

    assert fragment in refused.value.fields["ca_bundle"]
    assert "AAAA" not in refused.value.fields["ca_bundle"]


def test_an_expired_certificate_in_a_bundle_is_refused_by_position(
    certificates: Certificates,
) -> None:
    bundle = certificates.ca_pem + certificates.expired_server.cert_pem

    with pytest.raises(ValidationFailed) as refused:
        check_ca_bundle(bundle, NOW)

    assert "Certificate 2 of the bundle expired on 2021-01-01" in refused.value.fields["ca_bundle"]


def test_a_bundle_of_more_than_ten_certificates_is_refused(certificates: Certificates) -> None:
    with pytest.raises(ValidationFailed, match="more than 10"):
        check_ca_bundle(certificates.ca_pem * 11, NOW)


def test_a_stored_ca_bundle_is_kept_when_the_form_supplies_none(
    certificates: Certificates,
) -> None:
    current = stored(ca_bundle=certificates.ca_pem)

    planned = plan(spec(secret=SecretStr("")), current)

    assert planned.fields.ca_bundle == certificates.ca_pem


def test_ticking_remove_clears_the_stored_ca_bundle(certificates: Certificates) -> None:
    current = stored(ca_bundle=certificates.ca_pem)

    planned = plan(spec(remove_ca_bundle=True), current)

    assert planned.fields.ca_bundle is None


def test_a_bundle_with_verification_off_is_refused(certificates: Certificates) -> None:
    checked = check_ca_bundle(certificates.ca_pem, NOW)
    request = spec(authentication_enabled=False, verify_tls=False)

    with pytest.raises(ValidationFailed) as refused:
        plan_registry(request, None, checked)

    assert "used only when certificates are verified" in refused.value.fields["ca_bundle"]


def test_a_bundle_is_ignored_for_a_provider_with_no_tls_options(
    certificates: Certificates,
) -> None:
    checked = check_ca_bundle(certificates.ca_pem, NOW)

    planned = plan_registry(spec(provider="dockerhub", address=""), None, checked)

    assert planned.fields.ca_bundle is None


def test_provider_shapes_report_what_each_form_needs() -> None:
    gitlab = provider_shape("gitlab")
    hub = provider_shape("dockerhub")
    ecr = provider_shape("ecr")

    assert (gitlab.address_editable, gitlab.default_address, gitlab.tls_options_allowed) == (
        True,
        "registry.gitlab.com",
        True,
    )
    assert (hub.address_editable, hub.fixed_address, hub.tls_options_allowed) == (
        False,
        "docker.io",
        False,
    )
    assert (ecr.anonymous_allowed, ecr.secret_kind) == (False, "derived")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1:5000",
        "localhost:5000",
        "[::1]:5000",
        "169.254.169.254",
        "0.0.0.0:5000",
        "metadata.google.internal",
        "[::ffff:127.0.0.1]:5000",
    ],
)
def test_a_registry_address_that_is_internal_is_refused_on_its_field(address: str) -> None:
    assert problems_of(spec(address=address)) == {"address": REFUSAL_TEXT}


def test_a_permit_lets_an_internal_registry_address_be_saved(
    address_policy: Callable[..., None],
) -> None:
    address_policy("127.0.0.0/8")

    planned = plan(spec(address="127.0.0.1:5000"))

    assert planned.fields.url == "127.0.0.1:5000"
    assert problems_of(spec(address="169.254.169.254")) == {"address": REFUSAL_TEXT}


@pytest.mark.parametrize("address", ["10.1.2.3:5000", "registry.example.com", "192.168.0.9"])
def test_an_ordinary_private_or_public_registry_address_needs_no_permit(address: str) -> None:
    assert plan(spec(address=address)).fields.url == address
