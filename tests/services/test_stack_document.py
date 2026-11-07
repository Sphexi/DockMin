"""Tests for app/services/stack_document.py: names, intake, the YAML pre-check and variables."""

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from app.compose_cli.environment import (
    DISCLOSING_VARIABLES,
    RESERVED_NAMES,
    RESERVED_PREFIXES,
)
from app.compose_cli.environment import is_reserved as engine_is_reserved
from app.db.repositories.stack import StackKind
from app.services import stack_document as sd
from app.services.stack_document import ComposeDocumentRejected
from tests.support import present

WEB = "services:\n  web:\n    image: x\n"
LIMIT = sd.MAX_COMPOSE_BYTES
BIG_ALIAS_COUNT = 65
SERVICE_FIELD = "compose_file"


def rejection(call: Callable[..., object], *args: object) -> ComposeDocumentRejected:
    with pytest.raises(ComposeDocumentRejected) as raised:
        call(*args)
    return raised.value


def seq_bomb(levels: int) -> str:
    lines = ["x-a0: &a0 [x, x]"]
    lines += [f"x-a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, levels)]
    return "\n".join(lines) + "\n" + WEB


def merge_bomb(levels: int) -> str:
    lines = ["x-m0: &m0 {k0: 1}"]
    lines += [f"x-m{i}: &m{i} {{<<: [*m{i - 1}, *m{i - 1}], k{i}: 1}}" for i in range(1, levels)]
    return "\n".join(lines) + "\n" + WEB


def alias_chain(levels: int) -> str:
    lines = ["x-c0: &c0 [x]"]
    lines += [f"x-c{i}: &c{i} [*c{i - 1}]" for i in range(1, levels)]
    return "\n".join(lines) + "\n" + WEB


def block_depth(levels: int) -> str:
    return "".join("  " * i + f"k{i}:\n" for i in range(levels)) + "  " * levels + "v: 1\n"


def aliases(count: int) -> str:
    return "x-a: &a v\n" + "".join(f"x-b{i}: *a\n" for i in range(count)) + WEB


class TestStackName:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("shop", "shop"),
            (" shop ", "shop"),
            ("shop\n", "shop"),
            ("a--b_-c", "a--b_-c"),
            ("0abc", "0abc"),
            ("a", "a"),
            ("a" * 63, "a" * 63),
        ],
    )
    def test_a_legal_name_is_returned_trimmed(self, text: str, expected: Any) -> None:
        assert sd.stack_name(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Shop",
            "SHOP",
            "a b",
            "a;touch x",
            "$(id)",
            "`id`",
            "a'b",
            'a"b',
            "a|b",
            "a&b",
            "a>b",
            "a<b",
            "a*b",
            "a\nb",
            "a\x00b",
            "shop\x00",
            "_shop",
            "-p",
            "--project-directory=/",
            "a" * 64,
            "é",
            "sh\u043ep",
            "\uff53hop",
            "١٢",
            "a\ud800",
        ],
    )
    def test_an_illegal_name_is_refused_not_normalised(self, text: str) -> None:
        error = rejection(sd.stack_name, text)

        assert error.reason == "invalid"
        assert list(error.fields) == ["name"]

    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_a_blank_name_asks_for_one(self, text: str) -> None:
        error = rejection(sd.stack_name, text)

        assert error.reason == "invalid"
        assert error.fields == {"name": "Enter a name for the stack."}


class TestDecodeCompose:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"a: 1\n", "a: 1\n"),
            (b"\xef\xbb\xbfa: 1\n", "a: 1\n"),
            (b"a:\t1\r\n", "a:\t1\r\n"),
            ("é: 1\n".encode(), "é: 1\n"),
            (b"a: ; $(id) `x` | && '\"#\n", "a: ; $(id) `x` | && '\"#\n"),
        ],
    )
    def test_text_is_returned_verbatim_without_a_byte_order_mark(
        self, raw: bytes, expected: str
    ) -> None:
        assert sd.decode_compose(raw) == expected

    def test_a_file_of_exactly_the_cap_is_accepted(self) -> None:
        assert len(sd.decode_compose(b"a" * LIMIT)) == LIMIT

    def test_a_file_one_byte_over_the_cap_is_too_large(self) -> None:
        error = rejection(sd.decode_compose, b"a" * (LIMIT + 1))

        assert error.reason == "too_large"
        assert list(error.fields) == [SERVICE_FIELD]

    def test_the_size_cap_applies_before_decoding(self) -> None:
        error = rejection(sd.decode_compose, b"\xff" * (LIMIT + 1))

        assert error.reason == "too_large"

    @pytest.mark.parametrize(
        "raw",
        [
            b"\xff\xfe",
            b"\xed\xa0\x80",
            b"a\x00b",
            b"a\x1bb",
            b"a\x08b",
            b"",
            b"  \n",
            b"\xef\xbb\xbf",
        ],
    )
    def test_undecodable_empty_or_control_text_is_invalid(self, raw: bytes) -> None:
        error = rejection(sd.decode_compose, raw)

        assert error.reason == "invalid"
        assert list(error.fields) == [SERVICE_FIELD]


ACCEPTED_DOCUMENTS = {
    "plain service": WEB,
    "extension keys at top level and in a service": (
        "x-a: 1\nservices:\n  web:\n    image: x\n    x-b: {env_file: ignored}\n"
    ),
    "version key": "version: '3'\n" + WEB,
    "extends in the same file as a string": WEB + "  other:\n    extends: web\n",
    "extends mapping without a file": WEB + "  other:\n    extends: {service: web}\n",
    "configs content and secrets environment": (
        WEB + "configs:\n  c: {content: hi}\nsecrets:\n  s: {environment: V}\n"
    ),
    "a small alias": "x-a: &a {k: v}\nservices:\n  web:\n    image: x\n    labels: *a\n",
    "an explicit core tag": "services:\n  web:\n    image: !!str x\n",
    "flow nesting of 20": "x-d: " + "[" * 20 + "]" * 20 + "\n" + WEB,
    "block nesting of 55": (
        "x-d:\n"
        + "".join("  " * (i + 1) + f"k{i}:\n" for i in range(55))
        + "  " * 56
        + "v: 1\n"
        + WEB
    ),
    "exactly 64 aliases": aliases(64),
    "an alias chain of 40": alias_chain(40),
    "a doubling alias tree of 12 levels": seq_bomb(12),
    "shell metacharacters and unicode in values": (
        "services:\n  web:\n    image: x\n"
        "    command: ['sh', '-c', 'echo ; $(touch /tmp/x) `id` | && \"#', 'é$$']\n"
    ),
}


class TestPrecheckAccepts:
    @pytest.mark.parametrize("text", ACCEPTED_DOCUMENTS.values(), ids=ACCEPTED_DOCUMENTS.keys())
    def test_a_legitimate_document_passes(self, text: str) -> None:
        sd.precheck(text)


