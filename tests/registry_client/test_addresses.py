"""Registry address canonicalisation, endpoint derivation and WWW-Authenticate parsing."""

import pytest

from app.registry_client.addresses import (
    MAX_CHALLENGE_HEADER,
    MAX_CHALLENGE_PARAMS,
    MAX_CHALLENGES,
    api_base_url,
    canonical_address,
    parse_challenges,
    server_address,
    split_address,
)
from app.registry_client.errors import AddressInvalid, ChallengeInvalid

HUB_API = "https://registry-1.docker.io"
HUB_SERVER = "https://index.docker.io/v1/"
MAX_ADDRESS_LENGTH = 262
ZERO_WIDTH_SPACE = chr(0x200B)
LINE_SEPARATOR = chr(0x2028)
U_UMLAUT = chr(0xFC)
E_ACUTE = chr(0xE9)


@pytest.mark.parametrize(
    ("entered", "stored"),
    [
        ("registry.example.com", "registry.example.com"),
        ("Registry.Example.COM", "registry.example.com"),
        ("https://registry.example.com", "registry.example.com"),
        ("HTTPS://registry.example.com/", "registry.example.com"),
        ("registry.example.com:443", "registry.example.com"),
        ("registry.example.com:5000", "registry.example.com:5000"),
        ("registry.example.com.", "registry.example.com"),
        ("my_registry.internal", "my_registry.internal"),
        ("127.0.0.1:25443", "127.0.0.1:25443"),
        ("[::1]:5000", "[::1]:5000"),
        ("[0:0:0:0:0:0:0:1]", "[::1]"),
        ("[::1]:443", "[::1]"),
        (f"b{U_UMLAUT}cher.example", "xn--bcher-kva.example"),
    ],
)
def test_an_address_is_stored_in_its_canonical_host_and_port_form(
    entered: str, stored: str
) -> None:
    assert canonical_address(entered) == stored


@pytest.mark.parametrize(
    ("entered", "message"),
    [
        ("", "Enter the registry"),
        ("http://registry.example.com", "Plain-HTTP registries are not supported"),
        ("ftp://registry.example.com", "only the https scheme"),
        ("registry.example.com/v2", "without a path"),
        ("user@registry.example.com", "without a user name or password"),
        ("registry.example.com?x=1", "without a query string"),
        ("registry.example.com#top", "without a fragment"),
        ("registry%2eexample.com", "percent-encoded"),
        ("registry\\example.com", "backslashes"),
        ("registry example.com", "spaces or control characters"),
        (f"registry{ZERO_WIDTH_SPACE}example.com", "spaces or control characters"),
        ("registry\x00.example.com", "spaces or control characters"),
        (f"registry{LINE_SEPARATOR}example.com", "spaces or control characters"),
        ("::1", "square brackets"),
        ("[::1", "not valid"),
        ("[::1]x", "not valid"),
        ("registry.example.com:0", "port"),
        ("registry.example.com:65536", "port"),
        ("registry.example.com:80a", "port"),
        ("registry.example.com:", "port"),
        ("registry.example.com:123456", "port"),
        ("-registry.example.com", "host name is not valid"),
        (f"{'a' * 64}.example.com", "host name is not valid"),
        ("xn--", "host name is not valid"),
        ("1.2.3", "dotted IPv4"),
        ("0x7f.0x1", "dotted IPv4"),
        ("a" * (MAX_ADDRESS_LENGTH * 2 + 1), "too long"),
    ],
)
def test_an_address_that_is_not_a_plain_host_and_port_is_refused_with_a_named_reason(
    entered: str, message: str
) -> None:
    with pytest.raises(AddressInvalid, match=message):
        canonical_address(entered)


def test_the_longest_legal_host_with_a_port_fits_the_stored_column() -> None:
    longest_label = "a" * 63
    host = ".".join([longest_label] * 3 + ["b" * 61])

    stored = canonical_address(f"{host}:5000")

    assert len(stored) <= MAX_ADDRESS_LENGTH


def test_a_host_over_the_dns_limit_is_refused() -> None:
    host = ".".join(["a" * 63] * 4 + ["b"])

    with pytest.raises(AddressInvalid, match="host name is not valid"):
        canonical_address(host)


