"""Administrator admission of a Compose service's `build` section."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.services.access import RESERVED_LABELS
from app.services.stack_document import (
    FILE_NAME,
    MAX_VARIABLE_VALUE_BYTES,
    MAX_VARIABLES,
    VARIABLE_NAME,
    is_reserved_variable,
    path_part,
)

__all__ = [
    "DEFAULT_DOCKERFILE",
    "MAX_BUILD_ARGS",
    "MAX_BUILD_LABELS",
    "SECRET_IN_BUILD_ARG",
    "BuildAdmission",
    "BuildReview",
    "BuildSpec",
    "admit_build",
    "secret_build_arguments",
]

SECRET_IN_BUILD_ARG: Final = "secret_in_build_arg"
DEFAULT_DOCKERFILE: Final = "Dockerfile"
MAX_BUILD_ARGS: Final = MAX_VARIABLES
MAX_BUILD_LABELS: Final = 100
MAX_DOCKERFILE_PATH: Final = 255

_ALLOWED_KEYS: Final = frozenset({"context", "dockerfile", "args", "target", "labels", "no_cache"})
_RUN_OPTION: Final = "this build option changes how the build runs and is not supported."
_REFUSED_KEYS: Final[Mapping[str, str]] = {
    "additional_contexts": "additional build contexts are not supported.",
    "secrets": "build secrets are not supported.",
    "ssh": "SSH forwarding is not supported.",
    "network": _RUN_OPTION,
    "extra_hosts": _RUN_OPTION,
    "privileged": _RUN_OPTION,
    "entitlements": _RUN_OPTION,
    "isolation": _RUN_OPTION,
    "shm_size": _RUN_OPTION,
    "ulimits": _RUN_OPTION,
    "cache_from": "importing a build cache is not supported.",
    "cache_to": "exporting a build cache is not supported.",
    "pull": "dockmin pulls the base images itself.",
    "platforms": "multi-platform builds are not supported.",
    "tags": "dockmin names the built image.",
    "dockerfile_inline": "an inline Dockerfile is not supported; add a Dockerfile to the context.",
}
_UNKNOWN_KEY: Final = "this build option is not supported."
_SHAPE_MESSAGE: Final = "name a build context, or give a mapping with a `context`."
_CONTEXT_MESSAGE: Final = "Name a build context added to this stack, as `name` or `./name`."
_MISSING_CONTEXT_MESSAGE: Final = "No build context named `{name}` is added to this stack."
_DOCKERFILE_MESSAGE: Final = "use a relative path inside the context, without `..` or `$`."
_IMAGE_MESSAGE: Final = "an image name cannot be set beside `build`; dockmin names the built image."
_TARGET_MESSAGE: Final = "name a stage of the Dockerfile, without `$`."
_NO_CACHE_MESSAGE: Final = "this must be true or false."
_ARGS_MESSAGE: Final = "give the arguments as a mapping or as a list of NAME=value."
_LABELS_MESSAGE: Final = "give the labels as a mapping or as a list of key=value."
_VALUE_MESSAGE: Final = "give the argument a value, as NAME=value."
_SECRET_MESSAGE: Final = (
    "this argument holds the value of a secret and would be recorded in the image."
)

_TARGET: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LABEL_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}")
_PATH_UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f\\$]")
_NOT_SINGLE_LINE: Final = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_RESERVED_LABEL_PREFIXES: Final = ("com.docker.compose.", "com.docker.stack.")
_RESERVED_LABELS: Final = frozenset(label.casefold() for label in RESERVED_LABELS)


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """The admitted fields of one service's `build` section."""

    context: str
    dockerfile: str
    args: Mapping[str, str]
    target: str | None
    labels: Mapping[str, str]
    no_cache: bool


@dataclass(frozen=True, slots=True)
class BuildReview:
    """The outcome of reviewing one `build` section: its spec, or the refusals by Compose path."""

    spec: BuildSpec | None
    refusals: Mapping[str, str]