REFUSED_CONSTRUCTS = {
    "include as a string": ("include: other.yaml\n" + WEB, "include"),
    "an upper-case include": ("Include: [a]\n" + WEB, "Include"),
    "an include with a path outside the stack": ("include: [/etc/x.yml]\n" + WEB, "include[0]"),
    "an include with a project directory": (
        "include:\n  - path: a.yml\n    project_directory: /\n" + WEB,
        "include[0].project_directory",
    ),
    "an include with an env_file": (
        "include:\n  - path: a.yml\n    env_file: b\n" + WEB,
        "include[0].env_file",
    ),
    "models": ("models: {}\n" + WEB, "models"),
    "an unknown top-level key": ("foo: 1\n" + WEB, "foo"),
    "extends with a file": (
        WEB + "  other:\n    extends: {service: web, file: /etc/x.yml}\n",
        "services.other.extends.file",
    ),
    "extends with a null file": (
        WEB + "  other:\n    extends: {service: web, file: null}\n",
        "services.other.extends.file",
    ),
    "extends with a quoted file key": (
        WEB + "  o:\n    extends: {service: web, 'file': ../x}\n",
        "services.o.extends.file",
    ),
    "env_file as a string": (
        WEB + "    env_file: /run/secrets/dockmin_secret_key\n",
        "services.web.env_file",
    ),
    "env_file as a list": (WEB + "    env_file: [../a.env]\n", "services.web.env_file[0]"),
    "env_file with an unknown key": (
        WEB + "    env_file: [{path: a, extra: 1}]\n",
        "services.web.env_file[0].extra",
    ),
    "label_file": (WEB + "    label_file: ~/x\n", "services.web.label_file"),
    "build as a string": (WEB + "    build: .\n", "services.web.build"),
    "build as a mapping": (
        WEB + "    build: {context: ., dockerfile_inline: 'FROM x'}\n",
        "services.web.build",
    ),
    "provider": ("services:\n  web:\n    provider: {type: x}\n", "services.web.provider"),
    "develop": (WEB + "    develop: {watch: []}\n", "services.web.develop"),
    "pull_policy build": (WEB + "    pull_policy: build\n", "services.web.pull_policy"),
    "pull_policy build with padding and case": (
        WEB + "    pull_policy: ' Build '\n",
        "services.web.pull_policy",
    ),
    "configs file": (WEB + "configs:\n  c:\n    file: /etc/passwd\n", "configs.c.file"),
    "configs file with a subdirectory": (
        WEB + "configs:\n  c:\n    file: sub/x\n",
        "configs.c.file",
    ),
    "configs file with interpolation": (
        WEB + "configs:\n  c:\n    file: ${X}\n",
        "configs.c.file",
    ),
    "secrets file": (
        WEB + "secrets:\n  s:\n    file: /run/secrets/dockmin_secret_key\n",
        "secrets.s.file",
    ),
    "configs external": (WEB + "configs:\n  c:\n    external: true\n", "configs.c.external"),
    "secrets without a source": (WEB + "secrets:\n  s: {}\n", "secrets.s"),
    "a null configs entry": (WEB + "configs:\n  c:\n", "configs.c"),
    "a configs entry with an unknown key": (
        WEB + "configs:\n  c: {content: x, driver: d}\n",
        "configs.c.driver",
    ),
    "a non-text service key": (WEB + "    1: x\n", "services.web.<key>"),
    "a non-text top-level key": ("1: x\n" + WEB, "<key>"),
    "upper-case ENV_FILE": (WEB + "    ENV_FILE: a\n", "services.web.ENV_FILE"),
    "a key with trailing space": (WEB + '    "env_file ": a\n', "services.web.<key>"),
}

SPELLINGS_OF_ENV_FILE = {
    "double quoted": WEB + '    "env_file": a\n',
    "single quoted": WEB + "    'env_file': a\n",
    "escaped underscore": WEB + '    "env\\x5ffile": a\n',
    "escaped four-digit letter": WEB + '    "\\u0065nv_file": a\n',
    "escaped eight-digit letter": WEB + '    "\\U00000065nv_file": a\n',
    "explicit key marker": WEB + "    ? env_file\n    : a\n",
    "flow mapping service": "services:\n  web: {image: x, env_file: a}\n",
    "fully flow document": "{services: {web: {image: x, env_file: a}}}\n",
    "whole service aliased": ("x-s: &s {image: x, env_file: a}\nservices:\n  web: *s\n  db: *s\n"),
}

MERGE_KEYS = {
    "in a service from an anchor": (
        "x-c: &c {image: x}\nservices:\n  web:\n    <<: *c\n",
        "services.web.<<",
        4,
    ),
    "in a service from an inline mapping": (
        "services:\n  web:\n    image: x\n    <<: {env_file: [a]}\n",
        "services.web.<<",
        4,
    ),
    "in the services mapping": (
        "x-s: &s {web: {image: x}}\nservices:\n  <<: *s\n",
        "services.<<",
        3,
    ),
    "from a list of anchors": (
        "x-a: &a {image: x}\nx-b: &b {build: .}\nservices:\n  web:\n    <<: [*a, *b]\n",
        "services.web.<<",
        5,
    ),
    "in a config": (
        "x-c: &c {file: /etc/passwd}\nconfigs:\n  c:\n    <<: *c\n",
        "configs.c.<<",
        4,
    ),
    "in a secret": (
        "x-c: &c {file: a}\nsecrets:\n  c:\n    <<: *c\n",
        "secrets.c.<<",
        4,
    ),
    "in an include entry": (
        "x-c: &c {path: a.yaml}\ninclude:\n  - <<: *c\n" + WEB,
        "include[0].<<",
        3,
    ),
    "nested below a service property": (
        "x-c: &c {A: '1'}\nservices:\n  web:\n    image: x\n    environment:\n      <<: *c\n",
        "services.web.environment.<<",
        6,
    ),
}


class TestPrecheckRefusesConstructs:
    @pytest.mark.parametrize(
        ("text", "path"), REFUSED_CONSTRUCTS.values(), ids=REFUSED_CONSTRUCTS.keys()
    )
    def test_a_refused_construct_names_its_compose_path(self, text: str, path: str) -> None:
        error = rejection(sd.precheck, text)

        assert error.reason == "refused_construct"
        assert error.path == path
        assert error.fields[SERVICE_FIELD].startswith(f"`{path}`: ")

    @pytest.mark.parametrize("text", SPELLINGS_OF_ENV_FILE.values(), ids=SPELLINGS_OF_ENV_FILE)
    def test_env_file_is_read_in_every_spelling(self, text: str) -> None:
        error = rejection(sd.precheck, text)

        assert error.reason == "file_missing"
        assert "env_file" in present(error.path)

    @pytest.mark.parametrize(("text", "path", "line"), MERGE_KEYS.values(), ids=MERGE_KEYS.keys())
    def test_a_merge_key_is_refused_naming_its_path_and_line(
        self, text: str, path: str, line: int
    ) -> None:
        error = rejection(sd.precheck, text)

        assert (error.reason, error.path, error.file) == ("refused_construct", path, None)
        assert error.fields[SERVICE_FIELD] == f"`{path}`: {sd.MERGE_KEY_MESSAGE}"
        assert error.lines == {SERVICE_FIELD: line}

    def test_a_merge_key_in_a_companion_document_is_refused_naming_the_file(self) -> None:
        error = refused_with(
            "include: [a.yaml]\n" + WEB,
            companion("a.yaml", "x-c: &c {image: x}\nservices:\n  db:\n    <<: *c\n"),
        )

        assert (error.reason, error.path, error.file) == (
            "refused_construct",
            "services.db.<<",
            "a.yaml",
        )
        assert error.fields["files"].startswith("a.yaml: ")

    def test_a_merge_key_outside_the_checked_sections_is_not_refused(self) -> None:
        uses = sd.precheck("x-a: &a {k: v}\nx-b:\n  <<: *a\n" + WEB)

        assert uses == {}

    def test_reference_message_is_the_same_for_every_file_construct(self) -> None:
        include = rejection(sd.precheck, "include: a\n" + WEB)
        env_file = rejection(sd.precheck, WEB + "    env_file: /a\n")

        assert include.fields[SERVICE_FIELD].endswith(sd.REFERENCE_MESSAGE)
        assert env_file.fields[SERVICE_FIELD].endswith(sd.REFERENCE_MESSAGE)


