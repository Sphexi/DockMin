"""Provider extras, the key denylist, the provider table and the ECR address rule."""

from collections.abc import Mapping

import pytest

from app.registry_client.errors import AddressInvalid, ExtrasInvalid
from app.registry_client.providers import (
    DENIED_KEY_FRAGMENTS,
    MAX_ID,
    PROVIDERS,
    EcrExtras,
    GhcrExtras,
    ProviderKind,
    check_address_for_provider,
    forbidden_extras_keys,
    parse_extras,
)

ALL_PROVIDERS: tuple[ProviderKind, ...] = ("dockerhub", "ghcr", "acr", "gitlab", "ecr", "custom")
ECR_ADDRESS = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
ECR_CHINA_ADDRESS = "123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn"
UNKNOWN_SETTING = "contains a setting this provider does not take"
HOLDS_SECRETS = "may not hold secrets"
MAX_REGION = 32
DENIED_KEYS = [
    f"x_{spell(fragment)}_y"
    for fragment in DENIED_KEY_FRAGMENTS
    for spell in (str.lower, str.upper, str.title)
]
HOSTILE_VALUE = "value-that-must-not-be-quoted"


@pytest.mark.parametrize(
    ("kind", "stored", "expected"),
    [
        ("dockerhub", {}, {}),
        ("acr", {}, {}),
        ("custom", {}, {}),
        ("ghcr", {}, {"organisation": None, "use_organisation": False}),
        (
            "ghcr",
            {"organisation": "acme-org", "use_organisation": True},
            {"organisation": "acme-org", "use_organisation": True},
        ),
        (
            "gitlab",
            {},
            {"instance_url": "https://gitlab.com", "project_id": None, "project_path": None},
        ),
        (
            "gitlab",
            {
                "instance_url": "https://GitLab.Example.com/",
                "project_id": 42,
                "project_path": "group/sub/project",
            },
            {
                "instance_url": "https://gitlab.example.com",
                "project_id": 42,
                "project_path": "group/sub/project",
            },
        ),
        (
            "gitlab",
            {"instance_url": "https://example.com:8443/gitlab"},
            {
                "instance_url": "https://example.com:8443/gitlab",
                "project_id": None,
                "project_path": None,
            },
        ),
        ("ecr", {"region": "eu-west-2"}, {"region": "eu-west-2"}),
        ("ecr", {"region": "cn-north-1"}, {"region": "cn-north-1"}),
        ("ecr", {"region": "us-gov-west-1"}, {"region": "us-gov-west-1"}),
    ],
)
def test_each_providers_own_extras_are_accepted_and_normalised(
    kind: ProviderKind, stored: Mapping[str, object], expected: Mapping[str, object]
) -> None:
    assert parse_extras(kind, stored).model_dump() == expected


@pytest.mark.parametrize("kind", ALL_PROVIDERS)
def test_a_key_the_provider_does_not_take_is_refused_rather_than_stored(
    kind: ProviderKind,
) -> None:
    with pytest.raises(ExtrasInvalid) as refused:
        parse_extras(kind, {"unexpected_setting": "x"})

    assert refused.value.errors["extras"] == UNKNOWN_SETTING


@pytest.mark.parametrize(
    ("kind", "foreign"),
    [
        ("dockerhub", {"region": "eu-west-2"}),
        ("ecr", {"organisation": "acme"}),
        ("ghcr", {"instance_url": "https://gitlab.com"}),
        ("gitlab", {"region": "eu-west-2"}),
    ],
)
def test_extras_belonging_to_another_provider_are_refused(
    kind: ProviderKind, foreign: Mapping[str, object]
) -> None:
    with pytest.raises(ExtrasInvalid):
        parse_extras(kind, foreign)


@pytest.mark.parametrize("key", DENIED_KEYS)
def test_a_key_naming_a_secret_is_refused_in_any_case(key: str) -> None:
    with pytest.raises(ExtrasInvalid) as refused:
        parse_extras("custom", {key: "value"})

    assert refused.value.errors == {"extras": HOLDS_SECRETS}


@pytest.mark.parametrize(
    "extras",
    [
        {"region": "eu-west-2", "secret_access_key": "x"},
        {"nested": {"inner": {"api_token": "x"}}},
        {"items": [{"fine": 1}, {"Password": "x"}]},
        {"items": ({"private_key": "x"},)},
    ],
    ids=["beside-a-valid-key", "nested-mapping", "inside-a-list", "inside-a-tuple"],
)
def test_a_denylisted_key_is_found_at_any_depth_before_the_model_is_consulted(
    extras: Mapping[str, object],
) -> None:
    with pytest.raises(ExtrasInvalid) as refused:
        parse_extras("ecr", extras)

    assert refused.value.errors == {"extras": HOLDS_SECRETS}


def test_the_denylist_scan_reports_each_offending_key_name() -> None:
    found = forbidden_extras_keys({"ok": 1, "a_token": 2, "deep": {"clientSecret": 3}})

    assert found == ("a_token", "clientSecret")


def test_the_denylist_scan_leaves_ordinary_keys_alone() -> None:
    assert forbidden_extras_keys({"region": "x", "project_id": 1, "organisation": "y"}) == ()


@pytest.mark.parametrize("kind", ALL_PROVIDERS)
def test_no_extras_model_declares_a_field_the_denylist_would_refuse(kind: ProviderKind) -> None:
    names = tuple(PROVIDERS[kind].extras_model.model_fields)

    assert forbidden_extras_keys(dict.fromkeys(names, 0)) == ()


