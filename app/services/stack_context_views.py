"""Build contexts as shown, and where a stack's Compose file names them."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from yaml.nodes import MappingNode, Node, ScalarNode

from app.db.repositories.stack import StoredStack
from app.db.repositories.stack_build_context import StackContextRepository, StoredContext
from app.services.stack_deploy import StackRejected
from app.services.stack_document import (
    FILE_NAME,
    ComposeDocumentRejected,
    compose_nodes,
    file_name,
)
from app.services.stack_views import ContextView

__all__ = [
    "ContextRemoveConfirmation",
    "checked_context_name",
    "context_in_use",
    "context_references",
    "context_view",
    "context_views",
]

_CURRENT_DIRECTORY: Final = "./"


@dataclass(frozen=True, slots=True)
class ContextRemoveConfirmation:
    """What removing a context names; `in_use` is true while the Compose file builds from it."""

    environment_id: int
    environment_name: str
    stack: str
    context: ContextView
    expected_inputs_digest: str
    in_use: bool


def _entry(node: Node, key: str) -> Node | None:
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return cast(Node, value_node)
    return None


def _references(text: str) -> dict[str, tuple[str, ...]]:
    try:
        root = compose_nodes(text)
    except ComposeDocumentRejected:
        return {}
    services = _entry(root, "services")
    found: dict[str, list[str]] = {}
    if not isinstance(services, MappingNode):
        return {}
    for key_node, service in services.value:
        if not isinstance(key_node, ScalarNode):
            continue
        build = _entry(service, "build")
        path = f"services.{key_node.value}.build"
        if isinstance(build, MappingNode):
            build = _entry(build, "context")
            path = f"{path}.context"
        if not isinstance(build, ScalarNode):
            continue
        name = build.value.removeprefix(_CURRENT_DIRECTORY)
        if FILE_NAME.fullmatch(name) is not None:
            found.setdefault(name.casefold(), []).append(path)
    return {name: tuple(paths) for name, paths in found.items()}


async def context_references(text: str) -> Mapping[str, tuple[str, ...]]:
    """Return the Compose paths that name each context, keyed by lower-cased name.

    A service's `build` as text, or its `build.context`, names a context as `name` or `./name`.
    Empty when the text cannot be read.
    """
    return await asyncio.to_thread(_references, text)


def context_view(item: StoredContext, uses: tuple[str, ...]) -> ContextView:
    """Describe one stored context."""
    return ContextView(
        name=item.name,
        size=item.size_bytes,
        digest=item.content_digest,
        compressed=item.compressed,
        member_count=item.member_count,
        extracted_size=item.extracted_size,
        updated_at=item.updated_at,
        uses=uses,
    )


async def context_views(
    repository: StackContextRepository, stack: StoredStack
) -> tuple[ContextView, ...]:
    """Describe a stack's contexts by name, each with the places its Compose file names it."""
    items = await repository.contexts(stack.id)
    if not items:
        return ()
    uses = await context_references(stack.compose_file)
    return tuple(context_view(item, uses.get(item.name.casefold(), ())) for item in items)


def checked_context_name(text: str) -> str:
    """Return the trimmed context name; StackRejected keyed `name` when it breaks the grammar."""
    try:
        return file_name(text)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


def context_in_use(name: str, paths: tuple[str, ...]) -> StackRejected:
    """Return the refusal for removing a context that the Compose file still builds from."""
    return StackRejected(
        {"context": f"{name} is still used at {', '.join(sorted(set(paths)))}."}, "context_in_use"
    )
