"""The outcome of producing a stack's files from a host, and the helpers its producers share."""

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from app.db.repositories.stack import StackKind
from app.docker_engine.models import ContainerSummary, SwarmService
from app.services.stack_document import (
    COMPOSE_FIELD,
    MAX_COMPOSE_BYTES,
    VARIABLE_NAME,
    CompanionFile,
    ComposeDocumentRejected,
    VariableInput,
    check_variables,
    compose_nodes,
    decode_file,
    is_reserved_variable,
    precheck,
)

__all__ = [
    "SECRET_NAME",
    "FileSource",
    "HostReadResult",
    "NeedsFile",
    "Octal",
    "ReadFile",
    "ReadOutcome",
    "ReadVariable",
    "SecretVariables",
    "adoption_inputs",
    "compose_host_state",
    "cpu_amount",
    "dump_document",
    "duration_text",
    "finish",
    "is_secret_entry",
    "is_secret_name",
    "mask_secret_values",
    "swarm_host_state",
]

SECRET_NAME: Final = re.compile(
    r"PASS|PWD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH|PRIVATE|DSN", re.IGNORECASE
)
URL_CREDENTIALS: Final = re.compile(r"://[^/\s@]*:[^/\s@]*@")
HIDDEN: Final = "(hidden)"
CONFIG_HASH_LABEL: Final = "com.docker.compose.config-hash"
ONEOFF_LABEL: Final = "com.docker.compose.oneoff"