@pytest.mark.parametrize(
    ("entered", "host", "port"),
    [
        ("registry.example.com", "registry.example.com", 443),
        ("https://Registry.example.com:5000/", "registry.example.com", 5000),
        ("[::1]:5000", "::1", 5000),
        ("[::1]", "::1", 443),
    ],
)
def test_split_address_returns_an_unbracketed_host_and_the_port(
    entered: str, host: str, port: int
) -> None:
    assert split_address(entered) == (host, port)


@pytest.mark.parametrize(
    ("kind", "address", "api", "server"),
    [
        ("dockerhub", "docker.io", HUB_API, HUB_SERVER),
        ("custom", "docker.io", HUB_API, HUB_SERVER),
        ("ghcr", "ghcr.io", "https://ghcr.io", "ghcr.io"),
        ("acr", "contoso.azurecr.io", "https://contoso.azurecr.io", "contoso.azurecr.io"),
        (
            "custom",
            "registry.example.com:5000",
            "https://registry.example.com:5000",
            "registry.example.com:5000",
        ),
        ("custom", "[::1]:5000", "https://[::1]:5000", "[::1]:5000"),
    ],
)
def test_api_base_and_server_address_follow_the_provider(
    kind: str, address: str, api: str, server: str
) -> None:
    assert (api_base_url(kind, address), server_address(kind, address)) == (api, server)


def test_a_bearer_challenge_yields_its_scheme_and_parameters_by_lower_case_name() -> None:
    header = 'BEARER Realm="https://auth.docker.io/token",service="registry.docker.io"'

    (challenge,) = parse_challenges(header)

    assert challenge.scheme == "bearer"
    assert dict(challenge.params) == {
        "realm": "https://auth.docker.io/token",
        "service": "registry.docker.io",
    }
    assert challenge.param("realm") == "https://auth.docker.io/token"
    assert challenge.param("scope") is None


def test_several_challenges_in_one_header_are_returned_in_order() -> None:
    header = 'Basic realm="Registry", Bearer realm="https://auth.example/token", service="reg"'

    challenges = parse_challenges(header)

    assert [c.scheme for c in challenges] == ["basic", "bearer"]
    assert dict(challenges[0].params) == {"realm": "Registry"}
    assert dict(challenges[1].params) == {"realm": "https://auth.example/token", "service": "reg"}


def test_an_escaped_quote_in_a_quoted_value_is_unescaped() -> None:
    (challenge,) = parse_challenges(r'Basic realm="say \"hi\""')

    assert challenge.param("realm") == 'say "hi"'


def test_the_first_of_a_repeated_parameter_wins() -> None:
    (challenge,) = parse_challenges('Bearer realm="https://first/", realm="https://second/"')

    assert challenge.param("realm") == "https://first/"


def test_an_empty_header_has_no_challenges() -> None:
    assert parse_challenges("") == ()


@pytest.mark.parametrize(
    "header",
    [
        'Bearer realm="unterminated',
        'Bearer realm="x" trailing',
        "=orphan",
        f'Bearer realm="caf{E_ACUTE}"',
        'Bearer realm="a\x01b"',
        'Bearer realm="a\nb"',
        'Bearer realm="trailing-escape\\',
    ],
)
def test_a_malformed_challenge_is_refused(header: str) -> None:
    with pytest.raises(ChallengeInvalid, match="malformed"):
        parse_challenges(header)


def test_a_challenge_header_over_the_size_bound_is_refused() -> None:
    header = "Bearer realm=" + "a" * MAX_CHALLENGE_HEADER

    with pytest.raises(ChallengeInvalid, match="too large"):
        parse_challenges(header)


def test_the_parameter_bound_is_inclusive_and_one_more_is_refused() -> None:
    at_bound = "Bearer " + ",".join(f"p{i}=v" for i in range(MAX_CHALLENGE_PARAMS))
    over_bound = at_bound + ",extra=v"

    assert len(parse_challenges(at_bound)[0].params) == MAX_CHALLENGE_PARAMS
    with pytest.raises(ChallengeInvalid):
        parse_challenges(over_bound)


def test_the_challenge_count_bound_is_inclusive_and_one_more_is_refused() -> None:
    at_bound = ", ".join(["Basic"] * MAX_CHALLENGES)
    over_bound = ", ".join(["Basic"] * (MAX_CHALLENGES + 1))

    assert len(parse_challenges(at_bound)) == MAX_CHALLENGES
    with pytest.raises(ChallengeInvalid):
        parse_challenges(over_bound)
