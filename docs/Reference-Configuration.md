# Configuration

dockmin reads its configuration from environment variables prefixed `DOCKMIN_`. A `DOCKMIN_` variable that no table below lists is ignored. Configuration files are not read.

A blank value counts as unset for `DOCKMIN_SECRET_KEY`, `DOCKMIN_SECRET_KEY_FILE`, `DOCKMIN_PREVIOUS_SECRET_KEY`, `DOCKMIN_PREVIOUS_SECRET_KEY_FILE`, `DOCKMIN_COMPOSE_BINARY`, `DOCKMIN_BUILDX_BINARY`, `DOCKMIN_ACTIVITY_RETENTION_DAYS`, `DOCKMIN_TLS_CERT_FILE`, `DOCKMIN_TLS_KEY_FILE`, `DOCKMIN_HTTPS_PUBLIC_PORT`, `DOCKMIN_HTTP_MODE`, `DOCKMIN_JWT_HEADER`, `DOCKMIN_JWT_ISSUER`, `DOCKMIN_JWT_AUDIENCE`, `DOCKMIN_JWT_JWKS_URL`, `DOCKMIN_JWT_KEY_FILE` and `DOCKMIN_JWT_USERNAME_CLAIM`. A blank `DOCKMIN_HTTPS_PORT`, `DOCKMIN_SECRET_KEY_DIR`, `DOCKMIN_MAX_CONCURRENT_REQUESTS`, `DOCKMIN_JWT_ALGORITHMS`, `DOCKMIN_JWT_IDENTITY_CLAIM`, `DOCKMIN_JWT_MAX_AGE` or `DOCKMIN_JWT_PASSWORD_SIGN_IN` is its default, a blank `DOCKMIN_COOKIE_SECURE` or `DOCKMIN_TLS_SELF_SIGNED` is `true`, and a blank `DOCKMIN_ALLOW_KEY_ROTATION`, `DOCKMIN_DEVELOPMENT_CHECKS`, `DOCKMIN_ACCESS_LOG` or `DOCKMIN_JWT_CREATE_USERS` is `false`. A blank list variable is an empty list.

| Value type | Accepted form |
|---|---|
| string | Text, as given |
| integer | Whole number |
| boolean | `true`, `false`, `1`, `0`, `yes`, `no`, `on`, `off`, `t`, `f`, `y`, `n`, in any case |
| path | A file or directory path as the dockmin process sees it. In the image, a path inside the container |
| duration (seconds) | A whole number of seconds. An ISO 8601 duration such as `PT12H` is also accepted |
| list (comma-separated) | Entries separated by commas. Blanks around an entry are ignored |
| secret | Text the process keeps out of logs and error messages |

