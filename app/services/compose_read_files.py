"""The files a Compose file names, read from the host, with the text pointed at their copies."""

import asyncio
import posixpath
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from app.services.host_files import FileRead, FileRefusal, is_relative_path
from app.services.stack_document import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_INCLUDE_DEPTH,
    ComposeDocumentRejected,
    compose_nodes,
    decode_file,
    file_name,
    path_part,
)
from app.services.stack_read_result import NeedsFile, ReadFile

__all__ = [
    "Collected",
    "Reader",
    "Reference",
    "ReferenceProblem",
    "collect_references",
    "find_references",
]

type Kind = Literal["env", "label", "config", "secret", "include", "extends"]

_STR_TAG: Final = "tag:yaml.org,2002:str"
_PLAIN_STYLES: Final = (None, "'", '"')
_SECRET_KINDS: Final = frozenset({"env", "secret"})
_COMPOSE_KINDS: Final = frozenset({"include", "extends"})


class Reader(Protocol):
    """Reads one file below the project's working directory."""

    async def read(self, relative_path: str, *, limit: int) -> FileRead:
        """Return the file, or why it was refused."""
        ...


@dataclass(frozen=True, slots=True)
class Reference:
    """One scalar of a Compose file that names a file; `start` and `end` are its offsets."""

    path: str
    kind: Kind
    value: str
    start: int
    end: int
    quote: str


@dataclass(frozen=True, slots=True)
class Collected:
    """The documents with their references rewritten, and the files they name."""

    texts: tuple[str, ...]
    files: tuple[ReadFile, ...]
    names_compose_files: bool


class ReferenceProblem(Exception):
    """A reference that cannot be read, named or replaced in place."""

    def __init__(self, reason: str, path: str) -> None:
        super().__init__(reason)
        self.outcome = NeedsFile(reason, path)


def find_references(text: str) -> tuple[Reference, ...]:
    """Return every file reference of a Compose file, with the position of its scalar.

    ComposeDocumentRejected when the text is not Compose YAML; ReferenceProblem for a reference
    that is aliased, or is not a plain or quoted single-line scalar.
    """
    return _Finder(text).run()


class _Finder:
    def __init__(self, text: str) -> None:
        self._text = text
        self._root = compose_nodes(text)
        self._shared = {node for node, seen in _occurrences(self._root).items() if seen > 1}
        self._found: list[Reference] = []

    def run(self) -> tuple[Reference, ...]:
        for key, value in _pairs(self._root):
            if key == "include" and isinstance(value, SequenceNode):
                self._includes(value)
            elif key in ("configs", "secrets"):
                self._sources(key, value)
            elif key == "services" and isinstance(value, MappingNode):
                for name, service in value.value:
                    self._service(f"services.{path_part(_text(name))}", service)
        return tuple(self._found)

    def _scalar(self, node: Node, path: str, kind: Kind) -> None:
        if not isinstance(node, ScalarNode) or node.tag != _STR_TAG:
            return
        if id(node) in self._shared:
            raise ReferenceProblem("reference_aliased", path)
        start, end = node.start_mark.index, node.end_mark.index
        quote = node.style or ""
        spelled = f"{quote}{node.value}{quote}"
        if node.style not in _PLAIN_STYLES or self._text[start:end] != spelled:
            raise ReferenceProblem("reference_not_plain", path)
        self._found.append(Reference(path, kind, node.value, start, end, quote))

    def _scalars(self, node: Node, path: str, kind: Kind) -> None:
        if isinstance(node, SequenceNode):
            for position, item in enumerate(node.value):
                self._scalar(item, f"{path}[{position}]", kind)
        else:
            self._scalar(node, path, kind)

    def _includes(self, entries: SequenceNode) -> None:
        for position, entry in enumerate(entries.value):
            where = f"include[{position}]"
            if isinstance(entry, MappingNode):
                for field, target in _pairs(entry):
                    if field == "path":
                        self._scalars(target, f"{where}.path", "include")
            else:
                self._scalar(entry, where, "include")

    def _sources(self, section: str, entries: Node) -> None:
        kind: Kind = "config" if section == "configs" else "secret"
        for name, entry in _pairs(entries):
            for field, target in _pairs(entry):
                if field == "file":
                    self._scalar(target, f"{section}.{path_part(name)}.file", kind)

    def _service(self, base: str, service: Node) -> None:
        for field, value in _pairs(service):
            if field == "env_file":
                self._env_files(f"{base}.env_file", value)
            elif field == "label_file":
                self._scalars(value, f"{base}.label_file", "label")
            elif field == "extends":
                for sub, target in _pairs(value):
                    if sub == "file":
                        self._scalar(target, f"{base}.extends.file", "extends")

    def _env_files(self, path: str, value: Node) -> None:
        if not isinstance(value, SequenceNode):
            self._scalar(value, path, "env")
            return
        for position, item in enumerate(value.value):
            if isinstance(item, MappingNode):
                for sub, target in _pairs(item):
                    if sub == "path":
                        self._scalar(target, f"{path}[{position}].path", "env")
            else:
                self._scalar(item, f"{path}[{position}]", "env")