LOADER_REFUSALS = {
    "duplicate top-level key": "services: {}\nservices: {}\n",
    "duplicate service property": "services:\n  web:\n    image: a\n    image: b\n",
    "duplicate service name": "services:\n  web: {image: a}\n  web: {image: b}\n",
    "duplicate through a quoted spelling": 'services:\n  web:\n    image: a\n    "image": b\n',
    "duplicate through an escaped spelling": (
        'services:\n  web:\n    image: a\n    "im\\x61ge": b\n'
    ),
    "duplicate merge keys": "x-a: &a {k: v}\nservices:\n  web:\n    <<: *a\n    <<: *a\n",
    "two documents": WEB + "---\n" + WEB,
    "a second document after an end marker": WEB + "...\n---\nservices: {}\n",
    "the reset tag": "services:\n  web:\n    image: x\n    ports: !reset []\n",
    "the override tag": "services:\n  web:\n    image: x\n    ports: !override [a]\n",
    "a python object tag": (
        "services:\n  web:\n    image: !!python/object/apply:os.system ['echo hi']\n"
    ),
    "the binary tag": "x-a: !!binary aGk=\n" + WEB,
    "the set tag": "x-a: !!set {a, b}\n" + WEB,
    "an explicit merge tag on a key": (
        "x-a: &a {env_file: [a]}\nservices:\n  web:\n    !!merge '<<': *a\n"
    ),
    "the timestamp tag": "x-a: !!timestamp 2001-01-01\n" + WEB,
    "a custom local tag": "x-a: !foo bar\n" + WEB,
    "a verbatim tag": "x-a: !<tag:evil,2000:x> bar\n" + WEB,
    "flow nesting of 70": "x-d: " + "[" * 70 + "]" * 70 + "\n" + WEB,
    "block nesting of 70": block_depth(70),
    "an alias chain of 63 levels": alias_chain(63),
    "65 aliases": aliases(BIG_ALIAS_COUNT),
    "a recursive alias": "x-a: &a [*a]\n" + WEB,
    "an undefined alias": "x-a: *nope\n" + WEB,
    "a repeated anchor": "x-a: &a 1\nx-b: &a 2\n" + WEB,
    "a list at the root": "- a\n- b\n",
    "a scalar at the root": "just text\n",
    "an explicit empty document": "---\n",
    "a syntax error": "services:\n  web: [unclosed\n",
    "tab indentation": "services:\n\tweb: {}\n",
    "a DEL character": "services: {}\n# \x7f\n",
    "a lone surrogate": "services:\n  web:\n    image: \ud800\n",
    "a complex key": "? [a, b]\n: c\n",
}

EXPANSION_BOMBS = {
    "a doubling sequence tree": seq_bomb(30),
    "a doubling merge tree": merge_bomb(30),
}


class TestPrecheckRefusesDocuments:
    @pytest.mark.parametrize("text", LOADER_REFUSALS.values(), ids=LOADER_REFUSALS.keys())
    def test_an_unreadable_or_unsafe_document_is_invalid_compose(self, text: str) -> None:
        error = rejection(sd.precheck, text)

        assert error.reason == "invalid_compose"
        assert list(error.fields) == [SERVICE_FIELD]
        assert error.path is None

    @pytest.mark.parametrize("text", EXPANSION_BOMBS.values(), ids=EXPANSION_BOMBS.keys())
    def test_an_alias_bomb_is_refused_on_its_expanded_size_not_its_alias_count(
        self, text: str
    ) -> None:
        error = rejection(sd.precheck, text)

        assert error.reason == "invalid_compose"
        assert "expands to more than" in error.fields[SERVICE_FIELD]

    @pytest.mark.parametrize("text", ["", "# nothing\n"])
    def test_an_empty_document_is_invalid(self, text: str) -> None:
        error = rejection(sd.precheck, text)

        assert error.reason == "invalid"

    def test_alias_refusals_name_the_limit(self) -> None:
        error = rejection(sd.precheck, aliases(BIG_ALIAS_COUNT))

        assert str(sd.MAX_ALIASES) in error.fields[SERVICE_FIELD]

    def test_nesting_refusals_name_the_limit(self) -> None:
        error = rejection(sd.precheck, alias_chain(63))

        assert str(sd.MAX_DEPTH) in error.fields[SERVICE_FIELD]

    def test_a_duplicate_key_names_the_key_and_line(self) -> None:
        error = rejection(sd.precheck, "services: {}\nservices: {}\n")

        assert "`services`" in error.fields[SERVICE_FIELD]
        assert "line 2" in error.fields[SERVICE_FIELD]

    def test_a_custom_tag_is_named_in_a_printable_form(self) -> None:
        error = rejection(sd.precheck, "x-a: !<tag:evil,2000:x> bar\n" + WEB)

        assert "tag:evil,2000:x" in error.fields[SERVICE_FIELD]


class TestCheckStructure:
    @pytest.mark.parametrize("data", [["services"], "text", 3, None])
    def test_a_non_mapping_root_is_invalid_compose(self, data: Any) -> None:
        error = rejection(sd.check_structure, data)

        assert error.reason == "invalid_compose"

    @pytest.mark.parametrize(
        ("data", "path"),
        [
            ({1: "x"}, "<key>"),
            ({"services": {1: {}}}, "services.<key>"),
            ({"services": {"web": {2: "x"}}}, "services.web.<key>"),
            ({"configs": {1: {}}}, "configs.<key>"),
            ({"secrets": {"s": {"file": "/x"}}}, "secrets.s.file"),
            ({"services": {"web": {"extends": {"file": "/x"}}}}, "services.web.extends.file"),
        ],
    )
    def test_a_loaded_document_is_checked_the_same_way_as_text(
        self, data: dict[str, Any], path: str
    ) -> None:
        error = rejection(sd.check_structure, data)

        assert error.reason == "refused_construct"
        assert error.path == path

    def test_a_clean_document_passes(self) -> None:
        sd.check_structure({"services": {"web": {"image": "x"}}, "x-a": 1})

    def test_a_shared_service_object_is_checked_once_and_still_refused(self) -> None:
        shared = {"image": "x", "build": "."}

        error = rejection(sd.check_structure, {"services": {"a": shared, "b": shared}})

        assert error.path == "services.a.build"


class TestPathPart:
    @pytest.mark.parametrize("key", ["web", "a.b-c_1", "A" * 64])
    def test_a_plain_key_is_kept(self, key: str) -> None:
        assert sd.path_part(key) == key

    @pytest.mark.parametrize("key", ["a b", "a\nb", "é", "x" * 65, "", 3, None, ("a",)])
    def test_anything_else_becomes_a_placeholder(self, key: str) -> None:
        assert sd.path_part(key) == "<key>"

    def test_a_refused_path_never_carries_raw_key_text(self) -> None:
        error = rejection(sd.precheck, "evil\x1b[31m: 1\n" + WEB)

        assert "\x1b" not in error.fields[SERVICE_FIELD]


def row(
    index: int, name: str | None = None, value: str | None = None, **flags: str
) -> dict[str, str]:
    form: dict[str, str] = {}
    if name is not None:
        form[f"variable-{index}-name"] = name
    if value is not None:
        form[f"variable-{index}-value"] = value
    form.update({f"variable-{index}-{flag}": text for flag, text in flags.items()})
    return form


def single(name: str, value: str = "x") -> tuple[sd.VariableInput, ...]:
    return sd.parse_variables(row(0, name, value))


def parsed(*rows: dict[str, str]) -> tuple[sd.VariableInput, ...]:
    merged: dict[str, str] = {}
    for item in rows:
        merged.update(item)
    return sd.parse_variables(merged)


def shape(items: Iterable[sd.VariableInput]) -> list[tuple[str, str | None, bool, bool, int]]:
    return [(v.name, v.value, v.secret, v.remove, v.row) for v in items]


