"""Static check of a Dockerfile for refused constructs, and the image references it needs."""

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NoReturn

__all__ = [
    "MAX_DOCKERFILE_BYTES",
    "DockerfileReferences",
    "DockerfileRefusal",
    "check_dockerfile",
]

MAX_DOCKERFILE_BYTES: Final = 1_048_576

_INSTRUCTIONS: Final = frozenset(
    {
        "add",
        "arg",
        "cmd",
        "copy",
        "entrypoint",
        "env",
        "expose",
        "from",
        "healthcheck",
        "label",
        "maintainer",
        "onbuild",
        "run",
        "shell",
        "stopsignal",
        "user",
        "volume",
        "workdir",
    }
)
_BOM: Final = "\ufeff"
_HEREDOC_INSTRUCTIONS: Final = frozenset({"add", "copy", "run"})
_FLAG_INSTRUCTIONS: Final = frozenset({"add", "copy", "from", "run"})
_MOUNT_TYPES: Final = frozenset({"bind", "cache", "tmpfs"})
_ALLOWED_NETWORKS: Final = frozenset({"default", "none"})
_VALUELESS_MOUNT_FIELDS: Final = frozenset({"readonly", "ro", "rw", "readwrite", "required"})

_DIRECTIVE: Final = re.compile(r"#\s*([a-zA-Z][a-zA-Z0-9]*)\s*=\s*(.+?)\s*", re.ASCII)
_STRAY_DIRECTIVE: Final = re.compile(r"#\s*(syntax|escape)\s*=", re.ASCII | re.IGNORECASE)
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LEADING_SPACE: Final = re.compile(
    r"\A[\t\n\v\f\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)
_WHITESPACE: Final = re.compile(r"[\t\v\f\r ]+")
_HEREDOC_WORD: Final = re.compile(r"(\d*)<<(-?)([^<]*)", re.ASCII)
_REFERENCE: Final = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_STAGE_NAME: Final = re.compile(r"[a-z][a-z0-9_.-]*")
_UNSAFE_REFERENCE: Final = re.compile(r"[\s'\"\\$]")
_FLAG_LEXING: Final = re.compile(r"['\"\\`]")
_REMOTE_SOURCE: Final = re.compile(r"://|\A[^\s/:@]+@[^\s/:]+:")

_SYNTAX_MESSAGE: Final = "the `syntax` parser directive is not supported."
_ESCAPE_MESSAGE: Final = (
    "the `escape` parser directive must be the first line, and a backslash or backtick."
)
_FLAG_LEXING_MESSAGE: Final = "quotes, backslashes and backticks are not supported in flags."
_ONBUILD_MESSAGE: Final = "ONBUILD is not supported."
_REMOTE_ADD_MESSAGE: Final = (
    "ADD from a URL or a Git repository needs --checksum, and a variable cannot name its source."
)


@dataclass(frozen=True, slots=True)
class DockerfileReferences:
    """The images a Dockerfile needs: its `FROM` bases and its `--from` images."""

    bases: tuple[str, ...]
    copy_sources: tuple[str, ...]

    @property
    def images(self) -> tuple[str, ...]:
        """Return every image reference once, bases first."""
        return tuple(dict.fromkeys((*self.bases, *self.copy_sources)))


@dataclass(frozen=True, slots=True)
class DockerfileRefusal:
    """A refused construct; line is 1-based, and 0 when the problem is the file as a whole."""

    line: int
    message: str


class _Refused(Exception):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


def check_dockerfile(
    content: bytes, args: Mapping[str, str]
) -> DockerfileReferences | DockerfileRefusal:
    """Return the image references of a Dockerfile, or the first construct that is refused.

    `args` are the build's resolved arguments; `FROM` references resolve from them and from the
    defaults of the global `ARG` instructions they name.
    """
    try:
        return _Checker(_decode(content), args).run()
    except _Refused as refused:
        return DockerfileRefusal(refused.line, refused.message)


def _decode(content: bytes) -> list[str]:
    if len(content) > MAX_DOCKERFILE_BYTES:
        raise _Refused(0, "a Dockerfile can be at most 1 MiB.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _Refused(
            content.count(b"\n", 0, error.start) + 1, "a Dockerfile must be UTF-8."
        ) from None
    lines = [line.removesuffix("\r") for line in text.removeprefix(_BOM).split("\n")]
    for number, line in enumerate(lines, start=1):
        if _CONTROL.search(line) is not None:
            raise _Refused(number, "a Dockerfile cannot contain control characters.")
    return lines


def _words(text: str, escape: str) -> list[str] | None:
    """Split on unquoted whitespace, keeping quotes and escapes; None when a quote is open."""
    words: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            elif char == escape and quote == '"' and index + 1 < len(text):
                index += 1
                current.append(text[index])
        elif char.isspace():
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(char)
            if char in "'\"":
                quote = char
            elif char == escape and index + 1 < len(text):
                index += 1
                current.append(text[index])
            elif char == "$" and text[index + 1 : index + 2] == "{":
                end = text.find("}", index)
                end = len(text) - 1 if end < 0 else end
                current.append(text[index + 1 : end + 1])
                index = end
        index += 1
    if quote:
        return None
    if current:
        words.append("".join(current))
    return words


def _unquote(word: str, escape: str) -> str | None:
    """Remove the quotes and escapes of one word; None when a quote is open."""
    result: list[str] = []
    quote = ""
    index = 0
    while index < len(word):
        char = word[index]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                result.append(char)
        elif char == escape and index + 1 < len(word):
            following = word[index + 1]
            if quote == '"' and following not in ('"', "$", escape):
                result.append(char)
            else:
                index += 1
                result.append(following)
        elif char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        else:
            result.append(char)
        index += 1
    return None if quote else "".join(result)


def _split_flags(rest: str) -> tuple[list[str], str]:
    """Split the leading `--flag` words off an instruction's arguments on whitespace."""
    flags: list[str] = []
    position = 0
    while True:
        while position < len(rest) and rest[position].isspace():
            position += 1
        if not rest.startswith("--", position):
            return flags, rest[position:].strip()
        end = position
        while end < len(rest) and not rest[end].isspace():
            end += 1
        word = rest[position:end]
        position = end
        if word == "--":
            return flags, rest[position:].strip()
        flags.append(word)


def _flag(text: str) -> tuple[str, str | None]:
    name, separator, value = text[2:].partition("=")
    return name.lower(), value if separator else None


@dataclass(slots=True)
class _Heredoc:
    name: str
    chomp: bool


class _Checker:
    def __init__(self, lines: list[str], args: Mapping[str, str]) -> None:
        self.lines = lines
        self.args = args
        self.escape = "\\"
        self.position = 0
        self.stages: list[str | None] = []
        self.values: dict[str, str | None] = {}
        self.bases: dict[str, None] = {}
        self.copy_sources: dict[str, None] = {}

    def run(self) -> DockerfileReferences:
        self._directives()
        while self.position < len(self.lines):
            self._instruction_at()
        if not self.stages:
            raise _Refused(0, "a Dockerfile needs a FROM instruction.")
        return DockerfileReferences(tuple(self.bases), tuple(self.copy_sources))

    def _directives(self) -> None:
        seen: set[str] = set()
        while self.position < len(self.lines):
            match = _DIRECTIVE.fullmatch(_LEADING_SPACE.sub("", self.lines[self.position], count=1))
            if match is None or match[1].lower() not in ("syntax", "escape", "check"):
                return
            name, value = match[1].lower(), match[2]
            line = self.position + 1
            if name in seen:
                raise _Refused(line, f"the `{name}` parser directive can be given once.")
            seen.add(name)
            if name == "syntax":
                raise _Refused(line, _SYNTAX_MESSAGE)
            if name == "escape":
                if value not in ("\\", "`"):
                    raise _Refused(line, _ESCAPE_MESSAGE)
                self.escape = value
            self.position += 1

    def _comment(self, stripped: str, line: int) -> bool:
        if not stripped.startswith("#"):
            return False
        stray = _STRAY_DIRECTIVE.match(stripped)
        if stray is not None:
            raise _Refused(
                line, _SYNTAX_MESSAGE if stray[1].lower() == "syntax" else _ESCAPE_MESSAGE
            )
        return True

    def _continues(self, text: str) -> tuple[str, bool]:
        stripped = text.rstrip(" \t")
        if stripped.endswith(self.escape):
            return stripped[:-1], False
        return text, True

    def _instruction_at(self) -> None:
        start = self.position + 1
        first = _LEADING_SPACE.sub("", self.lines[self.position], count=1)
        self.position += 1
        if self._comment(first, start):
            return
        text, ended = self._continues(first)
        if ended and text == "":
            return
        parts = [text]
        while not ended and self.position < len(self.lines):
            raw = self.lines[self.position]
            probe = _LEADING_SPACE.sub("", raw, count=1)
            self.position += 1
            if self._comment(probe, self.position) or probe == "":
                continue
            text, ended = self._continues(raw)
            parts.append(text)
        self._dispatch("".join(parts), start)

    def _fail(self, line: int, message: str) -> NoReturn:
        raise _Refused(line, message)

    def _dispatch(self, logical: str, line: int) -> None:
        trimmed = logical.strip()
        if not trimmed:
            return
        pieces = _WHITESPACE.split(trimmed, maxsplit=1)
        command = pieces[0].lower()
        flags, rest = _split_flags(pieces[1] if len(pieces) == 2 else "")
        if command not in _INSTRUCTIONS:
            self._fail(line, "this instruction is not recognised.")
        if not self.stages and command not in ("arg", "from"):
            self._fail(line, "only ARG can come before the first FROM.")
        if command in _FLAG_INSTRUCTIONS and any(_FLAG_LEXING.search(flag) for flag in flags):
            self._fail(line, _FLAG_LEXING_MESSAGE)
        match command:
            case "from":
                self._from(flags, rest, line)
            case "arg":
                if not self.stages:
                    self._global_arg(rest, line)
            case "onbuild":
                self._fail(line, _ONBUILD_MESSAGE)
            case "run":
                self._check_run(flags, line)
            case "copy":
                self._copy(flags, line)
            case "add":
                self._copy(flags, line)
                self._add(flags, rest, line)
        if command in _HEREDOC_INSTRUCTIONS:
            self._skip_heredocs(rest, line)

    def _global_arg(self, rest: str, line: int) -> None:
        words = _words(rest, self.escape)
        if words is None:
            self._fail(line, "a quote is not closed.")
        for word in words:
            name, separator, raw = word.partition("=")
            default: str | None = None
            if separator:
                default = _unquote(raw, self.escape)
                if default is None:
                    self._fail(line, "a quote is not closed.")
                if "$" in default:
                    default = self._expand(default)
            self.values[name] = self.args.get(name, default)

    def _expand(self, text: str) -> str | None:
        """Substitute `$NAME` and `${NAME}`; None for any other `$` form or an unresolved name."""
        if "$" in _REFERENCE.sub("", text):
            return None
        names = {match[1] or match[2] for match in _REFERENCE.finditer(text)}
        if any(not self.values.get(name) for name in names):
            return None
        return _REFERENCE.sub(lambda match: self.values[match[1] or match[2]] or "", text)

    def _from(self, flags: list[str], rest: str, line: int) -> None:
        for flag in flags:
            if _flag(flag)[0] != "platform":
                self._fail(line, "this FROM flag is not supported.")
        words = _WHITESPACE.split(rest) if rest else []
        if len(words) not in (1, 3) or (len(words) == 3 and words[1].lower() != "as"):
            self._fail(line, "FROM takes an image, and optionally AS and a stage name.")
        reference = self._resolve(words[0], line)
        name: str | None = None
        if len(words) == 3:
            name = words[2].lower()
            if _STAGE_NAME.fullmatch(name) is None:
                self._fail(line, "this stage name is not valid.")
            if name in self.stages:
                self._fail(line, "this stage name is already used.")
        if reference.lower() not in self.stages and reference != "scratch":
            self.bases[reference] = None
        self.stages.append(name)

    def _resolve(self, token: str, line: int) -> str:
        if any(char in token for char in ("'", '"', "\\", self.escape)):
            self._fail(line, "quotes and escapes are not supported in a FROM reference.")
        if "$" in _REFERENCE.sub("", token):
            self._fail(line, "only $NAME and ${NAME} can be used in a FROM reference.")

        def value_of(match: re.Match[str]) -> str:
            name = match[1] or match[2]
            value = self.values.get(name)
            if not value:
                self._fail(line, f"the variable {name} in this FROM reference has no value.")
            return value

        resolved = _REFERENCE.sub(value_of, token)
        if not resolved or _UNSAFE_REFERENCE.search(resolved) is not None:
            self._fail(line, "this FROM reference is not a plain image reference.")
        return resolved

    def _check_run(self, flags: list[str], line: int) -> None:
        for flag in flags:
            name, value = _flag(flag)
            match name:
                case "mount":
                    self._mount(value, line)
                case "network":
                    if value is None or value.lower() not in _ALLOWED_NETWORKS:
                        self._fail(line, "RUN --network can be default or none.")
                case "security":
                    self._fail(line, "RUN --security is not supported.")
                case _:
                    self._fail(line, "this RUN flag is not supported.")

    def _mount(self, value: str | None, line: int) -> None:
        if not value:
            self._fail(line, "RUN --mount needs a value.")
        try:
            fields = next(csv.reader([value], strict=True))
        except (csv.Error, StopIteration):
            self._fail(line, "this RUN --mount value cannot be read.")
        problem: str | None = None
        sources: list[str] = []
        for field in fields:
            key, separator, setting = field.partition("=")
            key = key.strip().lower()
            if "$" in key or (key in ("type", "from") and "$" in setting):
                problem = "a variable cannot be used for the type or source of a mount."
            elif not separator:
                if key not in _VALUELESS_MOUNT_FIELDS:
                    problem = "this RUN --mount value cannot be read."
            elif key == "type":
                mount_type = setting.strip().lower()
                if mount_type in ("ssh", "secret"):
                    self._fail(line, f"RUN --mount=type={mount_type} is not supported.")
                if mount_type not in _MOUNT_TYPES:
                    problem = "this RUN --mount type is not supported."
            elif key == "from":
                sources.append(setting.strip())
        if problem is not None:
            self._fail(line, problem)
        for source in dict.fromkeys(sources):
            self._source(source, line)

    def _copy(self, flags: list[str], line: int) -> None:
        for flag in flags:
            name, value = _flag(flag)
            if name != "from":
                continue
            if not value or "$" in value:
                self._fail(line, "--from takes a stage name or an image, without variables.")
            self._source(value, line)

    def _add(self, flags: list[str], rest: str, line: int) -> None:
        if any(name == "checksum" and value for name, value in map(_flag, flags)):
            return
        for source in self._add_sources(rest, line):
            readings = (source, _unquote(source, self.escape) or "")
            if any("$" in text or _REMOTE_SOURCE.search(text) for text in readings):
                self._fail(line, _REMOTE_ADD_MESSAGE)

    def _add_sources(self, rest: str, line: int) -> list[str]:
        text = rest.strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except (ValueError, RecursionError):
                items = None
            if isinstance(items, list) and all(isinstance(item, str) for item in items):
                return items[:-1]
        words = _words(text, self.escape)
        if words is None:
            self._fail(line, "a quote is not closed.")
        return words[:-1]

    def _source(self, reference: str, line: int) -> None:
        if not reference or _UNSAFE_REFERENCE.search(reference) is not None:
            self._fail(line, "this --from value is not a stage or a plain image reference.")
        if reference.lower() in self.stages[:-1]:
            return
        if reference.isascii() and reference.isdigit():
            if int(reference) >= len(self.stages) - 1:
                self._fail(line, "this --from value names a stage that does not exist yet.")
            return
        self.copy_sources[reference] = None

    def _skip_heredocs(self, args: str, line: int) -> None:
        for heredoc in self._heredocs(args, line):
            while True:
                if self.position >= len(self.lines):
                    self._fail(line, "this heredoc is not terminated.")
                body = self.lines[self.position]
                self.position += 1
                if (body.lstrip("\t") if heredoc.chomp else body) == heredoc.name:
                    break

    def _heredocs(self, args: str, line: int) -> list[_Heredoc]:
        found: list[_Heredoc] = []
        for word in _words(args, self.escape) or ():
            match = _HEREDOC_WORD.fullmatch(word)
            if match is None or not match[3]:
                continue
            names = _words(match[3], self.escape)
            if names is None or len(names) != 1:
                continue
            name = _unquote(names[0], self.escape)
            if name is None:
                continue
            if not name or "$" in name:
                self._fail(line, "this heredoc delimiter is not supported.")
            found.append(_Heredoc(name, match[2] == "-"))
        return found