def _text(node: Node) -> str | None:
    return node.value if isinstance(node, ScalarNode) and node.tag == _STR_TAG else None


def _pairs(node: Node) -> list[tuple[str | None, Node]]:
    if not isinstance(node, MappingNode):
        return []
    return [(_text(key), value) for key, value in node.value]


def _occurrences(root: Node) -> Counter[int]:
    """Count how often each scalar is reached; an alias reaches the same scalar again."""
    counts: Counter[int] = Counter()
    pending = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, ScalarNode):
            counts[id(node)] += 1
        elif isinstance(node, MappingNode):
            for key, value in node.value:
                pending.extend((key, value))
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
    return counts


def _flat_name(relative: str, path: str) -> str:
    try:
        return file_name(relative.replace("/", "_"))
    except ComposeDocumentRejected:
        raise ReferenceProblem("file_name_unusable", path) from None


class _Collector:
    def __init__(self, reader: Reader, working_dir: str) -> None:
        self._reader = reader
        self._prefix = f"{working_dir}/"
        self._paths: dict[str, str] = {}
        self._secret: dict[str, bool] = {}
        self._composed: dict[str, bool] = {}
        self._content: dict[str, bytes] = {}
        self._order: list[str] = []
        self.names_compose_files = False

    def _resolve(self, base: str, reference: Reference) -> str:
        value = reference.value
        if "$" in value:
            raise ReferenceProblem("reference_has_variable", reference.path)
        if value.startswith("/"):
            if not value.startswith(self._prefix):
                raise ReferenceProblem("reference_outside", reference.path)
            value, base = value.removeprefix(self._prefix), ""
        relative = posixpath.normpath(posixpath.join(base, value))
        if not is_relative_path(relative):
            raise ReferenceProblem("reference_outside", reference.path)
        return relative

    async def scan(self, text: str, base: str, depth: int) -> str:
        """Return the text with each reference replaced by `./<name>` of its companion file."""
        replacements: list[tuple[Reference, str]] = []
        for reference in await asyncio.to_thread(find_references, text):
            relative = self._resolve(base, reference)
            replacements.append((reference, await self._claim(relative, reference, depth)))
        for reference, name in sorted(replacements, key=lambda item: -item[0].start):
            quote = reference.quote
            text = f"{text[: reference.start]}{quote}./{name}{quote}{text[reference.end :]}"
        return text

    async def _claim(self, relative: str, reference: Reference, depth: int) -> str:
        name = _flat_name(relative, reference.path)
        known = self._paths.get(name.casefold())
        if known is not None and known != relative:
            raise ReferenceProblem("file_name_collision", reference.path)
        secret = reference.kind in _SECRET_KINDS
        composed = reference.kind in _COMPOSE_KINDS
        if known is not None and self._composed[name] != composed:
            raise ReferenceProblem("file_claimed_twice", reference.path)
        if known is None:
            if len(self._order) >= MAX_FILES:
                raise ReferenceProblem("too_many_files", reference.path)
            if composed and depth >= MAX_INCLUDE_DEPTH:
                raise ReferenceProblem("too_deep", reference.path)
            self._paths[name.casefold()] = relative
            self._order.append(name)
            self._secret[name] = secret
            self._composed[name] = composed
            self._content[name] = await self._read(relative, name, reference, depth)
        else:
            self._secret[name] = self._secret[name] or secret
        self.names_compose_files = self.names_compose_files or composed
        return name

    async def _read(self, relative: str, name: str, reference: Reference, depth: int) -> bytes:
        found = await self._reader.read(relative, limit=MAX_FILE_BYTES)
        if isinstance(found, FileRefusal):
            raise ReferenceProblem(found.reason.value, reference.path)
        if reference.kind not in _COMPOSE_KINDS:
            return found.content
        try:
            text = decode_file(found.content, field=name)
        except ComposeDocumentRejected as rejected:
            raise ReferenceProblem(rejected.reason, reference.path) from None
        rewritten = await self.scan(text, posixpath.dirname(relative), depth + 1)
        return rewritten.encode("utf-8")

    def files(self) -> tuple[ReadFile, ...]:
        return tuple(
            ReadFile(name, self._content[name], self._secret[name]) for name in self._order
        )


async def collect_references(
    reader: Reader, working_dir: str, documents: Sequence[str]
) -> Collected | NeedsFile:
    """Read the files the documents name and rewrite each reference to name its copy.

    A reference resolves against the working directory, or against the directory of the
    included or extended file it sits in. A companion file is named after its relative path with
    `/` written `_`. Anything that cannot be read, named or rewritten gives the reason and the
    Compose path of the reference.
    """
    collector = _Collector(reader, working_dir)
    try:
        texts = tuple([await collector.scan(text, "", 0) for text in documents])
    except ReferenceProblem as problem:
        return problem.outcome
    except ComposeDocumentRejected as rejected:
        return NeedsFile(rejected.reason, rejected.path or "compose_file")
    return Collected(texts, collector.files(), collector.names_compose_files)
