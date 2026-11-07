"""Image reference grammar, the canonical key, the familiar form and repository digests."""

import pytest

from app.registry_client.addresses import canonical_address
from app.registry_client.references import (
    MAX_REFERENCE_LENGTH,
    ReferenceInvalid,
    digests_for,
    parse_reference,
    split_reference,
)

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
HUB_REPOSITORY = "docker.io/library/busybox"
REFUSED_INPUT = "host.example/Quoted_Marker_8821"

CANONICAL = [
    pytest.param("busybox", "docker.io/library/busybox:latest", "busybox:latest", id="bare-name"),
    pytest.param("busybox:1", "docker.io/library/busybox:1", "busybox:1", id="tag"),
    pytest.param("library/busybox", "docker.io/library/busybox:latest", "busybox:latest", id="lib"),
    pytest.param(
        "index.docker.io/library/busybox:1", "docker.io/library/busybox:1", "busybox:1", id="index"
    ),
    pytest.param(
        "registry-1.docker.io/busybox",
        "docker.io/library/busybox:latest",
        "busybox:latest",
        id="r1",
    ),
    pytest.param("docker.io/org/app:2", "docker.io/org/app:2", "org/app:2", id="hub-namespace"),
    pytest.param("org/app", "docker.io/org/app:latest", "org/app:latest", id="hub-user-repo"),
    pytest.param("ghcr.io/org/app:1", "ghcr.io/org/app:1", "ghcr.io/org/app:1", id="other-host"),
    pytest.param("GHCR.io/org/app", "ghcr.io/org/app:latest", "ghcr.io/org/app:latest", id="case"),
    pytest.param(
        "registry.example.com:443/app",
        "registry.example.com/app:latest",
        "registry.example.com/app:latest",
        id="port-443-dropped",
    ),
    pytest.param(
        "registry.example.com:5000/app:v1",
        "registry.example.com:5000/app:v1",
        "registry.example.com:5000/app:v1",
        id="port-kept",
    ),
    pytest.param("localhost/app", "localhost/app:latest", "localhost/app:latest", id="localhost"),
    pytest.param(
        "localhost:5000/app",
        "localhost:5000/app:latest",
        "localhost:5000/app:latest",
        id="local-port",
    ),
    pytest.param("[::1]:5000/app", "[::1]:5000/app:latest", "[::1]:5000/app:latest", id="ipv6"),
    pytest.param("  busybox  ", "docker.io/library/busybox:latest", "busybox:latest", id="trimmed"),
    pytest.param("a/b/c/d:1", "docker.io/a/b/c/d:1", "a/b/c/d:1", id="deep-path"),
    pytest.param("app__x", "docker.io/library/app__x:latest", "app__x:latest", id="separators"),
]

PINNED = [
    pytest.param(
        f"busybox@{DIGEST}",
        f"{HUB_REPOSITORY}@{DIGEST}",
        f"busybox@{DIGEST}",
        None,
        id="hub-digest",
    ),
    pytest.param(
        f"busybox:1@{DIGEST}",
        f"{HUB_REPOSITORY}@{DIGEST}",
        f"busybox@{DIGEST}",
        "1",
        id="tag-and-digest-key-on-the-digest",
    ),
    pytest.param(
        f"ghcr.io/o/a@{DIGEST}", f"ghcr.io/o/a@{DIGEST}", f"ghcr.io/o/a@{DIGEST}", None, id="host"
    ),
]

REFUSED = [
    pytest.param("", "Image is required.", id="empty"),
    pytest.param("   ", "Image is required.", id="blank"),
    pytest.param("Busybox", "lower-case", id="upper-case-name"),
    pytest.param("host.example/App", "lower-case", id="upper-case-path"),
    pytest.param("host.example//app", "lower-case", id="empty-component"),
    pytest.param("host.example/", "lower-case", id="trailing-slash"),
    pytest.param("/app", "lower-case", id="leading-slash"),
    pytest.param(".app", "lower-case", id="leading-period"),
    pytest.param("app.", "lower-case", id="trailing-period"),
    pytest.param("a/../b", "lower-case", id="dot-dot"),
    pytest.param("host.example/app?x", "lower-case", id="query"),
    pytest.param("app:tag:other", "lower-case", id="two-tags"),
    pytest.param("app:", "tag may use", id="empty-tag"),
    pytest.param("app:-x", "tag may use", id="tag-starts-with-hyphen"),
    pytest.param("app:" + "a" * 129, "tag may use", id="tag-too-long"),
    pytest.param("app@sha256:abc", "digest must be", id="short-digest"),
    pytest.param("app@sha256:" + "A" * 64, "digest must be", id="upper-case-digest"),
    pytest.param("app@md5:" + "a" * 32, "digest must be", id="other-algorithm"),
    pytest.param("host.example/app:tag@", "digest must be", id="empty-digest"),
    pytest.param("a b", "not a valid image reference", id="space"),
    pytest.param("app\x00", "not a valid image reference", id="nul"),
    pytest.param("app\x7f", "not a valid image reference", id="delete"),
    pytest.param("héllo", "not a valid image reference", id="non-ascii"),
    pytest.param("a" * (MAX_REFERENCE_LENGTH + 1), "not a valid image reference", id="too-long"),
    pytest.param("x/" + "a" * 300, "between 1 and 255", id="name-too-long"),
    pytest.param("-bad.example/app", "host is not valid", id="host-leading-hyphen"),
    pytest.param("bad_host.example/app", "host is not valid", id="host-underscore"),
    pytest.param("host.example:99999999999/app", "host is not valid", id="port-too-large"),
    pytest.param("[::1/app", "host is not valid", id="unclosed-bracket"),
]