class TestParseVariables:
    def test_rows_come_back_in_row_order_and_blank_rows_are_skipped(self) -> None:
        result = parsed(row(1, "B", "2"), row(0, "A", "1"), row(2, "", ""))

        assert shape(result) == [("A", "1", False, False, 0), ("B", "2", False, False, 1)]

    def test_a_secret_with_an_empty_value_keeps_the_stored_value(self) -> None:
        result = parsed(row(0, "S", "", secret="on"))

        assert shape(result) == [("S", None, True, False, 0)]

    def test_a_plain_variable_with_an_empty_value_is_the_empty_string(self) -> None:
        assert shape(parsed(row(0, "E", ""))) == [("E", "", False, False, 0)]

    def test_remove_clears_the_value(self) -> None:
        result = parsed(row(0, "S", "ignored", secret="on", remove="on"))

        assert shape(result) == [("S", None, True, True, 0)]

    @pytest.mark.parametrize("text", ["on", "true", "1", "yes", "ON", " True "])
    def test_truthy_spellings_mark_a_secret(self, text: str) -> None:
        assert parsed(row(0, "S", "v", secret=text))[0].secret is True

    @pytest.mark.parametrize("text", ["", "off", "false", "0", "no", "2"])
    def test_other_spellings_do_not(self, text: str) -> None:
        assert parsed(row(0, "S", "v", secret=text))[0].secret is False

    def test_names_are_trimmed_and_values_are_not(self) -> None:
        assert shape(parsed(row(0, "  A  ", " v "))) == [("A", " v ", False, False, 0)]

    def test_a_row_with_only_a_value_is_kept_for_the_name_error(self) -> None:
        assert shape(parsed(row(3, "", "v"))) == [("", "v", False, False, 3)]

    @pytest.mark.parametrize(
        "field", ["variable-x-name", "variable-1-other", "variables-1-name", "variable-12345-name"]
    )
    def test_unrelated_fields_are_ignored(self, field: str) -> None:
        assert sd.parse_variables({field: "A"}) == ()

    def test_the_repr_never_shows_the_value(self) -> None:
        item = sd.VariableInput("A", "hunter2-value", False, False)

        assert "hunter2-value" not in repr(item)


class TestCheckVariables:
    @pytest.mark.parametrize(
        "name",
        ["foo", "A", "_x", "A" * 128, "PROXY", "MY_PATH", "GOPATH", "LCX", "SSLX", "TMPFILE"],
    )
    def test_a_legal_name_passes(self, name: str) -> None:
        sd.check_variables(single(name))

    @pytest.mark.parametrize("name", ["1abc", "a-b", "a b", "a" * 129, "é", "A.B", "A\tB", "$X"])
    def test_a_malformed_name_is_refused_on_its_name_field(self, name: str) -> None:
        error = rejection(sd.check_variables, single(name))

        assert error.reason == "invalid"
        assert list(error.fields) == ["variable-0-name"]

    @pytest.mark.parametrize(
        "name",
        [
            "DOCKER_HOST",
            "docker_host",
            "Compose_Project_Name",
            "BUILDKIT_PROGRESS",
            "BUILDX_X",
            "DOCKMIN_SECRET_KEY",
            "LC_ALL",
            "LD_PRELOAD",
            "OTEL_X",
            "SSL_CERT_FILE",
            "XDG_CONFIG_HOME",
            "PATH",
            "path",
            "HOME",
            "TMPDIR",
            "LANG",
            "TZ",
            "GODEBUG",
            "GOMEMLIMIT",
            "HTTP_PROXY",
            "https_proxy",
            "x_proxy",
        ],
    )
    def test_a_reserved_name_is_refused(self, name: str) -> None:
        error = rejection(sd.check_variables, single(name))

        assert error.fields == {
            "variable-0-name": (
                "This name is reserved because it changes how Compose or the process runs."
            )
        }

    def test_a_duplicate_name_names_the_first_row(self) -> None:
        items = parsed(row(0, "A", "1"), row(1, "A", "2"))

        error = rejection(sd.check_variables, items)

        assert error.fields == {"variable-1-name": "This name is already used in row 1."}

    def test_names_that_differ_only_in_case_are_distinct(self) -> None:
        sd.check_variables(parsed(row(0, "A", "1"), row(1, "a", "2")))

    def test_a_value_of_exactly_8_kib_passes_and_one_more_byte_is_refused(self) -> None:
        sd.check_variables(single("A", "x" * sd.MAX_VARIABLE_VALUE_BYTES))

        error = rejection(sd.check_variables, single("A", "x" * (sd.MAX_VARIABLE_VALUE_BYTES + 1)))

        assert error.fields == {"variable-0-value": "A value can be at most 8 KiB."}

    def test_the_value_cap_counts_bytes_not_characters(self) -> None:
        error = rejection(sd.check_variables, single("A", "é" * 4097))

        assert list(error.fields) == ["variable-0-value"]

    def test_a_tab_in_a_value_is_allowed(self) -> None:
        sd.check_variables(single("A", "a\tb"))

    @pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b", "a\x1bb", "a\x7fb", "a\x85b"])
    def test_a_control_character_in_a_value_is_refused_as_not_a_single_line(
        self, value: str
    ) -> None:
        error = rejection(sd.check_variables, single("A", value))

        assert error.fields == {"variable-0-value": "A value must be a single line."}

    def test_a_lone_surrogate_in_a_value_is_refused_not_passed_on(self) -> None:
        error = rejection(sd.check_variables, single("A", "a\ud800b"))

        assert error.fields == {"variable-0-value": "A value must be valid text."}

    @pytest.mark.parametrize(
        "value", ["; $(touch /tmp/x) `x`", "a | b && c > d", "'\"#", "$$HOME ${X:-y}", "é"]
    )
    def test_shell_metacharacters_in_a_value_are_accepted_verbatim(self, value: str) -> None:
        items = single("A", value)

        sd.check_variables(items)

        assert items[0].value == value

    def test_one_hundred_variables_pass_and_one_hundred_and_one_do_not(self) -> None:
        def many(count: int) -> tuple[sd.VariableInput, ...]:
            return sd.parse_variables(
                {f"variable-{i}-name": f"V{i}" for i in range(count)}
                | {f"variable-{i}-value": "x" for i in range(count)}
            )

        sd.check_variables(many(sd.MAX_VARIABLES))

        error = rejection(sd.check_variables, many(sd.MAX_VARIABLES + 1))

        assert error.fields == {"variables": f"At most {sd.MAX_VARIABLES} variables are allowed."}

    def test_removed_rows_do_not_count_toward_the_cap(self) -> None:
        count = sd.MAX_VARIABLES + 1
        items = sd.parse_variables(
            {f"variable-{i}-name": f"V{i}" for i in range(count)}
            | {f"variable-{i}-remove": "on" for i in range(count)}
        )

        sd.check_variables(items)

    def test_values_totalling_over_64_kib_are_refused(self) -> None:
        count = 9
        items = sd.parse_variables(
            {f"variable-{i}-name": f"V{i}" for i in range(count)}
            | {f"variable-{i}-value": "x" * 8000 for i in range(count)}
        )

        error = rejection(sd.check_variables, items)

        assert error.fields == {"variables": "Variables can total at most 64 KiB."}

    def test_every_bad_row_is_reported_together(self) -> None:
        items = parsed(row(0, "1bad", "x"), row(1, "PATH", "x"), row(2, "OK", "a\nb"))

        error = rejection(sd.check_variables, items)

        assert sorted(error.fields) == ["variable-0-name", "variable-1-name", "variable-2-value"]


AGREEMENT_NAMES = [
    "docker_host",
    "HTTPS_PROXY",
    "ssl_cert_file",
    "PATH",
    "GOGC",
    "FOO",
    "Home",
    "compose_file",
    "x_Proxy",
    "PROXY",
    "MY_PATH",
    "LCX",
    "BUILDKIT",
    "",
    *sorted(RESERVED_NAMES),
    *[f"{prefix}X" for prefix in RESERVED_PREFIXES],
    *[f"{prefix.lower()}x" for prefix in RESERVED_PREFIXES],
]


