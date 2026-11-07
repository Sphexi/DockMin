# Contributing

## Contribution policy

| Contribution | How |
|---|---|
| Bug report | Open a GitHub issue with the [bug report template](https://github.com/Sphexi/DockMin/issues/new?template=bug_report.md). |
| Feature request | Open a GitHub issue with the [feature request template](https://github.com/Sphexi/DockMin/issues/new?template=feature_request.md). |
| Vulnerability | Report it privately as described in [SECURITY.md](https://github.com/Sphexi/DockMin/blob/main/SECURITY.md). Do not open an issue. |
| A different direction for the project | Fork the project. |
| Small addition, straight bug fix, or anything that adds without changing the overall functionality | Open a pull request. |
| Large change | Open an issue first. A large pull request without a prior issue is pushed back on. |

- The project is deliberately opinionated and is maintained for the maintainer's own use.
- A pull request passes the four checks in [Checks](#checks).

## Development setup

| Requirement | Version |
|---|---|
| Python | 3.12 or later |
| uv | Any recent release |

Run from the repository root.

```bash
uv sync
```

The sync creates `.venv` and installs the runtime and development dependencies from `uv.lock`. Every tool below runs from `.venv/bin/`.

## Checks

| Check | Tool | Scope |
|---|---|---|
| Lint | `ruff` | `app`, `tests`, `scripts`, `updater` |
| Format | `ruff` formatter | `app`, `tests`, `scripts`, `updater` |
| Types | `mypy` | `app`, `tests` and `updater`, strict |
| Tests | `pytest` | `tests`, 4 workers (`-n 4 --dist loadgroup`) |

Run all four in order. The run stops at the first failure.

```bash
.venv/bin/python scripts/check.py
```

Run each check alone:

```bash
.venv/bin/ruff check app tests scripts updater
.venv/bin/ruff format --check app tests scripts updater
.venv/bin/mypy
.venv/bin/pytest -n 4 --dist loadgroup
```

Apply the formatter:

```bash
.venv/bin/ruff format app tests scripts updater
```

- A change is ready when all four checks pass.
- Lint, format, type and test settings are in `pyproject.toml`. Line length is 100.
- `mypy` takes its paths (`app`, `tests`, `updater`) and strict mode from `[tool.mypy]` in `pyproject.toml`. Pass no path.
- The steps are listed in `scripts/check.py`.

## Running tests

Run the full suite once before submitting, on 4 workers with `-n 4 --dist loadgroup` (`pytest-xdist`). While working, run the tests that cover the code you touched.

```bash
.venv/bin/pytest tests/services/test_stacks.py
.venv/bin/pytest tests/web/test_theme.py
.venv/bin/pytest tests/services/test_stacks.py -k remove
.venv/bin/pytest --last-failed tests/services
.venv/bin/pytest -m "not integration" tests/docker_engine
```

- Pass a file, a directory or a node id (`path::test_name`).
- `--last-failed` runs every selected test when no failure is recorded. Give it a path.
- Run the tests from the repository root. `tests/` is a package.
- `asyncio_mode` is `auto`; every test gets its own event loop.
- Each worker builds its own application and template databases; a test must not depend on another test's state or order.

## Isolation of tests that start an application

| Name | Defined in | Effect |
|---|---|---|
| `attach_app` | `tests/conftest.py` | Gives one test the application built once per process, with the test's settings and fresh state |
| `module_started_app`, `module_browsers`, `module_app_guard` | `tests/conftest.py` | One started application for every test in a module whose tests only read |
| `pytest.mark.xdist_group("<group-name>")` | Module `pytestmark` | Keeps every test of the group on one worker. Takes effect only with `--dist loadgroup` |
| `--reverse-order` | `tests/conftest.py` | Runs the collected tests in reverse order |

A module that uses a module-scoped application passes three runs before it is submitted:

```bash
.venv/bin/pytest tests/web/test_htmx_routing.py
.venv/bin/pytest -n 4 --dist loadgroup
.venv/bin/pytest --reverse-order tests/web/test_htmx_routing.py
```

| Run | Failure it shows |
|---|---|
| The module alone | The module depends on state another module sets up |
| The suite | The module interferes with other modules on the same worker |
| `--reverse-order` | A test depends on the test that runs before it |

- The shared application is held by one module-scoped fixture at a time. A test in a module that uses `module_started_app` cannot also request `attach_app`; the fixture asserts.
- With `--dist loadgroup`, a module marked `xdist_group` runs on one worker. Modules that share a group name run on the same worker.

## Test layout

The suite is sized at about 1.0 to 1.2 lines of test per line of code under test. The ratio is a diagnostic: a test is not added to raise it or removed to lower it.

| Path | Covers | Runtime |
|---|---|---|
| `tests/crypto`, `tests/compose_cli`, `tests/registry_client`, `tests/object_storage`, `tests/docker_engine` | One client or primitive against a fake the test starts: key handling, the Compose wrapper, registry and S3 clients, the Engine client | Fast |
| `tests/db` | Repositories and migrations against a temporary SQLite file | Fast |
| `tests/services` | Business logic and authorisation against a migrated database and fake Docker, Compose and registry back ends | Fast |
| `tests/auth`, `tests/activity`, `tests/backup`, `tests/notifications`, `tests/scheduler`, `tests/subsystems`, `tests/serving` | One subsystem inside the process | Fast to moderate |
| `tests/updater` | The `updater` helper program against a fake Engine | Fast to moderate |
| `tests/api`, `tests/web` | The application through the ASGI test client: routes, templates, headers, accessibility and behaviour without JavaScript | Slow: most tests start an application |
| Modules at the top of `tests/` | Source scans and structure guards: import graph, SQL containment, activity-event conformance, container files, configuration and documentation | Moderate |
| `tests/integration`, and tests marked `integration` | A real S3-compatible server, or the host's Docker socket | Skipped when the server is absent |

- Add the test next to the behaviour. A bug fix starts with a test that fails without it.
- A test earns its place only when removing it would make a realistic failure harder to catch.
- Name a test as a sentence: the unit, the scenario, the result.
- A test has one act. Arrange, act and assert are separate. No loops or conditionals in a test body.
- Parametrise tests that need no application or file system. Merge tests that start an application and share setup.
- Shared fixtures are in `tests/conftest.py` and in the `conftest.py` of each directory. Modules named `*_support.py` hold builders.
- The `integration` marker is declared in `pyproject.toml`.

## Where tests may reach

| Allowed | Not allowed |
|---|---|
| Temporary directories from `tmp_path` | The user's home, the repository tree, any other fixed path |
| Servers a test starts on loopback | The network beyond loopback, including DNS |
| The Docker daemon, Compose and registries as fakes in `tests/` | The host's Docker daemon, outside tests marked `integration` |
| A scripted Compose binary (`tests/scripted_compose.py`, `tests/compose_stub.sh`) | The real Compose binary |
| The wall clock through the fake clock fixtures | Sleeping to wait for something |

- A test that dials loopback uses the `permit_loopback` fixture, and a test that resolves a name uses `fake_hosts`. Both are defined in `tests/conftest.py`. The default address policy refuses loopback.
- A test marked `integration` skips itself when its server is missing.

## Live S3 integration test

`tests/integration/test_backup_sync_s3.py` runs `backup-syncer` against an S3-compatible server. It is skipped unless the five required variables are set.

| Variable | Read by | Effect |
|---|---|---|
| `DOCKMIN_TEST_S3_ENDPOINT` | `tests/integration/test_backup_sync_s3.py` | Required. Endpoint address for path-style addressing |
| `DOCKMIN_TEST_S3_REGION` | `tests/integration/test_backup_sync_s3.py` | Required. Region |
| `DOCKMIN_TEST_S3_BUCKET` | `tests/integration/test_backup_sync_s3.py` | Required. An existing bucket |
| `DOCKMIN_TEST_S3_ACCESS_KEY_ID` | `tests/integration/test_backup_sync_s3.py` | Required. Access key ID |
| `DOCKMIN_TEST_S3_SECRET_ACCESS_KEY` | `tests/integration/test_backup_sync_s3.py` | Required. Secret access key |
| `DOCKMIN_TEST_S3_VIRTUAL_HOST_ENDPOINT` | `tests/integration/test_backup_sync_s3.py` | Optional. Endpoint address for virtual-host addressing. Adds the virtual-host case; without it that case is skipped |

- The credential needs Read, Write, List and Delete on the bucket.
- The test writes under a random prefix `dockmin-test-<12 hex digits>` and removes what it stored afterwards.

Run it by path, with the variables set in the shell:

```bash
.venv/bin/pytest tests/integration/test_backup_sync_s3.py
```

## Code and documentation standards

| Subject | Page |
|---|---|
| Comments and docstrings | [Code standards](https://github.com/Sphexi/DockMin/wiki/Contributing-Code-Standards) |
| Templates, partials, HTMX, CSS and JavaScript | [Frontend conventions](https://github.com/Sphexi/DockMin/wiki/Contributing-Frontend-Conventions) |

- Python 3.12 or later. Type hints on every signature, including returns. `str | None`, not `Optional[str]`.
- Blocking work never runs on the event loop. Every Docker call, HTTP call and subprocess is async.
- `pathlib.Path` for paths. `encoding="utf-8"` on every text file read and write.
- A module in `app/` is at most 2000 lines; `tests/test_module_size.py` checks it.
- SQL appears only in `app/db/repositories/`, `app/db/migrations/` and `app/db/engine.py`. Services call repositories.
- A service function takes the caller's identity and enforces authorisation itself. A router never queries the database.
- Every function that changes state records an activity event or is listed as exempt; `tests/test_activity_conformance.py` fails otherwise.
- The documentation is the project wiki. Each page is one Markdown file in `docs/`, named `Section-Page.md`. Pages are reference material: facts, tables and lists, with no introductory prose.
- A push to `main` publishes `docs/` to the wiki, with `CHANGELOG.md` as the Changelog page and `CONTRIBUTING.md` as the Contributing page (`.github/workflows/wiki.yml`). The wiki is never edited directly; the next publish overwrites it.
- A link between wiki pages is `[Text](Page-Name)` or `[Text](Page-Name#anchor)`, with no `.md` extension. A link to a repository file is an absolute `https://github.com/Sphexi/DockMin/blob/main/<path>` URL.
- A command in a document is a line in a `bash` block with placeholders written `<lower-kebab>`. Configuration goes in `yaml`, `json` or `ini` blocks and output in `text` blocks.
- `tests/test_documentation.py` checks every link and anchor in the documents.
- A `DOCKMIN_` variable named in a document appears in [Reference configuration](https://github.com/Sphexi/DockMin/wiki/Reference-Configuration). `tests/test_configuration_doc.py` compares that page with the settings model.

## Commit messages

- One subject line in the imperative mood: `Add`, `Fix`, `Remove`, not `Added` or `Adds`.
- The body, when there is one, says what changed in the code. No rationale, no history, no names.
- One logical change per commit.

## Releasing

Pushing a tag of the form `vX.Y.Z` publishes the image `ghcr.io/<owner>/<repository>:X.Y.Z` and `:latest` for
`linux/amd64` and `linux/arm64` (`.github/workflows/release.yml`). The workflow runs no tests.

| Item | Detail |
|---|---|
| Version | Taken from the tag at build time; `pyproject.toml` carries none |
| Revision | The release workflow passes the tagged commit as the `DOCKMIN_REVISION` build argument |
| Updater image | `ghcr.io/<owner>/<repository>-updater:X.Y.Z` and `:latest`, built from `Dockerfile.updater` for the same platforms. The workflow runs it once on each platform and fails unless it exits with status 2 |
| Local builds | Version `0+local`; set `DOCKMIN_VERSION` (environment for `uv build`, `--build-arg` for `docker build`) to override. `DOCKMIN_REVISION` is empty unless passed with `--build-arg`, and `application_started` then logs `revision` as `null` |
| Before tagging | All four checks pass on Python 3.12, the image's runtime: `uv sync --python 3.12`, then `.venv/bin/python scripts/check.py` |
| Changelog | `CHANGELOG.md` has the dated `X.Y.Z` entry |

## Licence

Contributions are accepted under the MIT licence in [LICENSE](https://github.com/Sphexi/DockMin/blob/main/LICENSE).
