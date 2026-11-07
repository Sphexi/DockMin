"""Tests for app/services/stack_builds.py and the build admission hooks of the stack checks."""

from dataclasses import dataclass
from typing import Any

import pytest

from app.services import stack_document as sd
from app.services import stack_restrictions as sr
from app.services.stack_builds import (
    BuildAdmission,
    BuildSpec,
    admit_build,
    secret_build_arguments,
)
from app.services.stack_document import ComposeDocumentRejected

CONTEXTS = frozenset({"web", "api"})
BASE = "services.web"
PATH = f"{BASE}.build"
DIRECTORY = "/work/stack"
SECRET = "alpha-secret-value"
ADMISSION = BuildAdmission(CONTEXTS)


def review(build: object, directory: str | None = None) -> tuple[BuildSpec | None, dict[str, str]]:
    result = admit_build(PATH, build, contexts=CONTEXTS, directory=directory)
    return result.spec, dict(result.refusals)


def refusals_of(build: object) -> dict[str, str]:
    spec, refusals = review(build)
    assert spec is None
    return refusals


@dataclass(frozen=True)
class Model:
    name: str
    services: dict[str, Any]
    networks: dict[str, Any]
    volumes: dict[str, Any]
    configs: dict[str, Any]
    secrets: dict[str, Any]
    document: dict[str, Any]


def model(services: dict[str, Any]) -> Model:
    document = {"name": "sample", "services": services}
    return Model("sample", services, {}, {}, {}, {}, document)


ADMITTED = {
    "short form": ("web", "web", "Dockerfile"),
    "short form with ./": ("./web", "web", "Dockerfile"),
    "mapping": ({"context": "api"}, "api", "Dockerfile"),
    "dockerfile in a subdirectory": (
        {"context": "web", "dockerfile": "docker/app.Dockerfile"},
        "web",
        "docker/app.Dockerfile",
    ),
    "dockerfile with ./": (
        {"context": "web", "dockerfile": "./Dockerfile.dev"},
        "web",
        "Dockerfile.dev",
    ),
}


class TestAdmitted:
    @pytest.mark.parametrize(("build", "context", "dockerfile"), ADMITTED.values(), ids=ADMITTED)
    def test_a_confined_build_is_admitted_with_its_context_and_dockerfile(
        self, build: object, context: str, dockerfile: str
    ) -> None:
        spec, refusals = review(build)

        assert refusals == {}
        assert spec is not None
        assert (spec.context, spec.dockerfile) == (context, dockerfile)

    def test_every_permitted_key_is_carried_into_the_spec(self) -> None:
        build = {
            "context": "web",
            "args": {"B": "2", "A": 1, "C": True},
            "target": "final",
            "labels": ["org.example.team=core", "org.example.tier=1"],
            "no_cache": True,
        }

        spec, refusals = review(build)

        assert refusals == {}
        assert spec == BuildSpec(
            context="web",
            dockerfile="Dockerfile",
            args={"A": "1", "B": "2", "C": "true"},
            target="final",
            labels={"org.example.team": "core", "org.example.tier": "1"},
            no_cache=True,
        )
        assert list(spec.args) == ["A", "B", "C"]

    def test_arguments_given_as_a_list_keep_everything_after_the_first_equals_sign(self) -> None:
        spec, _ = review({"context": "web", "args": ["URL=https://x/?a=b", "EMPTY="]})

        assert spec is not None
        assert dict(spec.args) == {"URL": "https://x/?a=b", "EMPTY": ""}

    def test_no_cache_accepts_the_text_forms_of_a_boolean(self) -> None:
        spec, _ = review({"context": "web", "no_cache": "False"})

        assert spec is not None
        assert spec.no_cache is False

    def test_a_resolved_context_beneath_the_project_directory_is_the_context_name(self) -> None:
        spec, refusals = review(
            {"context": f"{DIRECTORY}/web", "dockerfile": "Dockerfile"}, DIRECTORY
        )

        assert refusals == {}
        assert spec is not None
        assert spec.context == "web"