class TestReservedNameAgreement:
    @pytest.mark.parametrize("name", AGREEMENT_NAMES)
    def test_the_document_rule_and_the_environment_builder_agree(self, name: str) -> None:
        assert sd.is_reserved_variable(name) == engine_is_reserved(name)

    @pytest.mark.parametrize("name", [*sorted(RESERVED_NAMES), "DOCKER_HOST", "x_proxy"])
    def test_every_name_the_builder_reserves_is_refused_by_the_form_check(self, name: str) -> None:
        error = rejection(sd.check_variables, single(name))

        assert list(error.fields) == ["variable-0-name"]


SWARM_FILE_WITH_EXTERNALS = (
    WEB
    + "    configs: [shared]\n    secrets: [vault]\n"
    + "configs:\n  shared:\n    external: true\n"
    + "secrets:\n  vault:\n    external: true\n    name: held-elsewhere\n"
)


class TestSwarmStackName:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Shop", "Shop"),
            (" Shop ", "Shop"),
            ("MiXed-Case_1", "MiXed-Case_1"),
            ("9lives", "9lives"),
            ("A", "A"),
            ("A" * 63, "A" * 63),
        ],
    )
    def test_a_name_of_the_swarm_grammar_keeps_its_case(self, text: str, expected: Any) -> None:
        assert sd.swarm_stack_name(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "my.stack",
            "-shop",
            "_shop",
            "a b",
            "a;b",
            "$(id)",
            "a" * 64,
            "caf\u00e9",
            "a\nb",
            "\uff53hop",
        ],
    )
    def test_a_name_outside_the_grammar_is_refused_not_normalised(self, text: str) -> None:
        error = rejection(sd.swarm_stack_name, text)

        assert error.reason == "invalid"
        assert list(error.fields) == ["name"]

    @pytest.mark.parametrize("text", ["", "   "])
    def test_a_blank_name_asks_for_one(self, text: str) -> None:
        error = rejection(sd.swarm_stack_name, text)

        assert error.fields == {"name": "Enter a name for the stack."}

    def test_the_two_grammars_differ_only_in_upper_case(self) -> None:
        assert sd.swarm_stack_name("Shop") == "Shop"
        assert rejection(sd.stack_name, "Shop").reason == "invalid"


class TestSwarmPrecheck:
    def test_external_configs_and_secrets_are_admitted_for_a_swarm_file_only(self) -> None:
        sd.precheck(SWARM_FILE_WITH_EXTERNALS, kind="swarm")

        error = rejection(lambda text: sd.precheck(text, kind="compose"), SWARM_FILE_WITH_EXTERNALS)
        assert error.reason == "refused_construct"
        assert error.path == "configs.shared.external"

    def test_a_secret_value_in_the_file_is_refused_for_swarm_at_its_path(self) -> None:
        text = WEB + "    secrets: [s]\nsecrets:\n  s:\n    content: hunter2\n"

        error = rejection(lambda t: sd.precheck(t, kind="swarm"), text)

        assert (error.reason, error.path) == ("refused_construct", "secrets.s.content")
        assert "hunter2" not in " ".join(error.fields.values())

    def test_the_same_secret_content_is_accepted_for_a_compose_file(self) -> None:
        sd.precheck(WEB + "    secrets: [s]\nsecrets:\n  s:\n    content: x\n")

    def test_a_config_may_still_carry_content_for_swarm(self) -> None:
        sd.precheck(WEB + "    configs: [c]\nconfigs:\n  c:\n    content: x\n", kind="swarm")

    @pytest.mark.parametrize(
        ("text", "path"),
        [
            pytest.param("include: other.yaml\n" + WEB, "include", id="include"),
            pytest.param(WEB + "    env_file: /a\n", "services.web.env_file", id="env-file"),
            pytest.param(
                WEB + "    configs: [c]\nconfigs:\n  c:\n    file: /c\n",
                "configs.c.file",
                id="config-file",
            ),
            pytest.param(
                WEB + "    secrets: [s]\nsecrets:\n  s:\n    file: /s\n",
                "secrets.s.file",
                id="secret-file",
            ),
            pytest.param(
                WEB + "    configs: [c]\nconfigs:\n  c:\n    bogus: 1\n",
                "configs.c.bogus",
                id="unknown-key",
            ),
            pytest.param(
                WEB + "    configs: [c]\nconfigs:\n  c: {}\n", "configs.c", id="no-source"
            ),
        ],
    )
    def test_the_file_reading_refusals_hold_for_swarm(self, text: str, path: str) -> None:
        error = rejection(lambda t: sd.precheck(t, kind="swarm"), text)

        assert error.reason == "refused_construct"
        assert error.path == path

    def test_check_structure_takes_the_same_kind(self) -> None:
        loaded = {
            "services": {"web": {"image": "x"}},
            "configs": {"shared": {"external": True}},
        }

        sd.check_structure(loaded, kind="swarm")

        assert (
            rejection(lambda data: sd.check_structure(data), loaded).path
            == "configs.shared.external"
        )


RESERVED_REFERENCE = "the variable {} is reserved and cannot be used in a stack."
INTERPOLATED_NAME = "an environment variable name cannot be interpolated."
DISCLOSING = sorted(DISCLOSING_VARIABLES)
SECRET_WEB = "secrets:\n  s:\n    environment: {}\n"


def service_with(field: str, *, hook: str = "") -> str:
    return f"services:\n  web:\n    image: x\n{hook}{field}"


def image_reference(reference: str) -> str:
    return f'services:\n  web:\n    image: "{reference}"\n'


def environment_of(*lines: str, parent: str = "    ") -> str:
    return f"{parent}environment:\n" + "".join(f"{parent}  {line}\n" for line in lines)


def hook_with(hook: str, *lines: str) -> str:
    body = "".join(f"          {line}\n" for line in lines)
    return service_with(
        f"    {hook}:\n      - command: [x]\n        environment:\n{body}",
    )


REFUSED_REFERENCES = [
    ("$DOCKER_HOST", "DOCKER_HOST"),
    ("${DOCKER_HOST}", "DOCKER_HOST"),
    ("${DOCKER_HOST:-x}", "DOCKER_HOST"),
    ("${DOCKER_HOST-x}", "DOCKER_HOST"),
    ("${DOCKER_HOST:?missing}", "DOCKER_HOST"),
    ("${DOCKER_HOST?missing}", "DOCKER_HOST"),
    ("${DOCKER_HOST:+set}", "DOCKER_HOST"),
    ("${DOCKER_HOST+set}", "DOCKER_HOST"),
    ("${DOCKER_HOST:-}", "DOCKER_HOST"),
    ("${UNSET:-${DOCKER_HOST}}", "DOCKER_HOST"),
    ("${UNSET:-$DOCKER_HOST}", "DOCKER_HOST"),
    ("${UNSET:?$DOCKER_HOST}", "DOCKER_HOST"),
    ("prefix-$HOME-suffix", "HOME"),
    ("$$$HOME", "HOME"),
    ("${TMPDIR}", "TMPDIR"),
    ("${DOCKER_CONFIG}", "DOCKER_CONFIG"),
    ("${DOCKER_CERT_PATH}", "DOCKER_CERT_PATH"),
    ("${DOCKER_TLS_VERIFY}", "DOCKER_TLS_VERIFY"),
]