A value that fails validation stops the start with exit status `2` ([Serving](Reference-Deployment#serving)). The log line names the variable and never the value.

## Listeners and network

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_HOST` | string | `0.0.0.0` | — | Address both listeners bind |
| `DOCKMIN_PORT` | integer | `8000` | 1–65535 | Port of the plain HTTP listener ([HTTP modes](Reference-Deployment#http-modes)) |
| `DOCKMIN_TLS_CERT_FILE` | path | unset | — | PEM certificate file, leaf first, chain after. Set together with `DOCKMIN_TLS_KEY_FILE` or not at all ([Certificate](Administration-Certificate)) |
| `DOCKMIN_TLS_KEY_FILE` | path | unset | — | Unencrypted PEM private key file for the certificate |
| `DOCKMIN_TLS_SELF_SIGNED` | boolean | `true` | — | When neither `DOCKMIN_TLS_CERT_FILE` nor `DOCKMIN_TLS_KEY_FILE` is set: `true` generates a self-signed certificate under `DOCKMIN_DATA_DIR` and serves HTTPS; `false` serves no HTTPS. A value that is not a boolean ends the start with `configuration_invalid`, status `2`. Ignored when both files are set. Set `false` for a deployment where a reverse proxy reaches the plain listener ([Certificate](Administration-Certificate)) |
| `DOCKMIN_HTTPS_PORT` | integer | `8443` | 1–65535 | Port of the HTTPS listener. Must differ from `DOCKMIN_PORT` when TLS is configured: files set, or `DOCKMIN_TLS_SELF_SIGNED` `true` |
| `DOCKMIN_HTTPS_PUBLIC_PORT` | integer | unset | 1–65535 | Port a redirect names in `Location`. Unset: `DOCKMIN_HTTPS_PORT`. `443` is omitted from the URL |
| `DOCKMIN_HTTP_MODE` | one of `serve`, `redirect`, `off` | unset | — | What the plain listener does. Unset: `redirect` with TLS (supplied files or a self-signed certificate), `serve` without. `redirect` and `off` are refused without TLS, that is, with `DOCKMIN_TLS_SELF_SIGNED` `false` and no files. Case and surrounding blanks are ignored ([HTTP modes](Reference-Deployment#http-modes)) |
| `DOCKMIN_MAX_CONCURRENT_REQUESTS` | integer | `256` | 8–4096 | Requests and WebSocket sessions in flight across both listeners. Above it HTTP requests receive `503` ([Concurrency](Reference-Deployment#concurrency)) |
| `DOCKMIN_TRUSTED_PROXIES` | list (comma-separated) | empty | — | Addresses, networks or `*` whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed. Empty ignores both headers from every peer, loopback included ([Behind a reverse proxy](Reference-Deployment#behind-a-reverse-proxy)) |
| `DOCKMIN_TRUSTED_ORIGINS` | list (comma-separated) | empty | — | Public origins such as `https://dockmin.example.com`. Each is `http` or `https`, a host and an optional port. An entry with a path, query, fragment or user name is refused ([Terminals and streams](Reference-Deployment#terminals-and-streams-behind-a-reverse-proxy)) |
| `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` | list (comma-separated) | empty | — | Addresses or networks that operator-named endpoints may use although they are internal. A bare address is a single-address network. An entry that is neither is a configuration error ([Address policy](Reference-Security#address-policy)) |

## Storage

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_DATA_DIR` | path | `/data` | — | Directory holding the database, the instance lock, stack files and backup work directories ([Data directory](Reference-Deployment#data-directory)) |
| `DOCKMIN_DB_BUSY_TIMEOUT_MS` | integer | `5000` | 0–600000 | Milliseconds a statement waits for the database write lock before failing |
| `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS` | integer | `60` | 15–3600 | Seconds between refreshes of each environment's status and object counts. A refresh starts when Unix time is a whole multiple of the value ([Jobs](Administration-Jobs#system-tasks)). A snapshot older than three times the interval is marked `Stale`. A changed value applies at the next start ([Environments](Usage-Environments#snapshot-cache)) |
| `DOCKMIN_ACTIVITY_RETENTION_DAYS` | integer | unset | 7–3650 | Age in days after which activity events are removed. Unset keeps every event ([Activity](Administration-Activity#retention)) |

- `compose.yaml` passes `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS` and `DOCKMIN_ACTIVITY_RETENTION_DAYS` through from `.env`; `.env.example` sets the snapshot default.
- The snapshot interval is the schedule of the refresher's job on the Jobs page ([Jobs](Administration-Jobs)).
- A changed `DOCKMIN_ACTIVITY_RETENTION_DAYS` takes effect at the next start.

## Encryption key

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_SECRET_KEY` | secret | unset | — | Base64 of 32 random bytes. At most one of this and `DOCKMIN_SECRET_KEY_FILE` may be set; both is refused. With neither set, the key is generated in `DOCKMIN_SECRET_KEY_DIR`. The environment is visible to anyone who can inspect the container; prefer the file |
| `DOCKMIN_SECRET_KEY_FILE` | path | unset | — | File holding the base64 key. Whitespace around the value is ignored. `compose.yaml` mounts a Docker secret at `/run/secrets/dockmin_secret_key` |
| `DOCKMIN_SECRET_KEY_DIR` | path | `/key` | — | Directory holding `secret.key`, the generated key, when neither `DOCKMIN_SECRET_KEY` nor `DOCKMIN_SECRET_KEY_FILE` is set. Ignored otherwise. The file is created only for a new database while neither `DOCKMIN_PREVIOUS_SECRET_KEY` nor `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is set, and an existing file is never replaced. Must be an absolute path and must not equal, contain or lie inside `DOCKMIN_DATA_DIR`. To create the key, it must be a mounted volume that is not a `tmpfs`, owned by the process user and not writable by its group or others ([Encryption](Administration-Encryption#key-directory)) |
| `DOCKMIN_PREVIOUS_SECRET_KEY` | secret | unset | — | Base64 of the key that reads values at the previous key version, supplied while changing keys. At most one of this and `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` may be set; both is refused. Must differ from the current key. Visible to anyone who can inspect the container while it is set; prefer `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` ([Re-key](Administration-Encryption#re-key)) |
| `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` | path | unset | — | File holding the base64 previous key, read and decoded as `DOCKMIN_SECRET_KEY_FILE` is. At most one of it and `DOCKMIN_PREVIOUS_SECRET_KEY` may be set. Must differ from the current key ([Re-key](Administration-Encryption#re-key)) |
| `DOCKMIN_ALLOW_KEY_ROTATION` | boolean | `false` | — | When `true`, a start whose canary opens only with the previous key, supplied as `DOCKMIN_PREVIOUS_SECRET_KEY` or `DOCKMIN_PREVIOUS_SECRET_KEY_FILE`, re-seals the canary under the current key at the next key version. Stored values stay at the old version until they are re-encrypted ([Re-key](Administration-Encryption#re-key)). Without it that start is refused |

## Accounts and sessions

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_INSTANCE_NAME` | string | `dockmin` | 1–64 characters | Name returned as `instance_name` by `GET /api/settings/public`, written as `instance_name` on every container and service log record and on `application_started` |
| `DOCKMIN_SESSION_LIFETIME` | duration (seconds) | `43200` | > 0 | Time from sign-in after which a session ends |
| `DOCKMIN_SESSION_IDLE_TIMEOUT` | duration (seconds) | `3600` | > 0 | Time without a request after which a session ends. Activity is recorded at most once a minute |
| `DOCKMIN_COOKIE_SECURE` | boolean | `true` | — | Whether the session, CSRF and notice cookies carry the `Secure` attribute and the `__Host-` prefix. Set `false` only for plain HTTP during development ([Cookies](Reference-Security#cookies)) |
| `DOCKMIN_LOGIN_MAX_FAILURES` | integer | `5` | 1–1000 | Failed sign-ins within the window that lock a username ([Sign-in lock](Usage-Teams-and-Users#sign-in-lock)) |
| `DOCKMIN_LOGIN_LOCKOUT_WINDOW` | duration (seconds) | `900` | > 0 | Window in which failures are counted, and the time a lock lasts |
| `DOCKMIN_PASSWORD_MIN_LENGTH` | integer | `12` | 8–128 | Shortest accepted password |
| `DOCKMIN_PASSWORD_REQUIRED_CLASSES` | integer | `0` | 0–4 | Number of character classes a password must span, from lower case, upper case, digits and symbols. `0` requires none |
| `DOCKMIN_ARGON2_MEMORY_KIB` | integer | `65536` | 8192–4194304 | Argon2id memory cost in KiB |
| `DOCKMIN_ARGON2_ITERATIONS` | integer | `3` | 1–100 | Argon2id time cost |
| `DOCKMIN_ARGON2_LANES` | integer | `4` | 1–64 | Argon2id parallelism |
| `DOCKMIN_ARGON2_SALT_BYTES` | integer | `16` | 16–64 | Salt length of new password hashes |
| `DOCKMIN_ARGON2_HASH_BYTES` | integer | `32` | 16–64 | Output length of new password hashes |

- A stored hash weaker than the Argon2id settings is replaced at the user's next sign-in. A stronger one is kept and logged as `stored_hash_stronger_than_configured`.

## Access platform

Sign-in from a trusted access platform's token. Off while `DOCKMIN_JWT_HEADER` is unset ([Teams and users](Usage-Teams-and-Users)).

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_JWT_HEADER` | string | unset | — | Name of the request header that carries the token: 1 to 64 letters, digits and hyphens. Setting it turns the feature on. Requires `DOCKMIN_JWT_ISSUER`, `DOCKMIN_JWT_AUDIENCE` and exactly one of `DOCKMIN_JWT_JWKS_URL` and `DOCKMIN_JWT_KEY_FILE` |
| `DOCKMIN_JWT_ISSUER` | string | unset | — | Exact value of the token's `iss` claim. Text without control characters. Set only with `DOCKMIN_JWT_HEADER` |
| `DOCKMIN_JWT_AUDIENCE` | string | unset | — | Value the token's `aud` claim must be or contain. Text without control characters. Set only with `DOCKMIN_JWT_HEADER` |
| `DOCKMIN_JWT_JWKS_URL` | string | unset | — | `https:` URL of a JWKS document, at most 2,048 characters, without credentials or a fragment. Fetched under the address policy ([Address policy](Reference-Security#address-policy)). Not combined with `DOCKMIN_JWT_KEY_FILE` |
| `DOCKMIN_JWT_KEY_FILE` | path | unset | — | PEM public key or JWKS document, read once at start. An unreadable file, or one with no usable key, stops the start. Not combined with `DOCKMIN_JWT_JWKS_URL` |
| `DOCKMIN_JWT_ALGORITHMS` | list (comma-separated) | `ES256` | — | Accepted signature algorithms, from `ES256`, `ES384`, `ES512`, `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `EdDSA`. The default is `ES256` alone. Any other entry stops the start. `none` and `HS*` are never accepted. Repeated entries count once |
| `DOCKMIN_JWT_IDENTITY_CLAIM` | string | `sub` | — | Claim that holds the identity. Dotted names reach a nested claim, as `user.email`: up to 8 names of 1 to 64 letters, digits, `_`, `:`, `/`, `@` or `-` |
| `DOCKMIN_JWT_USERNAME_CLAIM` | string | unset | — | Claim that names a just-in-time account, in the form of the identity claim. Unset: the identity claim |
| `DOCKMIN_JWT_CREATE_USERS` | boolean | `false` | — | `true` creates a standard user for a verified identity that no account is linked to. Set only with `DOCKMIN_JWT_HEADER` ([Teams and users](Usage-Teams-and-Users)) |
| `DOCKMIN_JWT_MAX_AGE` | duration (seconds) | `86400` | > 0 | Time after the token's `iat` past which it is refused |
| `DOCKMIN_JWT_PASSWORD_SIGN_IN` | one of `all`, `administrators` | `all` | — | `administrators` refuses password sign-in for standard users while the feature is on. Administrators always sign in with a password ([Teams and users](Usage-Teams-and-Users)) |

- A configuration that breaks the rules in the Effect column stops the start with exit status `2` and the check `jwt_settings` ([Troubleshooting](Troubleshooting)).

## Docker Compose

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_COMPOSE_BINARY` | path | unset | — | Absolute path of the Compose binary. Unset: the search in [Binary location](Reference-Deployment#binary-location) |
| `DOCKMIN_COMPOSE_TIMEOUT_SECONDS` | integer | `600` | 60–3600 | Time limit of one stack deploy, redeploy, start, stop or remove, covering its image pulls and its Compose command |
| `DOCKMIN_BUILDX_BINARY` | path | unset | — | Absolute path of the buildx binary. Unset: the search in [Buildx binary](Reference-Deployment#buildx-binary). A binary that is missing or fails the requirements there leaves stack builds unavailable and changes nothing else |

## Updating dockmin

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_UPDATE_HEALTH_TIMEOUT_SECONDS` | integer | `180` | 30–1800 | Seconds the replacement container may take to report `healthy` after an update started from the Instance page. Past it the replacement is removed, the database copy is restored and the previous container is started again ([Instance](Administration-Instance#sequence)) |

- The update helper polls the replacement every 2 s from its start. A replacement that reports `unhealthy` or exits ends the wait at once.
- An image without a health check counts as healthy when the replacement is still running at the end of the time. The helper waits the whole time.
- A changed value applies at the next start. The review page states the value, and the updating page waits for the helper `DOCKMIN_UPDATE_HEALTH_TIMEOUT_SECONDS` plus the container's stop timeout plus 600 s before it shows the names of the containers to start by hand.
- A replacement with a health check that is not healthy in time is rolled back, and the update is recorded as `instance_update_rolled_back` with the reason `health_timeout` ([Instance](Administration-Instance#recorded-actions)).
- Set it in the environment of the dockmin container, like every setting above. It is not set in the interface.

## Logging and diagnostics

| Variable | Type | Default | Range | Effect |
|---|---|---|---|---|
| `DOCKMIN_LOG_LEVEL` | one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` | — | Log level of dockmin and uvicorn. Case is ignored |
| `DOCKMIN_ACCESS_LOG` | boolean | `false` | — | When `true`, one `http_request` line is logged per HTTP request, except `/api/health` and `/api/ready`. Fields: `method`, `path` (no query string, cut at 1,024 characters), `status`, `duration_ms`, `address`, `user_id` ([Logging events](Reference-Deployment#logging-events)) |
| `DOCKMIN_DEVELOPMENT_CHECKS` | boolean | `false` | — | When `true`, a failed job run logs its traceback, `development_checks_enabled` is logged at every start, and `/ui-kit` is served. Leave unset in production ([Development checks](Reference-Deployment#development-checks)) |

- `DOCKMIN_ACCESS_LOG` writes at `INFO`, so `DOCKMIN_LOG_LEVEL` must be `INFO` or lower for the lines to appear.
- The line is written for HTTP requests only: not for WebSocket connections, and not for the plain listener when `DOCKMIN_HTTP_MODE` is `redirect`.
- Each line carries the request's `request_id`. The query string is never logged.
- With the setting on, stdout volume rises with the request rate. The setting is read at start.
- Field definitions: [Access log](Reference-Deployment#access-log).

## Variables outside the settings model

| Variable | Read by | Effect |
|---|---|---|
| `DOCKMIN_IMAGE` | `compose.yaml` | Image `compose.yaml` runs. Default `dockmin:local` |
| `DOCKMIN_VERSION` | `Dockerfile` | Build argument: version of the installed package and the image's `org.opencontainers.image.version` label. Default `0+local`; a release build passes the tag's version |
| `DOCKMIN_REVISION` | `Dockerfile` | Build argument: the commit the image was built from, written to `/usr/share/dockmin/revision` and logged as `revision` by `application_started` ([Application started](Reference-Deployment#application-started)). Written only when it is 40 lowercase hexadecimal characters. Default empty; a release build passes the commit |
| `DOCKER_GID` | `compose.yaml` | Numeric group id that owns `/var/run/docker.sock` on the Docker host, added to the container user's groups. Default `999` |
| `DOCKMIN_TLS_CERT_SOURCE` | `examples/direct-https/compose.yaml` | Host file mounted as the certificate secret. Default `./secrets/tls_fullchain.pem` |
| `DOCKMIN_TLS_KEY_SOURCE` | `examples/direct-https/compose.yaml` | Host file mounted as the key secret. Default `./secrets/tls_privkey.pem` |
| `DOCKMIN_PROXY_ADDRESS` | `examples/reverse-proxy/nginx/compose.yaml` | Fixed address of nginx on the example network, trusted by dockmin as a `/32`. Must lie inside the subnet and outside the assignment range. Default `172.29.250.2` |
| `DOCKMIN_PROXY_SUBNET` | `examples/reverse-proxy/nginx/compose.yaml` | Subnet of the example network. Default `172.29.250.0/24` |
| `DOCKMIN_PROXY_IP_RANGE` | `examples/reverse-proxy/nginx/compose.yaml` | Range automatic address assignment draws from. Default `172.29.250.128/25` |
| `DOCKMIN_PROXY_TLS_CERT_SOURCE` | `examples/reverse-proxy/nginx/compose.yaml` | Host file mounted as nginx's certificate. Default `./secrets/proxy_fullchain.pem` |
| `DOCKMIN_PROXY_TLS_KEY_SOURCE` | `examples/reverse-proxy/nginx/compose.yaml` | Host file mounted as nginx's key. Default `./secrets/proxy_privkey.pem` |
| `DOCKMIN_UPDATE_ID` | process | Set by dockmin on the update helper container: the id of the update it runs ([Update helper](Administration-Instance#update-helper)). Not set by hand |
| `SQLITE_TMPDIR` | process | Directory for SQLite temporary files. The image sets `/data/sqlite-tmp`, created at start with mode `0700`. An unusable path fails the `data_directory` check |

## Variables passed by `compose.yaml`

| Passed | How |
|---|---|
| `DOCKMIN_PORT`, `DOCKMIN_SESSION_LIFETIME`, `DOCKMIN_COOKIE_SECURE`, `DOCKMIN_LOG_LEVEL`, `DOCKMIN_DB_BUSY_TIMEOUT_MS`, `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS`, `DOCKMIN_ACTIVITY_RETENTION_DAYS`, `DOCKMIN_MAX_CONCURRENT_REQUESTS`, `DOCKMIN_PERMITTED_INTERNAL_NETWORKS`, `DOCKMIN_TRUSTED_PROXIES`, `DOCKMIN_TRUSTED_ORIGINS` | From the shell or `.env`, with the default shown in the tables above |
| `DOCKMIN_HOST`, `DOCKMIN_DATA_DIR`, `DOCKMIN_SECRET_KEY_FILE`, `DOCKMIN_TLS_SELF_SIGNED` | Fixed values: `0.0.0.0`, `/data`, `/run/secrets/dockmin_secret_key`, `false` |
| `DOCKMIN_TLS_CERT_FILE`, `DOCKMIN_TLS_KEY_FILE`, `DOCKMIN_HTTPS_PORT`, `DOCKMIN_HTTPS_PUBLIC_PORT`, `DOCKMIN_HTTP_MODE` | By `examples/direct-https/compose.yaml` only |

Every other variable in the tables above is not passed.

[`.env.example`](https://github.com/Sphexi/DockMin/blob/main/.env.example) lists the variables `compose.yaml` and the examples interpolate, with their defaults. Copy it to `.env` next to `compose.yaml`; Compose reads `.env` for substitution only.

### Setting a variable `compose.yaml` does not pass

1. Create `compose.override.yaml` beside `compose.yaml`, or add to it. Compose reads that file only when no `-f` option is given. With `-f` options, add `-f compose.override.yaml` to every Compose command.

   ```yaml
   services:
     dockmin:
       environment:
         DOCKMIN_COMPOSE_TIMEOUT_SECONDS: "900"
   ```

2. Recreate the container. A restart does not apply changed environment values.

   ```bash
   docker compose up -d
   ```

   Compose reports the `dockmin` container as `Recreated`, then `Started`.

3. Confirm the value reached the container:

   ```bash
   docker compose exec dockmin printenv DOCKMIN_COMPOSE_TIMEOUT_SECONDS
   ```

   The command prints the value set in step 1. When it prints nothing, check the indentation of the override file and the `-f` options in use. When the container exits at start, read its log for `configuration_invalid` or `startup_check_failed` ([Troubleshooting](Troubleshooting)).

## Settings changed in the interface

These are stored in the database, not read from the environment.

| Setting | Where | Page |
|---|---|---|
| Subsystem on and off | Jobs, subsystem switches | [Jobs](Administration-Jobs) |
| Job and rule pause | Jobs, Automation | [Jobs](Administration-Jobs) |
| Automation rules: target, schedule, enabled | Automation | [Automation](Operations-Automation) |
| Environments, groups and tags | Environments | [Environments](Usage-Environments) |
| Environment access and per-object access | Access panels | [Access control](Usage-Access-Control) |
| Users and teams | Users, Teams | [Teams and users](Usage-Teams-and-Users) |
| API tokens | Account, API tokens | [Teams and users](Usage-Teams-and-Users) |
| Registry credential sets | Registries | [Registries](Administration-Registries) |
| Object storage targets | Object storage | [Object storage](Administration-Object-Storage) |
| Scheduled backups | Backups | [Backups](Administration-Backups) |
| Activity log export | Automation | [Activity](Administration-Activity) |
| Container log export | Automation | [Subsystems](Reference-Subsystems) |
| Notification channels, subscriptions, link base URL | Notifications | [Notifications](Administration-Notifications) |