_PLAIN: Final = re.compile(r"[A-Za-z_/][A-Za-z0-9_./:@+-]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_STR_TAG: Final = "tag:yaml.org,2002:str"
_INT_TAG: Final = "tag:yaml.org,2002:int"
_NOT_NAME: Final = re.compile(r"[^A-Za-z0-9_]")
_UNITS: Final = (
    ("h", 3_600_000_000_000),
    ("m", 60_000_000_000),
    ("s", 1_000_000_000),
    ("ms", 1_000_000),
    ("us", 1_000),
    ("ns", 1),
)

_NANO: Final = Decimal(10**9)

type FileSource = Literal["read", "merged", "generated", "reconstructed"]


def adoption_inputs(
    result: "HostReadResult",
) -> tuple[str, tuple[VariableInput, ...], tuple[CompanionFile, ...]]:
    """Return a result's text, variables and companion files in the form the stack services take."""
    variables = tuple(
        VariableInput(variable.name, variable.value, variable.secret, False, row)
        for row, variable in enumerate(result.variables)
    )
    files = tuple(
        CompanionFile(item.name, decode_file(item.content, field=item.name), item.secret)
        for item in result.files
    )
    return result.compose_text, variables, files


def is_secret_name(name: str) -> bool:
    """Report whether a variable or environment name marks its value as a secret."""
    return SECRET_NAME.search(name) is not None


def is_secret_entry(name: str, value: str) -> bool:
    """Report whether a value is a secret by its name or by carrying URL credentials."""
    return is_secret_name(name) or URL_CREDENTIALS.search(value) is not None


def compose_host_state(containers: Iterable[ContainerSummary]) -> tuple[str, ...]:
    """Fingerprint a project's containers by id and config hash; one-off containers are ignored."""
    return _state(
        (c.id, c.labels.get(CONFIG_HASH_LABEL, ""))
        for c in containers
        if c.labels.get(ONEOFF_LABEL, "").lower() != "true"
    )


def swarm_host_state(services: Iterable[SwarmService]) -> tuple[str, ...]:
    """Fingerprint a namespace's services by id and spec version index."""
    return _state((service.id, str(service.version.index)) for service in services)


def _state(entries: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(sorted(f"{identifier}={token}" for identifier, token in entries))


def mask_secret_values(text: str) -> str:
    """Return Compose text with every secret environment value replaced by `(hidden)`.

    For display only; the stored text is unchanged. A text that cannot be parsed is not shown.
    """
    try:
        root = compose_nodes(text)
    except ComposeDocumentRejected:
        return HIDDEN
    spans: list[tuple[int, int, str]] = []
    for service in _service_nodes(root):
        for key, value in _pairs(service):
            if key == "environment":
                spans.extend(_environment_spans(value))
    for start, end, replacement in sorted(set(spans), reverse=True):
        text = f"{text[:start]}{replacement}{text[end:]}"
    return text


def _pairs(node: Node) -> list[tuple[str | None, Node]]:
    if not isinstance(node, MappingNode):
        return []
    return [
        (key.value if isinstance(key, ScalarNode) else None, value) for key, value in node.value
    ]


def _service_nodes(root: Node) -> list[Node]:
    services = [value for key, value in _pairs(root) if key == "services"]
    return [service for group in services for _, service in _pairs(group)]


def _environment_spans(node: Node) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    if isinstance(node, MappingNode):
        for key, value in node.value:
            if (
                isinstance(key, ScalarNode)
                and isinstance(value, ScalarNode)
                and value.value
                and is_secret_entry(key.value, value.value)
            ):
                spans.append((value.start_mark.index, value.end_mark.index, f"'{HIDDEN}'"))
    elif isinstance(node, SequenceNode):
        for item in node.value:
            if not isinstance(item, ScalarNode):
                continue
            name, separator, value = item.value.partition("=")
            if separator and value and is_secret_entry(name, value):
                spans.append((item.start_mark.index, item.end_mark.index, f"'{name}={HIDDEN}'"))
    return spans


@dataclass(frozen=True, slots=True)
class ReadFile:
    """A companion file found with the stack's files."""

    name: str
    content: bytes = field(repr=False)
    secret: bool = False


@dataclass(frozen=True, slots=True)
class ReadVariable:
    """A stack variable found with the stack's files."""

    name: str
    value: str = field(repr=False)
    secret: bool = False


@dataclass(frozen=True, slots=True)
class HostReadResult:
    """A stack's Compose text with its companion files and variables, ready to be stored.

    `host_state` is a fingerprint of the host objects the read described, None when none was
    taken; an adoption of this result is refused once the host no longer matches it.
    """

    compose_text: str = field(repr=False)
    file_source: FileSource
    files: tuple[ReadFile, ...] = ()
    variables: tuple[ReadVariable, ...] = ()
    losses: tuple[str, ...] = ()
    host_state: tuple[str, ...] | None = None

    def matches_host(self, current: tuple[str, ...]) -> bool:
        """Report whether the host still looks as it did when this result was read."""
        return self.host_state is None or self.host_state == current


@dataclass(frozen=True, slots=True)
class NeedsFile:
    """No file could be produced; reason is a stable identifier and path names the field."""

    reason: str
    path: str


type ReadOutcome = HostReadResult | NeedsFile


class Octal(int):
    """An integer written as an octal literal, the form Compose reads a file mode in."""


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = None if _PLAIN.fullmatch(data) else "'"
    return dumper.represent_scalar(_STR_TAG, data, style=style)


def _represent_octal(dumper: yaml.SafeDumper, data: Octal) -> yaml.ScalarNode:
    return dumper.represent_scalar(_INT_TAG, f"0{int(data):o}")


_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(Octal, _represent_octal)


def dump_document(document: Mapping[str, Any]) -> str:
    """Write a document as block YAML with every string that is not a plain word quoted."""
    return yaml.dump(
        dict(document),
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1 << 20,
    )


def duration_text(nanoseconds: int) -> str:
    """Write a duration as a Go duration string such as 1m30s or 500ms."""
    if nanoseconds == 0:
        return "0s"
    remaining = nanoseconds
    parts: list[str] = []
    for unit, size in _UNITS:
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)


def cpu_amount(nanocpus: int) -> int | float:
    """Return a CPU quota in CPUs."""
    amount = Decimal(nanocpus) / _NANO
    return int(amount) if amount == amount.to_integral_value() else float(amount)


class SecretVariables:
    """Secret stack variables collected while writing a document, each named once."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def reference(self, scope: str, name: str, value: str) -> str | None:
        """Return `${VARIABLE}` standing for the value, or None when it cannot be a variable.

        Names are unique per value: a second value under the same environment name takes a name
        built from `scope`.
        """
        try:
            check_variables([VariableInput("PROBE", value, True, False)])
        except ComposeDocumentRejected:
            return None
        for candidate in self._candidates(scope, name):
            held = self._values.setdefault(candidate, value)
            if held == value:
                return f"${{{candidate}}}"
        return None

    @property
    def variables(self) -> tuple[ReadVariable, ...]:
        """Return the collected variables in the order they were first named."""
        return tuple(ReadVariable(name, value, True) for name, value in self._values.items())

    def _candidates(self, scope: str, name: str) -> Iterator[str]:
        yield from (
            candidate
            for candidate in (_name_of(name), _name_of(f"{scope}_{name}"))
            if self._usable(candidate)
        )
        number = 1
        while True:
            yield f"SECRET_{number}"
            number += 1

    @staticmethod
    def _usable(name: str) -> bool:
        return VARIABLE_NAME.fullmatch(name) is not None and not is_reserved_variable(name)


def _name_of(text: str) -> str:
    name = _NOT_NAME.sub("_", text)
    return f"_{name}" if name[:1].isdigit() else name


def finish(
    text: str,
    *,
    kind: StackKind,
    source: FileSource,
    files: Sequence[ReadFile] = (),
    variables: Sequence[ReadVariable] = (),
    losses: Sequence[str] = (),
) -> ReadOutcome:
    """Return the result when the text, files and variables pass the stack's own checks.

    Blocking and CPU-bound; call through asyncio.to_thread.
    """
    if len(text.encode("utf-8")) > MAX_COMPOSE_BYTES:
        return NeedsFile("too_large", COMPOSE_FIELD)
    companions: list[CompanionFile] = []
    for item in files:
        try:
            companions.append(
                CompanionFile(item.name, decode_file(item.content, field=item.name), item.secret)
            )
        except ComposeDocumentRejected as rejected:
            return NeedsFile(rejected.reason, item.name)
    try:
        precheck(text, kind=kind, files=companions)
        check_variables(
            [
                VariableInput(variable.name, variable.value, variable.secret, False, row)
                for row, variable in enumerate(variables)
            ]
        )
    except ComposeDocumentRejected as rejected:
        where = rejected.path or rejected.file or next(iter(rejected.fields), COMPOSE_FIELD)
        return NeedsFile(rejected.reason, where)
    return HostReadResult(text, source, tuple(files), tuple(variables), tuple(losses))