REFUSED_ENVIRONMENT_ENTRIES = {
    "a list item naming a set member with a value": (
        service_with(environment_of("- DOCKER_HOST=$DOCKER_HOST")),
        "services.web.environment[0]",
        RESERVED_REFERENCE.format("DOCKER_HOST"),
    ),
    "a list item that interpolates its name": (
        service_with(environment_of("- $NAME=1")),
        "services.web.environment[0]",
        INTERPOLATED_NAME,
    ),
    "a list item that interpolates its name without a value": (
        service_with(environment_of("- ${NAME}")),
        "services.web.environment[0]",
        INTERPOLATED_NAME,
    ),
    "a list item that interpolates inside its name": (
        service_with(environment_of("- A$B=1")),
        "services.web.environment[0]",
        INTERPOLATED_NAME,
    ),
    "a mapping key that interpolates": (
        service_with(environment_of('"$NAME": 1')),
        "services.web.environment.<key>",
        INTERPOLATED_NAME,
    ),
    "a mapping key that interpolates with a null value": (
        service_with(environment_of('"${NAME}":')),
        "services.web.environment.<key>",
        INTERPOLATED_NAME,
    ),
    "a hook's list item that interpolates its name": (
        hook_with("post_start", "- $NAME=1"),
        "services.web.post_start[0].environment[0]",
        INTERPOLATED_NAME,
    ),
    "a second service": (
        "services:\n  web:\n    image: x\n  db:\n    image: x\n"
        + environment_of("- HOME", parent="    "),
        "services.db.environment[0]",
        RESERVED_REFERENCE.format("HOME"),
    ),
    "an extension field": (
        "x-app:\n  environment: [DOCKER_HOST]\n" + service_with(""),
        "x-app.environment[0]",
        RESERVED_REFERENCE.format("DOCKER_HOST"),
    ),
    "a list reused through an alias after its first use": (
        "x-shared: &shared [DOCKER_HOST]\n" + service_with("    environment: *shared\n"),
        "services.web.environment[0]",
        RESERVED_REFERENCE.format("DOCKER_HOST"),
    ),
    "a mapping reused through an alias after its first use": (
        "x-shared: &shared {DOCKER_HOST: }\n" + service_with("    environment: *shared\n"),
        "services.web.environment.DOCKER_HOST",
        RESERVED_REFERENCE.format("DOCKER_HOST"),
    ),
}

ACCEPTED_ENVIRONMENT_USES = {
    "a value for a set member in a list": service_with(
        environment_of("- DOCKER_HOST=tcp://x:2375")
    ),
    "a set member with a fixed value": service_with(environment_of("- TZ=UTC")),
    "a set member as a mapping key with a value": service_with(
        environment_of("DOCKER_HOST: tcp://x")
    ),
    "a valueless proxy variable": service_with(environment_of("- HTTP_PROXY")),
    "a valueless lower-case spelling of a set member": service_with(
        environment_of("- docker_host")
    ),
    "a valueless mapping entry for a non-member": service_with(environment_of("HTTP_PROXY:")),
    "an escaped dollar before a set member": service_with(
        '    command: ["sh", "-c", "echo $$HOME"]\n'
    ),
    "a default for a name outside the set": image_reference("${TZ:-UTC}"),
    "a project name reference": image_reference("${COMPOSE_PROJECT_NAME}"),
    "a defaulted group id": image_reference("${DOCKER_GID:-9}"),
    "a label key that looks like a reference": service_with('    labels:\n      "$HOME": x\n'),
    "a config source outside the set": WEB
    + "configs:\n  c:\n    environment: COMPOSE_PROJECT_NAME\n",
    "a hook entry with a value": hook_with("pre_stop", "- DOCKER_HOST=tcp://x"),
}

REFUSED_SOURCES = [
    ("configs", "DOCKER_HOST", RESERVED_REFERENCE.format("DOCKER_HOST")),
    ("secrets", "DOCKER_CERT_PATH", RESERVED_REFERENCE.format("DOCKER_CERT_PATH")),
    ("configs", "HOME", RESERVED_REFERENCE.format("HOME")),
    ("secrets", "TMPDIR", RESERVED_REFERENCE.format("TMPDIR")),
    ("configs", "DOCKER_CONFIG", RESERVED_REFERENCE.format("DOCKER_CONFIG")),
    ("secrets", "DOCKER_TLS_VERIFY", RESERVED_REFERENCE.format("DOCKER_TLS_VERIFY")),
    ("configs", "${NAME}", INTERPOLATED_NAME),
    ("secrets", "$NAME", INTERPOLATED_NAME),
    ("configs", "${DOCKER_HOST}", INTERPOLATED_NAME),
    ("secrets", "A$B", INTERPOLATED_NAME),
]


class TestDisclosingVariables:
    @pytest.mark.parametrize(("reference", "name"), REFUSED_REFERENCES)
    def test_a_reference_to_a_set_member_is_refused_in_every_form(
        self, reference: str, name: str
    ) -> None:
        error = rejection(sd.precheck, image_reference(reference))

        assert error.reason == "refused_construct"
        assert error.path == "services.web.image"
        assert error.fields[SERVICE_FIELD] == (
            f"`services.web.image`: {RESERVED_REFERENCE.format(name)}"
        )

    @pytest.mark.parametrize("name", DISCLOSING)
    def test_a_valueless_list_item_naming_a_set_member_is_refused(self, name: str) -> None:
        error = rejection(sd.precheck, service_with(environment_of(f"- {name}")))

        assert error.reason == "refused_construct"
        assert error.path == "services.web.environment[0]"
        assert error.fields[SERVICE_FIELD] == (
            f"`services.web.environment[0]`: {RESERVED_REFERENCE.format(name)}"
        )

    @pytest.mark.parametrize("null", ["", " ~", " null"])
    @pytest.mark.parametrize("name", DISCLOSING)
    def test_a_mapping_entry_with_no_value_naming_a_set_member_is_refused(
        self, name: str, null: str
    ) -> None:
        error = rejection(sd.precheck, service_with(environment_of(f"{name}:{null}")))

        assert error.reason == "refused_construct"
        assert error.path == f"services.web.environment.{name}"
        assert error.fields[SERVICE_FIELD] == (
            f"`services.web.environment.{name}`: {RESERVED_REFERENCE.format(name)}"
        )

    @pytest.mark.parametrize("hook", ["post_start", "pre_stop"])
    @pytest.mark.parametrize(
        ("line", "tail"),
        [
            ("- DOCKER_HOST", "[0]"),
            ("DOCKER_HOST:", ".DOCKER_HOST"),
            ("DOCKER_HOST: ~", ".DOCKER_HOST"),
        ],
    )
    def test_a_valueless_entry_in_a_lifecycle_hook_is_refused(
        self, hook: str, line: str, tail: str
    ) -> None:
        error = rejection(sd.precheck, hook_with(hook, line))

        assert error.reason == "refused_construct"
        assert error.path == f"services.web.{hook}[0].environment{tail}"
        assert error.fields[SERVICE_FIELD].endswith(RESERVED_REFERENCE.format("DOCKER_HOST"))

    @pytest.mark.parametrize("hook", ["post_start", "pre_stop"])
    def test_a_reference_in_a_lifecycle_hook_is_refused(self, hook: str) -> None:
        error = rejection(sd.precheck, hook_with(hook, 'A: "$DOCKER_HOST"'))

        assert error.path == f"services.web.{hook}[0].environment.A"
        assert error.fields[SERVICE_FIELD].endswith(RESERVED_REFERENCE.format("DOCKER_HOST"))

    @pytest.mark.parametrize(
        ("text", "path", "message"),
        REFUSED_ENVIRONMENT_ENTRIES.values(),
        ids=REFUSED_ENVIRONMENT_ENTRIES.keys(),
    )
    def test_an_environment_entry_that_would_read_or_build_a_name_is_refused(
        self, text: str, path: str, message: str
    ) -> None:
        error = rejection(sd.precheck, text)

        assert error.reason == "refused_construct"
        assert error.path == path
        assert error.fields[SERVICE_FIELD] == f"`{path}`: {message}"

    @pytest.mark.parametrize("kind", ["compose", "swarm"])
    @pytest.mark.parametrize(("section", "variable", "message"), REFUSED_SOURCES)
    def test_a_config_or_secret_environment_source_is_refused_raw(
        self, kind: StackKind, section: str, variable: str, message: str
    ) -> None:
        text = WEB + f"{section}:\n  s:\n    environment: '{variable}'\n"

        error = rejection(lambda t: sd.precheck(t, kind=kind), text)

        assert error.reason == "refused_construct"
        assert error.path == f"{section}.s.environment"
        assert error.fields[SERVICE_FIELD] == f"`{section}.s.environment`: {message}"

    @pytest.mark.parametrize(
        "text", ACCEPTED_ENVIRONMENT_USES.values(), ids=ACCEPTED_ENVIRONMENT_USES.keys()
    )
    def test_a_use_that_discloses_nothing_passes(self, text: str) -> None:
        sd.precheck(text)

    @pytest.mark.parametrize("name", ["docker_host", "Home", "tmpdir", "Docker_Config"])
    def test_a_different_case_is_not_a_set_member(self, name: str) -> None:
        sd.precheck(service_with(environment_of(f"- {name}")))
        sd.precheck(image_reference(f"${{{name}}}"))


