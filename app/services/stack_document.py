"""Stack names, Compose file intake, the YAML pre-check and variable rules; no I/O."""

import dataclasses
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, NoReturn

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from app.auth.errors import ValidationFailed
from app.compose_cli.environment import DISCLOSING_VARIABLES, SOURCE_PREFIX, is_reserved
from app.db.repositories.stack import StackKind
from app.db.repositories.stack_file import MAX_FILE_BYTES, MAX_FILES, MAX_FILES_TOTAL_BYTES

__all__ = [
    "COMPOSE_FIELD",
    "FILE_NAME",
    "MAX_COMPOSE_BYTES",
    "MAX_FILES",
    "MAX_FILES_TOTAL_BYTES",
    "MAX_FILE_BYTES",
    "MAX_INCLUDE_DEPTH",
    "MAX_VARIABLES",
    "SOURCE_PREFIX",
    "STACK_NAME",
    "SWARM_STACK_NAME",
    "TOP_LEVEL_KEYS",
    "VARIABLE_NAME",
    "CompanionFile",
    "ComposeDocumentRejected",
    "FileRole",
    "FileUse",
    "FileUses",
    "ServiceBuildCheck",
    "VariableInput",
    "check_files",
    "check_structure",
    "check_variables",
    "compose_nodes",
    "decode_compose",
    "decode_file",
    "file_name",
    "is_reserved_variable",
    "line_index",
    "parse_files",
    "parse_variables",
    "path_part",
    "precheck",
    "source_variable_refusal",
    "stack_name",
    "swarm_stack_name",
]

STACK_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
STACK_NAME_MESSAGE: Final = (
    "Use 1 to 63 lower-case letters, digits, '-' and '_', starting with a letter or digit."
)
SWARM_STACK_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
SWARM_STACK_NAME_MESSAGE: Final = (
    "Use 1 to 63 letters, digits, '-' and '_', starting with a letter or digit."
)
MAX_COMPOSE_BYTES: Final = 131072
COMPOSE_FIELD: Final = "compose_file"

MAX_ALIASES: Final = 64
MAX_DEPTH: Final = 64
MAX_EXPANDED_VALUES: Final = 200_000

TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "services", "networks", "volumes", "configs", "secrets", "version", "include"}
)

MAX_INCLUDE_DEPTH: Final = 4

RESERVED_MESSAGE: Final = "the variable {name} is reserved and cannot be used in a stack."
INTERPOLATED_NAME_MESSAGE: Final = "an environment variable name cannot be interpolated."
FILE_MESSAGE: Final = "dockmin cannot read other files on the server for a stack."
REFERENCE_MESSAGE: Final = "Name a file added to this stack, as `name` or `./name`."
MISSING_MESSAGE: Final = "No file named `{name}` is added to this stack."
INCLUDE_MESSAGE: Final = "only `path` is supported for an include."
BUILD_MESSAGE: Final = "dockmin cannot build images for a stack."
PROVIDER_MESSAGE: Final = "dockmin does not run provider services."
DEVELOP_MESSAGE: Final = "this key is not supported."
SOURCE_MESSAGE: Final = "only a `content` or `environment` source is supported."
SWARM_SOURCE_MESSAGE: Final = "only a `content`, `environment` or `external` source is supported."
SWARM_SECRET_CONTENT_MESSAGE: Final = (
    "a secret value must come from a secret variable, not from the file."
)

ServiceBuildCheck = Callable[[str, Mapping[Any, Any]], Mapping[str, str]]

REFUSED_SERVICE_KEYS: Final[Mapping[str, str]] = {
    "build": BUILD_MESSAGE,
    "provider": PROVIDER_MESSAGE,
    "develop": DEVELOP_MESSAGE,
}
_FILE_SERVICE_KEYS: Final = frozenset({"env_file", "label_file"})
_ENV_FILE_ENTRY_KEYS: Final = frozenset({"path", "required", "format"})
_SOURCE_ENTRY_KEYS: Final = frozenset({"content", "environment", "file", "name", "labels"})
_SWARM_SOURCE_ENTRY_KEYS: Final = _SOURCE_ENTRY_KEYS | {"external"}
_SWARM_SOURCES: Final = ("content", "environment", "external", "file")

_CORE_TAGS: Final = frozenset(
    f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null", "seq", "map")
)
_TIMESTAMP_TAG: Final = "tag:yaml.org,2002:timestamp"
_MERGE_TAG: Final = "tag:yaml.org,2002:merge"
_NO_MERGE_SECTIONS: Final = frozenset({"services", "configs", "secrets", "include"})
MERGE_KEY_MESSAGE: Final = "YAML merge keys (`<<`) are not supported."
_STR_TAG: Final = "tag:yaml.org,2002:str"

_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REFERENCE: Final = re.compile(r"\$(?:\$|\{?([A-Za-z_][A-Za-z0-9_]*))")
_SAFE_PART: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NOT_PRINTABLE: Final = re.compile(r"[^\x20-\x7e]")
_BOM: Final = b"\xef\xbb\xbf"
_EXPORT_PREFIX: Final = re.compile(r"^export\s+")