@pytest.mark.parametrize(
    ("kind", "stored", "field"),
    [
        ("ghcr", {"organisation": "-bad"}, "organisation"),
        ("ghcr", {"organisation": "a--b"}, "organisation"),
        ("ghcr", {"organisation": "x" * 40}, "organisation"),
        ("ghcr", {"organisation": "bad name"}, "organisation"),
        ("ghcr", {"use_organisation": True}, "use_organisation"),
        ("ghcr", {"use_organisation": "true"}, "use_organisation"),
        ("gitlab", {"instance_url": "http://gitlab.example.com"}, "instance_url"),
        ("gitlab", {"instance_url": "https://user@gitlab.example.com"}, "instance_url"),
        ("gitlab", {"instance_url": "https://gitlab.example.com?x=1"}, "instance_url"),
        ("gitlab", {"instance_url": "https://gitlab.example.com/#top"}, "instance_url"),
        ("gitlab", {"instance_url": "https://gitlab.example.com/a b"}, "instance_url"),
        ("gitlab", {"instance_url": "https://"}, "instance_url"),
        ("gitlab", {"project_id": 0}, "project_id"),
        ("gitlab", {"project_id": -1}, "project_id"),
        ("gitlab", {"project_id": MAX_ID + 1}, "project_id"),
        ("gitlab", {"project_id": True}, "project_id"),
        ("gitlab", {"project_id": "7"}, "project_id"),
        ("gitlab", {"project_path": "single"}, "project_path"),
        ("gitlab", {"project_path": "a//b"}, "project_path"),
        ("gitlab", {"project_path": "a/b c"}, "project_path"),
        ("ecr", {}, "region"),
        ("ecr", {"region": "us-east"}, "region"),
        ("ecr", {"region": "US-EAST-1"}, "region"),
        ("ecr", {"region": "eu-west-2x"}, "region"),
        ("ecr", {"region": "a" * (MAX_REGION + 1)}, "region"),
    ],
)
def test_invalid_extras_name_the_offending_field_and_never_quote_the_value(
    kind: ProviderKind, stored: Mapping[str, object], field: str
) -> None:
    with pytest.raises(ExtrasInvalid) as refused:
        parse_extras(kind, stored)

    assert field in refused.value.errors


def test_a_refusal_never_quotes_the_value_it_refused() -> None:
    with pytest.raises(ExtrasInvalid) as refused:
        parse_extras("ghcr", {"organisation": HOSTILE_VALUE + " !"})

    assert HOSTILE_VALUE not in str(refused.value)
    assert HOSTILE_VALUE not in repr(refused.value.errors)


@pytest.mark.parametrize(
    ("kind", "secret_kind", "anonymous", "tls_options", "fixed", "destinations"),
    [
        ("dockerhub", "static", True, False, "docker.io", ()),
        ("ghcr", "static", True, False, "ghcr.io", ()),
        ("acr", "static", True, False, None, ()),
        ("gitlab", "static", True, True, None, ("instance_url",)),
        ("ecr", "derived", False, False, None, ("region",)),
        ("custom", "static", True, True, None, ()),
    ],
)
def test_the_provider_table_holds_the_rules_each_provider_is_defined_by(
    kind: ProviderKind,
    secret_kind: str,
    anonymous: bool,
    tls_options: bool,
    fixed: str | None,
    destinations: tuple[str, ...],
) -> None:
    spec = PROVIDERS[kind]

    assert (
        spec.secret_kind,
        spec.anonymous_allowed,
        spec.tls_options_allowed,
        spec.fixed_address,
        spec.destination_extras,
    ) == (secret_kind, anonymous, tls_options, fixed, destinations)


@pytest.mark.parametrize("kind", ALL_PROVIDERS)
def test_every_destination_extra_is_a_field_of_its_providers_model(kind: ProviderKind) -> None:
    spec = PROVIDERS[kind]

    assert set(spec.destination_extras) <= set(spec.extras_model.model_fields)


@pytest.mark.parametrize(
    ("address", "region"),
    [
        (ECR_ADDRESS, "eu-west-2"),
        (ECR_CHINA_ADDRESS, "cn-north-1"),
    ],
)
def test_an_ecr_address_agreeing_with_the_region_is_accepted(address: str, region: str) -> None:
    check_address_for_provider("ecr", address, EcrExtras(region=region))


@pytest.mark.parametrize(
    ("address", "region", "message"),
    [
        ("registry.example.com", "eu-west-2", "looks like"),
        ("12345.dkr.ecr.eu-west-2.amazonaws.com", "eu-west-2", "looks like"),
        (ECR_ADDRESS, "eu-west-1", "must match"),
        (ECR_ADDRESS, "cn-north-1", "must match"),
        (ECR_CHINA_ADDRESS, "eu-west-2", "must match"),
        (ECR_CHINA_ADDRESS, "cn-northwest-1", "must match"),
    ],
)
def test_an_ecr_address_disagreeing_with_the_region_is_refused(
    address: str, region: str, message: str
) -> None:
    with pytest.raises(AddressInvalid, match=message):
        check_address_for_provider("ecr", address, EcrExtras(region=region))


@pytest.mark.parametrize(("kind", "address"), [("dockerhub", "docker.io"), ("ghcr", "ghcr.io")])
def test_a_fixed_address_provider_accepts_only_its_own_address(
    kind: ProviderKind, address: str
) -> None:
    check_address_for_provider(kind, address, parse_extras(kind, {}))
    with pytest.raises(AddressInvalid, match="always"):
        check_address_for_provider(kind, "registry.example.com", parse_extras(kind, {}))


@pytest.mark.parametrize("kind", ["acr", "gitlab", "custom"])
def test_an_editable_address_provider_places_no_constraint_on_the_host(kind: ProviderKind) -> None:
    check_address_for_provider(kind, "anything.example.com:5000", parse_extras(kind, {}))


def test_an_ecr_address_checked_with_another_providers_extras_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        check_address_for_provider("ecr", ECR_ADDRESS, GhcrExtras())