def companion(
    name: str, content: str = "A=1\n", *, secret: bool = False, row: int | None = None
) -> sd.CompanionFile:
    return sd.CompanionFile(name, content, secret, row)


def with_files(text: str, *files: sd.CompanionFile, kind: StackKind = "compose") -> sd.FileUses:
    return sd.precheck(text, kind=kind, files=files)


def refused_with(
    text: str, *files: sd.CompanionFile, kind: StackKind = "compose"
) -> ComposeDocumentRejected:
    return rejection(lambda: sd.precheck(text, kind=kind, files=files))


ENV_FORMS = {
    "a bare name": "env_file: app.env",
    "a dotted name": "env_file: ./app.env",
    "a list": "env_file: [app.env, ./app.env]",
    "mappings": "env_file: [{path: app.env, required: false, format: raw}]",
    "mixed": "env_file: [app.env, {path: ./app.env}]",
}


class TestCompanionFileNames:
    @pytest.mark.parametrize("name", ["a", "app.env", "A-b_c.1", "x" * 128])
    def test_a_name_in_the_grammar_is_kept(self, name: str) -> None:
        assert sd.file_name(f" {name} ") == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            ".env",
            "-a",
            "a/b",
            "../a",
            "a b",
            "x" * 129,
            "é",
            "compose.yaml",
            "Compose.YML",
            "docker-compose.override.yaml",
            "COMPOSE.override.yml",
        ],
    )
    def test_any_other_name_is_refused(self, name: str) -> None:
        assert rejection(sd.file_name, name).fields.keys() == {"name"}

    def test_the_field_key_is_chosen_by_the_caller(self) -> None:
        assert rejection(lambda: sd.file_name("", field="file-3-name")).fields.keys() == {
            "file-3-name"
        }


class TestCompanionFileContent:
    def test_a_byte_order_mark_is_removed_and_line_endings_are_kept(self) -> None:
        assert sd.decode_file(b"\xef\xbb\xbfA=1\r\nB=2\n") == "A=1\r\nB=2\n"
        assert sd.decode_file("\ufeffA=1") == "A=1"

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (b"", "invalid"),
            (b"\xff\xfe", "invalid"),
            (b"a\x00b", "invalid"),
            (b"a\x1bb", "invalid"),
            (b"x" * (sd.MAX_FILE_BYTES + 1), "too_large"),
            ("\ud800", "invalid"),
        ],
    )
    def test_content_outside_the_rules_is_refused(self, raw: bytes | str, reason: str) -> None:
        assert rejection(sd.decode_file, raw).reason == reason

    def test_tab_and_the_limit_itself_are_accepted(self) -> None:
        assert sd.decode_file("a\tb") == "a\tb"
        assert len(sd.decode_file(b"x" * sd.MAX_FILE_BYTES)) == sd.MAX_FILE_BYTES


class TestCompanionFileSet:
    def test_the_limits_are_accepted_exactly(self) -> None:
        full = [companion(f"f{i}", "x" * 6400) for i in range(sd.MAX_FILES)]

        sd.check_files(full)

    def test_more_files_than_the_limit_are_too_large(self) -> None:
        files = [companion(f"f{i}") for i in range(sd.MAX_FILES + 1)]

        assert rejection(sd.check_files, files).reason == "too_large"

    def test_a_total_over_the_limit_is_too_large(self) -> None:
        files = [companion(f"f{i}", "x" * sd.MAX_FILE_BYTES) for i in range(3)]

        error = rejection(sd.check_files, files)

        assert (error.reason, set(error.fields)) == ("too_large", {"files"})

    def test_names_differing_only_by_case_are_refused_at_the_later_row(self) -> None:
        error = rejection(sd.check_files, [companion("A.env", row=0), companion("a.ENV", row=1)])

        assert error.fields.keys() == {"file-1-name"}

    def test_an_error_without_a_row_names_the_file(self) -> None:
        error = rejection(sd.check_files, [companion("a.env", "")])

        assert error.fields == {"files": "a.env: The file is empty."}


class TestReferences:
    @pytest.mark.parametrize("form", ENV_FORMS.values(), ids=ENV_FORMS.keys())
    def test_every_env_file_form_is_admitted_and_its_use_recorded(self, form: str) -> None:
        uses = with_files(WEB + f"    {form}\n", companion("app.env"))

        assert [use.role for use in uses["app.env"]] == ["env"] * len(uses["app.env"])
        assert all(use.path.startswith("services.web.env_file") for use in uses["app.env"])

    def test_a_label_file_a_config_a_secret_an_include_and_an_extends_pass(self) -> None:
        text = (
            "include: [inc.yaml, {path: [inc2.yaml]}]\n"
            + WEB
            + "    label_file: [labels.txt]\n"
            + "  o:\n    extends: {file: base.yaml, service: b}\n"
            + "configs:\n  c: {file: ./cfg.conf}\n"
            + "secrets:\n  s: {file: key.txt}\n"
        )

        uses = with_files(
            text,
            companion("inc.yaml", "services: {}\n"),
            companion("inc2.yaml", "services: {}\n"),
            companion("base.yaml", "services:\n  b: {image: x}\n"),
            companion("labels.txt", "a=b\n"),
            companion("cfg.conf", "x\n"),
            companion("key.txt", "k\n", secret=True),
        )

        assert {name: [use.role for use in found] for name, found in uses.items()} == {
            "inc.yaml": ["compose"],
            "inc2.yaml": ["compose"],
            "base.yaml": ["compose"],
            "labels.txt": ["env"],
            "cfg.conf": ["source"],
            "key.txt": ["source"],
        }

    @pytest.mark.parametrize(
        "value",
        [
            "/etc/passwd",
            "../a.env",
            "sub/a.env",
            "~/a.env",
            "https://x/a.env",
            "oci://x/a",
            "git@host:o/r.git",
            "a.env$",
            "${X}",
            "$$",
            "././app.env",
            "''",
            "app.env/",
            "App.env",
        ],
    )
    def test_a_reference_outside_the_two_forms_is_refused_at_its_path(self, value: str) -> None:
        error = refused_with(WEB + f"    env_file: {value}\n", companion("app.env"))

        assert error.path == "services.web.env_file"
        assert error.reason in {"refused_construct", "file_missing"}
        assert error.reason == ("file_missing" if value == "App.env" else "refused_construct")

    def test_a_well_formed_name_that_is_not_stored_is_file_missing(self) -> None:
        error = refused_with(WEB + "    env_file: nope.env\n", companion("app.env"))

        assert error.reason == "file_missing"
        assert error.path == "services.web.env_file"
        assert "nope.env" in error.fields[SERVICE_FIELD]

    def test_an_include_with_a_project_directory_or_env_file_is_refused(self) -> None:
        text = "include:\n  - path: a.yaml\n    env_file: b\n" + WEB

        assert refused_with(text, companion("a.yaml", WEB)).path == "include[0].env_file"

    def test_a_non_boolean_required_and_a_non_text_format_are_refused(self) -> None:
        files = (companion("app.env"),)

        assert (
            refused_with(WEB + "    env_file: [{path: app.env, required: 'no'}]\n", *files).path
            == "services.web.env_file[0].required"
        )
        assert (
            refused_with(WEB + "    env_file: [{path: app.env, format: 3}]\n", *files).path
            == "services.web.env_file[0].format"
        )

    def test_a_respelled_file_key_is_refused(self) -> None:
        error = refused_with(WEB + "    Env_File: app.env\n", companion("app.env"))

        assert (error.reason, error.path) == ("refused_construct", "services.web.Env_File")

    def test_a_secret_needs_a_secret_file_and_a_swarm_config_a_plain_one(self) -> None:
        plain = companion("p.txt")
        secret = companion("s.txt", secret=True)

        assert refused_with(WEB + "secrets:\n  s: {file: p.txt}\n", plain).path == "secrets.s.file"
        sd.precheck(WEB + "configs:\n  c: {file: s.txt}\n", files=[secret])
        error = refused_with(WEB + "configs:\n  c: {file: s.txt}\n", secret, kind="swarm")
        assert error.path == "configs.c.file"

    def test_a_secret_file_may_be_an_env_file(self) -> None:
        with_files(WEB + "    env_file: s.env\n", companion("s.env", "K=v\n", secret=True))