REFUSED_KEYS = [
    "additional_contexts",
    "secrets",
    "ssh",
    "network",
    "extra_hosts",
    "cache_from",
    "cache_to",
    "pull",
    "platforms",
    "privileged",
    "entitlements",
    "isolation",
    "shm_size",
    "ulimits",
    "tags",
    "dockerfile_inline",
    "unheard_of",
    "x-extension",
    "Context",
    " args",
]

REFUSED_VALUES = {
    "context missing": ({"dockerfile": "Dockerfile"}, f"{PATH}.context"),
    "context not added to the stack": ({"context": "other"}, f"{PATH}.context"),
    "context with a different case": ({"context": "WEB"}, f"{PATH}.context"),
    "context dot": ({"context": "."}, f"{PATH}.context"),
    "context parent": ({"context": "../web"}, f"{PATH}.context"),
    "context absolute": ({"context": "/web"}, f"{PATH}.context"),
    "context nested": ({"context": "web/sub"}, f"{PATH}.context"),
    "context url": ({"context": "https://example.com/x.git"}, f"{PATH}.context"),
    "context interpolated": ({"context": "${CTX}"}, f"{PATH}.context"),
    "context not text": ({"context": 5}, f"{PATH}.context"),
    "dockerfile parent": ({"context": "web", "dockerfile": "../Dockerfile"}, f"{PATH}.dockerfile"),
    "dockerfile buried parent": (
        {"context": "web", "dockerfile": "a/../Dockerfile"},
        f"{PATH}.dockerfile",
    ),
    "dockerfile absolute": ({"context": "web", "dockerfile": "/etc/passwd"}, f"{PATH}.dockerfile"),
    "dockerfile interpolated": ({"context": "web", "dockerfile": "$F"}, f"{PATH}.dockerfile"),
    "dockerfile empty": ({"context": "web", "dockerfile": ""}, f"{PATH}.dockerfile"),
    "dockerfile empty segment": ({"context": "web", "dockerfile": "a//b"}, f"{PATH}.dockerfile"),
    "dockerfile backslash": ({"context": "web", "dockerfile": "a\\b"}, f"{PATH}.dockerfile"),
    "dockerfile not text": ({"context": "web", "dockerfile": ["x"]}, f"{PATH}.dockerfile"),
    "target with a flag shape": ({"context": "web", "target": "-x"}, f"{PATH}.target"),
    "target interpolated": ({"context": "web", "target": "$T"}, f"{PATH}.target"),
    "no_cache not a boolean": ({"context": "web", "no_cache": "yes"}, f"{PATH}.no_cache"),
    "args not a collection": ({"context": "web", "args": "A=1"}, f"{PATH}.args"),
    "arg name outside the grammar": (
        {"context": "web", "args": {"1A": "x"}},
        f"{PATH}.args.1A",
    ),
    "arg name interpolated": ({"context": "web", "args": ["${N}=x"]}, f"{PATH}.args[0]"),
    "arg name reserved by prefix": (
        {"context": "web", "args": {"BUILDKIT_SYNTAX": "x"}},
        f"{PATH}.args.BUILDKIT_SYNTAX",
    ),
    "arg name reserved by suffix": (
        {"context": "web", "args": {"HTTP_PROXY": "x"}},
        f"{PATH}.args.HTTP_PROXY",
    ),
    "arg name reserved exactly": ({"context": "web", "args": {"path": "x"}}, f"{PATH}.args.path"),
    "arg without a value in a list": ({"context": "web", "args": ["A"]}, f"{PATH}.args[0]"),
    "arg without a value in a map": ({"context": "web", "args": {"A": None}}, f"{PATH}.args.A"),
    "arg set twice": ({"context": "web", "args": ["A=1", "A=2"]}, f"{PATH}.args[1]"),
    "arg value with a newline": ({"context": "web", "args": {"A": "x\ny"}}, f"{PATH}.args.A"),
    "arg value too long": ({"context": "web", "args": {"A": "x" * 8193}}, f"{PATH}.args.A"),
    "label on the membership key": (
        {"context": "web", "labels": {"com.docker.compose.project": "x"}},
        f"{PATH}.labels.com.docker.compose.project",
    ),
    "label under the compose prefix": (
        {"context": "web", "labels": ["COM.docker.compose.service=x"]},
        f"{PATH}.labels[0]",
    ),
    "label without a value": ({"context": "web", "labels": ["a"]}, f"{PATH}.labels[0]"),
    "labels not a collection": ({"context": "web", "labels": "a=b"}, f"{PATH}.labels"),
    "build not text or a mapping": (["web"], PATH),
}


