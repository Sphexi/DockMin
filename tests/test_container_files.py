"""Structure of the Dockerfile, the Compose files and the build context."""

import importlib.util
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import ENV_PREFIX, Settings

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
UPDATER_DOCKERFILE = ROOT / "Dockerfile.updater"
COMPOSE = ROOT / "compose.yaml"
PROXY_EXAMPLE = ROOT / "examples" / "reverse-proxy" / "nginx" / "compose.yaml"
DIRECT_EXAMPLE = ROOT / "examples" / "direct-https" / "compose.yaml"
NGINX_CONF = ROOT / "examples" / "reverse-proxy" / "nginx" / "nginx.conf"
DOCKERIGNORE = ROOT / ".dockerignore"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
LICENSE = ROOT / "LICENSE"
PYPROJECT = ROOT / "pyproject.toml"
NON_ROOT_ID = 10001
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
SECRET_FILE_PREFIX = "/run/secrets/"
COMPOSE_LICENCE_DESTINATION = "/usr/share/licenses/docker-compose/"
COMPOSE_LICENCE_FILES = ("LICENSE", "NOTICE")
BUILDX_LICENCE_DESTINATION = "/usr/share/licenses/buildx/"
BUILDX_LICENCE_SOURCE = "licenses/buildx/LICENSE"
BUILDX_PLUGIN_PATH = "/usr/local/libexec/docker/cli-plugins/docker-buildx"
OWNED_DIRECTORIES = f"install -d -o {NON_ROOT_ID} -g {NON_ROOT_ID} -m 0700 /data /key"
REMOVE_SETUID = "find / -xdev -perm /6000 -type f -exec chmod a-s {} +"
REMOVE_GROUP_WRITE = "find / -xdev -perm -g+w ! -perm -1000 ! -type l -exec chmod g-w {} +"
SECURE_COMPOSE_ENVIRONMENT = {"DOCKMIN_TLS_SELF_SIGNED": "false"}
IMAGE_PLATFORMS = "linux/amd64,linux/arm64"
BINFMT_IMAGE = re.compile(r"^docker\.io/tonistiigi/binfmt:qemu-v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
OCI_LABELS = ("version", "revision", "source", "created")
RELEASE_BUILDS = (
    pytest.param("Dockerfile", "meta", id="application"),
    pytest.param("Dockerfile.updater", "meta_updater", id="updater"),
)
LOCAL_VERSION = "0+local"
REVISION_ARGUMENT = "DOCKMIN_REVISION"
REVISION_PATH = "/usr/share/dockmin"
FULL_HASH = "0123456789abcdef0123456789abcdef01234567"


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose files, whose merge tags are not standard YAML."""


for tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(
        tag,
        lambda loader, node: (
            loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else None
        ),
    )


def load_compose(path: Path) -> dict[str, Any]:
    returned: dict[str, Any] = yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)
    return returned


def instructions(text: str) -> list[tuple[str, str]]:
    """Return (INSTRUCTION, arguments) pairs with continuation lines joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", text)
    pairs: list[tuple[str, str]] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            word, _, rest = stripped.partition(" ")
            pairs.append((word.upper(), rest.strip()))
    return pairs


@pytest.fixture(scope="module")
def dockerfile() -> list[tuple[str, str]]:
    return instructions(DOCKERFILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return load_compose(COMPOSE)


def arguments_of(parsed: list[tuple[str, str]], instruction: str) -> list[str]:
    return [rest for word, rest in parsed if word == instruction]


def test_every_base_image_is_pinned_by_digest(dockerfile: list[tuple[str, str]]) -> None:
    images = [rest.split()[0] for rest in arguments_of(dockerfile, "FROM")]

    assert len(images) == 4
    assert [image for image in images if not DIGEST.search(image)] == []


def test_the_image_runs_as_a_numeric_non_root_user(dockerfile: list[tuple[str, str]]) -> None:
    users = arguments_of(dockerfile, "USER")

    assert users == [f"{NON_ROOT_ID}:{NON_ROOT_ID}"]
    assert dockerfile[-1][0] == "CMD"
    assert dockerfile.index(("USER", users[0])) < dockerfile.index(dockerfile[-1])


@pytest.mark.parametrize("instruction", ["CMD", "HEALTHCHECK"])
def test_the_start_command_and_the_health_command_are_in_exec_form(
    dockerfile: list[tuple[str, str]], instruction: str
) -> None:
    [arguments] = arguments_of(dockerfile, instruction)

    command = arguments.split("CMD", 1)[1].strip() if instruction == "HEALTHCHECK" else arguments
    assert command.startswith("[") and command.endswith("]")
    assert '"' in command


def test_the_commands_name_modules_that_exist() -> None:
    assert importlib.util.find_spec("app.serving.__main__") is not None
    assert importlib.util.find_spec("app.serving.healthcheck") is not None


def test_only_the_compose_binary_is_copied_in_from_the_compose_stage(
    dockerfile: list[tuple[str, str]],
) -> None:
    copies = [rest for rest in arguments_of(dockerfile, "COPY") if "--from=compose" in rest]

    assert len(copies) == 1
    sources = [part for part in copies[0].split() if part.startswith("/")]
    assert not any(part.endswith(("/docker", "/dockerd", "/containerd")) for part in sources)
    assert sources[0] == "/docker-compose"
    assert "--chown=0:0" in copies[0] and "--chmod=0755" in copies[0]


def test_the_compose_stage_is_the_compose_binary_image_pinned_by_digest(
    dockerfile: list[tuple[str, str]],
) -> None:
    [image] = [
        rest.split()[0] for rest in arguments_of(dockerfile, "FROM") if rest.endswith("AS compose")
    ]

    assert image.startswith("docker/compose-bin:v")
    assert DIGEST.search(image)


def test_the_buildx_stage_is_the_buildx_binary_image_pinned_by_digest(
    dockerfile: list[tuple[str, str]],
) -> None:
    [image] = [
        rest.split()[0] for rest in arguments_of(dockerfile, "FROM") if rest.endswith("AS buildx")
    ]

    assert re.match(r"docker/buildx-bin:\d+\.\d+\.\d+@", image)
    assert DIGEST.search(image)


def test_only_the_buildx_binary_is_copied_in_root_owned_and_executable_beside_compose(
    dockerfile: list[tuple[str, str]],
) -> None:
    [copy] = [rest for rest in arguments_of(dockerfile, "COPY") if "--from=buildx" in rest]
    arguments = [part for part in copy.split() if not part.startswith("--")]

    assert arguments == ["/buildx", BUILDX_PLUGIN_PATH]
    assert "--chown=0:0" in copy and "--chmod=0755" in copy


def test_the_image_carries_the_buildx_licence(dockerfile: list[tuple[str, str]]) -> None:
    [copy] = [
        rest
        for rest in arguments_of(dockerfile, "COPY")
        if rest.endswith(BUILDX_LICENCE_DESTINATION)
    ]
    sources = [part for part in copy.split() if not part.startswith("--")][:-1]

    assert sources == [BUILDX_LICENCE_SOURCE]
    assert "--chown=0:0" in copy and "--chmod=0644" in copy
    assert (ROOT / BUILDX_LICENCE_SOURCE).is_file()


def test_both_plugin_binaries_are_in_place_before_the_user_is_dropped(
    dockerfile: list[tuple[str, str]],
) -> None:
    user = dockerfile.index(("USER", f"{NON_ROOT_ID}:{NON_ROOT_ID}"))
    plugin_copies = [
        index
        for index, (word, rest) in enumerate(dockerfile)
        if word == "COPY" and ("--from=buildx" in rest or "--from=compose" in rest)
    ]

    assert len(plugin_copies) == 2
    assert all(index < user for index in plugin_copies)


def test_dependencies_are_installed_from_hashed_locks_and_pip_does_not_ship(
    dockerfile: list[tuple[str, str]],
) -> None:
    runs = " ".join(arguments_of(dockerfile, "RUN"))

    assert runs.count("--require-hashes") == 2
    assert "--no-build-isolation" in runs and "--no-index" in runs
    assert "--only-binary=:all:" in runs
    assert "rm -rf" in runs and "site-packages/pip" in runs


def test_the_image_declares_its_ports_data_directory_and_version(
    dockerfile: list[tuple[str, str]],
) -> None:
    text = " ".join(f"{word} {rest}" for word, rest in dockerfile)

    assert arguments_of(dockerfile, "EXPOSE") == ["8000 8443"]
    assert "DOCKMIN_DATA_DIR=/data" in text
    assert 'org.opencontainers.image.version="${DOCKMIN_VERSION}"' in text


def test_the_package_version_is_taken_from_the_build_argument(
    dockerfile: list[tuple[str, str]],
) -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    runs = arguments_of(dockerfile, "RUN")

    assert "version" not in pyproject["project"]
    assert pyproject["project"]["dynamic"] == ["version"]
    assert arguments_of(dockerfile, "ARG").count(f"DOCKMIN_VERSION={LOCAL_VERSION}") == 2
    assert any(run.startswith('DOCKMIN_VERSION="${DOCKMIN_VERSION}" pip ') for run in runs)


def test_the_data_and_key_directories_are_created_for_the_image_user(
    dockerfile: list[tuple[str, str]],
) -> None:
    runs = arguments_of(dockerfile, "RUN")

    assert any(OWNED_DIRECTORIES in run for run in runs)


def test_the_image_declares_no_volume(dockerfile: list[tuple[str, str]]) -> None:
    assert arguments_of(dockerfile, "VOLUME") == []


def test_setuid_bits_and_group_write_are_removed_before_the_user_switch(
    dockerfile: list[tuple[str, str]],
) -> None:
    [hardening] = [
        index
        for index, (word, rest) in enumerate(dockerfile)
        if word == "RUN" and REMOVE_SETUID in rest
    ]

    assert REMOVE_GROUP_WRITE in dockerfile[hardening][1]
    assert hardening < dockerfile.index(("USER", f"{NON_ROOT_ID}:{NON_ROOT_ID}"))
    assert all(word != "FROM" for word, _ in dockerfile[hardening:])


def test_the_image_carries_the_licence_file_and_declares_its_licence(
    dockerfile: list[tuple[str, str]],
) -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    text = " ".join(f"{word} {rest}" for word, rest in dockerfile)
    build_copies = " ".join(
        rest for rest in arguments_of(dockerfile, "COPY") if "--from" not in rest
    )

    assert LICENSE.is_file()
    assert "LICENSE" in build_copies.split()
    assert f'org.opencontainers.image.licenses="{project["license"]}"' in text


def test_nothing_in_the_image_is_installed_with_a_compiler_in_the_final_stage(
    dockerfile: list[tuple[str, str]],
) -> None:
    final = dockerfile[max(i for i, (word, _) in enumerate(dockerfile) if word == "FROM") :]
    text = " ".join(rest for _, rest in final)

    assert "gcc" not in text and "apt-get" not in text and "pip install" not in text


def test_the_build_context_admits_exactly_what_the_dockerfiles_copy() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    admitted = {line.removeprefix("!").rstrip("/") for line in lines if line.startswith("!")}
    copied = {
        part.removeprefix("./").split("/")[0]
        for path in (DOCKERFILE, UPDATER_DOCKERFILE)
        for rest in arguments_of(instructions(path.read_text(encoding="utf-8")), "COPY")
        if "--from" not in rest
        for part in rest.split()[:-1]
        if not part.startswith("--")
    }

    assert lines[0] == "*"
    assert admitted == copied


def test_the_image_carries_the_compose_licence_and_notice_beside_the_plugin(
    dockerfile: list[tuple[str, str]],
) -> None:
    [copy] = [
        rest
        for rest in arguments_of(dockerfile, "COPY")
        if rest.endswith(COMPOSE_LICENCE_DESTINATION)
    ]
    sources = [part for part in copy.split() if not part.startswith("--")][:-1]

    assert sources == [f"licenses/docker-compose/{name}" for name in COMPOSE_LICENCE_FILES]
    assert "--chown=0:0" in copy and "--chmod=0644" in copy
    assert all((ROOT / source).is_file() for source in sources)


def test_the_compose_file_hardens_the_container_and_builds_nothing(
    compose: dict[str, Any],
) -> None:
    service = compose["services"]["dockmin"]

    assert "build" not in service
    assert "${DOCKMIN_IMAGE" in service["image"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    [tmp] = [entry for entry in service["tmpfs"] if entry.startswith("/tmp")]
    assert {"noexec", "nosuid", "nodev"} <= set(tmp.split(":")[1].split(","))
    assert service["restart"] == "unless-stopped"
    assert service["deploy"]["resources"]["limits"].keys() >= {"cpus", "memory", "pids"}


def test_the_encryption_key_is_a_file_secret_and_never_an_environment_value(
    compose: dict[str, Any],
) -> None:
    service = compose["services"]["dockmin"]

    assert "DOCKMIN_SECRET_KEY" not in service["environment"]
    assert service["environment"]["DOCKMIN_SECRET_KEY_FILE"].startswith(SECRET_FILE_PREFIX)
    assert service["secrets"] == ["dockmin_secret_key"]
    assert list(compose["secrets"]["dockmin_secret_key"]) == ["file"]


def test_the_compose_file_turns_the_self_signed_listener_off(compose: dict[str, Any]) -> None:
    environment = compose["services"]["dockmin"]["environment"]

    assert {name: environment[name] for name in SECURE_COMPOSE_ENVIRONMENT} == (
        SECURE_COMPOSE_ENVIRONMENT
    )


def release_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    [job] = workflow["jobs"].values()
    returned: list[dict[str, Any]] = job["steps"]
    return returned


def release_build(dockerfile: str = "Dockerfile") -> dict[str, Any]:
    [build] = [
        step["with"]
        for step in release_steps()
        if step.get("uses", "").startswith("docker/build-push-action@")
        and step["with"].get("file", "Dockerfile") == dockerfile
    ]
    returned: dict[str, Any] = build
    return returned


def build_arguments(build: dict[str, Any]) -> list[str]:
    return str(build["build-args"]).splitlines()


def index_of_action(steps: list[dict[str, Any]], action: str) -> int:
    [index] = [i for i, step in enumerate(steps) if step.get("uses", "").startswith(f"{action}@")]
    return index


def test_the_release_sets_up_the_emulator_before_the_builder() -> None:
    steps = release_steps()

    assert index_of_action(steps, "docker/setup-qemu-action") < index_of_action(
        steps, "docker/setup-buildx-action"
    )
    assert steps[index_of_action(steps, "docker/setup-qemu-action")]["with"]["platforms"] == "arm64"


@pytest.mark.parametrize(("dockerfile", "meta"), RELEASE_BUILDS)
def test_the_release_builds_both_architectures(dockerfile: str, meta: str) -> None:
    assert release_build(dockerfile)["platforms"] == IMAGE_PLATFORMS


def test_the_release_pins_the_emulator_image_and_does_not_cache_it() -> None:
    steps = release_steps()

    qemu = steps[index_of_action(steps, "docker/setup-qemu-action")]["with"]

    assert BINFMT_IMAGE.match(qemu["image"])
    assert qemu["cache-image"] is False


def test_the_release_sets_up_the_emulator_and_builder_before_logging_in() -> None:
    steps = release_steps()

    login = index_of_action(steps, "docker/login-action")

    assert index_of_action(steps, "docker/setup-qemu-action") < login
    assert index_of_action(steps, "docker/setup-buildx-action") < login


@pytest.mark.parametrize(("dockerfile", "meta"), RELEASE_BUILDS)
def test_the_release_builds_the_version_named_by_the_tag(dockerfile: str, meta: str) -> None:
    build = release_build(dockerfile)

    assert f"DOCKMIN_VERSION=${{{{ steps.{meta}.outputs.version }}}}" in build_arguments(build)
    assert build["tags"] == f"${{{{ steps.{meta}.outputs.tags }}}}"


def test_the_release_passes_the_commit_to_the_application_build_and_not_to_the_updater() -> None:
    application = build_arguments(release_build("Dockerfile"))
    updater = build_arguments(release_build("Dockerfile.updater"))

    assert f"{REVISION_ARGUMENT}=${{{{ github.sha }}}}" in application
    assert [argument for argument in updater if REVISION_ARGUMENT in argument] == []


def test_the_revision_argument_is_declared_in_the_final_stage_before_the_user_is_dropped(
    dockerfile: list[tuple[str, str]],
) -> None:
    declared = dockerfile.index(("ARG", f'{REVISION_ARGUMENT}=""'))
    final_stage = max(i for i, (word, _) in enumerate(dockerfile) if word == "FROM")
    recording = next(
        i
        for i, (word, rest) in enumerate(dockerfile)
        if word == "RUN" and f"{REVISION_PATH}/revision" in rest
    )

    assert arguments_of(dockerfile, "ARG").count(f'{REVISION_ARGUMENT}=""') == 1
    assert final_stage < declared < recording < dockerfile.index(("USER", "10001:10001"))
    assert f"install -d -o 0 -g 0 -m 0755 {REVISION_PATH}" in dockerfile[recording][1]
    assert f"chmod 0644 {REVISION_PATH}/revision" in dockerfile[recording][1]


def recording_step(dockerfile: list[tuple[str, str]], directory: Path) -> str:
    """Return the Dockerfile's revision step with its install moved under `directory`."""
    [run] = [
        rest for word, rest in dockerfile if word == "RUN" and f"{REVISION_PATH}/revision" in rest
    ]
    return run[run.index("if [") :].replace(REVISION_PATH, str(directory))


def recorded_by_the_build(
    dockerfile: list[tuple[str, str]], directory: Path, value: str
) -> list[str]:
    subprocess.run(
        ["sh", "-c", recording_step(dockerfile, directory)],
        env={"DOCKMIN_REVISION": value, "PATH": "/usr/bin:/bin"},
        check=True,
    )
    return [entry.read_text(encoding="utf-8") for entry in directory.iterdir()]


def test_the_build_writes_a_full_hash_to_the_revision_file(
    dockerfile: list[tuple[str, str]], tmp_path: Path
) -> None:
    assert recorded_by_the_build(dockerfile, tmp_path, FULL_HASH) == [f"{FULL_HASH}\n"]


@pytest.mark.parametrize(
    "value",
    ["", FULL_HASH[:-1], FULL_HASH + "0", FULL_HASH.upper(), "g" * 40, f"{FULL_HASH[:-1]}\n"],
    ids=["empty", "short", "long", "upper-case", "not-hex", "embedded-newline"],
)
def test_the_build_writes_nothing_for_any_other_value(
    dockerfile: list[tuple[str, str]], tmp_path: Path, value: str
) -> None:
    assert recorded_by_the_build(dockerfile, tmp_path, value) == []


@pytest.mark.parametrize(("dockerfile", "meta"), RELEASE_BUILDS)
def test_the_release_build_pushes_with_provenance_and_an_sbom(dockerfile: str, meta: str) -> None:
    build = release_build(dockerfile)

    assert build["push"] is True
    assert build["provenance"] == "mode=max"
    assert build["sbom"] is True


def test_the_release_builds_each_image_in_its_own_gha_cache() -> None:
    application = release_build()
    updater = release_build("Dockerfile.updater")

    assert application["cache-from"] == "type=gha"
    assert application["cache-to"] == "type=gha,mode=max"
    assert updater["cache-from"] == "type=gha,scope=updater"
    assert updater["cache-to"] == "type=gha,scope=updater,mode=max"


@pytest.mark.parametrize(("dockerfile", "meta"), RELEASE_BUILDS)
def test_the_release_build_adds_the_four_oci_labels_and_no_others(
    dockerfile: str, meta: str
) -> None:
    labels = release_build(dockerfile)["labels"].splitlines()

    assert [line.split("=", 1)[0] for line in labels] == [
        f"org.opencontainers.image.{name}" for name in OCI_LABELS
    ]
    assert all(f"steps.{meta}.outputs.json" in line for line in labels)


def test_every_action_the_release_uses_is_pinned_to_a_commit() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    references = [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]

    assert [ref for ref in references if not re.search(r"@[0-9a-f]{40}$", ref)] == []


def test_the_published_port_is_bound_to_the_loopback_address(compose: dict[str, Any]) -> None:
    ports = compose["services"]["dockmin"]["ports"]

    assert all(port.startswith("127.0.0.1:") for port in ports)


def test_the_grace_period_covers_the_slowest_orderly_stop(compose: dict[str, Any]) -> None:
    from app.serving.launcher import GRACEFUL_SHUTDOWN_SECONDS

    seconds = int(str(compose["services"]["dockmin"]["stop_grace_period"]).removesuffix("s"))

    assert seconds > 2 * GRACEFUL_SHUTDOWN_SECONDS


def test_every_setting_the_compose_file_passes_is_one_the_application_reads(
    compose: dict[str, Any],
) -> None:
    known = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}

    passed = set(compose["services"]["dockmin"]["environment"])

    assert passed - known == set()


def test_the_compose_file_does_not_mount_the_data_or_the_socket_read_only_by_accident(
    compose: dict[str, Any],
) -> None:
    volumes = compose["services"]["dockmin"]["volumes"]

    assert "dockmin-data:/data" in volumes
    assert "/var/run/docker.sock:/var/run/docker.sock" in volumes


def test_the_proxy_example_trusts_one_fixed_address_and_publishes_no_application_port() -> None:
    example = load_compose(PROXY_EXAMPLE)
    dockmin, nginx = example["services"]["dockmin"], example["services"]["nginx"]

    assert dockmin["environment"]["DOCKMIN_TRUSTED_PROXIES"].endswith("/32")
    assert dockmin["ports"] is None or dockmin["ports"] == []
    assert DIGEST.search(nginx["image"])
    assert nginx["user"] == "101:101"
    assert nginx["read_only"] is True and nginx["cap_drop"] == ["ALL"]
    assert (
        nginx["networks"]["default"]["ipv4_address"].split(":-")[-1].rstrip("}")
        in (dockmin["environment"]["DOCKMIN_TRUSTED_PROXIES"])
    )


def test_the_proxy_example_buffers_nothing_for_the_restore_upload_and_the_terminal() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    assert re.search(
        r"location\s*=?\s*/restore/archive\s*\{[^}]*proxy_request_buffering\s+off", conf
    )
    assert re.search(r"terminal/ws[^{]*\{[^}]*proxy_buffering\s+off", conf)
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in conf
    assert "server_tokens off" in conf


def test_the_direct_https_example_passes_a_certificate_pair_by_file_secrets() -> None:
    example = load_compose(DIRECT_EXAMPLE)
    dockmin = example["services"]["dockmin"]

    assert dockmin["environment"]["DOCKMIN_TLS_CERT_FILE"].startswith(SECRET_FILE_PREFIX)
    assert dockmin["environment"]["DOCKMIN_TLS_KEY_FILE"].startswith(SECRET_FILE_PREFIX)
    assert set(dockmin["secrets"]) == {"dockmin_tls_cert", "dockmin_tls_key"}
    assert set(example["secrets"]) == {"dockmin_tls_cert", "dockmin_tls_key"}