def admit_build(
    path: str,
    build: object,
    *,
    contexts: Collection[str],
    directory: str | None = None,
) -> BuildReview:
    """Review the `build` value found at `path`; the spec is None when anything is refused.

    `contexts` holds the stack's context names, matched exactly. `directory` is the project
    directory of a resolved model, whose contexts are absolute paths beneath it.
    """
    refusals: dict[str, str] = {}
    short_form = isinstance(build, str)
    body = {"context": build} if short_form else build
    if not isinstance(body, Mapping):
        return BuildReview(None, {path: _SHAPE_MESSAGE})
    names = frozenset(contexts)
    context: str | None = None
    dockerfile = DEFAULT_DOCKERFILE
    args: dict[str, str] = {}
    labels: dict[str, str] = {}
    target: str | None = None
    no_cache = False
    for key, value in body.items():
        where = path if short_form else f"{path}.{path_part(key)}"
        if not isinstance(key, str) or key not in _ALLOWED_KEYS:
            folded = key.strip().casefold() if isinstance(key, str) else ""
            refusals[where] = _REFUSED_KEYS.get(folded, _UNKNOWN_KEY)
            continue
        match key:
            case "context":
                context = _context(where, value, names, directory, refusals)
            case "dockerfile":
                dockerfile = _dockerfile(where, value, refusals) or dockerfile
            case "args":
                args = _arguments(where, value, refusals)
            case "labels":
                labels = _labels(where, value, refusals)
            case "target":
                target = _target(where, value, refusals)
            case _:
                no_cache = _no_cache(where, value, refusals)
    if "context" not in body:
        refusals[f"{path}.context"] = _CONTEXT_MESSAGE
    if refusals or context is None:
        return BuildReview(None, refusals)
    spec = BuildSpec(
        context=context,
        dockerfile=dockerfile,
        args=MappingProxyType(dict(sorted(args.items()))),
        target=target,
        labels=MappingProxyType(dict(sorted(labels.items()))),
        no_cache=no_cache,
    )
    return BuildReview(spec, {})


@dataclass(frozen=True, slots=True)
class BuildAdmission:
    """The stack's context names, as the build check the pre-check and the resolved scan take."""

    contexts: frozenset[str]
    directory: str | None = None

    def check(self, base: str, service: Mapping[Any, Any]) -> dict[str, str]:
        """Return the refusals by path for the service at `base`; empty when it has no `build`."""
        if "build" not in service:
            return {}
        found = dict(
            admit_build(
                f"{base}.build",
                service["build"],
                contexts=self.contexts,
                directory=self.directory,
            ).refusals
        )
        for key in service:
            if isinstance(key, str) and key.strip().casefold() == "image":
                found[f"{base}.{path_part(key)}"] = _IMAGE_MESSAGE
        return found

    def review(self, base: str, service: Mapping[Any, Any]) -> BuildReview | None:
        """Return the review of the service's `build`, or None when it has none."""
        if "build" not in service:
            return None
        return admit_build(
            f"{base}.build", service["build"], contexts=self.contexts, directory=self.directory
        )


def secret_build_arguments(
    services: Mapping[str, Mapping[str, Any]], terms: Collection[str]
) -> dict[str, str]:
    """Return the build arguments whose resolved value holds a secret term, keyed by path.

    Run on the resolved model: a value still to be interpolated cannot hold the secret yet.
    The reason code for a refusal built from this is SECRET_IN_BUILD_ARG.
    """
    needles = tuple(term for term in terms if term)
    found: dict[str, str] = {}
    if not needles:
        return found
    for name, service in services.items():
        build = service.get("build") if isinstance(service, Mapping) else None
        if not isinstance(build, Mapping) or "args" not in build:
            continue
        where = f"services.{path_part(name)}.build.args"
        values, _ = _argument_values(where, build["args"])
        for arg_path, value in values.items():
            if any(term in value for term in needles):
                found[arg_path] = _SECRET_MESSAGE
    return found


def _context(
    path: str,
    value: object,
    names: frozenset[str],
    directory: str | None,
    refusals: dict[str, str],
) -> str | None:
    if not isinstance(value, str) or "$" in value:
        refusals[path] = _CONTEXT_MESSAGE
        return None
    name = value
    if directory is not None and value.startswith(prefix := f"{directory.rstrip('/')}/"):
        name = value[len(prefix) :]
    name = name.removeprefix("./")
    if FILE_NAME.fullmatch(name) is None:
        refusals[path] = _CONTEXT_MESSAGE
        return None
    if name not in names:
        refusals[path] = _MISSING_CONTEXT_MESSAGE.format(name=name)
        return None
    return name


def _dockerfile(path: str, value: object, refusals: dict[str, str]) -> str | None:
    if isinstance(value, str):
        relative = value.removeprefix("./")
        segments = relative.split("/")
        if (
            0 < len(relative) <= MAX_DOCKERFILE_PATH
            and _PATH_UNSAFE.search(relative) is None
            and not any(segment in ("", ".", "..") for segment in segments)
        ):
            return relative
    refusals[path] = _DOCKERFILE_MESSAGE
    return None