class TestRefused:
    @pytest.mark.parametrize("key", REFUSED_KEYS)
    def test_a_key_outside_the_permitted_set_is_refused_with_its_path(self, key: str) -> None:
        found = refusals_of({"context": "web", key: "x"})

        assert set(found) == {f"{PATH}.{sd.path_part(key)}"}

    @pytest.mark.parametrize(("build", "path"), REFUSED_VALUES.values(), ids=REFUSED_VALUES)
    def test_a_value_outside_its_rule_is_refused_with_its_path(
        self, build: object, path: str
    ) -> None:
        assert path in refusals_of(build)

    def test_the_short_form_is_refused_at_the_build_path_itself(self) -> None:
        assert set(refusals_of("other")) == {PATH}

    def test_every_refusal_is_reported_not_only_the_first(self) -> None:
        found = refusals_of({"context": "web", "ssh": ["default"], "cache_to": "x", "tags": ["t"]})

        assert set(found) == {f"{PATH}.ssh", f"{PATH}.cache_to", f"{PATH}.tags"}

    def test_a_resolved_context_outside_the_project_directory_is_refused(self) -> None:
        spec, refusals = review({"context": "/elsewhere/web"}, DIRECTORY)

        assert spec is None
        assert f"{PATH}.context" in refusals


class TestServiceCheck:
    def test_a_service_without_build_has_nothing_to_refuse(self) -> None:
        assert ADMISSION.check(BASE, {"image": "x"}) == {}

    def test_an_image_beside_build_is_refused_at_the_image_key(self) -> None:
        found = ADMISSION.check(BASE, {"image": "x", "build": "web"})

        assert set(found) == {f"{BASE}.image"}

    def test_the_review_of_a_service_without_build_is_none(self) -> None:
        assert ADMISSION.review(BASE, {"image": "x"}) is None

    def test_the_review_of_a_service_carries_the_spec(self) -> None:
        reviewed = ADMISSION.review(BASE, {"build": {"context": "api"}})

        assert reviewed is not None
        assert reviewed.spec is not None
        assert reviewed.spec.context == "api"


def compose(service_lines: str) -> str:
    return f"services:\n  web:\n{service_lines}"


class TestComposeDocumentPrecheck:
    def test_build_is_admitted_for_an_administrator_and_refused_without_the_check(self) -> None:
        text = compose("    build:\n      context: web\n      args: {A: '1'}\n")

        sd.precheck(text, builds=ADMISSION.check)
        with pytest.raises(ComposeDocumentRejected) as refused:
            sd.precheck(text)

        assert refused.value.path == PATH

    def test_a_refused_build_key_is_refused_at_its_path_for_an_administrator(self) -> None:
        text = compose("    build:\n      context: web\n      cache_to: [type=local]\n")

        with pytest.raises(ComposeDocumentRejected) as refused:
            sd.precheck(text, builds=ADMISSION.check)

        assert refused.value.path == f"{PATH}.cache_to"
        assert refused.value.reason == "refused_construct"

    @pytest.mark.parametrize(
        ("lines", "path"),
        [
            ("    build: web\n    image: x\n", f"{BASE}.image"),
            ("    build: web\n    pull_policy: build\n", f"{BASE}.pull_policy"),
            ("    build: web\n    develop: {watch: []}\n", f"{BASE}.develop"),
            ("    build: web\n    provider: {type: x}\n", f"{BASE}.provider"),
            ("    Build: web\n", f"{BASE}.Build"),
            ("    build: other\n", PATH),
        ],
        ids=["image", "pull_policy", "develop", "provider", "build key in another case", "context"],
    )
    def test_a_construct_beside_an_admitted_build_is_still_refused(
        self, lines: str, path: str
    ) -> None:
        with pytest.raises(ComposeDocumentRejected) as refused:
            sd.precheck(compose(lines), builds=ADMISSION.check)

        assert refused.value.path == path

    def test_a_swarm_document_refuses_build_whatever_the_check(self) -> None:
        with pytest.raises(ComposeDocumentRejected) as refused:
            sd.precheck(compose("    build: web\n"), kind="swarm", builds=ADMISSION.check)

        assert refused.value.path == PATH