class TestCompanionDocuments:
    def test_an_included_document_is_checked_with_the_same_rules(self) -> None:
        error = refused_with(
            "include: [inc.yaml]\n" + WEB,
            companion("inc.yaml", "services:\n  b:\n    privileged: true\n    build: .\n", row=2),
        )

        assert error.file == "inc.yaml"
        assert error.path == "services.b.build"
        assert error.fields.keys() == {"file-2-content"}
        assert error.fields["file-2-content"].startswith("inc.yaml: `services.b.build`")

    def test_an_included_document_that_names_a_server_path_is_refused(self) -> None:
        error = refused_with(
            "include: [inc.yaml]\n" + WEB,
            companion("inc.yaml", "services:\n  b:\n    env_file: /etc/passwd\n"),
        )

        assert (error.file, error.path) == ("inc.yaml", "services.b.env_file")

    def test_an_included_document_with_invalid_yaml_names_the_file(self) -> None:
        error = refused_with("include: [inc.yaml]\n" + WEB, companion("inc.yaml", "a: [\n"))

        assert error.reason == "invalid_compose"
        assert error.fields["files"].startswith("inc.yaml: ")

    def test_an_include_cycle_is_refused(self) -> None:
        error = refused_with(
            "include: [a.yaml]\n" + WEB,
            companion("a.yaml", "include: [b.yaml]\n"),
            companion("b.yaml", "include: [a.yaml]\n"),
        )

        assert error.reason == "refused_construct"
        assert error.file == "b.yaml"
        assert error.path == "include[0]"

    def test_a_file_included_twice_is_checked_once(self) -> None:
        uses = with_files(
            "include: [a.yaml, b.yaml]\n" + WEB,
            companion("a.yaml", "include: [c.yaml]\n"),
            companion("b.yaml", "include: [c.yaml]\n"),
            companion("c.yaml", "services: {}\n"),
        )

        assert [use.document for use in uses["c.yaml"]] == ["a.yaml", "b.yaml"]

    def test_depth_is_limited_to_four_levels_below_the_stack_file(self) -> None:
        chain = [companion(f"f{i}.yaml", f"include: [f{i + 1}.yaml]\n") for i in range(4)]
        text = "include: [f0.yaml]\n" + WEB

        with_files(text, *chain[:3], companion("f3.yaml", "services: {}\n"))
        error = refused_with(text, *chain, companion("f4.yaml", "services: {}\n"))

        assert (error.file, error.path) == ("f3.yaml", "include[0]")

    def test_a_secret_file_cannot_be_a_compose_file(self) -> None:
        error = refused_with(
            "include: [a.yaml]\n" + WEB, companion("a.yaml", "services: {}\n", secret=True)
        )

        assert error.path == "include[0]"

    @pytest.mark.parametrize(
        "text",
        [
            "include: [a.yaml]\n" + WEB + "    env_file: a.yaml\n",
            WEB + "    env_file: a.yaml\ninclude: [a.yaml]\n",
            "include: [a.yaml]\n" + WEB + "configs:\n  c: {file: a.yaml}\n",
        ],
    )
    def test_a_compose_file_is_used_in_no_other_role(self, text: str) -> None:
        error = refused_with(text, companion("a.yaml", "services: {}\n"))

        assert error.reason == "refused_construct"

    def test_extends_reads_a_companion_document_too(self) -> None:
        error = refused_with(
            WEB + "  o:\n    extends: {file: base.yaml, service: b}\n",
            companion("base.yaml", "services:\n  b:\n    develop: {}\n"),
        )

        assert (error.file, error.path) == ("base.yaml", "services.b.develop")


class TestEnvironmentFileScan:
    @pytest.mark.parametrize(
        ("content", "line"),
        [
            ("A=${HOME}\n", 1),
            ("OK=1\nB=$DOCKER_CERT_PATH\n", 2),
            ("C=${DOCKER_HOST:-x}", 1),
            ("\nHOME\n", 2),
            ("export TMPDIR\n", 1),
            ("D=${DOCKMIN_SOURCE_1}\n", 1),
            ("DOCKMIN_SOURCE_2\n", 1),
        ],
    )
    @pytest.mark.parametrize(
        ("key", "secret"), [("env_file", False), ("env_file", True), ("label_file", False)]
    )
    def test_a_disclosing_reference_is_refused_naming_the_file_and_line(
        self, content: str, line: int, key: str, secret: bool
    ) -> None:
        error = refused_with(
            WEB + f"    {key}: v.env\n", companion("v.env", content, secret=secret)
        )

        assert (error.reason, error.path, error.file) == (
            "refused_construct",
            f"services.web.{key}",
            "v.env",
        )
        assert f"v.env line {line}" in error.fields["files"]

    @pytest.mark.parametrize(
        "content", ["export\tTMPDIR\n", "export  HOME\n", "export\tA=${HOME}\n"]
    )
    def test_export_followed_by_any_whitespace_is_read_as_export(self, content: str) -> None:
        error = refused_with(WEB + "    env_file: v.env\n", companion("v.env", content))

        assert error.reason == "refused_construct"
        assert "v.env line 1" in error.fields["files"]

    def test_a_secret_label_file_is_refused_as_not_plain(self) -> None:
        error = refused_with(
            WEB + "    label_file: v.env\n", companion("v.env", "A=1\n", secret=True)
        )

        assert (error.reason, error.path) == ("refused_construct", "services.web.label_file")
        assert "plain file" in error.fields[SERVICE_FIELD]

    def test_the_refusal_never_quotes_the_line(self) -> None:
        error = refused_with(
            WEB + "    env_file: v.env\n", companion("v.env", "TOKEN=hunter2 ${HOME}\n")
        )

        assert "hunter2" not in " ".join(error.fields.values())

    @pytest.mark.parametrize(
        "content", ["A=$$HOME\n", "A=${OTHER}\n", "# nothing\n", "HOMEPAGE=x\n"]
    )
    def test_other_lines_pass(self, content: str) -> None:
        with_files(WEB + "    env_file: v.env\n", companion("v.env", content))


class TestGeneratedSourceNames:
    @pytest.mark.parametrize(
        "text",
        [
            WEB + "    command: ${DOCKMIN_SOURCE_1}\n",
            WEB + "    environment: [DOCKMIN_SOURCE_1]\n",
            WEB + "configs:\n  c:\n    environment: DOCKMIN_SOURCE_3\n",
        ],
    )
    def test_a_document_referring_to_one_is_refused(self, text: str) -> None:
        assert rejection(sd.precheck, text).reason == "refused_construct"