def _target(path: str, value: object, refusals: dict[str, str]) -> str | None:
    if isinstance(value, str) and _TARGET.fullmatch(value) is not None:
        return value
    refusals[path] = _TARGET_MESSAGE
    return None


def _no_cache(path: str, value: object, refusals: dict[str, str]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in ("true", "false"):
        return value.casefold() == "true"
    refusals[path] = _NO_CACHE_MESSAGE
    return False


def _scalar(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return None


def _pairs(path: str, value: object) -> tuple[list[tuple[str, str, object]], str | None]:
    """Return (entry path, name, value) for a mapping or a NAME=value list, or a shape problem."""
    if isinstance(value, Mapping):
        return [
            (f"{path}.{path_part(key)}", key if isinstance(key, str) else "", item)
            for key, item in value.items()
        ], None
    if isinstance(value, list):
        entries: list[tuple[str, str, object]] = []
        for index, entry in enumerate(value):
            where = f"{path}[{index}]"
            if not isinstance(entry, str):
                entries.append((where, "", None))
                continue
            name, separator, text = entry.partition("=")
            entries.append((where, name, text if separator else None))
        return entries, None
    return [], "shape"


def _argument_values(path: str, value: object) -> tuple[dict[str, str], dict[str, str]]:
    """Return the resolved value by entry path, and the refusals of the whole `args` value."""
    refusals: dict[str, str] = {}
    entries, problem = _pairs(path, value)
    if problem is not None:
        return {}, {path: _ARGS_MESSAGE}
    if len(entries) > MAX_BUILD_ARGS:
        refusals[path] = f"a build can have at most {MAX_BUILD_ARGS} arguments."
    values: dict[str, str] = {}
    seen: set[str] = set()
    for where, name, item in entries:
        if VARIABLE_NAME.fullmatch(name) is None:
            refusals[where] = "use letters, digits and '_', starting with a letter or '_'."
        elif is_reserved_variable(name):
            refusals[where] = "this name is reserved because it changes how the build runs."
        elif name in seen:
            refusals[where] = "this argument is already set."
        else:
            seen.add(name)
        text = _scalar(item)
        if text is None:
            refusals.setdefault(where, _VALUE_MESSAGE)
        elif len(text.encode("utf-8", "replace")) > MAX_VARIABLE_VALUE_BYTES:
            refusals.setdefault(where, "a value can be at most 8 KiB.")
        elif _NOT_SINGLE_LINE.search(text) is not None:
            refusals.setdefault(where, "a value must be a single line.")
        else:
            values[where] = text
    return values, refusals


def _arguments(path: str, value: object, refusals: dict[str, str]) -> dict[str, str]:
    values, problems = _argument_values(path, value)
    refusals.update(problems)
    entries, _ = _pairs(path, value)
    names = {where: name for where, name, _ in entries}
    return {names[where]: text for where, text in values.items()}


def _label_problem(name: str, text: str | None) -> str | None:
    folded = name.casefold()
    if _LABEL_KEY.fullmatch(name) is None:
        return "use letters, digits and '.', '_', ':', '/' or '-', starting with a letter or digit."
    if folded in _RESERVED_LABELS or folded.startswith(_RESERVED_LABEL_PREFIXES):
        return "the stack membership labels are set by the product."
    if text is None:
        return "give the label a value, as key=value."
    if len(text.encode("utf-8", "replace")) > MAX_VARIABLE_VALUE_BYTES:
        return "a value can be at most 8 KiB."
    if _NOT_SINGLE_LINE.search(text) is not None:
        return "a value must be a single line."
    return None


def _labels(path: str, value: object, refusals: dict[str, str]) -> dict[str, str]:
    entries, problem = _pairs(path, value)
    if problem is not None:
        refusals[path] = _LABELS_MESSAGE
        return {}
    if len(entries) > MAX_BUILD_LABELS:
        refusals[path] = f"a build can set at most {MAX_BUILD_LABELS} labels."
    labels: dict[str, str] = {}
    for where, name, item in entries:
        text = _scalar(item)
        message = _label_problem(name, text)
        if message is None and name in labels:
            message = "this label is already set."
        if message is not None:
            refusals[where] = message
        elif text is not None:
            labels[name] = text
    return labels
