"""The volume export manifest: its fields, its encoding and what it may record of a volume."""

import json
from datetime import UTC, datetime

import pytest

from app.services.volume_manifest import (
    FORMAT_VERSION,
    VolumeManifest,
    build_manifest,
    filter_labels,
    filter_options,
    manifest_bytes,
)

EXPORTED_AT = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
PASSWORD = "hunter2-sentinel"
SMB_OPTIONS = f"addr=10.0.0.5,username=svc,password={PASSWORD},vers=3.0"


def manifest(
    labels: dict[str, str] | None = None, options: dict[str, str] | None = None
) -> VolumeManifest:
    return build_manifest(
        exported_at=EXPORTED_AT,
        instance_version="0.3.0",
        name="data",
        driver="local",
        labels=labels or {},
        options=options or {},
    )


def test_the_manifest_carries_version_kind_source_and_the_data_directory() -> None:
    document = json.loads(manifest_bytes(manifest({"team": "core"}, {"type": "nfs"})))

    assert document == {
        "data_path": "data",
        "driver": "local",
        "exported_at": "2024-05-06T07:08:09Z",
        "format_version": FORMAT_VERSION,
        "instance_version": "0.3.0",
        "kind": "volume",
        "labels": {"team": "core"},
        "name": "data",
        "options": {"type": "nfs"},
        "removed_option_keys": [],
    }


def test_the_manifest_is_written_with_sorted_keys_and_reads_back_as_the_same_model() -> None:
    written = manifest_bytes(manifest())

    keys = list(json.loads(written))
    assert keys == sorted(keys)
    assert VolumeManifest.model_validate_json(written) == manifest()


@pytest.mark.parametrize("label", ["com.docker.compose.project", "COM.DOCKER.COMPOSE.service"])
def test_compose_labels_are_not_exported(label: str) -> None:
    assert filter_labels({label: "x", "team": "core"}) == {"team": "core"}


@pytest.mark.parametrize("label", ["com.docker.stack.namespace", "dockmin.owner", "Dockmin.x"])
def test_stack_namespace_and_product_labels_are_not_exported(label: str) -> None:
    assert filter_labels({label: "x", "keep": "me"}) == {"keep": "me"}


@pytest.mark.parametrize(
    ("options", "kept", "removed"),
    [
        pytest.param({"type": "nfs", "device": ":/x"}, {"type": "nfs", "device": ":/x"}, []),
        pytest.param(
            {"type": "nfs", "device": "10.0.0.5:/exports/data", "o": "addr=10.0.0.5,vers=4"},
            {"type": "nfs", "device": "10.0.0.5:/exports/data", "o": "addr=10.0.0.5,vers=4"},
            [],
            id="a-colon-without-an-at-sign-is-not-userinfo",
        ),
        pytest.param(
            {"type": "cifs", "o": SMB_OPTIONS},
            {"type": "cifs"},
            ["o"],
            id="password-inside-the-mount-options",
        ),
        pytest.param(
            {"o": f"addr=1,password={PASSWORD},tail,more"},
            {},
            ["o"],
            id="a-password-containing-commas-leaves-no-tail",
        ),
        pytest.param({"o": "username=svc%swordfish,addr=1"}, {}, ["o"], id="user-percent-password"),
        pytest.param({"o": "credentials=/root/.smb,addr=1"}, {}, ["o"], id="credentials-file"),
        pytest.param(
            {"password": "x", "type": "cifs"}, {"type": "cifs"}, ["password"], id="own-key"
        ),
        pytest.param({"device": "//svc:swordfish@host/share"}, {}, ["device"], id="userinfo"),
        pytest.param(
            {"device": "//svc:sword/fish@host/share"}, {}, ["device"], id="slash-in-the-password"
        ),
        pytest.param(
            {"device": "//svc:sword@fish@host/share"}, {}, ["device"], id="at-sign-in-the-password"
        ),
    ],
)
def test_driver_options_that_carry_a_credential_are_dropped_whole_and_listed_by_key(
    options: dict[str, str], kept: dict[str, str], removed: list[str]
) -> None:
    result = filter_options(options)

    assert result == (kept, removed)


def test_no_removed_value_appears_anywhere_in_the_manifest() -> None:
    written = manifest_bytes(manifest(options={"o": SMB_OPTIONS})).decode("utf-8")

    assert (
        PASSWORD not in written and "svc" not in written and '"removed_option_keys": [' in written
    )


def test_a_manifest_without_a_format_version_is_refused() -> None:
    document = json.loads(manifest_bytes(manifest()))
    del document["format_version"]

    with pytest.raises(ValueError, match="format_version"):
        VolumeManifest.model_validate(document)
