"""Helpers for the image page tests: credential sets over the registry pages, bindings, paths."""

from html import unescape
from typing import Any

from httpx import AsyncClient, Response

from app.registry_client.manifests import ManifestCheckFailed, ManifestCheckKind
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from tests.access_support import container_id
from tests.auth_support import STANDARD_USERNAME
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import digest, full_id
from tests.registry_support import bind_image
from tests.web.environment_support import REMOTE_NAME, World
from tests.web.helpers import parse_tags
from tests.web.registry_support import create_set, custom_form, registry_form, visible_text


def images_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/images"


def image_path(
    world: World, image: str, suffix: str = "", environment_id: int | None = None
) -> str:
    return f"{images_path(world, environment_id)}/{image}{suffix}"


async def make_set(
    client: AsyncClient,
    world: World,
    name: str,
    *,
    address: str = "registry.example.com",
    users: bool = False,
    default: bool = False,
    environment_name: str = REMOTE_NAME,
    **form: Any,
) -> int:
    """Create a credential set through the registry pages and grant it to the remote host."""
    set_id = await create_set(client, custom_form(client, name, address=address, **form))
    await client.post(
        f"/registries/{set_id}/environments",
        data=registry_form(client, environment_name=environment_name),
    )
    if users:
        await client.post(
            f"/registries/{set_id}/access",
            data=registry_form(client, grantee_kind="user", grantee_name=STANDARD_USERNAME),
        )
    if default:
        await client.post(
            f"/registries/{set_id}/default", data=registry_form(client, enabled="yes")
        )
    return set_id


async def bind(
    world: World, set_id: int | None, reference: str, environment_id: int | None = None
) -> None:
    async with world.app.state.database.session() as db:
        await bind_image(db, set_id, environment_id or world.remote_id, reference)


NO_STORE = "no-store"
NGINX, GHCR, DANGLING, SPARE = 1, 2, 3, 4
GHCR_REFERENCE = "ghcr.io/o/app:2"
SECRET_ENV = "image-env-secret-marker-5521"
SECRET_LABEL = "image-label-secret-marker-5522"
BUILD_COMMAND = "RUN build-command-marker-3321"
USING_CONTAINER = container_id(0xC1)


def stock_images[E: ContainerEngine](host: E) -> E:
    """A used two-tag image, a pulled image, a dangling image and an unused spare."""
    host.add_image("nginx:1", full_id(NGINX), size=300_000_000)
    host.add_image("nginx:latest", full_id(NGINX))
    host.add_image(
        GHCR_REFERENCE,
        full_id(GHCR),
        size=100_000_000,
        digests=(f"ghcr.io/o/app@{digest(GHCR)}",),
        config={
            "Env": [f"DB_PASSWORD={SECRET_ENV}", "MODE=prod"],
            "Cmd": ["serve"],
            "Entrypoint": ["/entry"],
            "Labels": {"maintainer": "ops", "registry.api_key": SECRET_LABEL},
            "ExposedPorts": {"80/tcp": {}},
        },
        history=[
            {
                "Id": full_id(GHCR),
                "Created": 1_700_000_000,
                "CreatedBy": BUILD_COMMAND,
                "Tags": [GHCR_REFERENCE],
                "Size": 12,
            }
        ],
    )
    host.add_image(None, full_id(DANGLING), size=70_000_000)
    host.add_image("spare:1", full_id(SPARE), size=10_000_000)
    host.add(USING_CONTAINER, "uses-nginx", image="nginx:1", image_id=full_id(NGINX))
    return host


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def links_in(response: Response) -> set[str]:
    return {
        unescape(tag.attrs["href"])
        for tag in parse_tags(response.text)
        if tag.name == "a" and "href" in tag.attrs
    }


def hidden_values(response: Response, name: str) -> list[str]:
    return [
        unescape(tag.attrs["value"])
        for tag in parse_tags(response.text)
        if tag.name == "input" and tag.attrs.get("name") == name and "value" in tag.attrs
    ]


def assert_no_store(*responses: Response) -> None:
    assert {r.headers.get("cache-control") for r in responses} == {NO_STORE}


class ScriptedManifests:
    """Answers a registry digest check from a script and records what was asked."""

    def __init__(self) -> None:
        self.answers: dict[str, str | ManifestCheckKind] = {}
        self.calls: list[tuple[RegistryTarget, ImageReference, RegistryLogin | None]] = []

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        self.calls.append((target, reference, login))
        answer = self.answers.get(reference.key, digest(2))
        if isinstance(answer, ManifestCheckKind):
            raise ManifestCheckFailed(answer)
        return answer

    async def aclose(self) -> None:
        """Nothing is held."""
