# JSON API

## Routes

| Method and path | Role | Fields |
|---|---|---|
| `GET /api/health` | None | [Unauthenticated routes](#unauthenticated-routes) |
| `GET /api/ready` | None | [Unauthenticated routes](#unauthenticated-routes) |
| `GET /api/settings/public` | None | [Unauthenticated routes](#unauthenticated-routes) |
| `GET /api/v1/environments` | Any user | [Environments](Usage-Environments#fields) |
| `GET /api/v1/environments/{id}` | Any user | [Environments](Usage-Environments#fields) |
| `GET /api/v1/environments/{id}/containers` | Any user | [Containers](Usage-Containers#container-fields) |
| `GET /api/v1/environments/{id}/containers/{container_id}` | Any user | [Containers](Usage-Containers#container-fields) |
| `POST /api/v1/environments/{id}/containers/{container_id}/start` | Any user | [Containers](Usage-Containers#action-fields) |
| `POST /api/v1/environments/{id}/containers/{container_id}/stop` | Any user | [Containers](Usage-Containers#action-fields) |
| `POST /api/v1/environments/{id}/containers/{container_id}/restart` | Any user | [Containers](Usage-Containers#action-fields) |
| `POST /api/v1/environments/{id}/containers/{container_id}/recreate` | Any user | [Containers](Usage-Containers#recreate-request) |
| `GET /api/v1/environments/{id}/stacks` | Any user | [Stacks](Usage-Stacks#stack-fields) |
| `GET /api/v1/environments/{id}/stacks/{name}` | Any user | [Stacks](Usage-Stacks#stack-fields) |
| `POST /api/v1/environments/{id}/stacks` | Any user | [Stacks](Usage-Stacks#create-request) |
| `POST /api/v1/environments/{id}/stacks/{name}/redeploy` | Any user | [Stacks](Usage-Stacks#redeploy-request) |
| `POST /api/v1/environments/{id}/stacks/{name}/start` | Any user | [Stacks](Usage-Stacks#action-object) |
| `POST /api/v1/environments/{id}/stacks/{name}/stop` | Any user | [Stacks](Usage-Stacks#action-object) |
| `POST /api/v1/environments/{id}/stacks/{name}/restart` | Any user | [Stacks](Usage-Stacks#restart-request) |
| `PUT /api/v1/environments/{id}/stacks/{name}/files/{file}` | Any user | [Stacks](Usage-Stacks#file-requests) |
| `DELETE /api/v1/environments/{id}/stacks/{name}/files/{file}` | Any user | [Stacks](Usage-Stacks#file-requests) |
| `GET /api/v1/jobs` | Administrator | [Jobs](Administration-Jobs#job-fields) |
| `GET /api/v1/jobs/{id}` | Administrator | [Jobs](Administration-Jobs#job-fields) |
| `GET /api/v1/jobs/{id}/runs` | Administrator | [Jobs](Administration-Jobs#run-fields) |
| `GET /api/v1/jobs/-/runs/{run_id}` | Administrator | [Jobs](Administration-Jobs#run-fields) |
| `GET /api/v1/rules` | Any user | [Automation rules](#automation-rules) |
| `GET /api/v1/rules/{id}` | Any user | [Rule fields](#rule-fields) |
| `GET /api/v1/rules/{id}/runs` | Any user | [Jobs](Administration-Jobs#run-fields) |
| `GET /api/v1/rules/{id}/runs/{run_id}` | Any user | [Jobs](Administration-Jobs#run-fields) |
| `POST /api/v1/rules/{id}/pause` | Any user | [Rule requests](#rule-requests) |
| `POST /api/v1/rules/{id}/resume` | Any user | [Rule requests](#rule-requests) |
| `POST /api/v1/rules/{id}/disable` | Any user | [Rule requests](#rule-requests) |
| `POST /api/v1/rules/{id}/run` | Any user | [Rule requests](#rule-requests) |
| `GET /api/v1/activity` | Administrator | [Activity](Administration-Activity#json-api) |
| `POST /api/v1/hooks/{token}` | Webhook token | [Webhook calls](#webhook-calls) |
| `POST /api/v1/hooks` | Webhook token | [Webhook calls](#webhook-calls) |

| Role | Meaning |
|---|---|
| None | No credential. The public settings route is rate-limited per address when the limit is enforced; health and readiness are not ([Rate limit](Reference-Deployment#rate-limit)) |
| Any user | A valid token. The environment gate and resource controls decide what the token's user sees and changes ([Access control](Usage-Access-Control)) |
| Administrator | A valid token of an administrator. Another role answers `403` |
| Webhook token | The webhook's own token, in the path or as the bearer credential. An API token is not accepted |

## Authentication

| Item | Rule |
|---|---|
| Header | `Authorization: Bearer <api-token>` |
| Create a token | The signed-in user, at `/account/tokens`, for themselves. The value is shown once ([Teams and users](Usage-Teams-and-Users)) |
| Acts as | The token's user, with that user's role and access |
| Checked | On every request, from the database. A revocation, an expiry, a disabled account and a changed role apply to the next request |
| Revoked by | The user or an administrator revoking it; a password change or reset; ending all of a user's sessions ([Teams and users](Usage-Teams-and-Users)) |
| Session cookie alone | `401` with `WWW-Authenticate: Bearer` on every `/api/v1` route except webhook calls |
| Header present, token empty or wrong | `401` (`404` on webhook calls). The request is authenticated by the token alone; cookies are not consulted and CSRF does not apply |
| Header with another scheme | Not a bearer request: treated as a request with no token |
| Header outside `/api` | Ignored |
| Access platform token | The `/api` routes never read the access platform's header. A JWT sent as `Authorization: Bearer` is an invalid token: `401`. Tokens work as without the feature ([Teams and users](Usage-Teams-and-Users)) |
| WebSocket | A token cannot open one |

```bash
curl -H "Authorization: Bearer <api-token>" "http://127.0.0.1:8000/api/v1/environments?limit=5"
```

The address and port are those the deployment publishes ([Deployment](Reference-Deployment)). With the shipped `compose.yaml` the plain listener is published on `127.0.0.1:8000`.

## Request id

| Item | Rule |
|---|---|
| Response header | `X-Request-Id` on every HTTP response |
| Request header | An `X-Request-Id` of 1 to 64 letters, digits, `.`, `_` or `-` is reused. Any other value is replaced by a generated 32-digit hexadecimal id |
| Where it appears | The `request_id` of log lines and activity events of the request ([Activity](Administration-Activity#request-id)) |

## Pagination

Every list route takes the parameters below and answers `{"items": [...], "next_cursor": string or null}`. `next_cursor` is `null` on the last page.

| Parameter | Rule |
|---|---|
| `limit` | 1 to 500. Default 25 |
| `cursor` | The `next_cursor` of the previous page. At most 512 characters. An invalid value answers `400` |

| List | Order | Also in the response | Further parameters |
|---|---|---|---|
| Environments | Name, then id | | [Environments](Usage-Environments#list-parameters) |
| Containers of an environment | Name (case-insensitive), then id | `total`: every match | [Containers](Usage-Containers#list-parameters) |
| Stacks of an environment | Name | | [Stacks](Usage-Stacks#list-parameters) |
| Jobs | Ascending id | | [Jobs](Administration-Jobs#list-parameters) |
| Runs of a job | Newest first | | [Jobs](Administration-Jobs#list-parameters) |
| Rules | Ascending id | | [Rule list parameters](#rule-list-parameters) |
| Runs of a rule | Newest first | | |
| Activity | Newest first | | [Activity](Administration-Activity#json-api) |

- A cursor is opaque. Do not construct one.

## Automation rules

An automation rule is one automation ([Automation](Operations-Automation)). The routes read a rule and its runs, pause, resume and disable it, and start a run.

| Item | Rule |
|---|---|
| Who | The token's user for a rule they own. An administrator for every rule. A rule of another user answers `404` for a standard user |
| Not available here | Create, update, delete, enable, release of a held container, and rotation and revocation of a webhook URL. These are web pages ([Not exposed](#not-exposed)) |

### Rule list parameters

| Parameter | Rule |
|---|---|
| `limit`, `cursor` | As in [Pagination](#pagination) |
| `subsystem` | A subsystem name; at most 64 characters. An unknown name answers `422` |
| `environment_id` | An environment id, 1 to 9223372036854775807 |

### Rule fields

| Field | Type | Notes |
|---|---|---|
| `id` | integer | |
| `name` | string | |
| `subsystem` | string | |
| `environment_id` | integer or null | `null` for a rule that acts on this instance |
| `environment_name` | string or null | |
| `selector` | object or null | The stored target, parsed. `null` for a rule with no target |
| `target` | string | The target in words |
| `parameters` | object | The rule's settings |
| `enabled` | boolean | |
| `paused_until` | string or null | UTC, `YYYY-MM-DDTHH:MM:SSZ`. The pause in force |
| `state` | string | As the job `state` ([Jobs](Administration-Jobs#states)); `on_call` for a webhook rule with no call waiting |
| `schedule` | object | `{kind, expression, time_zone, description}`. A webhook rule has `kind` `webhook`, an empty `expression` and the description `When called` |
| `next_run_at` | string or null | For a webhook rule, set while a call is waiting |
| `last_run` | object or null | The newest run that was not a dry run, as a [run object](Administration-Jobs#run-fields) |
| `supports_dry_run` | boolean | |
| `subsystem_enabled` | boolean | |
| `available` | boolean | |
| `owner` | object or null | `{id, username}`. Present for an administrator's token only; `null` otherwise, and for a rule with no owner |
| `created_at`, `updated_at` | string | UTC |
| `digest` | string | SHA-256 of the target, the settings and the schedule. A real run names it as `expected_digest` |
| `webhook` | object or null | A `webhook-receiver` rule's URL, as in [Webhook object](#webhook-object). `null` for every other rule |

| Route | Returns |
|---|---|
| `GET /api/v1/rules` | A page of rules by ascending id |
| `GET /api/v1/rules/{id}` | One rule |
| `GET /api/v1/rules/{id}/runs` | A page of the rule's runs, newest first. `limit` and `cursor` only |
| `GET /api/v1/rules/{id}/runs/{run_id}` | One run of the rule. A run of another job answers `404` |

### Webhook object

| Field | Type | Notes |
|---|---|---|
| `prefix` | string | The 8 characters after `dkwh_` in the token |
| `state` | string | `active`, `revoked` or `revoked_by_recovery` |
| `issued_at` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ`. When the current token was issued |
| `last_called_at` | string or null | UTC. The last accepted call. `null` when never called |

- No field holds the token or its hash. The token is shown once, on the page that creates or rotates it ([Automation](Operations-Automation)).
- The caller's address and the waiting call count are shown on the automation page only.

### Rule requests

| Route | Body | Answer |
|---|---|---|
| `POST /api/v1/rules/{id}/pause` | `{"duration": "1h"}`; `duration` is `1h`, `4h`, `24h` or `7d` | `200` and the rule |
| `POST /api/v1/rules/{id}/resume` | None | `200` and the rule. Needs the access that enabling needs |
| `POST /api/v1/rules/{id}/disable` | None | `200` and the rule. Nothing is deleted |
| `POST /api/v1/rules/{id}/run` | `{"dry_run": bool, "expected_digest": string}` | `202` and `{"run_id": integer}` |

- A body carries no other key. An unknown key answers `422`.
- `expected_digest` is 64 lower-case hexadecimal characters. A real run (`dry_run` absent or `false`) without it answers `422`: `A real run needs expected_digest.` A dry run needs none and may be asked of a disabled rule.
- A real run starts only if the rule's digest, read when the run is claimed, equals `expected_digest`. Otherwise `409`, reason `changed_since_review`, and nothing runs. Read `digest` from the rule, then post it.
- A real run needs the same write access to the target as `Run now` ([Automation](Operations-Automation)); without it `403`.

| Status | Cause |
|---|---|
| 200, 202 | Done, or the run started |
| 400 | Invalid cursor |
| 401 | Missing or invalid token |
| 403 | The token's user lacks the access a resume or a real run needs |
| 404 | No such rule or run, or a rule the token's user may not see |
| 409 | A run was refused. `error.reason` is `running`, `auto_disabled`, `rule_disabled`, `subsystem_disabled`, `unavailable`, `dry_run_unsupported`, `busy` (no run slot is free, the owner already has 2 runs, or the scheduler is stopping) or `changed_since_review` |
| 422 | A body or parameter outside its bounds, an unknown `subsystem`, an unknown body key, or a real run without `expected_digest` |
| 503 | The host could not be read for the access check. `error.reason` names the failure |

## Errors

An error under `/api` is JSON, whatever its cause.

```json
{"error": {"status": 422, "message": "Unprocessable Entity", "fields": [{"loc": ["query", "limit"], "msg": "Input should be less than or equal to 500"}]}}
```

| Key | Content |
|---|---|
| `error.status` | The HTTP status |
| `error.message` | The status phrase, or the product's text for a domain failure ([Containers](Usage-Containers#responses), [Stacks](Usage-Stacks#responses)) |
| `error.fields` | On `400` and `422` for a parameter or body: a list of `{loc, msg}`. The submitted value is never echoed |
| `error.reason`, `error.holder` | On some domain failures: a machine-readable reason, and the holder of a busy lock |
| `error.candidates`, `error.blockers`, `error.objects` | On some container and stack failures: the credential sets to choose between, the reasons a container cannot be recreated, the objects already present |
| `error.exit_code`, `error.events`, `error.progress`, `error.stderr`, `error.installed`, `error.required`, `error.stack_created`, `error.stack` | On stack operation failures ([Stacks](Usage-Stacks#responses)) |

| Status | Cause |
|---|---|
| 400 | Invalid cursor or parameter value, or a stack the checks refuse, such as `port_range_too_large` |
| 401 | Missing or invalid token. `WWW-Authenticate: Bearer` |
| 403 | The role or access level is not enough |
| 404 | The object does not exist, or the token's user may not see it. The two are indistinguishable |
| 409 | Conflict, or the object is busy. A domain conflict carries `error.reason`, such as `changed_since_review`, `container_name_in_use`, `port_in_use`, `file_in_use`, `file_missing`, `not_running`, `not_deployed`, `object_busy` or `self_protected` ([Stacks](Usage-Stacks#responses)) |
| 413 | A request body over 256 KiB |
| 422 | A parameter or field outside its bounds |
| 429 | An address's anonymous bucket is empty (`Retry-After` in seconds), a webhook's call limit is reached (`Retry-After`), or the stack operation limit is reached (`error.reason` `too_many_operations`) |
| 502, 503 | The Docker host rejected or could not answer the request; the instance is offline for a restore; the concurrency limit is reached (`Retry-After: 1`) |

## Unauthenticated routes

| Path | Response |
|---|---|
| `/api/health` | `200` `{"status": "ok"}` while the process serves requests |
| `/api/ready` | `200` `{"status": "ready", "checks": {"data_dir": "up", "database": "up"}}`. `503` `{"status": "not_ready", ...}` when a check is `down` |
| `/api/settings/public` | `200` `{"authentication_methods": ["internal"], "instance_name": string}`. While access platform sign-in is on, `authentication_methods` is `["internal", "jwt"]` |

- `/api/health` and `/api/ready` answer before the first administrator exists and are not rate-limited. `/api/ready` counts toward the concurrency limit; `/api/health` does not ([Concurrency](Reference-Deployment#concurrency)).
- `checks` holds `data_dir`, and `database` once the database has opened at start. A check that raises is reported `down` and logged as `readiness_check_failed` with `check`.
- `/api/settings/public` carries those two fields and no others. `instance_name` is `DOCKMIN_INSTANCE_NAME` ([Configuration](Reference-Configuration#accounts-and-sessions)).
- During a restore the gate refuses these routes in some phases ([Backups](Administration-Backups)).

## Not exposed

| Not exposed | Detail |
|---|---|
| The Docker Engine API | No route proxies it. The browser and API clients never reach a Docker host directly |
| Mutations other than those listed | Containers: `start`, `stop`, `restart`, `recreate`. Stacks: create, `redeploy`, `start`, `stop`, `restart`, and put and delete of one companion file. Rules: `pause`, `resume`, `disable`, `run`. Everything else is read-only |
| Container `kill`, `pause`, `unpause`, `remove`, create, duplicate, logs, terminals, attach | Web pages only ([Containers](Usage-Containers#json-api)) |
| Stack check, save, remove, forget, adopt, read from host, Swarm stacks | Web pages only. A Swarm stack answers `404` on the stack routes ([Stacks](Usage-Stacks#json-api)) |
| Images, networks, volumes, Swarm services, nodes, configs and secrets | No JSON route. Changing the credential set of an image binding without a pull is a web page ([Images](Usage-Images#rebind-without-a-pull)) |
| Registries, object storage targets, backups, restore | No JSON route. The `Authorization` header is read only under `/api`, so a request to these pages with a token carries no credential and is redirected to the sign-in page ([Backups](Administration-Backups)) |
| Users, teams, groups, tags, grants, access controls, API tokens | No JSON route. Tokens are created only in the browser |
| Automation rule create, update, delete and enable; release of a held container; rotation and revocation of a webhook URL | Web pages only ([Automation](Operations-Automation)) |
| Jobs | Read-only ([Jobs](Administration-Jobs#json-api)) |
| Terminals, log streams, stats streams, progress streams | Browser only: WebSocket and server-sent events. A token cannot open a WebSocket |
| Secrets and TLS material | No response holds a secret value, the content of a secret file, a private key or certificate text |
| Unauthenticated routes | Under `/api`, only the health, readiness and public settings routes |
| API documentation routes | `/docs`, `/redoc` and `/openapi.json` answer `404` |

## Webhook calls

| Item | Rule |
|---|---|
| Token form | `dkwh_<8 characters>_<43 characters>`, 57 characters, shown once when the webhook is created or its token is rotated. Only a hash is stored |
| Path form | `POST /api/v1/hooks/{token}` |
| Header form | `POST /api/v1/hooks` with `Authorization: Bearer <token>` |
| Request | No body is read; a body over 256 KiB answers `413`. A query string answers `400` |
| CSRF and cookies | Not required; no cookie is read or set |
| Transport | With TLS enabled, a call that did not arrive over HTTPS answers `400` |
| Rate | Six calls, refilled one every 10 seconds, per webhook. Runs start at least 60 seconds apart. The anonymous per-address limit applies as well ([Rate limit](Reference-Deployment#rate-limit)) |

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status": "accepted"}` | The call armed the webhook's run |
| `202` | `{"status": "already_pending"}` | A call was already waiting; this one joined it |
| `400` | `reason` `use_https` or `query_not_supported` | Refused before the token is looked up |
| `404` | Same as an unknown `/api` path | Malformed, unknown, wrong, revoked or rotated-out token; an API token; deleted webhook; disabled or missing owner |
| `409` | `reason` `disabled`, `auto_disabled`, `paused`, `subsystem_disabled` or `unavailable` | A valid token on a webhook that is not accepting calls |
| `429` | `Retry-After` | The webhook's call limit, or the per-address limit |

Deployment behind a proxy: [Webhook calls behind a reverse proxy](Reference-Deployment#webhook-calls-behind-a-reverse-proxy). Full behaviour of a call, the limits and the run it starts: [Automation](Operations-Automation).
