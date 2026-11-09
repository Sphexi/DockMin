"""What a container export reads out of the create form's state."""

import pytest

from app.docker_engine.models import ImageInspect, Network
from app.services.container_export_content import (
    EnvironmentRow,
    group_counts,
    image_reference,
    mount_rows,
    network_entry,
    review_digest,
    split_form,
    user_defined_network_names,
)
from app.services.export_manifest import ImageReference

DIGEST = "sha256:" + "cd" * 32
VALUES = {
    "image": "nginx:1.27",
    "env-0-key": "A",
    "env-0-value": "1",
    "env-2-key": "B",
    "env-3-value": "orphan",
    "mount-0-type": "volume",
    "mount-0-source": "data",
    "mount-0-target": "/data",
    "mount-1-type": "bind",
    "mount-1-source": "/srv",
    "mount-1-target": "/host",
    "mount-1-read_only": "on",
    "network-0-name": "bridge",
    "network-1-name": "app",
    "network-2-name": "app",
    "port-0-container_port": "80",
}


def test_the_environment_rows_are_split_from_the_form_in_row_order_without_keyless_rows() -> None:
    form, rows = split_form(VALUES)

    assert [(r.name, r.value) for r in rows] == [("A", "1"), ("B", "")]
    assert not any(key.startswith("env-") for key in form) and form["image"] == "nginx:1.27"


def test_an_environment_row_keeps_its_value_out_of_the_repr() -> None:
    assert "secret-value" not in repr(EnvironmentRow("KEY", "secret-value"))


def test_mounts_and_networks_are_read_in_row_order_without_the_built_in_networks() -> None:
    mounts = mount_rows(VALUES)

    assert [(m.kind, m.source, m.read_only) for m in mounts] == [
        ("volume", "data", False),
        ("bind", "/srv", True),
    ]
    assert user_defined_network_names(VALUES) == ("app",)
    assert group_counts(VALUES) == (
        ("environment variables", 3),
        ("port mappings", 1),
        ("mounts", 2),
        ("networks", 3),
    )


def test_a_network_entry_drops_credentials_and_access_labels_and_lists_the_keys() -> None:
    network = Network.model_validate(
        {
            "Id": "a" * 64,
            "Name": "app",
            "Driver": "bridge",
            "Internal": True,
            "Labels": {"team": "core", "com.docker.compose.project": "shop"},
            "Options": {"password": "x", "mtu": "1400"},
            "IPAM": {"Driver": "default", "Config": [{"Subnet": "10.0.0.0/24"}]},
        }
    )

    entry = network_entry(network)

    assert entry.labels == {"team": "core"} and entry.options == {"mtu": "1400"}
    assert entry.removed_option_keys == ["password"] and entry.internal
    assert [p.subnet for p in entry.pools] == ["10.0.0.0/24"] and entry.ipam_driver == "default"


@pytest.mark.parametrize(
    ("reference", "digests", "tag", "digest"),
    [
        ("nginx:1.27", [f"nginx@{DIGEST}"], "1.27", DIGEST),
        ("nginx:1.27", [f"other@{DIGEST}"], "1.27", None),
        ("nginx", [], "latest", None),
        (f"nginx@{DIGEST}", [], None, DIGEST),
        ("NOT A REFERENCE", [], None, None),
    ],
)
def test_an_image_reference_carries_the_tag_and_the_digest_the_host_holds(
    reference: str, digests: list[str], tag: str | None, digest: str | None
) -> None:
    image = ImageInspect.model_validate({"Id": "sha256:" + "1" * 64, "RepoDigests": digests})

    result = image_reference(reference, image, "web")

    assert (result.tag, result.digest, result.used_by) == (tag, digest, ["web"])


def digest_with(value: str) -> str:
    return review_digest(
        container_id="1" * 64,
        form={"image": "nginx:1"},
        environment=[EnvironmentRow("A", value)],
        networks=[],
        image=ImageReference(reference="nginx:1"),
        volumes=[],
    )


def test_the_review_digest_changes_with_a_value_and_with_nothing_else() -> None:
    assert digest_with("1") == digest_with("1") != digest_with("2")