FILE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RESERVED_FILE_NAMES: Final = frozenset(
    f"{stem}{middle}.{extension}"
    for stem in ("compose", "docker-compose")
    for middle in ("", ".override")
    for extension in ("yaml", "yml")
)
FILE_NAME_MESSAGE: Final = (
    "Use 1 to 128 letters, digits, '.', '_' and '-', starting with a letter or digit."
)
VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MAX_VARIABLES: Final = 100
MAX_VARIABLE_VALUE_BYTES: Final = 8192
MAX_VARIABLES_TOTAL_BYTES: Final = 65536
_ROW: Final = re.compile(r"variable-([0-9]{1,4})-(name|value|secret|remove)")
_FILE_ROW: Final = re.compile(r"file-([0-9]{1,4})-(name|content|upload|secret)")
_TRUTHY: Final = frozenset({"on", "true", "1", "yes"})


class ComposeDocumentRejected(ValidationFailed):
    """Raised for an unacceptable stack name, Compose file, variable set or companion file.

    reason is the activity failure reason; path names the Compose path of a refused construct;
    file names the companion file the refusal was found in, None for the stack's own file.
    lines maps a field key to the 1-based line of the text in that field the refusal is found at.
    """

    def __init__(
        self,
        fields: dict[str, str],
        reason: str,
        *,
        path: str | None = None,
        file: str | None = None,
        lines: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(fields)
        self.reason = reason
        self.path = path
        self.file = file
        self.lines = dict(lines or {})


def _reject(field: str, message: str, reason: str) -> ComposeDocumentRejected:
    return ComposeDocumentRejected({field: message}, reason)


def _refuse(path: str, message: str) -> NoReturn:
    raise ComposeDocumentRejected(
        {COMPOSE_FIELD: f"`{path}`: {message}"}, "refused_construct", path=path
    )


def path_part(key: object) -> str:
    """Return a key as a Compose path segment, or "<key>" when it is not plain text."""
    return key if isinstance(key, str) and _SAFE_PART.fullmatch(key) else "<key>"


def _shown(text: object) -> str:
    return _NOT_PRINTABLE.sub("?", str(text))[:48]


def _fold(key: object) -> str | None:
    return key.strip().casefold() if isinstance(key, str) else None


def stack_name(text: str) -> str:
    """Return the trimmed stack name, or raise ComposeDocumentRejected; never normalises case."""
    name = text.strip()
    if not name:
        raise _reject("name", "Enter a name for the stack.", "invalid")
    if STACK_NAME.fullmatch(name) is None:
        raise _reject("name", STACK_NAME_MESSAGE, "invalid")
    return name


def swarm_stack_name(text: str) -> str:
    """Return the trimmed Swarm stack name, or raise ComposeDocumentRejected; keeps case."""
    name = text.strip()
    if not name:
        raise _reject("name", "Enter a name for the stack.", "invalid")
    if SWARM_STACK_NAME.fullmatch(name) is None:
        raise _reject("name", SWARM_STACK_NAME_MESSAGE, "invalid")
    return name


def decode_compose(raw: bytes) -> str:
    """Return the file as text after the size, UTF-8 and control-character rules."""
    if len(raw) > MAX_COMPOSE_BYTES:
        raise _reject(COMPOSE_FIELD, "The Compose file can be at most 128 KiB.", "too_large")
    body = raw.removeprefix(_BOM)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _reject(COMPOSE_FIELD, "The Compose file must be UTF-8 text.", "invalid") from None
    if not text.strip():
        raise _reject(COMPOSE_FIELD, "The Compose file is empty.", "invalid")
    if _CONTROL.search(text) is not None:
        raise _reject(COMPOSE_FIELD, "The Compose file contains control characters.", "invalid")
    return text


class _Refused(Exception):
    """Carries a loader limit out of the YAML library's call stack."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


@dataclass(frozen=True, slots=True)
class _Measure:
    node: Node
    depth: int
    weight: int
    pairs: int


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that enforces the document limits while the node graph is composed.

    Limits are measured on composed nodes, before anything is constructed, so an alias or merge
    expansion is refused before it can allocate.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._aliases = 0
        self._depth = 0
        self._measured: dict[int, _Measure] = {}

    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        is_alias = isinstance(event, yaml.AliasEvent)
        if is_alias:
            self._aliases += 1
            if self._aliases > MAX_ALIASES:
                raise _Refused(f"The Compose file uses more than {MAX_ALIASES} YAML aliases.")
        else:
            tag = getattr(event, "tag", None)
            if tag is not None and tag != "!" and tag not in _CORE_TAGS:
                raise _Refused(
                    f"The Compose file uses the YAML tag `{_shown(tag)}`, which is not supported."
                )
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise _Refused(f"The Compose file nests more than {MAX_DEPTH} levels deep.")
        try:
            node = super().compose_node(parent, index)
        finally:
            if not is_alias:
                self._depth -= 1
        if node is None:
            return None
        if is_alias:
            if id(node) not in self._measured:
                raise _Refused("The Compose file contains an alias that refers to itself.")
        else:
            self._measure(node)
        return node

    def _measure(self, node: Node) -> None:
        depth, weight, pairs = 1, 1, 0
        if isinstance(node, SequenceNode):
            for child in node.value:
                measure = self._measured[id(child)]
                depth = max(depth, 1 + measure.depth)
                weight += measure.weight
        elif isinstance(node, MappingNode):
            seen: set[tuple[str, str]] = set()
            merged = 0
            for key, value in node.value:
                if isinstance(key, ScalarNode):
                    identity = (key.tag, key.value)
                    if identity in seen:
                        raise _Refused(
                            f"The Compose file repeats the key `{_shown(key.value)}` "
                            f"on line {key.start_mark.line + 1}.",
                            key.start_mark.line + 1,
                        )
                    seen.add(identity)
                key_measure = self._measured[id(key)]
                value_measure = self._measured[id(value)]
                depth = max(depth, 1 + key_measure.depth, 1 + value_measure.depth)
                weight += key_measure.weight + value_measure.weight
                if isinstance(key, ScalarNode) and key.tag == _MERGE_TAG:
                    merged += self._merge_size(value)
                else:
                    pairs += 1
            pairs += merged
            weight += merged
        if depth > MAX_DEPTH:
            raise _Refused(f"The Compose file nests more than {MAX_DEPTH} levels deep.")
        if weight > MAX_EXPANDED_VALUES or pairs > MAX_EXPANDED_VALUES:
            raise _Refused(
                f"The Compose file expands to more than {MAX_EXPANDED_VALUES} values once "
                "aliases and merge keys are resolved."
            )
        self._measured[id(node)] = _Measure(node, depth, weight, pairs)

    def _merge_size(self, value: Node) -> int:
        if isinstance(value, MappingNode):
            return self._measured[id(value)].pairs
        if isinstance(value, SequenceNode):
            return sum(
                self._measured[id(item)].pairs
                for item in value.value
                if isinstance(item, MappingNode)
            )
        return 0


_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _yaml_line(error: yaml.YAMLError) -> int | None:
    mark = getattr(error, "problem_mark", None)
    return mark.line + 1 if mark is not None else None


def _path_lines(root: Node) -> dict[str, int]:
    """Index the first line of each literal key path and sequence position in a composed graph."""
    lines: dict[str, int] = {}
    visited: set[int] = set()

    def walk(node: Node, path: str) -> None:
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.tag != _STR_TAG:
                    continue
                segment = path_part(key.value)
                if segment == "<key>":
                    continue
                child = f"{path}.{segment}" if path else segment
                lines.setdefault(child, key.start_mark.line + 1)
                walk(value, child)
        elif isinstance(node, SequenceNode):
            for position, item in enumerate(node.value):
                child = f"{path}[{position}]"
                lines.setdefault(child, item.start_mark.line + 1)
                walk(item, child)

    walk(root, "")
    return lines


def _line_of(index: Mapping[str, int], path: str) -> int | None:
    """Return the line of a path, else of its nearest enclosing path the index holds."""
    while path:
        line = index.get(path)
        if line is not None:
            return line
        cut = max(path.rfind("."), path.rfind("["))
        path = path[:cut] if cut > 0 else ""
    return None


def line_index(text: str) -> Mapping[str, int]:
    """Return the line of each literal Compose path in the text; empty when it is not readable.

    Blocking and CPU-bound; call through asyncio.to_thread.
    """
    try:
        loader = _StrictLoader(text)
    except yaml.YAMLError:
        return {}
    try:
        node = loader.get_single_node()
        return {} if node is None else _path_lines(node)
    except (_Refused, yaml.YAMLError, RecursionError, ValueError, TypeError, OverflowError):
        return {}
    finally:
        loader.dispose()


def compose_nodes(text: str) -> Node:
    """Return the composed YAML node graph of a Compose file, under the pre-check's limits.

    Each node carries its position in `text`. ComposeDocumentRejected when the text is empty or
    cannot be read as Compose YAML. Blocking and CPU-bound; call through asyncio.to_thread.
    """
    try:
        loader = _StrictLoader(text)
    except yaml.YAMLError as error:
        raise _reject(COMPOSE_FIELD, _yaml_message(error), "invalid_compose") from None
    try:
        node = loader.get_single_node()
    except _Refused as refused:
        raise _reject(COMPOSE_FIELD, refused.message, "invalid_compose") from None
    except yaml.YAMLError as error:
        raise _reject(COMPOSE_FIELD, _yaml_message(error), "invalid_compose") from None
    except (RecursionError, ValueError, TypeError, OverflowError):
        raise _reject(
            COMPOSE_FIELD, "The Compose file cannot be read as Compose YAML.", "invalid_compose"
        ) from None
    finally:
        loader.dispose()
    if node is None:
        raise _reject(COMPOSE_FIELD, "The Compose file is empty.", "invalid")
    return node


def _yaml_message(error: yaml.YAMLError) -> str:
    if getattr(error, "context", None) == "expected a single document in the stream":
        return "The Compose file holds more than one YAML document."
    problem = getattr(error, "problem", None) or getattr(error, "reason", None)
    mark = getattr(error, "problem_mark", None)
    detail = f": {_shown(problem)}" if isinstance(problem, str) else ""
    where = f" (line {mark.line + 1})" if mark is not None else ""
    return f"The Compose file is not valid YAML{detail}{where}."


@dataclass(frozen=True, slots=True)
class CompanionFile:
    """One companion file of a stack.

    row is the form row that submitted it and keys its field errors; a file read from storage
    has none, and its errors are keyed `files`.
    """

    name: str
    content: str = dataclasses.field(repr=False)
    secret: bool = False
    row: int | None = None


type FileRole = Literal["env", "source", "compose"]


@dataclass(frozen=True, slots=True)
class FileUse:
    """One place a companion file is named: the Compose path, how it is read, and the document.

    document is None for the stack's own file, else the name of the companion Compose file.
    """

    path: str
    role: FileRole
    document: str | None


type FileUses = Mapping[str, tuple[FileUse, ...]]


def _file_field(item: CompanionFile | None, part: str) -> str:
    return "files" if item is None or item.row is None else f"file-{item.row}-{part}"


def _merge_key(root: Node) -> tuple[str, int] | None:
    """Return the path and line of the first merge key under a section that refuses them."""
    if not isinstance(root, MappingNode):
        return None
    visited: set[int] = set()

    def walk(node: Node, path: str) -> tuple[str, int] | None:
        if id(node) in visited:
            return None
        visited.add(id(node))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if isinstance(key, ScalarNode) and key.tag == _MERGE_TAG:
                    return f"{path}.<<", key.start_mark.line + 1
                segment = path_part(key.value) if isinstance(key, ScalarNode) else "<key>"
                found = walk(value, f"{path}.{segment}")
                if found is not None:
                    return found
        elif isinstance(node, SequenceNode):
            for position, item in enumerate(node.value):
                found = walk(item, f"{path}[{position}]")
                if found is not None:
                    return found
        return None

    for key, value in root.value:
        if isinstance(key, ScalarNode) and key.value in _NO_MERGE_SECTIONS:
            found = walk(value, key.value)
            if found is not None:
                return found
    return None


def _load(text: str, item: CompanionFile | None = None) -> tuple[Any, dict[str, int]]:
    key = COMPOSE_FIELD if item is None else _file_field(item, "content")

    def fail(message: str, reason: str, line: int | None = None) -> ComposeDocumentRejected:
        lines = {} if line is None else {key: line}
        if item is None:
            return ComposeDocumentRejected({key: message}, reason, lines=lines)
        return ComposeDocumentRejected(
            {key: f"{item.name}: {message}"}, reason, file=item.name, lines=lines
        )

    try:
        loader = _StrictLoader(text)
    except yaml.YAMLError as error:
        raise fail(_yaml_message(error), "invalid_compose", _yaml_line(error)) from None
    try:
        node = loader.get_single_node()
        if node is None:
            raise fail("The Compose file is empty.", "invalid")
        index = _path_lines(node)
        merged = _merge_key(node)
        if merged is not None:
            path, line = merged
            message = f"`{path}`: {MERGE_KEY_MESSAGE}"
            raise ComposeDocumentRejected(
                {key: message if item is None else f"{item.name}: {message}"},
                "refused_construct",
                path=path,
                file=None if item is None else item.name,
                lines={key: line},
            )
        return loader.construct_document(node), index
    except _Refused as refused:
        raise fail(refused.message, "invalid_compose", refused.line) from None
    except yaml.YAMLError as error:
        raise fail(_yaml_message(error), "invalid_compose", _yaml_line(error)) from None
    except (RecursionError, ValueError, TypeError, OverflowError):
        raise fail("The Compose file cannot be read as Compose YAML.", "invalid_compose") from None
    finally:
        loader.dispose()


def precheck(
    text: str,
    *,
    kind: StackKind = "compose",
    files: Sequence[CompanionFile] = (),
    builds: ServiceBuildCheck | None = None,
) -> FileUses:
    """Refuse a Compose file that would make Compose read other files or run a binary.

    Blocking and CPU-bound; call through asyncio.to_thread. A swarm file may also name external
    configs and secrets, and may not put a secret's value in the file. A file is read only by
    the name of one of `files`, and each companion Compose file and environment file it names is
    checked too. Returns where each file in use is named. `builds` admits a Compose service's
    `build` section: it maps a service path and the service to the refusals by path, and without
    it `build` is refused.
    """
    check_files(files)
    data, index = _load(text)
    return check_structure(data, kind=kind, files=files, lines=index, builds=builds)


def check_structure(
    data: object,
    *,
    kind: StackKind = "compose",
    files: Sequence[CompanionFile] = (),
    lines: Mapping[str, int] | None = None,
    builds: ServiceBuildCheck | None = None,
) -> FileUses:
    """Apply the refused-construct rules to a loaded document and the files it names.

    `lines` is the path-to-line index of the document, which gives a refusal its line.
    `builds` is as for `precheck`; a swarm document refuses `build` whatever it is.
    """
    walk = _Walk(kind, files, builds)
    walk.document(data, 0, None, lines or {})
    return {name: tuple(uses) for name, uses in walk.uses.items()}


def file_name(text: str, *, field: str = "name") -> str:
    """Return the trimmed companion file name, or raise ComposeDocumentRejected."""
    name = text.strip()
    if not name:
        raise _reject(field, "Enter a name for the file.", "invalid")
    problem = _name_problem(name)
    if problem is not None:
        raise _reject(field, problem, "invalid")
    return name


def _name_problem(name: str) -> str | None:
    if FILE_NAME.fullmatch(name) is None:
        return FILE_NAME_MESSAGE
    if name.casefold() in _RESERVED_FILE_NAMES:
        return "This name is used by Compose for its own files."
    return None


def _content_problem(text: str) -> tuple[str, str] | None:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return "A file must be UTF-8 text.", "invalid"
    if size == 0:
        return "The file is empty.", "invalid"
    if size > MAX_FILE_BYTES:
        return "A file can be at most 64 KiB.", "too_large"
    if _CONTROL.search(text) is not None:
        return "The file contains control characters.", "invalid"
    return None


def decode_file(raw: bytes | str, *, field: str = "content") -> str:
    """Return a companion file's text after the UTF-8, size and control-character rules.

    A leading byte order mark is removed and line endings are kept.
    """
    if isinstance(raw, bytes):
        if len(raw) > MAX_FILE_BYTES + len(_BOM):
            raise _reject(field, "A file can be at most 64 KiB.", "too_large")
        try:
            text = raw.removeprefix(_BOM).decode("utf-8")
        except UnicodeDecodeError:
            raise _reject(field, "A file must be UTF-8 text.", "invalid") from None
    else:
        text = raw.removeprefix("﻿")
    problem = _content_problem(text)
    if problem is not None:
        raise _reject(field, *problem)
    return text


def check_files(files: Sequence[CompanionFile]) -> None:
    """Raise ComposeDocumentRejected for a name, content, count or total that breaks the rules."""
    errors: dict[str, str] = {}
    exceeded = False
    seen: set[str] = set()
    total = 0
    if len(files) > MAX_FILES:
        errors["files"] = f"At most {MAX_FILES} files are allowed."
        exceeded = True
    for item in files:
        label = "" if item.row is not None else f"{_shown(item.name)}: "
        problem = _name_problem(item.name)
        if problem is not None:
            errors.setdefault(_file_field(item, "name"), label + problem)
        elif item.name.casefold() in seen:
            errors.setdefault(
                _file_field(item, "name"), label + "This name is already used, ignoring case."
            )
        seen.add(item.name.casefold())
        content = _content_problem(item.content)
        if content is not None:
            errors.setdefault(_file_field(item, "content"), label + content[0])
            exceeded = exceeded or content[1] == "too_large"
        else:
            total += len(item.content.encode("utf-8"))
    if total > MAX_FILES_TOTAL_BYTES:
        errors.setdefault("files", "Files can total at most 128 KiB.")
        exceeded = True
    if errors:
        raise ComposeDocumentRejected(errors, "too_large" if exceeded else "invalid")


def _located(rejected: ComposeDocumentRejected, item: CompanionFile) -> ComposeDocumentRejected:
    message = "; ".join(rejected.fields.values())
    line = min(rejected.lines.values(), default=None)
    return ComposeDocumentRejected(
        {_file_field(item, "content"): f"{item.name}: {message}"},
        rejected.reason,
        path=rejected.path,
        file=item.name,
        lines=None if line is None else {_file_field(item, "content"): line},
    )


def _forbidden_variable(name: str | None) -> bool:
    return name is not None and (name in DISCLOSING_VARIABLES or name.startswith(SOURCE_PREFIX))


def _env_line_problem(line: str) -> str | None:
    candidate = _EXPORT_PREFIX.sub("", line.strip(), count=1).strip()
    if candidate and "=" not in candidate and _forbidden_variable(candidate):
        return RESERVED_MESSAGE.format(name=_shown(candidate))
    for match in _REFERENCE.finditer(line):
        if _forbidden_variable(match[1]):
            return RESERVED_MESSAGE.format(name=_shown(match[1]))
    return None


class _Walk:
    """One pre-check: the stored files, where each is named, and the documents already read."""

    def __init__(
        self,
        kind: StackKind,
        files: Sequence[CompanionFile],
        builds: ServiceBuildCheck | None = None,
    ) -> None:
        self.kind = kind
        self.builds = builds if kind == "compose" else None
        self.files = {item.name: item for item in files}
        self.uses: dict[str, list[FileUse]] = {}
        self._roles: dict[str, set[FileRole]] = {}
        self._scanned: dict[str, None] = {}
        self._done: dict[str, None] = {}
        self._active: list[str] = []
        self._document: str | None = None
        self._queue: list[tuple[str, str]] = []

    def document(
        self, data: object, depth: int, item: CompanionFile | None, lines: Mapping[str, int]
    ) -> None:
        outer_queue, self._queue = self._queue, []
        outer_document, self._document = self._document, None if item is None else item.name
        try:
            try:
                self._structure(data)
                queue = self._queue
            finally:
                self._queue, self._document = outer_queue, outer_document
            for name, path in queue:
                self._descend(name, path, depth)
        except ComposeDocumentRejected as rejected:
            if rejected.path is not None and rejected.file is None and not rejected.lines:
                line = _line_of(lines, rejected.path)
                if line is not None:
                    rejected.lines[next(iter(rejected.fields))] = line
            raise

    def reference(
        self,
        path: str,
        value: object,
        role: FileRole,
        *,
        needs: Literal["secret", "plain"] | None = None,
        plain_message: str = "a config must be read from a plain file.",
    ) -> None:
        if not isinstance(value, str) or "$" in value:
            _refuse(path, REFERENCE_MESSAGE)
        name = value.removeprefix("./")
        if FILE_NAME.fullmatch(name) is None:
            _refuse(path, REFERENCE_MESSAGE)
        item = self.files.get(name)
        if item is None:
            raise ComposeDocumentRejected(
                {COMPOSE_FIELD: f"`{path}`: {MISSING_MESSAGE.format(name=name)}"},
                "file_missing",
                path=path,
            )
        if role == "compose" and item.secret:
            _refuse(path, "a Compose file added to a stack must be a plain file.")
        if needs == "secret" and not item.secret:
            _refuse(path, "a secret must be read from a secret file.")
        if needs == "plain" and item.secret:
            _refuse(path, plain_message)
        roles = self._roles.setdefault(name, set())
        if (role == "compose" and roles - {"compose"}) or (
            role != "compose" and "compose" in roles
        ):
            _refuse(path, f"the file {name} is used as a Compose file and for something else.")
        self._roles[name] = {*roles, role}
        self.uses.setdefault(name, []).append(FileUse(path, role, self._document))
        if role == "env":
            self._scan(item, path)
        elif role == "compose":
            self._queue.append((name, path))

    def _scan(self, item: CompanionFile, path: str) -> None:
        if item.name in self._scanned:
            return
        self._scanned[item.name] = None
        for number, line in enumerate(item.content.splitlines(), start=1):
            problem = _env_line_problem(line)
            if problem is not None:
                raise ComposeDocumentRejected(
                    {
                        _file_field(item, "content"): f"`{path}`: {item.name} line {number}: "
                        f"{problem}"
                    },
                    "refused_construct",
                    path=path,
                    file=item.name,
                    lines={_file_field(item, "content"): number},
                )

    def _descend(self, name: str, path: str, depth: int) -> None:
        if depth + 1 > MAX_INCLUDE_DEPTH:
            _refuse(path, f"Compose files nest more than {MAX_INCLUDE_DEPTH} levels deep.")
        if name in self._active:
            _refuse(path, f"this leads back to the file {name}.")
        if name in self._done:
            return
        item = self.files[name]
        self._active.append(name)
        try:
            loaded, index = _load(item.content, item)
            try:
                self.document(loaded, depth + 1, item, index)
            except ComposeDocumentRejected as rejected:
                if rejected.file is not None:
                    raise
                raise _located(rejected, item) from None
        finally:
            self._active.pop()
        self._done[name] = None

    def _structure(self, data: object) -> None:
        if not isinstance(data, Mapping):
            raise _reject(
                COMPOSE_FIELD,
                "The Compose file must be a mapping at the top level.",
                "invalid_compose",
            )
        for key in data:
            folded = _fold(key)
            if folded is None:
                _refuse("<key>", "keys must be text.")
            if folded == "include" and key != "include":
                _refuse(path_part(key), FILE_MESSAGE)
            if folded == "models":
                _refuse("models", "this key is not supported.")
            if not (key in TOP_LEVEL_KEYS or key.startswith("x-")):
                _refuse(path_part(key), "this top-level key is not supported.")
        if "include" in data:
            self._include(data["include"])
        seen: set[int] = set()
        services = data.get("services")
        if isinstance(services, Mapping):
            for name, service in services.items():
                if not isinstance(name, str):
                    _refuse("services.<key>", "keys must be text.")
                if isinstance(service, Mapping) and id(service) not in seen:
                    seen.add(id(service))
                    self._service(f"services.{path_part(name)}", service)
        for section in ("configs", "secrets"):
            entries = data.get(section)
            if isinstance(entries, Mapping):
                for name, entry in entries.items():
                    if not isinstance(name, str):
                        _refuse(f"{section}.<key>", "keys must be text.")
                    self._source(f"{section}.{path_part(name)}", entry, secret=section == "secrets")
        _check_references("", data, set())

    def _include(self, entries: object) -> None:
        if not isinstance(entries, list):
            _refuse("include", REFERENCE_MESSAGE)
        for index, entry in enumerate(entries):
            path = f"include[{index}]"
            if isinstance(entry, Mapping):
                for key in entry:
                    if key != "path":
                        _refuse(f"{path}.{path_part(key)}", INCLUDE_MESSAGE)
                target = entry.get("path")
                if isinstance(target, list):
                    for position, item in enumerate(target):
                        self.reference(f"{path}.path[{position}]", item, "compose")
                else:
                    self.reference(f"{path}.path", target, "compose")
            else:
                self.reference(path, entry, "compose")

    def _service(self, base: str, service: Mapping[Any, Any]) -> None:
        for key, value in service.items():
            folded = _fold(key)
            if folded is None:
                _refuse(f"{base}.<key>", "keys must be text.")
            path = f"{base}.{path_part(key)}"
            if folded in _FILE_SERVICE_KEYS and key != folded:
                _refuse(path, FILE_MESSAGE)
            if key == "env_file":
                self._env_file(path, value)
            elif key == "label_file":
                self._label_file(path, value)
            message = REFUSED_SERVICE_KEYS.get(folded)
            if message is not None and not (key == "build" and self.builds is not None):
                _refuse(path, message)
            if folded == "extends" and isinstance(value, Mapping):
                self._extends(path, key, value)
            if folded == "pull_policy" and isinstance(value, str) and _fold(value) == "build":
                _refuse(path, BUILD_MESSAGE)
        if self.builds is not None:
            for path, message in self.builds(base, service).items():
                _refuse(path, message)

    def _env_file(self, path: str, value: object) -> None:
        if not isinstance(value, list):
            self.reference(path, value, "env")
            return
        for index, entry in enumerate(value):
            where = f"{path}[{index}]"
            if not isinstance(entry, Mapping):
                self.reference(where, entry, "env")
                continue
            for key in entry:
                if key not in _ENV_FILE_ENTRY_KEYS:
                    _refuse(f"{where}.{path_part(key)}", REFERENCE_MESSAGE)
            self.reference(f"{where}.path", entry.get("path"), "env")
            if not isinstance(entry.get("required", True), bool):
                _refuse(f"{where}.required", "this must be true or false.")
            if not isinstance(entry.get("format", ""), str):
                _refuse(f"{where}.format", "this must be text.")

    def _label_file(self, path: str, value: object) -> None:
        entries = value if isinstance(value, list) else [value]
        for index, entry in enumerate(entries):
            self.reference(
                f"{path}[{index}]" if isinstance(value, list) else path,
                entry,
                "env",
                needs="plain",
                plain_message="a label file must be a plain file.",
            )

    def _extends(self, path: str, key: object, value: Mapping[Any, Any]) -> None:
        for sub in value:
            if _fold(sub) != "file":
                continue
            if sub != "file" or key != "extends":
                _refuse(f"{path}.file", FILE_MESSAGE)
            self.reference(f"{path}.file", value[sub], "compose")

    def _source(self, path: str, entry: object, *, secret: bool) -> None:
        swarm = self.kind == "swarm"
        message = SWARM_SOURCE_MESSAGE if swarm else SOURCE_MESSAGE
        allowed = _SWARM_SOURCE_ENTRY_KEYS if swarm else _SOURCE_ENTRY_KEYS
        sources = _SWARM_SOURCES if swarm else ("content", "environment", "file")
        if not isinstance(entry, Mapping):
            _refuse(path, message)
        for key in entry:
            if _fold(key) == "file" and key != "file":
                _refuse(f"{path}.file", FILE_MESSAGE)
        if swarm and secret and "content" in entry:
            _refuse(f"{path}.content", SWARM_SECRET_CONTENT_MESSAGE)
        for key in entry:
            if not (key in allowed or (isinstance(key, str) and key.startswith("x-"))):
                _refuse(f"{path}.{path_part(key)}", message)
        if not any(source in entry for source in sources):
            _refuse(path, message)
        if "file" in entry:
            needs: Literal["secret", "plain"] | None = (
                "secret" if secret else ("plain" if swarm else None)
            )
            self.reference(f"{path}.file", entry["file"], "source", needs=needs)
        variable = entry.get("environment")
        refusal = source_variable_refusal(variable) if isinstance(variable, str) else None
        if refusal is not None:
            _refuse(f"{path}.environment", refusal)


def _check_references(path: str, node: object, seen: set[int]) -> None:
    """Refuse the references and valueless entries that would read a disclosing variable."""
    if isinstance(node, str):
        _check_text(path, node)
    elif isinstance(node, Mapping):
        if id(node) in seen:
            return
        seen.add(id(node))
        for key, value in node.items():
            child = path_part(key)
            where = f"{path}.{child}" if path else child
            if _fold(key) == "environment" and isinstance(value, Mapping | list):
                _check_environment(where, value)
            _check_references(where, value, seen)
    elif isinstance(node, list):
        if id(node) in seen:
            return
        seen.add(id(node))
        for index, item in enumerate(node):
            _check_references(f"{path}[{index}]", item, seen)


def _check_text(path: str, text: str) -> None:
    for match in _REFERENCE.finditer(text):
        if _forbidden_variable(match[1]):
            _refuse(path or "<document>", RESERVED_MESSAGE.format(name=_shown(match[1])))


def _check_environment(path: str, entries: Mapping[Any, Any] | list[Any]) -> None:
    if isinstance(entries, Mapping):
        for name, value in entries.items():
            if isinstance(name, str):
                _check_environment_name(f"{path}.{path_part(name)}", name, valueless=value is None)
        return
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            name, separator, _ = entry.partition("=")
            _check_environment_name(f"{path}[{index}]", name, valueless=not separator)


def _check_environment_name(path: str, name: str, *, valueless: bool) -> None:
    if "$" in name:
        _refuse(path, INTERPOLATED_NAME_MESSAGE)
    # Compose fills a valueless entry from its own process environment, which is case-sensitive.
    if valueless and _forbidden_variable(name.strip()):
        _refuse(path, RESERVED_MESSAGE.format(name=_shown(name.strip())))


def source_variable_refusal(variable: str) -> str | None:
    """Return why a config or secret `environment` source may not name this variable, or None."""
    name = variable.strip()
    if "$" in name:
        return INTERPOLATED_NAME_MESSAGE
    if _forbidden_variable(name):
        return RESERVED_MESSAGE.format(name=_shown(name))
    return None


@dataclass(frozen=True, slots=True)
class VariableInput:
    """One submitted variable row.

    A None value on a secret keeps the stored value; remove deletes the variable. row is the form
    row index, used to key field errors.
    """

    name: str
    value: str | None = dataclasses.field(repr=False)
    secret: bool
    remove: bool
    row: int = 0


def is_reserved_variable(name: str) -> bool:
    """Report whether a variable name steers Compose or the process, case-insensitively."""
    return is_reserved(name)


def parse_variables(form: Mapping[str, str]) -> tuple[VariableInput, ...]:
    """Collect the `variable-N-*` form rows in row order, skipping blank rows; never raises."""
    rows: dict[int, dict[str, str]] = {}
    for field, value in form.items():
        match = _ROW.fullmatch(field)
        if match is not None:
            rows.setdefault(int(match[1]), {})[match[2]] = value
    result: list[VariableInput] = []
    for index in sorted(rows):
        row = rows[index]
        name = row.get("name", "").strip()
        value = row.get("value", "")
        secret = row.get("secret", "").strip().casefold() in _TRUTHY
        remove = row.get("remove", "").strip().casefold() in _TRUTHY
        if not name and value == "":
            continue
        if remove:
            kept: str | None = None
        else:
            kept = None if secret and value == "" else value
        result.append(VariableInput(name, kept, secret, remove, index))
    return tuple(result)


def parse_files(form: Mapping[str, str | bytes]) -> tuple[CompanionFile, ...]:
    """Collect the `file-N-*` form rows in row order, skipping blank rows.

    A row gives a name and exactly one of `content` (pasted text) and `upload` (the file's
    bytes). ComposeDocumentRejected keyed `file-N-name` or `file-N-content` for a row that names
    no file, gives neither or both, or whose text breaks the content rules. Names, counts and
    totals are checked by `check_files`.
    """
    rows: dict[int, dict[str, str | bytes]] = {}
    for field, value in form.items():
        match = _FILE_ROW.fullmatch(field)
        if match is not None:
            rows.setdefault(int(match[1]), {})[match[2]] = value
    files: list[CompanionFile] = []
    errors: dict[str, str] = {}
    first_reason = "invalid"
    for index in sorted(rows):
        row = rows[index]
        name = _text(row.get("name", "")).strip()
        pasted = _text(row.get("content", ""))
        uploaded = row.get("upload", b"")
        has_upload = len(uploaded) > 0
        if not name and not has_upload and not pasted.strip():
            continue
        name_field, content_field = f"file-{index}-name", f"file-{index}-content"
        if not name:
            errors[name_field] = "Enter a name for the file."
        if has_upload == (pasted != ""):
            errors[content_field] = (
                "Use either the text box or a file, not both."
                if has_upload
                else "Enter the file's content or choose a file to upload."
            )
            continue
        try:
            text = decode_file(uploaded if has_upload else pasted, field=content_field)
        except ComposeDocumentRejected as rejected:
            errors.update(rejected.fields)
            if rejected.reason == "too_large":
                first_reason = "too_large"
            continue
        secret = _text(row.get("secret", "")).strip().casefold() in _TRUTHY
        files.append(CompanionFile(name, text, secret, index))
    if errors:
        raise ComposeDocumentRejected(errors, first_reason)
    return tuple(files)


def _text(value: str | bytes) -> str:
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def check_variables(variables: Sequence[VariableInput]) -> None:
    """Raise ComposeDocumentRejected for a name, value, count or total that breaks the rules."""
    errors: dict[str, str] = {}
    first_row: dict[str, int] = {}
    kept = 0
    total = 0
    for item in variables:
        name_field = f"variable-{item.row}-name"
        value_field = f"variable-{item.row}-value"
        if VARIABLE_NAME.fullmatch(item.name) is None:
            errors.setdefault(
                name_field,
                "Use letters, digits and '_', starting with a letter or '_', "
                "at most 128 characters.",
            )
            continue
        if item.name in first_row:
            errors.setdefault(
                name_field, f"This name is already used in row {first_row[item.name] + 1}."
            )
            continue
        first_row[item.name] = item.row
        if is_reserved_variable(item.name):
            errors.setdefault(
                name_field,
                "This name is reserved because it changes how Compose or the process runs.",
            )
            continue
        if item.remove:
            continue
        kept += 1
        if item.value is None:
            total += len(item.name)
            continue
        size = len(item.value.encode("utf-8", "replace"))
        if size > MAX_VARIABLE_VALUE_BYTES:
            errors.setdefault(value_field, "A value can be at most 8 KiB.")
        elif any(unicodedata.category(char) == "Cs" for char in item.value):
            errors.setdefault(value_field, "A value must be valid text.")
        elif any(char != "\t" and unicodedata.category(char) == "Cc" for char in item.value):
            errors.setdefault(value_field, "A value must be a single line.")
        total += len(item.name) + size
    if kept > MAX_VARIABLES:
        errors.setdefault("variables", f"At most {MAX_VARIABLES} variables are allowed.")
    if total > MAX_VARIABLES_TOTAL_BYTES:
        errors.setdefault("variables", "Variables can total at most 64 KiB.")
    if errors:
        raise ComposeDocumentRejected(errors, "invalid")
