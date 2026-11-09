# Deployment

## Serving

The `app.serving` module is the image's command. It loads the settings, prepares the TLS certificate, builds the application and serves one or two listeners in one process.

| Item | Value |
|---|---|
| Processes | One. One worker. No setting adds more |
| Listeners | The HTTPS listener when TLS is on ([Certificate](Administration-Certificate)), and the plain HTTP listener ([HTTP modes](#http-modes)) |
| Application lifespan | Started and stopped once, by the primary listener: the HTTPS listener when TLS is on, else the plain listener |
| WebSocket message size | 65,536 bytes. A larger message closes the connection with code 1009 |
| Server header | Not sent |
| uvicorn access log | Off. uvicorn's loggers follow `DOCKMIN_LOG_LEVEL`; `trace` is not reachable. dockmin writes its own request lines when `DOCKMIN_ACCESS_LOG` is `true` ([Access log](#access-log)) |
| Signals | `SIGTERM` and `SIGINT` start the shutdown in [Startup and shutdown order](#startup-and-shutdown-order). A second `SIGINT` forces both listeners to exit and skips steps 1 to 6 of that order when the primary listener has not yet started them; a second `SIGTERM` does nothing |
| Open connections at stop | Given 10 s on each listener. A stream still open after that is cancelled |

| Exit status | Meaning |
|---|---|
| `0` | Clean stop |
| `2` | The configuration is invalid ([Startup self-check](#startup-self-check)). Logged as `startup_check_failed` when `check` is `encryption_key`, `tls_material` or `jwt_settings`, otherwise as `configuration_invalid` |
| `3` | A startup check failed, the application failed to start, a TLS file could not be loaded or the self-signed pair could not be stored, the encryption key was refused, or a listener could not bind |

### Settings by subject

Types, defaults and ranges of every variable: [Configuration](Reference-Configuration).

| Subject | Variables | Section |
|---|---|---|
| Listeners | `DOCKMIN_HOST`, `DOCKMIN_PORT`, `DOCKMIN_HTTPS_PORT`, `DOCKMIN_HTTPS_PUBLIC_PORT`, `DOCKMIN_HTTP_MODE` | [HTTP modes](#http-modes) |
| TLS | `DOCKMIN_TLS_CERT_FILE`, `DOCKMIN_TLS_KEY_FILE`, `DOCKMIN_TLS_SELF_SIGNED` | [Certificate](Administration-Certificate) |
| Encryption key | `DOCKMIN_SECRET_KEY`, `DOCKMIN_SECRET_KEY_FILE`, `DOCKMIN_SECRET_KEY_DIR` | [Encryption](Administration-Encryption) |
| Request limit | `DOCKMIN_MAX_CONCURRENT_REQUESTS` | [Concurrency](#concurrency) |
| Outbound addresses | `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` | [Address policy](Reference-Security#address-policy) |
| Proxy | `DOCKMIN_TRUSTED_PROXIES`, `DOCKMIN_TRUSTED_ORIGINS` | [Behind a reverse proxy](#behind-a-reverse-proxy) |

## HTTP modes

`DOCKMIN_HTTP_MODE` selects what the plain listener on `DOCKMIN_PORT` does.

| TLS on | Mode | Listeners |
|---|---|---|
| No | `serve` (default) | The application on HTTP |
| Yes | `redirect` (default) | The application on HTTPS; the redirect on HTTP |
| Yes | `serve` | The application on HTTPS and on HTTP |
| Yes | `off` | The application on HTTPS only |

TLS is on with a supplied certificate or a generated self-signed one ([Certificate](Administration-Certificate)). With the self-signed default, the redirect answers on `DOCKMIN_PORT` inside the container and `DOCKMIN_HTTPS_PORT` (`8443`) is the port to publish.

| Mode | Plain listener |
|---|---|
| `serve` | The same application, behind the same limits. The session and CSRF cookies are `Secure` by default, so a browser cannot sign in over it; it serves health checks and a proxy on a private network. The sign-in and setup forms are served, and a password or setup value submitted through them crosses the network in clear before the request is refused |
| `redirect` | The redirect described below. It reads no forwarded header and touches no application state |
| `off` | None |

### Redirect

| Request | Answer |
|---|---|
| `GET`, `HEAD` | `308`, `Location: https://<host>[:<public port>]<path>?<query>`, `Cache-Control: no-store`, an empty body |
| Any other method | `400`, `Use HTTPS`, `Connection: close`. No redirect, so a request body sent in clear is not forwarded |
| A `Host` that is not accepted, or a target that is not a path | `400`, `Invalid request`, `Connection: close` |
| WebSocket handshake | Refused (close code 1008) |
| Logging | With `DOCKMIN_ACCESS_LOG` on, a `400` and a refused WebSocket handshake each write a line ([Access log](#access-log)). A `308` writes none |

| `Host` rule | Value |
|---|---|
| Header count | Exactly one |
| Host | A DNS name or IP address, or an IPv6 address in brackets. At most 253 characters. No user name |
| Port in `Host` | Digits, at most 65535. Ignored: the redirect names the public port |
| Allowed hosts | When `DOCKMIN_TRUSTED_ORIGINS` lists `https` origins, the host must be the host of one of them. Origins with another scheme are ignored |
| Target | Starts with `/`; the path and the query hold printable bytes only |

The port in `Location` is `DOCKMIN_HTTPS_PUBLIC_PORT`, else `DOCKMIN_HTTPS_PORT`; `443` is omitted. The redirect sends the [security headers](#security-headers) except `Strict-Transport-Security`.

## Behind a reverse proxy

dockmin reads the client address and the request scheme from `X-Forwarded-For` and `X-Forwarded-Proto`, but only when the connection comes from an address listed in `DOCKMIN_TRUSTED_PROXIES`. The launcher passes the same list to uvicorn as `forwarded_allow_ips`. When the variable is empty the launcher turns uvicorn's proxy header handling off: both headers are ignored from every peer, loopback included.

| Setting | Value |
|---|---|
| `DOCKMIN_TRUSTED_PROXIES` | IP addresses or networks, such as `172.18.0.2` or `10.0.0.0/8`. `*` trusts every peer and is safe only when nothing but the proxy can reach the port. A single address is a `/32` (or `/128`). Format: [Configuration](Reference-Configuration#listeners-and-network) |

| Proxy behaviour | Requirement |
|---|---|
| Client address | Send `X-Forwarded-For` with the client's address |
| Scheme | Send `X-Forwarded-Proto: https`. dockmin then treats the request as HTTPS and sends `Strict-Transport-Security` |
| `Host` | Preserve the public host, or list the public origin in `DOCKMIN_TRUSTED_ORIGINS` ([below](#terminals-and-streams-behind-a-reverse-proxy)) |
| Request bodies | Allow at least 256 KiB. Larger bodies are for `POST /restore/archive` ([Reverse proxy limits for backups](#reverse-proxy-limits-for-backups)) and the build context upload ([Reverse proxy limits for build contexts](#reverse-proxy-limits-for-build-contexts)) only |
| Security headers | dockmin sends its own ([Security headers](#security-headers)). A header the proxy adds is sent in addition |
| TLS and renewal | The proxy's |
| Proxy on the same host | List `127.0.0.1` and `::1` in `DOCKMIN_TRUSTED_PROXIES` |
| Self-signed certificate | Set `DOCKMIN_TLS_SELF_SIGNED=false`. With the default, the plain listener answers every request with a redirect to the HTTPS port, and the proxy receives the redirect. The shipped `compose.yaml` sets it |
| Proxy connecting to port 8443 | The proxy sets HSTS itself ([Security headers](#security-headers)) |
| Container started with `docker run` | Add `-e DOCKMIN_TLS_SELF_SIGNED=false`, and publish `DOCKMIN_PORT` (`8000`) to the proxy in place of `8443` ([Installation](Getting-Started-Installation#behind-a-reverse-proxy)) |

If the proxy is not listed, every client appears to come from the proxy's address. dockmin then:

- counts failed sign-ins per address and records the address in the activity log, but does not refuse a sign-in because of an address's count. Lockout by username applies in every case;
- counts anonymous requests per address, but does not refuse any ([Rate limit](#rate-limit)).

Per-address lockout and the rate limit turn on when `DOCKMIN_TRUSTED_PROXIES` is set, and on requests that arrive on dockmin's own HTTPS listener.

Docker's published ports can also hide the client address. A container reached through a published port may see the Docker gateway as the peer; list that address as well, or run the proxy on the same Docker network and list the proxy.

## Terminals and streams behind a reverse proxy

| Path | Requirement |
|---|---|
| `/environments/*/containers/*/terminal/ws` | Pass WebSocket upgrades: HTTP/1.1 to dockmin, with the `Upgrade` and `Connection` headers forwarded |
| `/environments/*/containers/*/stats/stream`, `/environments/*/containers/*/logs/stream`, `/environments/*/images/jobs/*/events`, `/environments/*/stacks/*/logs/stream`, `/environments/*/stacks/-/jobs/*/events`, `/environments/*/swarm/services/*/logs/stream`, `/environments/*/swarm/stacks/*/logs/stream`, `/environments/*/swarm/stacks/-/jobs/*/events` | Server-sent events. Do not buffer the response. dockmin sends `X-Accel-Buffering: no`; a proxy that ignores it needs response buffering turned off for these paths |

- The WebSocket handshake is refused (`403`) unless its `Origin` header names the host in the request's `Host` header, or is listed in `DOCKMIN_TRUSTED_ORIGINS`. A handshake with no `Origin` header is refused. A proxy that rewrites `Host` must either preserve the original or list the public origin.
- Each `DOCKMIN_TRUSTED_ORIGINS` entry is an origin such as `https://dockmin.example.com`, without a path, query or user name ([Configuration](Reference-Configuration#listeners-and-network)).
- A terminal, a stream or a download counts toward `DOCKMIN_MAX_CONCURRENT_REQUESTS` for as long as it is open.

Terminal limits and timeouts: [Containers](Usage-Containers#terminal-limits).

## Webhook calls behind a reverse proxy

Webhook calls ([Webhook calls](Reference-JSON-API#webhook-calls)) are `POST /api/v1/hooks/<webhook-token>` and `POST /api/v1/hooks`.

| Requirement | Detail |
|---|---|
| Scheme | Forward `X-Forwarded-Proto: https` from a proxy listed in `DOCKMIN_TRUSTED_PROXIES`. When dockmin has a certificate, a call that reaches the plain listener without it answers `400`, reason `use_https`. A proxy that connects to the HTTPS listener needs nothing more |
| Client address | Forward `X-Forwarded-For` from a listed proxy. Otherwise the address recorded as `caller_address` is the proxy's, and the per-address limit does not refuse ([Rate limit](#rate-limit)) |
| Query string | Do not add one. A call with a query string answers `400`, reason `query_not_supported` |
| `Authorization` | Pass it unchanged. The header form carries the token in it |
| Path | Pass `/api/v1/hooks` and `/api/v1/hooks/<webhook-token>` unchanged |
| Access log | The request path of the path form holds the token. Leave the path of `/api/v1/hooks/` out of the proxy's access log, or have senders use the header form. The access log of `examples/reverse-proxy/nginx` records the path |
| Caching | Responses carry `Cache-Control: no-store`, except `404`, which is the answer for any unknown `/api` path |
| Body size | A call needs no body. The proxy limit of 256 KiB or more applies as for any request ([Body size](#body-size)) |

- A proxy that terminates TLS and reaches dockmin on the plain listener with `DOCKMIN_TLS_SELF_SIGNED=false` has no certificate on dockmin, so the scheme check does not apply ([Automation](Operations-Automation)).
- In any dockmin log line, including the access log, the secret part of a webhook token is replaced with `[REDACTED]`.

## Security headers

Every HTTP response the application produces carries these headers: pages, fragments, JSON, static files, redirects, and every error status including `404`, `413`, `429`, `500` and `503`. The redirect listener sends them as well. Values set by a route are replaced.

| Header | Value | When |
|---|---|---|
| `Content-Security-Policy` | The policy chosen for the response ([below](#content-security-policy)) | Always |
| `X-Content-Type-Options` | `nosniff` | Always |
| `Referrer-Policy` | `same-origin` | Always |
| `X-Frame-Options` | `DENY` | Always |
| `Strict-Transport-Security` | `max-age=31536000` | Only when the request's scheme is `https`: served on the HTTPS listener with a supplied certificate, or forwarded as `https` by a trusted proxy. Never on the self-signed listener, so a proxy that connects to it sets the header itself. No `includeSubDomains`, no `preload` |

- A WebSocket handshake refusal carries no headers.
- No report endpoint exists. `Permissions-Policy`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy` are not sent.

### Content-Security-Policy

| Policy | Used for | Value |
|---|---|---|
| Default | Every response except the one below | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'` |
| Terminal | The page rendered by `POST /environments/{id}/containers/{container_id}/terminal` | The default policy plus `style-src-elem 'self' 'unsafe-inline'` |

- `script-src` is `'self'` in both policies: no inline script, no `eval`, no nonce, no hash.
- No page carries an inline `<script>`, an `on*` attribute or a `javascript:` URL.
- The terminal policy allows inline `<style>` elements, which xterm.js creates at run time. No other page loads xterm.js.
- `connect-src 'self'` admits the same-origin WebSocket of the terminal.
- `/docs`, `/redoc` and `/openapi.json` are not served (`404`).

## Request limits

Applied in front of routing, outermost first.

| Layer | Limit |
|---|---|
| Security headers | [Security headers](#security-headers) |
| Request id | Assigns the request id ([Request id](Reference-JSON-API#request-id)) |
| Send idle bound | [Send idle bound](#send-idle-bound) |
| Concurrency | [Concurrency](#concurrency) |
| Restore gate | [Backups](Administration-Backups) |
| Body size | [Body size](#body-size) |
| Rate limit | [Rate limit](#rate-limit). Runs with the application's dependencies, after the signed-in user is resolved |

An error from these layers is an HTML page, an `error_state` fragment for an HTMX request, or `{"error": {"status", "message"}}` under `/api`, by the path.

### Body size

| Item | Value |
|---|---|
| Cap | 256 KiB for every HTTP request with a body, whatever the method and content type |
| Declared `Content-Length` over the cap | `413`, nothing read |
| No length (chunked) | Bytes are counted as received. The read stops at the first message that passes the cap: `413` |
| Slow body | `408` with `Connection: close` when no body message arrives for 15 s, or the whole body takes more than 30 s |
| After the response has started | The same conditions end the request as a client disconnect |
| Exempt | `POST /restore/archive`. Its limits: [Backups](Administration-Backups#upload) |
| Own cap | `POST /environments/{id}/stacks/{name}/contexts/-/upload`: 16 MiB and 64 KiB, within 300 s. Its limits: [Stacks](Usage-Stacks#build-contexts) |
| Memory | Multipart parts under the cap stay in memory; nothing is written to `/tmp` |

Limits inside the cap are checked by the routes: a Compose file is at most 131,072 bytes and each PEM field at most 65,536 bytes.

### Concurrency

| Item | Value |
|---|---|
| Counted | HTTP requests and WebSocket sessions in flight, across both listeners, until the response or the socket is finished. A stream counts while it is open |
| Limit | `DOCKMIN_MAX_CONCURRENT_REQUESTS` ([Configuration](Reference-Configuration#listeners-and-network)) |
| Over the limit, HTTP | `503` with `Retry-After: 1` |
| Over the limit, WebSocket | The handshake is refused before it is accepted, with close code 1013 |
| Exempt | `/api/health`. `/api/ready` is counted |
| Not counted | The redirect listener |
| Logged | `concurrency_limit_reached` with `limit`, at most once a minute |

### Rate limit

An address is limited on requests that resolve to no signed-in user.

| Item | Value |
|---|---|
| Counted | A request or WebSocket handshake with no session and no valid API token. One token from the address's bucket. A webhook call is counted whatever its token |
| Not counted | Authenticated requests; `/static/`; `/api/health` and `/api/ready`; a request that matches no route (`404`) or whose body fails to parse (`422`), because no dependency runs for them |
| Burst | 60 |
| Refill | 1 token a second |
| Key | The client address. An IPv6 address is keyed by its `/64`; an IPv4-mapped IPv6 address by its IPv4 address |
| Tracked addresses | At most 10,000. The least recently used is dropped |
| Empty bucket, HTTP | `429` with `Retry-After` in seconds |
| Empty bucket, WebSocket | The handshake is refused (close code 1008) |
| Order | Setup gate, sign-in resolution, rate limit, CSRF. A request refused for CSRF has been counted |

The limit refuses requests only where client addresses are per-client:

| Deployment | Refuses |
|---|---|
| `DOCKMIN_TRUSTED_PROXIES` set | Yes, on both listeners |
| No trusted proxies, request on dockmin's own HTTPS listener | Yes |
| No trusted proxies, request on the plain listener serving the application (`DOCKMIN_HTTP_MODE=serve`, or no TLS) | No |
| Redirect listener | Not applicable: it serves no application |

Where it does not refuse:

- The bucket is still counted. An empty bucket is logged as `rate_limit_exceeded` with `enforced: false`, at most once a minute.
- At start, `rate_limit_not_enforced` with `listener: "http"` is logged when the plain listener serves the application and no proxy is trusted.

Where every client reaches the HTTPS listener from one address, such as Docker's userland proxy or rootless Docker, all anonymous clients share one bucket and an empty bucket refuses all of them. No setting turns the limit off on the HTTPS listener.

### Send idle bound

| Item | Value |
|---|---|
| Rule | Each response body chunk must be accepted by the client within 60 s |
| On expiry | The application is stopped, so the response's cleanup runs (export slot, database session, backup work directory, Docker streams). The connection is closed without completing the response |
| Logged | `response_send_stalled` with `path` (the route template, or `unmatched`) and `bytes_sent`, once per stalled response |
| Covers | Every streamed response: downloads, exports, log files, server-sent events |
| Does not cover | WebSocket sends |
| TCP timeout | On Linux, the listening sockets set `TCP_USER_TIMEOUT` to 120 s: a connection whose sent data stays unacknowledged for 120 s is closed |

## Engine reply limits

| Reply | Limit |
|---|---|
| Container list, Swarm config list | 32 MiB |
| Image, network, volume, Swarm service, task and secret lists | 16 MiB |
| Node list | 4 MiB |
| Container, image, network, volume, Swarm service and config detail | 4 MiB |
| Swarm, task, node and secret detail, image history | 1 MiB |
| Action reply, API version negotiation | 64 KiB |
| Engine error body parsed for a message | 64 KiB |
| One line of the event stream | 256 KiB |
| Decoded size of a list, estimated | 128 MiB |
| List replies read and decoded at once, per process | 2 |

A list over a limit is not shown; the page reads `This host reports more <kind> data than can be listed here (limit N MiB).` The Swarm list message ends after `here.` without the limit.

## Single instance

One dockmin process runs against a data directory. A second process on the same directory refuses to start.

| Item | Value |
|---|---|
| Lock file | `<DOCKMIN_DATA_DIR>/dockmin.lock` (`/data/dockmin.lock` in the image) |
| Mechanism | An exclusive, non-blocking `flock` held from start until shutdown or process exit |
| Contents | Empty. Nothing is written to it |
| Created with | Mode `0600`. A file with another mode is reset to `0600`. A symbolic link is not followed |
| Required of the file | A regular file, owned by the user dockmin runs as, with one hard link |
| Taken | Before the database is opened. Released last at shutdown |
| Taken by | `python -m app restore`, which exits `3` while another process holds the lock ([Backups](Administration-Backups#restore-from-the-command-line)) |
| Not taken by | `python -m app clear-lockout` ([Teams and users](Usage-Teams-and-Users)) |

| Situation | Result |
|---|---|
| Another process holds the lock | The new process exits with status `3`. Logged as `instance_already_running` with the path, then `startup_check_failed` (check `data_directory`). The running process is unaffected |
| The path is a symbolic link, is not a regular file, or cannot be opened | The process does not start (status `3`). Logged as `instance_lock_failed` with the path and errno, then `startup_check_failed` |
| The file has more than one hard link, or another user owns it | The process does not start (status `3`). Logged as `instance_lock_unsafe` with the path, then `startup_check_failed` |
| The data directory is writable by its group or by others | The process starts. Logged as `data_dir_writable_by_others` at warning level, with the path |
| The platform has no `fcntl` | The process starts without the lock. Logged as `instance_lock_unavailable`. The image runs on Linux and is not affected |

- Do not mount one data volume into two dockmin containers.
- The lock is released when the process exits, however it exits. A stale `dockmin.lock` file left on disk does not block a start.

To clear the `data_dir_writable_by_others` warning for a data directory on the host:

1. Remove the write permission of the group and of others:

   ```bash
   chmod go-w <data-directory>
   ```

2. Restart the container. The log no longer holds `data_dir_writable_by_others` after the next start. When it still does, check the mode of the directory with `stat -c %a <data-directory>`: the last two digits must not include `2`, `3`, `6` or `7`.

## Startup and shutdown order

| Start | Step |
|---|---|
| 0 | The launcher loads the settings (exit `2` on failure) and configures logging. With a self-signed certificate it checks the data directory and creates or reuses the pair in `server-tls/` (exit `3` on failure). It loads the TLS files (exit `3` on failure) and builds the application |
| 1 | Data directory check, then the instance lock. Then the warnings `data_directory_not_mounted` (generated key, data directory not a mount point), `rate_limit_not_enforced` and `cookie_secure_disabled`, when they apply |
| 2 | Leftovers of a restore are swept: `backup-work/` is deleted, and each `restore/<restore-id>/` without `previous/` or `rejected/` is deleted |
| 3 | The address policy is set. The SQLite temporary directory is created. The database is opened and migrated |
| 4 | The Docker connection work directory is prepared. The master keys are resolved: read from the variable or the file, or, with a generated key, read from the key directory or created there for a new database ([Encryption](Administration-Encryption#key-directory)). Encryption key check. The password hasher is prepared. With no administrator, the one-time setup value is written to the log |
| 5 | Compose work directory and `stacks/` are prepared, and the Compose and buildx binaries are located. Stack directories with no stack are removed. Docker Engine access, then the Compose version is read, then registries. Then the container dockmin runs in is identified ([Instance](Administration-Instance)). Updates that finished while dockmin was stopped are recorded |
| 6 | `development_checks_enabled` when set. Scheduler: subsystems registered, the snapshot refresher's job created or updated, recent user actions reloaded, runs left in progress recorded `interrupted`, due jobs served, loop started |

`application_started` is logged when step 6 has finished ([Application started](#application-started)).

| Shutdown | Step |
|---|---|
| 0a | With two listeners, the primary listener closes its listening sockets and accepts no new connection. Connections it already holds stay open |
| 0b | The secondary listener stops: the plain listener when TLS is on and `DOCKMIN_HTTP_MODE` is `serve` or `redirect`. Open connections are given 10 s. Step 0c begins when it has finished |
| 0c | The primary listener drains: the HTTPS listener when TLS is on, else the plain listener. Open connections are given 10 s. It owns the application lifespan, so steps 1 to 6 follow its stop |
| 1 | A running encryption check or re-encryption is stopped. A restore in progress is stopped and ended with reason `shutdown` ([Backups](Administration-Backups#ending-a-restore)) |
| 2 | The update dispatcher, the health event watchers, the container identification watch and the notification poker are stopped. Scheduler: every running run is cancelled and recorded `interrupted`, reason `shutdown`. Waits up to 10 s |
| 3 | Image jobs and stack jobs close together; then Compose |
| 4 | Registries, Docker Engine access |
| 5 | Database |
| 6 | Instance lock. `application_stopped` is logged afterwards |

- A request on the plain listener finishes or is cancelled before the primary listener is told to stop.
- The secondary listener runs no application lifespan. `DOCKMIN_HTTP_MODE` `off`, and a deployment with TLS off, have no secondary listener.
- The order is the same when a listener ends by itself: the secondary stops first, then the primary.
- Steps 0c to 6 run even when stopping the secondary listener failed. The original error is still raised after they have run.
- A second `SIGINT` sets both listeners to exit at once. A primary listener forced out before its lifespan shutdown starts does not run steps 1 to 6: the database is not closed in order and the scheduler does not record its runs. Those runs are recorded `interrupted` at the next start.
- A step that fails is logged as `shutdown_step_failed` with `step` and `error`; the steps after it still run.
- A run not recorded within the 10 s, or lost to a crash, is recorded `interrupted` with reason `process_stopped` at the next start ([Jobs](Administration-Jobs#heartbeat-shutdown-and-restart)).
- Compose stops a container after 10 s by default. The shipped `compose.yaml` sets `stop_grace_period: 45s`. A container killed before the scheduler has recorded its runs leaves them for the next start.

## Startup self-check

A failed precondition is one log line, `startup_check_failed`, and no traceback. Fields:

| Field | Value |
|---|---|
| `check` | `encryption_key`, `database`, `data_directory`, `compose_cli`, `buildx`, `tls_material` or `jwt_settings` |
| `problem` | What is wrong |
| `remedy` | What to change |
| `fatal` | `true` when the process does not start |
| `during` | `startup`, or `restore` when a restore restarts the runtime |

The line is logged at error level when fatal and at warning level otherwise. A check that fails while a restore restarts the runtime is logged with `fatal: false`; the process keeps running ([Backups](Administration-Backups#restore)). A start that fails a check also logs uvicorn's `Application startup failed. Exiting.` Any other startup exception keeps its traceback.

| Check | Fails when | Exit |
|---|---|---|
| `encryption_key` | Both `DOCKMIN_SECRET_KEY` and `DOCKMIN_SECRET_KEY_FILE` are set; the value is not base64 of 32 bytes; the file named by `DOCKMIN_SECRET_KEY_FILE` is unreadable | `2` |
| `encryption_key` | Both `DOCKMIN_PREVIOUS_SECRET_KEY` and `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` are set; the previous value is not base64 of 32 bytes; the file named by `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is unreadable; the previous key equals a supplied current key | `2` |
| `encryption_key` | Neither variable is set and `DOCKMIN_SECRET_KEY_DIR` is relative, or equals, contains or lies inside `DOCKMIN_DATA_DIR` | `2` |
| `encryption_key` | The configured key does not open the database | `3` |
| `encryption_key` | Neither variable is set, `secret.key` does not exist, and the database holds encrypted data or a `crypto.*` setting row, data kept by an earlier restore exists under `restore/`, or `DOCKMIN_PREVIOUS_SECRET_KEY` or `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is set. No key is generated | `3` |
| `encryption_key` | Neither variable is set, `secret.key` does not exist, and the key directory is missing, is not a directory, is a symbolic link, is the data directory or inside it, is not a mount point, is a `tmpfs` or `ramfs` mount, is not owned by the process user, is writable by its group or others, or is not writable. No key is generated | `3` |
| `encryption_key` | `secret.key` exists and is a symbolic link, is not a regular file, cannot be read, or does not hold base64 of 32 bytes. The file is not replaced | `3` |
| `encryption_key` | With a generated key, the previous key equals the key in `secret.key` | `3` |
| `database` | The schema is newer than this build. `problem`: `The database schema check failed: This database was written by a newer dockmin (schema N; this build supports M). Run that version or restore a backup.` The database is not modified | `3` |
| `database` | The schema ledger has a gap, an applied migration file is missing or has another name, or a migration file name does not match `NNNN_name.sql`. Migration files are compared with the ledger by version and name only | `3` |
| `database` | The database cannot be opened, is damaged, cannot be created or read, or cannot be read while the encryption key is checked | `3` |
| `data_directory` | The directory is missing, is not a directory, is a symbolic link, or is not writable by the process user | `3` |
| `data_directory` | Another process holds the instance lock, or the lock file is unusable | `3` |
| `data_directory` | `SQLITE_TMPDIR` cannot be created, or the Docker connection work directory, the Compose work directory or `stacks/` cannot be prepared | `3` |
| `compose_cli` | `DOCKMIN_COMPOSE_BINARY` fails the [binary requirements](#binary-location) | `3` |
| `compose_cli` | No binary found, the version cannot be read, or the version is below v2.32.0. Not fatal: the process starts and every Compose operation is refused | — |
| `buildx` | No trusted buildx binary found ([Buildx binary](#buildx-binary)). Not fatal: the process starts, and a deploy that would build an image is refused (`buildx_unavailable`) | — |
| `tls_material` | A TLS file is unreadable, holds no PEM certificate, is not an unencrypted PEM key, or the key does not match the first certificate | `3` |
| `tls_material` | Only one of the two TLS variables is set | `2` |
| `tls_material` | The self-signed pair cannot be stored: `server-tls` is a symbolic link or not a directory, or the directory or its files cannot be created or written | `3` |
| `jwt_settings` | An access platform setting is set without `DOCKMIN_JWT_HEADER`; the header is set without an issuer, an audience or exactly one key source; a value is malformed; the key file is unreadable, empty or holds no usable key ([Configuration](Reference-Configuration#access-platform)) | `2` |

The `remedy` field of each line names the change to make. The exact `problem` and `remedy` texts, with causes and actions for each check: [Troubleshooting](Troubleshooting).

A configuration error that is none of these is logged as `configuration_invalid` with `problem`, and exits `2`. That includes `DOCKMIN_HTTPS_PORT` equal to `DOCKMIN_PORT`, `DOCKMIN_HTTP_MODE` set to `redirect` or `off` with TLS off, and a `DOCKMIN_TLS_SELF_SIGNED` value that is not a boolean.

## Logging events

| Event | Level | Fields |
|---|---|---|
| `startup_check_failed` | error, warning | `check`, `problem`, `remedy`, `fatal`, `during` |
| `configuration_invalid` | error | `problem` |
| `tls_certificate_expiring` | warning | `days_left`, `subject`, `not_before`, `not_after` |
| `tls_certificate_invalid_now` | warning | `state`, `subject`, `not_before`, `not_after` |
| `tls_certificate_self_signed` | info | `sha256_fingerprint`, `not_after`, `generated`, `reason` |
| `encryption_key_generated` | warning | `key_file`, `key_fingerprint`, `note` |
| `encryption_key_loaded` | info | `key_version`, `key_fingerprint` |
| `encryption_key_file_permissions` | warning | `key_file`, `mode` |
| `data_directory_not_mounted` | warning | `path`, `note` |
| `data_dir_writable_by_others` | warning | `path` |
| `instance_already_running` | error | `path` |
| `instance_lock_failed` | error | `path`, `errno` |
| `instance_lock_unsafe` | error | `path` |
| `instance_lock_unavailable` | warning | — |
| `migration_applied` | info | `migration` |
| `compose_detected` | info | `version` |
| `compose_binary_not_found`, `compose_binary_untrusted` | warning | `path` on the untrusted line |
| `compose_below_floor` | warning | `installed`, `required` |
| `compose_version_unreadable` | warning | `error` |
| `buildx_binary_untrusted`, `buildx_binary_unusable` | warning | `path` |
| `concurrency_limit_reached` | warning | `limit` |
| `rate_limit_exceeded` | warning | `enforced` |
| `rate_limit_not_enforced` | warning | `listener` |
| `webhook_refused` | warning | `counts`: the refused calls by answer since the last line, at most one line a minute |
| `jwt_refused` | warning | `reason`; at most one line a minute for each reason ([Teams and users](Usage-Teams-and-Users)) |
| `jwks_fetch_failed` | warning | `reason` |
| `cookie_secure_disabled` | warning | — |
| `response_send_stalled` | warning | `path`, `bytes_sent` |
| `response_cleanup_failed` | warning | `error` |
| `limit_response_render_failed` | error | `error` |
| `readiness_check_failed` | error | `check`, with a traceback |
| `unhandled_exception` | error | A traceback; the request id is on the line |
| `outbound_address_refused` | warning | `address` |
| `self_identity_resolved` | info | `source`, `verified_environments`, `restartable`, `unreadable_environments` |
| `self_identity_resolution_failed`, `self_identity_probe_failed` | warning | `error` |
| `instance_restart_failed` | error | `environment_id`, `error` |
| `instance_restart_sent` | info | `environment_id`, `error` |
| `instance_restart_unconfirmed` | warning | `environment_id`, `error` |
| `shutdown_step_failed` | error | `step`, `error` |
| `application_started` | info | `version`, `revision`, `instance_name`, `http_mode`, `https_port`, `http_port`, `access_log` ([Application started](#application-started)) |
| `application_stopped` | info | — |
| `login_succeeded` | info | `user_id`, `username`, `address`, `method` ([Sign-in lines](#sign-in-lines)) |
| `login_failed` | warning | `user_id`, `reason`, `address`, `method`, and `username` when the sign-in was a password one and `user_id` is set |
| `login_refused` | warning | `reason`, `address`, `method`, and `user_id` and `username` when the account exists |
| `logout_succeeded` | info | `user_id`, `username`, `address`, `method`, and `reason` when an access platform session ended |
| `http_request` | info | `method`, `path`, `status`, `duration_ms`, `address`, `user_id`, and `listener` on the redirect listener; only when `DOCKMIN_ACCESS_LOG` is `true` ([Access log](#access-log)) |
| `websocket_connection` | info | `path`, `status`, `close_code`, `duration_ms`, `address`, `user_id`, and `listener` on the redirect listener; only when `DOCKMIN_ACCESS_LOG` is `true` ([Access log](#access-log)) |

- `httpx`, `httpcore` and the `websockets` loggers log at `WARNING` or above whatever the log level: at lower levels they log full request URLs or frame contents.
- Secrets are scrubbed from every line before it is written.

### Line format

Every line is one JSON object on stdout, ending in a newline.

| Key | Content |
|---|---|
| `timestamp` | UTC, ISO 8601 with the offset `+00:00`. The fractional seconds are omitted when zero |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `logger` | The logger's name. Lines of uvicorn's `uvicorn.error` logger are written with `uvicorn.server` |
| `message` | The event name of the tables on this page, or the library's own text |
| `request_id` | The id of the HTTP request or WebSocket connection being served ([Request id](Reference-JSON-API#request-id)). `null` outside a request and on every line of the redirect listener |
| Event fields | The fields of the event, after `request_id` |
| `exception` | The formatted traceback, when the line carries one |

- Keys appear in the order above.
- `color_message`, which uvicorn adds to its lines, is not written.
- The characters ESC (U+001B) and CSI (U+009B) in a string field value are written as the text `\u001b` and `\u009b`.
- Secrets are scrubbed before the line is serialised ([Logging events](#logging-events)).
- Parsing the lines with the other logs dockmin writes: [Activity](Administration-Activity).
- The lines have no `schema_version`. For a record family that has one, it changes only when an existing field changes meaning or is removed. A new event or a new optional field does not change it: `websocket_connection` and `listener` are additions of this kind.

### Application started

`application_started` is written once, when the application has finished starting.

| Field | Type | Content |
|---|---|---|
| `version` | string | The product version |
| `revision` | string or `null` | The 40-character commit the image was built from, read from `/usr/share/dockmin/revision`. `null` when the file is absent, unreadable, or not 40 lowercase hexadecimal characters |
| `instance_name` | string | `DOCKMIN_INSTANCE_NAME` |
| `http_mode` | string | The plain listener's effective mode: `serve`, `redirect` or `off` ([HTTP modes](#http-modes)) |
| `https_port` | integer or `null` | The HTTPS listener's port. `null` when TLS is off |
| `http_port` | integer or `null` | The plain listener's port. `null` when `http_mode` is `off` |
| `access_log` | boolean | The value of `DOCKMIN_ACCESS_LOG` |

### Sign-in lines

| Line | Written when | Fields |
|---|---|---|
| `login_succeeded` | A sign-in succeeded | `user_id`, `username`, `address`, `method` |
| `login_failed` | A sign-in was judged and failed | `user_id`, `reason`, `address`, `method`. A password sign-in adds `username` when `user_id` is set |
| `login_refused` | A sign-in was refused before the password was judged | `reason`, `address`, `method`. `user_id` and `username` are added when the submitted name belongs to an account |
| `logout_succeeded` | A user signed out, or an access platform session ended | `user_id`, `username`, `address`, `method`. An ended access platform session adds `reason` |

- `method` is `password` or `jwt`.
- `login_failed` `reason` for a password sign-in: `unknown_user`, `bad_password`, `account_disabled` or `password_sign_in_disabled`. `user_id` is `null` for `unknown_user`.
- `login_failed` `reason` for an access platform sign-in: `jwt_unlinked`, `jwt_account_disabled`, `jwt_username_invalid` or `jwt_username_taken`. `user_id` is set when the account is known. The line carries no `username`. It is written once for each distinct token, like the activity row.
- `login_refused` `reason`: `locked_out` or `server_busy`.
- `logout_succeeded` `reason` for an ended access platform session: `platform_assertion_ended` or `platform_identity_changed`.
- `username` is the account's stored name, read by `user_id`. The name typed on the sign-in form is never logged: a wrong password is often typed into that field.
- An attempt on a name that belongs to no account logs `reason`, `address` and `method` only, and for `login_failed` a `null` `user_id`.
- `address` is the client address after the trusted proxy headers are applied ([Behind a reverse proxy](#behind-a-reverse-proxy)).
- The request's `request_id` is on each line. The activity rows of the same request carry the same id ([Activity](Administration-Activity#request-id)).

### Access log

`DOCKMIN_ACCESS_LOG` is `false` by default ([Configuration](Reference-Configuration#logging-and-diagnostics)). When `true`, dockmin writes one `http_request` line for each HTTP request and one `websocket_connection` line for each WebSocket connection. The redirect listener writes lines for refusals only.

| Item | Value |
|---|---|
| Logger, level | `dockmin.access`, `INFO`. Written only when `DOCKMIN_LOG_LEVEL` is `INFO` or lower |
| `http_request` written | After the response has been sent, or after the application raised |
| `websocket_connection` written | Once, when the application call ends: after the connection closed, after the application raised, or when the upgrade was refused |
| Not logged | `/api/health` and `/api/ready`, on every listener; a `308` redirect of the redirect listener |
| Covered | Every answer of the application, including `413`, `429` and `503` from the request limits |

#### `http_request`

| Field | Type | Content |
|---|---|---|
| `method` | string | The HTTP method |
| `path` | string | The request path, without the query string, cut at 1,024 characters with `…` appended. A restore progress path is written as `/restore/progress/[REDACTED]` |
| `status` | integer | The status sent. `500` when the application raised before it sent one |
| `duration_ms` | integer | Milliseconds from the start of the request until the response was sent or the application raised |
| `address` | string | The client address after the trusted proxy headers are applied. `unknown` when there is none |
| `user_id` | integer or `null` | The user of the session or API token. `null` for an anonymous request and for a webhook call |
| `listener` | string | `redirect`, on lines written by the redirect listener. Absent on every other line |

- A standard user who is answered `404` on an administrator route keeps their `user_id` on the line.

#### `websocket_connection`

The container terminal is the only WebSocket route.

| Field | Type | Content |
|---|---|---|
| `path` | string | As in `http_request` |
| `status` | integer | `101` for an accepted connection. For a connection not accepted: the status of the HTTP response the application sent; `403` when it closed the connection before accepting; `500` when it raised or returned without answering |
| `close_code` | integer or `null` | The first close of the connection, sent by dockmin or received from the client. `1005` when the client's disconnect carries no code. `1000` when the application returned without a close. `1011` when it raised after the connection was accepted. `null` for a connection refused by an HTTP response or without a close |
| `duration_ms` | integer | Milliseconds from the start of the handshake until the application call ended |
| `address` | string | As in `http_request` |
| `user_id` | integer or `null` | As in `http_request` |
| `listener` | string | `redirect`, on lines written by the redirect listener. Absent on every other line |

- The line has no `method`.
- `request_id` is the id of the connection: the `X-Request-Id` header of the upgrade request when it is 1 to 64 letters, digits, `.`, `_` or `-`, otherwise a generated 32-digit hexadecimal id. The id is not sent back. The terminal's activity rows carry the same id.
- No ticket, header value or frame content is logged. The terminal ticket is sent as the first message, not in the path.

#### Redirect listener

Written only when `DOCKMIN_HTTP_MODE` is `redirect`, with `listener` set to `redirect`.

| Response | Line | `status` | `close_code` |
|---|---|---|---|
| `400`, another method or a `Host` that is not accepted | `http_request` | `400` | — |
| WebSocket handshake | `websocket_connection` | `403` | `1008` |
| `308` | None | — | — |

- `user_id` and `request_id` are `null`: the listener binds neither.
- `address` is the address of the connecting socket. The listener reads no forwarded header.
- In `serve` mode the plain listener runs the application and writes the lines of the other listeners, without `listener`.

#### Redaction

- Query strings are never logged.
- A webhook token in a path is written as `dkwh_<prefix>_[REDACTED]`, as in every line.

## Terminals and streams behind a reverse proxy

| Path | Requirement |
|---|---|
| `/environments/*/containers/*/terminal/ws` | Pass WebSocket upgrades: HTTP/1.1 to dockmin, with the `Upgrade` and `Connection` headers forwarded |
| `/environments/*/containers/*/stats/stream`, `/environments/*/containers/*/logs/stream`, `/environments/*/images/jobs/*/events`, `/environments/*/stacks/*/logs/stream`, `/environments/*/stacks/-/jobs/*/events`, `/environments/*/swarm/services/*/logs/stream`, `/environments/*/swarm/stacks/*/logs/stream`, `/environments/*/swarm/stacks/-/jobs/*/events` | Server-sent events. Do not buffer the response. dockmin sends `X-Accel-Buffering: no`; a proxy that ignores it needs response buffering turned off for these paths |

- The WebSocket handshake is refused (`403`) unless its `Origin` header names the host in the request's `Host` header, or is listed in `DOCKMIN_TRUSTED_ORIGINS`. A handshake with no `Origin` header is refused. A proxy that rewrites `Host` must either preserve the original or list the public origin.
- Each `DOCKMIN_TRUSTED_ORIGINS` entry is an origin such as `https://dockmin.example.com`, without a path, query or user name ([Configuration](Reference-Configuration#listeners-and-network)).
- A terminal, a stream or a download counts toward `DOCKMIN_MAX_CONCURRENT_REQUESTS` for as long as it is open.

Terminal limits and timeouts: [Containers](Usage-Containers#terminal-limits).

## Webhook calls behind a reverse proxy

Webhook calls ([Webhook calls](Reference-JSON-API#webhook-calls)) are `POST /api/v1/hooks/<webhook-token>` and `POST /api/v1/hooks`.

| Requirement | Detail |
|---|---|
| Scheme | Forward `X-Forwarded-Proto: https` from a proxy listed in `DOCKMIN_TRUSTED_PROXIES`. When dockmin has a certificate, a call that reaches the plain listener without it answers `400`, reason `use_https`. A proxy that connects to the HTTPS listener needs nothing more |
| Client address | Forward `X-Forwarded-For` from a listed proxy. Otherwise the address recorded as `caller_address` is the proxy's, and the per-address limit does not refuse ([Rate limit](#rate-limit)) |
| Query string | Do not add one. A call with a query string answers `400`, reason `query_not_supported` |
| `Authorization` | Pass it unchanged. The header form carries the token in it |
| Path | Pass `/api/v1/hooks` and `/api/v1/hooks/<webhook-token>` unchanged |
| Access log | The request path of the path form holds the token. Leave the path of `/api/v1/hooks/` out of the proxy's access log, or have senders use the header form. The access log of `examples/reverse-proxy/nginx` records the path |
| Caching | Responses carry `Cache-Control: no-store`, except `404`, which is the answer for any unknown `/api` path |
| Body size | A call needs no body. The proxy limit of 256 KiB or more applies as for any request ([Body size](#body-size)) |

- A proxy that terminates TLS and reaches dockmin on the plain listener with `DOCKMIN_TLS_SELF_SIGNED=false` has no certificate on dockmin, so the scheme check does not apply ([Automation](Operations-Automation)).
- In any dockmin log line, including the access log, the secret part of a webhook token is replaced with `[REDACTED]`.

## Security headers

Every HTTP response the application produces carries these headers: pages, fragments, JSON, static files, redirects, and every error status including `404`, `413`, `429`, `500` and `503`. The redirect listener sends them as well. Values set by a route are replaced.

| Header | Value | When |
|---|---|---|
| `Content-Security-Policy` | The policy chosen for the response ([below](#content-security-policy)) | Always |
| `X-Content-Type-Options` | `nosniff` | Always |
| `Referrer-Policy` | `same-origin` | Always |
| `X-Frame-Options` | `DENY` | Always |
| `Strict-Transport-Security` | `max-age=31536000` | Only when the request's scheme is `https`: served on the HTTPS listener with a supplied certificate, or forwarded as `https` by a trusted proxy. Never on the self-signed listener, so a proxy that connects to it sets the header itself. No `includeSubDomains`, no `preload` |

- A WebSocket handshake refusal carries no headers.
- No report endpoint exists. `Permissions-Policy`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy` are not sent.

### Content-Security-Policy

| Policy | Used for | Value |
|---|---|---|
| Default | Every response except the one below | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'` |
| Terminal | The page rendered by `POST /environments/{id}/containers/{container_id}/terminal` | The default policy plus `style-src-elem 'self' 'unsafe-inline'` |

- `script-src` is `'self'` in both policies: no inline script, no `eval`, no nonce, no hash.
- No page carries an inline `<script>`, an `on*` attribute or a `javascript:` URL.
- The terminal policy allows inline `<style>` elements, which xterm.js creates at run time. No other page loads xterm.js.
- `connect-src 'self'` admits the same-origin WebSocket of the terminal.
- `/docs`, `/redoc` and `/openapi.json` are not served (`404`).

## Request limits

Applied in front of routing, outermost first.

| Layer | Limit |
|---|---|
| Security headers | [Security headers](#security-headers) |
| Request id | Assigns the request id ([Request id](Reference-JSON-API#request-id)) |
| Send idle bound | [Send idle bound](#send-idle-bound) |
| Concurrency | [Concurrency](#concurrency) |
| Restore gate | [Backups](Administration-Backups) |
| Body size | [Body size](#body-size) |
| Rate limit | [Rate limit](#rate-limit). Runs with the application's dependencies, after the signed-in user is resolved |

An error from these layers is an HTML page, an `error_state` fragment for an HTMX request, or `{"error": {"status", "message"}}` under `/api`, by the path.

### Body size

| Item | Value |
|---|---|
| Cap | 256 KiB for every HTTP request with a body, whatever the method and content type |
| Declared `Content-Length` over the cap | `413`, nothing read |
| No length (chunked) | Bytes are counted as received. The read stops at the first message that passes the cap: `413` |
| Slow body | `408` with `Connection: close` when no body message arrives for 15 s, or the whole body takes more than 30 s |
| After the response has started | The same conditions end the request as a client disconnect |
| Exempt | `POST /restore/archive`. Its limits: [Backups](Administration-Backups#upload) |
| Own cap | `POST /environments/{id}/stacks/{name}/contexts/-/upload`: 16 MiB and 64 KiB, within 300 s. Its limits: [Stacks](Usage-Stacks#build-contexts) |
| Memory | Multipart parts under the cap stay in memory; nothing is written to `/tmp` |

Limits inside the cap are checked by the routes: a Compose file is at most 131,072 bytes and each PEM field at most 65,536 bytes.

### Concurrency

| Item | Value |
|---|---|
| Counted | HTTP requests and WebSocket sessions in flight, across both listeners, until the response or the socket is finished. A stream counts while it is open |
| Limit | `DOCKMIN_MAX_CONCURRENT_REQUESTS` ([Configuration](Reference-Configuration#listeners-and-network)) |
| Over the limit, HTTP | `503` with `Retry-After: 1` |
| Over the limit, WebSocket | The handshake is refused before it is accepted, with close code 1013 |
| Exempt | `/api/health`. `/api/ready` is counted |
| Not counted | The redirect listener |
| Logged | `concurrency_limit_reached` with `limit`, at most once a minute |

### Rate limit

An address is limited on requests that resolve to no signed-in user.

| Item | Value |
|---|---|
| Counted | A request or WebSocket handshake with no session and no valid API token. One token from the address's bucket. A webhook call is counted whatever its token |
| Not counted | Authenticated requests; `/static/`; `/api/health` and `/api/ready`; a request that matches no route (`404`) or whose body fails to parse (`422`), because no dependency runs for them |
| Burst | 60 |
| Refill | 1 token a second |
| Key | The client address. An IPv6 address is keyed by its `/64`; an IPv4-mapped IPv6 address by its IPv4 address |
| Tracked addresses | At most 10,000. The least recently used is dropped |
| Empty bucket, HTTP | `429` with `Retry-After` in seconds |
| Empty bucket, WebSocket | The handshake is refused (close code 1008) |
| Order | Setup gate, sign-in resolution, rate limit, CSRF. A request refused for CSRF has been counted |

The limit refuses requests only where client addresses are per-client:

| Deployment | Refuses |
|---|---|
| `DOCKMIN_TRUSTED_PROXIES` set | Yes, on both listeners |
| No trusted proxies, request on dockmin's own HTTPS listener | Yes |
| No trusted proxies, request on the plain listener serving the application (`DOCKMIN_HTTP_MODE=serve`, or no TLS) | No |
| Redirect listener | Not applicable: it serves no application |

Where it does not refuse:

- The bucket is still counted. An empty bucket is logged as `rate_limit_exceeded` with `enforced: false`, at most once a minute.
- At start, `rate_limit_not_enforced` with `listener: "http"` is logged when the plain listener serves the application and no proxy is trusted.

Where every client reaches the HTTPS listener from one address, such as Docker's userland proxy or rootless Docker, all anonymous clients share one bucket and an empty bucket refuses all of them. No setting turns the limit off on the HTTPS listener.

### Send idle bound

| Item | Value |
|---|---|
| Rule | Each response body chunk must be accepted by the client within 60 s |
| On expiry | The application is stopped, so the response's cleanup runs (export slot, database session, backup work directory, Docker streams). The connection is closed without completing the response |
| Logged | `response_send_stalled` with `path` (the route template, or `unmatched`) and `bytes_sent`, once per stalled response |
| Covers | Every streamed response: downloads, exports, log files, server-sent events |
| Does not cover | WebSocket sends |
| TCP timeout | On Linux, the listening sockets set `TCP_USER_TIMEOUT` to 120 s: a connection whose sent data stays unacknowledged for 120 s is closed |

## Engine reply limits

| Reply | Limit |
|---|---|
| Container list, Swarm config list | 32 MiB |
| Image, network, volume, Swarm service, task and secret lists | 16 MiB |
| Node list | 4 MiB |
| Container, image, network, volume, Swarm service and config detail | 4 MiB |
| Swarm, task, node and secret detail, image history | 1 MiB |
| Action reply, API version negotiation | 64 KiB |
| Engine error body parsed for a message | 64 KiB |
| One line of the event stream | 256 KiB |
| Decoded size of a list, estimated | 128 MiB |
| List replies read and decoded at once, per process | 2 |

A list over a limit is not shown; the page reads `This host reports more <kind> data than can be listed here (limit N MiB).` The Swarm list message ends after `here.` without the limit.

## Single instance

One dockmin process runs against a data directory. A second process on the same directory refuses to start.

| Item | Value |
|---|---|
| Lock file | `<DOCKMIN_DATA_DIR>/dockmin.lock` (`/data/dockmin.lock` in the image) |
| Mechanism | An exclusive, non-blocking `flock` held from start until shutdown or process exit |
| Contents | Empty. Nothing is written to it |
| Created with | Mode `0600`. A file with another mode is reset to `0600`. A symbolic link is not followed |
| Required of the file | A regular file, owned by the user dockmin runs as, with one hard link |
| Taken | Before the database is opened. Released last at shutdown |
| Taken by | `python -m app restore`, which exits `3` while another process holds the lock ([Backups](Administration-Backups#restore-from-the-command-line)) |
| Not taken by | `python -m app clear-lockout` ([Teams and users](Usage-Teams-and-Users)) |

| Situation | Result |
|---|---|
| Another process holds the lock | The new process exits with status `3`. Logged as `instance_already_running` with the path, then `startup_check_failed` (check `data_directory`). The running process is unaffected |
| The path is a symbolic link, is not a regular file, or cannot be opened | The process does not start (status `3`). Logged as `instance_lock_failed` with the path and errno, then `startup_check_failed` |
| The file has more than one hard link, or another user owns it | The process does not start (status `3`). Logged as `instance_lock_unsafe` with the path, then `startup_check_failed` |
| The data directory is writable by its group or by others | The process starts. Logged as `data_dir_writable_by_others` at warning level, with the path |
| The platform has no `fcntl` | The process starts without the lock. Logged as `instance_lock_unavailable`. The image runs on Linux and is not affected |

- Do not mount one data volume into two dockmin containers.
- The lock is released when the process exits, however it exits. A stale `dockmin.lock` file left on disk does not block a start.

To clear the `data_dir_writable_by_others` warning for a data directory on the host:

1. Remove the write permission of the group and of others:

   ```bash
   chmod go-w <data-directory>
   ```

2. Restart the container. The log no longer holds `data_dir_writable_by_others` after the next start. When it still does, check the mode of the directory with `stat -c %a <data-directory>`: the last two digits must not include `2`, `3`, `6` or `7`.

## Startup and shutdown order

| Start | Step |
|---|---|
| 0 | The launcher loads the settings (exit `2` on failure) and configures logging. With a self-signed certificate it checks the data directory and creates or reuses the pair in `server-tls/` (exit `3` on failure). It loads the TLS files (exit `3` on failure) and builds the application |
| 1 | Data directory check, then the instance lock. Then the warnings `data_directory_not_mounted` (generated key, data directory not a mount point), `rate_limit_not_enforced` and `cookie_secure_disabled`, when they apply |
| 2 | Leftovers of a restore are swept: `backup-work/` is deleted, and each `restore/<restore-id>/` without `previous/` or `rejected/` is deleted |
| 3 | The address policy is set. The SQLite temporary directory is created. The database is opened and migrated |
| 4 | The Docker connection work directory is prepared. The master keys are resolved: read from the variable or the file, or, with a generated key, read from the key directory or created there for a new database ([Encryption](Administration-Encryption#key-directory)). Encryption key check. The password hasher is prepared. With no administrator, the one-time setup value is written to the log |
| 5 | Compose work directory and `stacks/` are prepared, and the Compose and buildx binaries are located. Stack directories with no stack are removed. Docker Engine access, then the Compose version is read, then registries. Then the container dockmin runs in is identified ([Instance](Administration-Instance)). Updates that finished while dockmin was stopped are recorded |
| 6 | `development_checks_enabled` when set. Scheduler: subsystems registered, the snapshot refresher's job created or updated, recent user actions reloaded, runs left in progress recorded `interrupted`, due jobs served, loop started |

`application_started` is logged when step 6 has finished ([Application started](#application-started)).

| Shutdown | Step |
|---|---|
| 0a | With two listeners, the primary listener closes its listening sockets and accepts no new connection. Connections it already holds stay open |
| 0b | The secondary listener stops: the plain listener when TLS is on and `DOCKMIN_HTTP_MODE` is `serve` or `redirect`. Open connections are given 10 s. Step 0c begins when it has finished |
| 0c | The primary listener drains: the HTTPS listener when TLS is on, else the plain listener. Open connections are given 10 s. It owns the application lifespan, so steps 1 to 6 follow its stop |
| 1 | A running encryption check or re-encryption is stopped. A restore in progress is stopped and ended with reason `shutdown` ([Backups](Administration-Backups#ending-a-restore)) |
| 2 | The update dispatcher, the health event watchers, the container identification watch and the notification poker are stopped. Scheduler: every running run is cancelled and recorded `interrupted`, reason `shutdown`. Waits up to 10 s |
| 3 | Image jobs and stack jobs close together; then Compose |
| 4 | Registries, Docker Engine access |
| 5 | Database |
| 6 | Instance lock. `application_stopped` is logged afterwards |

- A request on the plain listener finishes or is cancelled before the primary listener is told to stop.
- The secondary listener runs no application lifespan. `DOCKMIN_HTTP_MODE` `off`, and a deployment with TLS off, have no secondary listener.
- The order is the same when a listener ends by itself: the secondary stops first, then the primary.
- Steps 0c to 6 run even when stopping the secondary listener failed. The original error is still raised after they have run.
- A second `SIGINT` sets both listeners to exit at once. A primary listener forced out before its lifespan shutdown starts does not run steps 1 to 6: the database is not closed in order and the scheduler does not record its runs. Those runs are recorded `interrupted` at the next start.
- A step that fails is logged as `shutdown_step_failed` with `step` and `error`; the steps after it still run.
- A run not recorded within the 10 s, or lost to a crash, is recorded `interrupted` with reason `process_stopped` at the next start ([Jobs](Administration-Jobs#heartbeat-shutdown-and-restart)).
- Compose stops a container after 10 s by default. The shipped `compose.yaml` sets `stop_grace_period: 45s`. A container killed before the scheduler has recorded its runs leaves them for the next start.

## Startup self-check

A failed precondition is one log line, `startup_check_failed`, and no traceback. Fields:

| Field | Value |
|---|---|
| `check` | `encryption_key`, `database`, `data_directory`, `compose_cli`, `buildx`, `tls_material` or `jwt_settings` |
| `problem` | What is wrong |
| `remedy` | What to change |
| `fatal` | `true` when the process does not start |
| `during` | `startup`, or `restore` when a restore restarts the runtime |

The line is logged at error level when fatal and at warning level otherwise. A check that fails while a restore restarts the runtime is logged with `fatal: false`; the process keeps running ([Backups](Administration-Backups#restore)). A start that fails a check also logs uvicorn's `Application startup failed. Exiting.` Any other startup exception keeps its traceback.

| Check | Fails when | Exit |
|---|---|---|
| `encryption_key` | Both `DOCKMIN_SECRET_KEY` and `DOCKMIN_SECRET_KEY_FILE` are set; the value is not base64 of 32 bytes; the file named by `DOCKMIN_SECRET_KEY_FILE` is unreadable | `2` |
| `encryption_key` | Both `DOCKMIN_PREVIOUS_SECRET_KEY` and `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` are set; the previous value is not base64 of 32 bytes; the file named by `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is unreadable; the previous key equals a supplied current key | `2` |
| `encryption_key` | Neither variable is set and `DOCKMIN_SECRET_KEY_DIR` is relative, or equals, contains or lies inside `DOCKMIN_DATA_DIR` | `2` |
| `encryption_key` | The configured key does not open the database | `3` |
| `encryption_key` | Neither variable is set, `secret.key` does not exist, and the database holds encrypted data or a `crypto.*` setting row, data kept by an earlier restore exists under `restore/`, or `DOCKMIN_PREVIOUS_SECRET_KEY` or `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is set. No key is generated | `3` |
| `encryption_key` | Neither variable is set, `secret.key` does not exist, and the key directory is missing, is not a directory, is a symbolic link, is the data directory or inside it, is not a mount point, is a `tmpfs` or `ramfs` mount, is not owned by the process user, is writable by its group or others, or is not writable. No key is generated | `3` |
| `encryption_key` | `secret.key` exists and is a symbolic link, is not a regular file, cannot be read, or does not hold base64 of 32 bytes. The file is not replaced | `3` |
| `encryption_key` | With a generated key, the previous key equals the key in `secret.key` | `3` |
| `database` | The schema is newer than this build. `problem`: `The database schema check failed: This database was written by a newer dockmin (schema N; this build supports M). Run that version or restore a backup.` The database is not modified | `3` |
| `database` | The schema ledger has a gap, an applied migration file is missing or has another name, or a migration file name does not match `NNNN_name.sql`. Migration files are compared with the ledger by version and name only | `3` |
| `database` | The database cannot be opened, is damaged, cannot be created or read, or cannot be read while the encryption key is checked | `3` |
| `data_directory` | The directory is missing, is not a directory, is a symbolic link, or is not writable by the process user | `3` |
| `data_directory` | Another process holds the instance lock, or the lock file is unusable | `3` |
| `data_directory` | `SQLITE_TMPDIR` cannot be created, or the Docker connection work directory, the Compose work directory or `stacks/` cannot be prepared | `3` |
| `compose_cli` | `DOCKMIN_COMPOSE_BINARY` fails the [binary requirements](#binary-location) | `3` |
| `compose_cli` | No binary found, the version cannot be read, or the version is below v2.32.0. Not fatal: the process starts and every Compose operation is refused | — |
| `buildx` | No trusted buildx binary found ([Buildx binary](#buildx-binary)). Not fatal: the process starts, and a deploy that would build an image is refused (`buildx_unavailable`) | — |
| `tls_material` | A TLS file is unreadable, holds no PEM certificate, is not an unencrypted PEM key, or the key does not match the first certificate | `3` |
| `tls_material` | Only one of the two TLS variables is set | `2` |
| `tls_material` | The self-signed pair cannot be stored: `server-tls` is a symbolic link or not a directory, or the directory or its files cannot be created or written | `3` |
| `jwt_settings` | An access platform setting is set without `DOCKMIN_JWT_HEADER`; the header is set without an issuer, an audience or exactly one key source; a value is malformed; the key file is unreadable, empty or holds no usable key ([Configuration](Reference-Configuration#access-platform)) | `2` |

The `remedy` field of each line names the change to make. The exact `problem` and `remedy` texts, with causes and actions for each check: [Troubleshooting](Troubleshooting).

A configuration error that is none of these is logged as `configuration_invalid` with `problem`, and exits `2`. That includes `DOCKMIN_HTTPS_PORT` equal to `DOCKMIN_PORT`, `DOCKMIN_HTTP_MODE` set to `redirect` or `off` with TLS off, and a `DOCKMIN_TLS_SELF_SIGNED` value that is not a boolean.

## Logging events

| Event | Level | Fields |
|---|---|---|
| `startup_check_failed` | error, warning | `check`, `problem`, `remedy`, `fatal`, `during` |
| `configuration_invalid` | error | `problem` |
| `tls_certificate_expiring` | warning | `days_left`, `subject`, `not_before`, `not_after` |
| `tls_certificate_invalid_now` | warning | `state`, `subject`, `not_before`, `not_after` |
| `tls_certificate_self_signed` | info | `sha256_fingerprint`, `not_after`, `generated`, `reason` |
| `encryption_key_generated` | warning | `key_file`, `key_fingerprint`, `note` |
| `encryption_key_loaded` | info | `key_version`, `key_fingerprint` |
| `encryption_key_file_permissions` | warning | `key_file`, `mode` |
| `data_directory_not_mounted` | warning | `path`, `note` |
| `data_dir_writable_by_others` | warning | `path` |
| `instance_already_running` | error | `path` |
| `instance_lock_failed` | error | `path`, `errno` |
| `instance_lock_unsafe` | error | `path` |
| `instance_lock_unavailable` | warning | — |
| `migration_applied` | info | `migration` |
| `compose_detected` | info | `version` |
| `compose_binary_not_found`, `compose_binary_untrusted` | warning | `path` on the untrusted line |
| `compose_below_floor` | warning | `installed`, `required` |
| `compose_version_unreadable` | warning | `error` |
| `buildx_binary_untrusted`, `buildx_binary_unusable` | warning | `path` |
| `concurrency_limit_reached` | warning | `limit` |
| `rate_limit_exceeded` | warning | `enforced` |
| `rate_limit_not_enforced` | warning | `listener` |
| `webhook_refused` | warning | `counts`: the refused calls by answer since the last line, at most one line a minute |
| `jwt_refused` | warning | `reason`; at most one line a minute for each reason ([Teams and users](Usage-Teams-and-Users)) |
| `jwks_fetch_failed` | warning | `reason` |
| `cookie_secure_disabled` | warning | — |
| `response_send_stalled` | warning | `path`, `bytes_sent` |
| `response_cleanup_failed` | warning | `error` |
| `limit_response_render_failed` | error | `error` |
| `readiness_check_failed` | error | `check`, with a traceback |
| `unhandled_exception` | error | A traceback; the request id is on the line |
| `outbound_address_refused` | warning | `address` |
| `self_identity_resolved` | info | `source`, `verified_environments`, `restartable`, `unreadable_environments` |
| `self_identity_resolution_failed`, `self_identity_probe_failed` | warning | `error` |
| `instance_restart_failed` | error | `environment_id`, `error` |
| `instance_restart_sent` | info | `environment_id`, `error` |
| `instance_restart_unconfirmed` | warning | `environment_id`, `error` |
| `shutdown_step_failed` | error | `step`, `error` |
| `application_started` | info | `version`, `revision`, `instance_name`, `http_mode`, `https_port`, `http_port`, `access_log` ([Application started](#application-started)) |
| `application_stopped` | info | — |
| `login_succeeded` | info | `user_id`, `username`, `address`, `method` ([Sign-in lines](#sign-in-lines)) |
| `login_failed` | warning | `user_id`, `reason`, `address`, `method`, and `username` when the sign-in was a password one and `user_id` is set |
| `login_refused` | warning | `reason`, `address`, `method`, and `user_id` and `username` when the account exists |
| `logout_succeeded` | info | `user_id`, `username`, `address`, `method`, and `reason` when an access platform session ended |
| `http_request` | info | `method`, `path`, `status`, `duration_ms`, `address`, `user_id`, and `listener` on the redirect listener; only when `DOCKMIN_ACCESS_LOG` is `true` ([Access log](#access-log)) |
| `websocket_connection` | info | `path`, `status`, `close_code`, `duration_ms`, `address`, `user_id`, and `listener` on the redirect listener; only when `DOCKMIN_ACCESS_LOG` is `true` ([Access log](#access-log)) |

- `httpx`, `httpcore` and the `websockets` loggers log at `WARNING` or above whatever the log level: at lower levels they log full request URLs or frame contents.
- Secrets are scrubbed from every line before it is written.

### Line format

Every line is one JSON object on stdout, ending in a newline.

| Key | Content |
|---|---|
| `timestamp` | UTC, ISO 8601 with the offset `+00:00`. The fractional seconds are omitted when zero |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `logger` | The logger's name. Lines of uvicorn's `uvicorn.error` logger are written with `uvicorn.server` |
| `message` | The event name of the tables on this page, or the library's own text |
| `request_id` | The id of the HTTP request or WebSocket connection being served ([Request id](Reference-JSON-API#request-id)). `null` outside a request and on every line of the redirect listener |
| Event fields | The fields of the event, after `request_id` |
| `exception` | The formatted traceback, when the line carries one |

- Keys appear in the order above.
- `color_message`, which uvicorn adds to its lines, is not written.
- The characters ESC (U+001B) and CSI (U+009B) in a string field value are written as the text `\u001b` and `\u009b`.
- Secrets are scrubbed before the line is serialised ([Logging events](#logging-events)).
- Parsing the lines with the other logs dockmin writes: [Activity](Administration-Activity).
- The lines have no `schema_version`. For a record family that has one, it changes only when an existing field changes meaning or is removed. A new event or a new optional field does not change it: `websocket_connection` and `listener` are additions of this kind.

### Application started

`application_started` is written once, when the application has finished starting.

| Field | Type | Content |
|---|---|---|
| `version` | string | The product version |
| `revision` | string or `null` | The 40-character commit the image was built from, read from `/usr/share/dockmin/revision`. `null` when the file is absent, unreadable, or not 40 lowercase hexadecimal characters |
| `instance_name` | string | `DOCKMIN_INSTANCE_NAME` |
| `http_mode` | string | The plain listener's effective mode: `serve`, `redirect` or `off` ([HTTP modes](#http-modes)) |
| `https_port` | integer or `null` | The HTTPS listener's port. `null` when TLS is off |
| `http_port` | integer or `null` | The plain listener's port. `null` when `http_mode` is `off` |
| `access_log` | boolean | The value of `DOCKMIN_ACCESS_LOG` |

### Sign-in lines

| Line | Written when | Fields |
|---|---|---|
| `login_succeeded` | A sign-in succeeded | `user_id`, `username`, `address`, `method` |
| `login_failed` | A sign-in was judged and failed | `user_id`, `reason`, `address`, `method`. A password sign-in adds `username` when `user_id` is set |
| `login_refused` | A sign-in was refused before the password was judged | `reason`, `address`, `method`. `user_id` and `username` are added when the submitted name belongs to an account |
| `logout_succeeded` | A user signed out, or an access platform session ended | `user_id`, `username`, `address`, `method`. An ended access platform session adds `reason` |

- `method` is `password` or `jwt`.
- `login_failed` `reason` for a password sign-in: `unknown_user`, `bad_password`, `account_disabled` or `password_sign_in_disabled`. `user_id` is `null` for `unknown_user`.
- `login_failed` `reason` for an access platform sign-in: `jwt_unlinked`, `jwt_account_disabled`, `jwt_username_invalid` or `jwt_username_taken`. `user_id` is set when the account is known. The line carries no `username`. It is written once for each distinct token, like the activity row.
- `login_refused` `reason`: `locked_out` or `server_busy`.
- `logout_succeeded` `reason` for an ended access platform session: `platform_assertion_ended` or `platform_identity_changed`.
- `username` is the account's stored name, read by `user_id`. The name typed on the sign-in form is never logged: a wrong password is often typed into that field.
- An attempt on a name that belongs to no account logs `reason`, `address` and `method` only, and for `login_failed` a `null` `user_id`.
- `address` is the client address after the trusted proxy headers are applied ([Behind a reverse proxy](#behind-a-reverse-proxy)).
- The request's `request_id` is on each line. The activity rows of the same request carry the same id ([Activity](Administration-Activity#request-id)).

### Access log

`DOCKMIN_ACCESS_LOG` is `false` by default ([Configuration](Reference-Configuration#logging-and-diagnostics)). When `true`, dockmin writes one `http_request` line for each HTTP request.

| Item | Value |
|---|---|
| Logger, level | `dockmin.access`, `INFO`. Written only when `DOCKMIN_LOG_LEVEL` is `INFO` or lower |
| Written | After the response has been sent, or after the application raised |
| Not logged | `/api/health` and `/api/ready`; WebSocket connections; requests to the plain listener when `DOCKMIN_HTTP_MODE` is `redirect` |
| Covered | Every answer of the application, including `413`, `429` and `503` from the request limits |

| Field | Type | Content |
|---|---|---|
| `method` | string | The HTTP method |
| `path` | string | The request path, without the query string, cut at 1,024 characters with `…` appended. A restore progress path is written as `/restore/progress/[REDACTED]` |
| `status` | integer | The status sent. `500` when the application raised before it sent one |
| `duration_ms` | integer | Milliseconds from the start of the request until the response was sent or the application raised |
| `address` | string | The client address after the trusted proxy headers are applied. `unknown` when there is none |
| `user_id` | integer or `null` | The user of the session or API token. `null` for an anonymous request and for a webhook call |

- A standard user who is answered `404` on an administrator route keeps their `user_id` on the line.
- A webhook token in a path is written as `dkwh_<prefix>_[REDACTED]`, as in every line.
- Query strings are never logged.

## Development checks

`DOCKMIN_DEVELOPMENT_CHECKS` turns on diagnostics for development ([Configuration](Reference-Configuration#logging-and-diagnostics)).

- When `true`, a failed job run logs `job_run_failed` with the error's traceback. When `false` the line carries `run_id`, `job_id` and the error's class name only.
- When `true`, dockmin logs `development_checks_enabled` at warning level at every start.
- When `true`, the component sample page `/ui-kit` and its `UI kit` link are served to signed-in users. When `false` the page answers `404`.
- Leave it unset in production. A traceback can contain text the failing code handled.
- The shipped `compose.yaml` does not pass the variable.

## The image

The published image is run as described in [Quick start](Getting-Started-Quick-Start).

To build the image from the repository root:

1. Build it:

   ```bash
   docker build -t dockmin:local .
   ```

   The build ends with a line that names `dockmin:local`. To set the version and revision, add `--build-arg DOCKMIN_VERSION=<version>` and `--build-arg DOCKMIN_REVISION=<40-character-commit>`.

2. Read the version label:

   ```bash
   docker image inspect dockmin:local --format '{{ index .Config.Labels "org.opencontainers.image.version" }}'
   ```

   The command prints `0+local` when no version was passed. When the build fails, check network access to Docker Hub and the Python package index, and that BuildKit is enabled ([Prerequisites](Getting-Started-Prerequisites)).

| Item | Value |
|---|---|
| Base | `python:3.12-slim`, pinned by digest. Debian root certificates at `/etc/ssl/certs/ca-certificates.crt` |
| User | `dockmin`, uid and gid `10001`. `USER 10001:10001` |
| Data directory | `/data`, owned by uid 10001, mode `0700` |
| Key directory | `/key`, owned by uid 10001, mode `0700`. A named volume mounted there takes the owner and mode when it is first mounted. A bind mount keeps its host owner and mode ([Encryption](Administration-Encryption#key-directory)) |
| Volumes | None declared. `/data` and `/key` are mounted by the command that runs the image |
| Special permission bits | No file has the setuid or setgid bit. No path is group-writable, except sticky directories and symbolic links |
| Platforms | `linux/amd64` and `linux/arm64`, as built by the release workflow |
| Python environment | `/opt/venv`, first on `PATH`. Dependencies installed from `requirements.lock` with hashes and binary wheels only. The project is installed as a wheel built with build tools from `build-requirements.lock`, so its version is the `DOCKMIN_VERSION` build argument: the release version in a published image, `0+local` by default |
| Revision | `/usr/share/dockmin/revision`, owned by root, mode `0644`, holding the `DOCKMIN_REVISION` build argument. Absent when the argument is not 40 lowercase hexadecimal characters |
| Package tools | `pip` is removed. The Python environment has none. No compiler, no `docker` CLI |
| Environment | `DOCKMIN_DATA_DIR=/data`, `SQLITE_TMPDIR=/data/sqlite-tmp`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` |
| Ports | `EXPOSE 8000 8443` |
| Command | `["python", "-m", "app.serving"]` |
| Health check | The `app.serving.healthcheck` module, every 30 s, timeout 5 s, start period 15 s, 3 retries |
| Labels | `org.opencontainers.image.title`, `org.opencontainers.image.version`, `org.opencontainers.image.licenses` |

| Binary | Version | Path | Licence files |
|---|---|---|---|
| Docker Compose | v5.5.1 | `/usr/local/libexec/docker/cli-plugins/docker-compose` | `/usr/share/licenses/docker-compose/` |
| Docker buildx | 0.38.2 | `/usr/local/libexec/docker/cli-plugins/docker-buildx` | `/usr/share/licenses/buildx/` |

- Each binary belongs to `root`, mode `0755`, and is run directly by dockmin. The image has no `docker` CLI.
- The buildx binary is 68,374,690 bytes on `linux/amd64` and 63,766,690 bytes on `linux/arm64`. The image is larger by that size.
- buildx runs only for [stack builds](Usage-Stacks#builds). A server that never builds stacks still carries it. Outside the image, `DOCKMIN_BUILDX_BINARY` points at another binary ([Buildx binary](#buildx-binary)).
- Writable paths with a read-only root filesystem: `/data` (a volume), `/key` (a volume, written when the key is generated or replaced) and `/tmp` (a `tmpfs`). Without a volume at `/data`, or at `/key` when the key is generated, a read-only root filesystem makes the start fail. SQLite temporary files go to `/data/sqlite-tmp`, created at start with mode `0700`.
- Compose work directories are per invocation, and per image build, under `/data/compose-work`.

### Health check

| Item | Value |
|---|---|
| Probe | `GET /api/health` on `DOCKMIN_HOST`, with `0.0.0.0` read as `127.0.0.1` and `::` as `::1`. Timeout 4 s |
| Listener | The HTTPS listener, with the certificate not verified, when both `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE` are set in the environment, or when `DOCKMIN_TLS_SELF_SIGNED` is unset, blank, or not one of the false spellings below; otherwise the plain listener |
| Port | `DOCKMIN_HTTPS_PORT` or `DOCKMIN_PORT`, by the listener |
| Result | Exit `0` on `200`, else `1` |
| False spellings of `DOCKMIN_TLS_SELF_SIGNED` | `0`, `false`, `f`, `no`, `n`, `off`, in any case, with surrounding whitespace ignored |
| Input | The process environment only. The probe reads no settings file and no other variable than `DOCKMIN_HOST`, `DOCKMIN_PORT`, `DOCKMIN_HTTPS_PORT` and the three TLS variables. Variable names are read in any case, as the settings read them |

## Docker Compose

Stacks need the Docker Compose v2 CLI on the dockmin server ([Stacks](Usage-Stacks)).

| Requirement | Value |
|---|---|
| Version | v2.32.0 or later |
| Form | The standalone Compose plugin binary (`docker-compose`), run directly with an argument list. The Docker CLI and its Compose subcommand are not used |
| The image | Carries the binary at `/usr/local/libexec/docker/cli-plugins/docker-compose`, v5.5.1 |
| Connections | Compose connects from the dockmin server to the environment's host: the local socket, or TCP with the environment's TLS material. The host of a TCP environment passes the [address policy](Reference-Security#address-policy) first. Compose contacts no registry; the host pulls images ([Stacks](Usage-Stacks#images-and-credentials)). Proxy variables are not set |

### Binary location

The binary is located once at start and run by its absolute path. `PATH` is not consulted again.

| Order | Source |
|---|---|
| 1 | `DOCKMIN_COMPOSE_BINARY`, when set |
| 2 | The first that exists of `/usr/local/libexec/docker/cli-plugins/docker-compose`, `/usr/libexec/docker/cli-plugins/docker-compose`, `/usr/local/lib/docker/cli-plugins/docker-compose`, `/usr/lib/docker/cli-plugins/docker-compose` |
| 3 | `docker-compose` on the `PATH` of the dockmin process |

| Requirement | Detail |
|---|---|
| Path | Absolute, to a regular executable file. Symbolic links are followed; the checks below apply to the target and its directory |
| Ownership | The file and its directory belong to `root` or to the user dockmin runs as |
| Mode | Neither the file nor its directory is writable by its group or by others |

| Situation | Result |
|---|---|
| `DOCKMIN_COMPOSE_BINARY` fails the requirements | dockmin does not start (status `3`). Logged as `startup_check_failed` |
| A path in the search fails the ownership or mode requirement | It is skipped. Logged as `compose_binary_untrusted` with the path; the search continues |
| No binary found | dockmin starts. Logged as `compose_binary_not_found` and `startup_check_failed` (not fatal). Stack pages render; every Compose operation is refused |
| Version older than v2.32.0 | dockmin starts. Logged as `compose_below_floor` with `installed` and `required`, and `startup_check_failed` (not fatal). Every Compose operation is refused |
| Version cannot be read | dockmin starts. Logged as `compose_version_unreadable` and `startup_check_failed` (not fatal). The next page load or operation tries again, at most once a minute |
| Version read | Logged as `compose_detected` |

The version is read once. Replacing the binary needs a restart.

### Compose settings

`DOCKMIN_COMPOSE_BINARY` and `DOCKMIN_COMPOSE_TIMEOUT_SECONDS` ([Configuration](Reference-Configuration#docker-compose)).

- The shipped `compose.yaml` passes neither variable. Set them in an override file ([Setting a variable `compose.yaml` does not pass](Reference-Configuration#setting-a-variable-composeyaml-does-not-pass)).
- `DOCKMIN_COMPOSE_TIMEOUT_SECONDS` limits one stack deploy, redeploy, start, stop or remove, covering its image pulls and its Compose command.
- At the time limit the Compose process group receives `SIGTERM`, then `SIGKILL` after 10 seconds. The stack's result is `outcome_unknown` ([Stacks](Usage-Stacks#errors)).
- Version, check and container reads have fixed limits of 10, 30 and 30 seconds. At most 4 Compose processes run at once.

### Buildx binary

Stack builds need the buildx binary on the dockmin server ([Stacks](Usage-Stacks#builds)).

| Requirement | Value |
|---|---|
| Form | The standalone buildx plugin binary (`docker-buildx`), run directly with an argument list as `docker-buildx build …`. The Docker CLI is not used |
| Version | No floor is checked. The image carries 0.38.2 |
| The image | Carries the binary at `/usr/local/libexec/docker/cli-plugins/docker-buildx` |
| Connections | buildx connects from the dockmin server to the environment's host, as Compose does ([Docker Compose](#docker-compose)). It contacts no registry: dockmin pulls the base images. Proxy variables are not set |

The binary is located once at start and run by its absolute path. The requirements of [Binary location](#binary-location) apply: an absolute path to a regular executable file, owned with its directory by `root` or the dockmin user, writable by neither group nor others.

| Order | Source |
|---|---|
| 1 | `DOCKMIN_BUILDX_BINARY`, when set |
| 2 | The first that exists of `/usr/local/libexec/docker/cli-plugins/docker-buildx`, `/usr/libexec/docker/cli-plugins/docker-buildx`, `/usr/local/lib/docker/cli-plugins/docker-buildx`, `/usr/lib/docker/cli-plugins/docker-buildx` |
| 3 | `docker-buildx` on the `PATH` of the dockmin process |

| Situation | Result |
|---|---|
| A path in the search fails the ownership or mode requirement | It is skipped. Logged as `buildx_binary_untrusted` with the path; the search continues |
| `DOCKMIN_BUILDX_BINARY` is missing or fails the requirements | It is not replaced by a search. Logged as `buildx_binary_unusable` with the path, and `buildx_binary_untrusted` when the cause is ownership or mode |
| No usable binary | dockmin starts. Logged as `startup_check_failed` with `check` `buildx` (not fatal). Stack pages render, and every other stack operation works. A deploy or redeploy that would build an image is refused (`buildx_unavailable`); one that reuses every built image proceeds |

- `DOCKMIN_BUILDX_BINARY` ([Configuration](Reference-Configuration#docker-compose)) never stops the start, unlike `DOCKMIN_COMPOSE_BINARY`.
- Replacing the binary needs a restart.
- A build runs for at most 30 minutes. The operation's time limit grows by 30 minutes for each image it builds ([Stacks](Usage-Stacks#how-a-deploy-builds)).
- At most 2 builds run at once.

### Data directory

dockmin creates three directories beneath `DOCKMIN_DATA_DIR` (`/data` in the image) at start. Start fails when any of them is a symbolic link. A self-signed certificate adds `server-tls/`, and backups and restores create two more on demand.

| Path | Mode | Content |
|---|---|---|
| `stacks/` | `0700` | One `0700` directory per stack, named by 32 random hexadecimal digits, holding `compose.yaml` (`0600`). The working copy of the stack's file; the database holds the authoritative one. Directories with no stack are removed at start |
| `compose-work/` | `0700` | One directory per running Compose invocation and per running image build. For a remote environment it holds the CA, client certificate and decrypted client key (`0600`) while the process runs. A build's directory also holds the extracted build context. The directory is deleted when the invocation or build ends, and everything in `compose-work/` is deleted at start |
| `tls-work/` | `0700` | A short-lived directory for each Docker connection that loads a client identity: the client certificate and the key re-encrypted under a random passphrase, in `0600` files. Deleted as soon as the identity is loaded |

- All three directories must be on a filesystem the dockmin user can write, and are not shared with other users.
- `stacks/` holds Compose files only. Variable values are never written to disk outside the database.
- Details: [Stacks](Usage-Stacks#files-on-the-server).

| Path | Mode | Content |
|---|---|---|
| `dockmin.db`, `dockmin.db-wal`, `dockmin.db-shm` | — | The database and its journal files |
| `dockmin.lock` | `0600` | The instance lock ([Single instance](#single-instance)) |
| `server-tls/` | `0700` | The self-signed certificate and its key ([Self-signed certificate](Administration-Certificate#self-signed-certificate)). Created at start only when the certificate is self-signed |
| `sqlite-tmp/` | `0700` | SQLite temporary files, when `SQLITE_TMPDIR` names it (the image does). Created at start |
| `backup-work/` | `0700` | One directory per archive being produced, holding the snapshot and the finished archive. Removed when the download or upload ends, and deleted whole at every process start |
| `restore/` | `0700` | One directory per restore, named by the restore id: the uploaded or fetched archive (`archive.tar.gz` for a command-line restore), `staging/`, and after a swap `previous/` (the replaced data) and `rejected/` (restored files that did not start). Deleted at the restore's end or at start, except a directory holding `previous/` or `rejected/`, which is kept and logged as `restore_previous_data_present`. The product never deletes `previous/` or `rejected/` ([Backups](Administration-Backups#paths-and-previous-data)) |

- `backup-work/` and `restore/` must be on the same filesystem as `dockmin.db` and `stacks/`. A restore moves data with renames.

### Free space

| Operation | Needs free on the data volume |
|---|---|
| Producing an archive | 2 × (database size + write-ahead log size) + 64 MiB, plus the archive itself |
| Receiving a restore upload, or fetching an archive from a bucket | The archive's size, and at least 256 MiB left at every write |
| Restoring from the command line | The archive's size plus 256 MiB for the copy the command makes, then the staging space below |
| Staging a restore | 2 × the total size of the archive's members + 64 MiB |
| A restore that completes | The uploaded archive, the staged copy, and the replaced data kept in `previous/` |

### Reverse proxy limits for backups

| Path | Requirement |
|---|---|
| `POST /restore/archive` | Allow a request body as large as the archive, up to 7 GiB. nginx: `client_max_body_size 7g;` (its default is 1 MiB). Do not spool the body to a small disk: nginx `proxy_request_buffering off;`. dockmin applies no body cap to this route other than 7 GiB, and answers `408` when no data arrives for 120 s |
| `POST /backups/download` | The first byte is sent after the archive is produced. A response timeout shorter than production time ends the request. The response is a stream: do not buffer it. A client that stops reading for 60 s is disconnected ([Send idle bound](#send-idle-bound)) |
| `GET /restore/progress/*` | Polled every 2 s while a restore runs |

- During a restore's swap dockmin answers only the progress page, `/static/` and `/api/health` ([Backups](Administration-Backups#the-offline-gate)). A health check that uses another path fails for the duration of the swap.
- A gate in phase `failed` refuses `/api/health` as well ([Backups](Administration-Backups#failed-gate)).

### Reverse proxy limits for build contexts

| Path | Requirement |
|---|---|
| `POST /environments/{id}/stacks/{name}/contexts/-/upload` | Allow a request body of at least 16 MiB and 64 KiB. nginx: `client_max_body_size 17m;` (its default is 1 MiB). dockmin refuses a body over 16 MiB and 64 KiB with `413`, and answers `408` when no data arrives for 15 s or the body takes more than 300 s |