@pytest.mark.parametrize(("text", "key", "familiar"), CANONICAL)
def test_a_tag_reference_has_one_canonical_key_and_familiar_form(
    text: str, key: str, familiar: str
) -> None:
    parsed = parse_reference(text)

    assert (parsed.key, parsed.familiar) == (key, familiar)
    assert parsed.pinned is False
    assert parsed.digest is None


@pytest.mark.parametrize(("text", "key", "familiar", "tag"), PINNED)
def test_a_reference_with_a_digest_is_pinned_and_keys_on_the_digest_alone(
    text: str, key: str, familiar: str, tag: str | None
) -> None:
    parsed = parse_reference(text)

    assert (parsed.key, parsed.familiar, parsed.tag) == (key, familiar, tag)
    assert (parsed.pinned, parsed.digest) == (True, DIGEST)


@pytest.mark.parametrize(("text", "key", "familiar"), CANONICAL)
def test_the_familiar_form_parses_back_to_the_same_key(text: str, key: str, familiar: str) -> None:
    assert parse_reference(familiar).key == key


@pytest.mark.parametrize(("text", "key", "familiar"), CANONICAL)
def test_the_registry_address_of_a_reference_is_the_canonical_address_of_its_host(
    text: str, key: str, familiar: str
) -> None:
    parsed = parse_reference(text)

    assert parsed.registry_address == canonical_address(parsed.domain)
    assert key.startswith(f"{parsed.registry_address}/")


def test_a_hub_reference_has_the_docker_hub_address_whatever_alias_names_it() -> None:
    aliases = [
        "busybox",
        "docker.io/busybox",
        "index.docker.io/busybox",
        "registry-1.docker.io/busybox",
    ]

    addresses = {parse_reference(alias).registry_address for alias in aliases}

    assert addresses == {"docker.io"}


@pytest.mark.parametrize(("text", "fragment"), REFUSED)
def test_a_malformed_reference_is_refused_with_a_product_message(text: str, fragment: str) -> None:
    with pytest.raises(ReferenceInvalid) as refused:
        parse_reference(text)

    assert fragment in refused.value.message


def test_a_refusal_never_quotes_the_input() -> None:
    with pytest.raises(ReferenceInvalid) as refused:
        parse_reference(REFUSED_INPUT)

    assert "Quoted_Marker_8821" not in refused.value.message
    assert "Quoted_Marker_8821" not in str(refused.value)


def test_split_reference_keeps_what_was_typed_without_defaults_or_aliases() -> None:
    parts = split_reference("Index.Docker.io/library/busybox:1")

    assert (parts.domain, parts.path, parts.tag, parts.digest) == (
        "Index.Docker.io",
        "library/busybox",
        "1",
        None,
    )
    assert parts.typed() == "Index.Docker.io/library/busybox:1"


def test_split_reference_adds_latest_only_when_there_is_no_tag_and_no_digest() -> None:
    assert split_reference("busybox").typed() == "busybox:latest"
    assert split_reference(f"busybox@{DIGEST}").typed() == f"busybox@{DIGEST}"


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        pytest.param([f"busybox@{DIGEST}"], (DIGEST,), id="familiar-hub-name"),
        pytest.param([f"{HUB_REPOSITORY}@{DIGEST}"], (DIGEST,), id="canonical-name"),
        pytest.param(
            [f"busybox@{DIGEST}", f"busybox@{OTHER_DIGEST}"], (DIGEST, OTHER_DIGEST), id="in-order"
        ),
        pytest.param([f"busybox@{DIGEST}", f"busybox@{DIGEST}"], (DIGEST,), id="no-repeats"),
        pytest.param([f"ghcr.io/o/a@{DIGEST}"], (), id="another-repository"),
        pytest.param(["busybox:1", "not a reference", "busybox@sha256:short"], (), id="malformed"),
        pytest.param([f"busybox@{DIGEST}" + "x" * 700], (), id="over-long-entry"),
        pytest.param([], (), id="none"),
    ],
)
def test_digests_for_returns_the_digests_one_repository_holds(
    entries: list[str], expected: tuple[str, ...]
) -> None:
    assert digests_for(entries, HUB_REPOSITORY) == expected
