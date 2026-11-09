"""Reading a host's whole image and container lists; a partial list is never used."""

import asyncio

from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.models import ContainerSummary, ImageSummary
from app.docker_engine.resources import IMAGE_LIST_BYTES
from app.services.containers import ContainerListTooLarge
from app.services.object_inspector import ObjectTooLarge, unavailable_from


class ImageListTooLarge(ObjectTooLarge):
    """The host's image list is larger than the read limit; the message names the limit."""

    def __init__(self) -> None:
        limit = IMAGE_LIST_BYTES // (1024 * 1024)
        super().__init__(
            f"This host reports more image data than can be listed here (limit {limit} MiB)."
        )


class DiskUsageTooLarge(ObjectTooLarge):
    """The host's disk-usage reply is larger than the read limit."""

    def __init__(self) -> None:
        super().__init__("This host reports more disk usage data than can be read here.")


async def read_image_disk_usage(client: EngineClient) -> int:
    """Read the bytes the host's images occupy; DiskUsageTooLarge or EnvironmentUnavailable."""
    try:
        return (await client.system.image_disk_usage()).layers_size
    except ResponseTooLarge:
        raise DiskUsageTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_images(client: EngineClient) -> list[ImageSummary]:
    """Read every image; ImageListTooLarge past the limit, EnvironmentUnavailable otherwise."""
    try:
        return await client.images.list()
    except ResponseTooLarge:
        raise ImageListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_containers(client: EngineClient) -> list[ContainerSummary]:
    """Read every container; ContainerListTooLarge past the limit, else EnvironmentUnavailable."""
    try:
        return await client.containers.list(all=True)
    except ResponseTooLarge:
        raise ContainerListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_host_listing(
    client: EngineClient,
) -> tuple[list[ImageSummary], list[ContainerSummary]]:
    """Read every image and every container of a host together."""
    images, containers = await asyncio.gather(read_images(client), read_containers(client))
    return images, containers
