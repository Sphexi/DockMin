"""The reference rule of the pre-check, applied to every key that names a companion file."""

from typing import Any

import pytest

from app.services import stack_document as sd
from app.services.stack_document import CompanionFile, ComposeDocumentRejected

WEB = "services:\n  web:\n    image: x\n"
STORED = "app"
COMPOSE_DOCUMENT = "services:\n  web:\n    image: x\n"

PLAIN = CompanionFile(STORED, "A=1\n")
SECRET = CompanionFile(STORED, "A=1\n", True)
DOCUMENT = CompanionFile(STORED, COMPOSE_DOCUMENT)

USES = {
    "env_file": (WEB + "    env_file: '{value}'\n", "services.web.env_file", PLAIN),
    "env_file list": (WEB + "    env_file: ['{value}']\n", "services.web.env_file", PLAIN),
    "env_file mapping": (
        WEB + "    env_file: [{{path: '{value}'}}]\n",
        "services.web.env_file",
        PLAIN,
    ),
    "env_file of a secret file": (
        WEB + "    env_file: ['{value}']\n",
        "services.web.env_file",
        SECRET,
    ),
    "label_file": (WEB + "    label_file: '{value}'\n", "services.web.label_file", PLAIN),
    "label_file list": (WEB + "    label_file: ['{value}']\n", "services.web.label_file", PLAIN),
    "configs file": (WEB + "configs:\n  c: {{file: '{value}'}}\n", "configs.c.file", PLAIN),
    "secrets file": (WEB + "secrets:\n  c: {{file: '{value}'}}\n", "secrets.c.file", SECRET),
    "include list": ("include: ['{value}']\n" + WEB, "include", DOCUMENT),
    "include path": ("include: [{{path: '{value}'}}]\n" + WEB, "include", DOCUMENT),
    "include path list": ("include: [{{path: ['{value}']}}]\n" + WEB, "include", DOCUMENT),
    "extends file": (
        WEB + "  o:\n    extends: {{file: '{value}', service: web}}\n",
        "services.o.extends.file",
        DOCUMENT,
    ),
}
ACCEPTED_FORMS = [STORED, f"./{STORED}"]
REFUSED_FORMS = {
    "an absolute path": "/etc/passwd",
    "a parent directory": "../app",
    "a path through a parent directory": "./../app",
    "a subdirectory": "sub/app",
    "a home directory": "~/app",
    "a bare tilde": "~",
    "a URL": "https://host/app",
    "an oci reference": "oci://host/app",
    "a git reference": "git@host:org/repo.git",
    "a git URL": "git+https://host/repo.git#main",
    "a dollar sign after the name": "app$",
    "an interpolation": "${HOME}",
    "an escaped dollar": "$$app",
    "an empty value": "",
    "a doubled dot prefix": "././app",
    "a trailing slash": "app/",
    "a name with a space": "app x",
}
CASES = [
    pytest.param(use, value, id=f"{use} - {form}")
    for use in USES
    for form, value in REFUSED_FORMS.items()
]


def build(use: str, value: str) -> tuple[str, str, CompanionFile]:
    text, root, stored = USES[use]
    return text.format(value=value), root, stored


def rejected(text: str, *files: CompanionFile) -> ComposeDocumentRejected:
    with pytest.raises(ComposeDocumentRejected) as raised:
        sd.precheck(text, files=files)
    return raised.value


class TestAcceptedReferences:
    @pytest.mark.parametrize("value", ACCEPTED_FORMS)
    @pytest.mark.parametrize("use", USES)
    def test_a_bare_or_dotted_name_of_a_stored_file_is_admitted(self, use: str, value: str) -> None:
        text, _, stored = build(use, value)

        uses: Any = sd.precheck(text, files=[stored])

        assert list(uses) == [STORED]


class TestRefusedReferences:
    @pytest.mark.parametrize(("use", "value"), CASES)
    def test_a_reference_outside_the_two_forms_is_refused_at_the_key_that_holds_it(
        self, use: str, value: str
    ) -> None:
        text, root, stored = build(use, value)

        error = rejected(text, stored)

        assert error.reason == "refused_construct"
        assert (error.path or "").startswith(root)
        assert error.fields["compose_file"].endswith(sd.REFERENCE_MESSAGE)

    @pytest.mark.parametrize("use", USES)
    def test_a_well_formed_name_that_is_not_stored_is_file_missing(self, use: str) -> None:
        text, root, stored = build(use, "absent")

        error = rejected(text, stored)

        assert error.reason == "file_missing"
        assert (error.path or "").startswith(root)
        assert "absent" in error.fields["compose_file"]

    @pytest.mark.parametrize("use", USES)
    def test_a_name_differing_only_in_case_is_not_the_stored_file(self, use: str) -> None:
        text, _, stored = build(use, STORED.upper())

        error = rejected(text, stored)

        assert error.reason == "file_missing"