def resolved(build: object, **extra: Any) -> Model:
    return model({"web": {"build": build, **extra}})


class TestResolvedModel:
    def test_build_is_refused_by_default_as_for_a_standard_user(self) -> None:
        found = sr.resolved_refusals(resolved({"context": f"{DIRECTORY}/web"}))

        assert set(found) == {PATH}

    def test_a_standard_users_scan_does_not_know_the_build_key(self) -> None:
        found = sr.scan(resolved({"context": f"{DIRECTORY}/web"}), default_runtime=None).findings

        assert [(item.option, item.path) for item in found] == [("unknown_option", PATH)]

    def test_an_administrators_resolved_build_is_admitted(self) -> None:
        admission = BuildAdmission(CONTEXTS, DIRECTORY)

        found = sr.resolved_refusals(
            resolved({"context": f"{DIRECTORY}/web", "dockerfile": "Dockerfile"}),
            builds=admission.check,
        )

        assert found == {}

    def test_a_refused_resolved_key_is_refused_at_its_path_for_an_administrator(self) -> None:
        admission = BuildAdmission(CONTEXTS, DIRECTORY)

        found = sr.resolved_refusals(
            resolved({"context": f"{DIRECTORY}/web", "network": "host"}, image="x"),
            builds=admission.check,
        )

        assert set(found) == {f"{PATH}.network", f"{BASE}.image"}

    def test_pull_policy_build_stays_refused_beside_an_admitted_build(self) -> None:
        admission = BuildAdmission(CONTEXTS, DIRECTORY)

        found = sr.resolved_refusals(
            resolved({"context": f"{DIRECTORY}/web"}, pull_policy="build"), builds=admission.check
        )

        assert set(found) == {f"{BASE}.pull_policy"}

    def test_a_swarm_model_refuses_build_because_the_check_is_not_passed(self) -> None:
        found = sr.swarm_refusals(resolved({"context": f"{DIRECTORY}/web"}))

        assert PATH in found


class TestSecretBuildArguments:
    def test_an_argument_holding_a_secret_value_is_refused_with_its_path(self) -> None:
        services = {"web": {"build": {"args": {"TOKEN": f"prefix-{SECRET}", "SAFE": "x"}}}}

        found = secret_build_arguments(services, {SECRET})

        assert set(found) == {f"{PATH}.args.TOKEN"}

    def test_an_argument_in_list_form_is_checked_too(self) -> None:
        services = {"web": {"build": {"args": [f"TOKEN={SECRET}"]}}}

        assert set(secret_build_arguments(services, {SECRET})) == {f"{PATH}.args[0]"}

    def test_every_service_is_checked(self) -> None:
        services: dict[str, dict[str, Any]] = {
            "web": {"build": {"args": {"A": SECRET}}},
            "api": {"build": {"args": {"B": SECRET}}},
            "db": {"image": "x"},
        }

        found = secret_build_arguments(services, {SECRET})

        assert set(found) == {f"{PATH}.args.A", "services.api.build.args.B"}

    @pytest.mark.parametrize(
        "terms", [(), ("",), ("other-value",)], ids=["none", "empty", "absent"]
    )
    def test_nothing_is_refused_when_no_term_appears_in_an_argument(
        self, terms: tuple[str, ...]
    ) -> None:
        services = {"web": {"build": {"args": {"A": SECRET}}}}

        assert secret_build_arguments(services, terms) == {}

    def test_a_value_set_in_the_stack_that_is_not_a_secret_is_not_refused(self) -> None:
        services = {"web": {"build": {"args": {"A": "plain"}}}}

        assert secret_build_arguments(services, {SECRET}) == {}
