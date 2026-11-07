# Quick start

Pulls the published image and starts dockmin on the local Docker host with HTTPS. The encryption key and the certificate are generated at the first start. Platforms: `linux/amd64` and `linux/arm64`.

Before starting: the server meets the [Prerequisites](Getting-Started-Prerequisites). A server without Docker: [Install Docker](Getting-Started-Prerequisites#install-docker). Other deployments: [Installation](Getting-Started-Installation).

## Start dockmin

1. Run the container.

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  ghcr.io/sphexi/dockmin:latest
```

Success: Docker prints the container id. The first run pulls the image, which needs access to `ghcr.io`. Failure: `Conflict. The container name "/dockmin" is already in use` means a container of that name exists. Remove it ([Removing](Getting-Started-Installation#removing)) or choose another `--name` and use that name in every later command.

| Part | Effect |
|---|---|
| `--name dockmin` | The name every later command uses |
| `--restart unless-stopped` | Restarts the container when it exits and when the Docker daemon starts, unless it was stopped with `docker stop`. A stopped container stays stopped across Docker daemon restarts |
| `-p 8443:8443` | Publishes the HTTPS listener on every interface of the host. The listener that redirects to it runs inside the container and is not published |
| `--group-add` | Adds the group that owns the Docker socket to the container user (uid 10001). Without it the container starts and the connection test of the local socket environment fails at the `Socket` stage ([Local socket](Troubleshooting#local-socket)) |
| `-v /var/run/docker.sock:/var/run/docker.sock` | The Docker socket of the server, to manage the server itself |
| `-v dockmin-data:/data` | Named volume: database, stack files and the generated certificate. Without it the database is kept in the container's writable layer and is lost when the container is replaced |
| `-v dockmin-key:/key` | Named volume: the generated encryption key. Separate from the data volume |

Docker creates both volumes at the first start. The image owns `/data` and `/key` as uid 10001 with mode `0700`, and a new named volume takes that owner and mode.

At the first start dockmin:

| Action | Log line | Level |
|---|---|---|
| Generates the key in `/key/secret.key` | `encryption_key_generated`, with `key_file` and `key_fingerprint`. The log never holds the key | `WARNING` |
| Generates a self-signed certificate in `/data/server-tls/` | `tls_certificate_self_signed`, with `sha256_fingerprint`, `not_after`, `generated` and `reason` | `INFO` |
| Serves HTTPS on port 8443 | None | |
| Warns that `/data` is not a mounted volume, when the key is generated and `/data` is not a mount | `data_directory_not_mounted`, with `path`. The start continues | `WARNING` |

The key directory must be a volume or bind mount of its own, not a `tmpfs`, and not the data directory or a path inside it. Every refusal: [Key directory](Administration-Encryption#key-directory).

2. Wait until the container reports `healthy`.

```bash
docker ps --filter name=dockmin
```

Success: the `STATUS` column reads `Up <time> (healthy)`. `(health: starting)` shows until the first probe passes. The probe interval is 30 seconds.

Failure: the container is `Exited` or restarts repeatedly. A failed start logs one `startup_check_failed` line, with the fields `check`, `problem` and `remedy`, and exits.

```bash
docker logs dockmin
```

Causes and actions: [Troubleshooting](Troubleshooting#startup-self-check-failures) and [A container that restarts repeatedly](Troubleshooting#a-container-that-restarts-repeatedly).

3. Read the one-time setup value. Until an administrator exists, every request is redirected to `/setup`, which needs it.

```bash
docker logs dockmin | grep "First-run setup"
```

Success: one line.

```text
{"timestamp": "…", "level": "WARNING", "logger": "app.auth.bootstrap", "message": "First-run setup is required. Open /setup and submit this one-time value: <value>", "request_id": null}
```

| Item | Value |
|---|---|
| One-time value | Held in memory only. A restart before setup completes issues a new one and logs it again |
| Wrong value | Refused and logged as `bootstrap_token_rejected` (`WARNING`) |
| Exempt from the redirect | `/setup`, `/api/health`, `/api/ready` and the favicon |

Failure: no line. An administrator already exists in this database, or the container did not finish starting. Check step 2.

4. Open `https://<server-host>:8443/`. `<server-host>` is the host name or address of the server, as the browser reaches it. The browser warns that the certificate is not trusted ([Certificate warning](#certificate-warning)). Compare the certificate with the log, then accept the warning.

Success: the browser shows the page `Set up dockmin`. Failure: the page does not load. Check that port 8443 is open between the browser and the server ([Prerequisites](Getting-Started-Prerequisites#check-the-server)).

<!-- screenshot: Getting-Started-Quick-Start-1: the Set up dockmin page with the four fields visible and empty; instance with no administrator -->

5. Fill in the form and choose `Create administrator`.

| Field | Value |
|---|---|
| `Setup token` | The one-time value from step 3. Surrounding whitespace is ignored |
| `Administrator username` | 3 to 64 letters, digits, dots, hyphens, underscores or at signs, starting with a letter or digit |
| `Password` | At least `DOCKMIN_PASSWORD_MIN_LENGTH` characters (default 12, range 8 to 128). The page shows the rule in effect. Must not equal the username (case ignored) |
| `Confirm password` | The same password |

Success: the user is created as an administrator and the browser is sent to the `Sign in` page (`/login`). No variable or command creates an administrator. Once one exists, `/setup` answers `404` for good.

Failures:

| Message | Cause | Status |
|---|---|---|
| `The setup token is not correct.` under `Setup token` | The value differs from the one in the log. A restart issues a new value | 403 |
| `Use 3 to 64 letters, digits, dots, hyphens, underscores or at signs, starting with a letter or digit.` | The username does not match the rule | 422 |
| `The passwords do not match.` | `Password` and `Confirm password` differ | 422 |
| `Use at least <n> characters.` | The password is shorter than `DOCKMIN_PASSWORD_MIN_LENGTH` | 422 |
| `The password must not be the same as the username.` | The password equals the username, ignoring case | 422 |
| `Mix at least <n> of: lower case, upper case, digits, symbols.` | `DOCKMIN_PASSWORD_REQUIRED_CLASSES` is set and the password spans fewer classes | 422 |
| `That username is taken.` | The username exists | 422 |
| Panel `Setup is paused`, with a wait time | Wrong setup values from one address reached `DOCKMIN_LOGIN_MAX_FAILURES` (default 5) within `DOCKMIN_LOGIN_LOCKOUT_WINDOW` (default 15 minutes). The address lock applies when dockmin serves HTTPS itself or `DOCKMIN_TRUSTED_PROXIES` is set. The `Retry-After` header carries the wait | 429 |

A rejected setup value is recorded in the activity log as a failed `bootstrap` event with the reason `bad_setup_token` ([Activity](Administration-Activity)). Settings: [Configuration](Reference-Configuration#accounts-and-sessions).

6. Sign in with the new username and password.

Success: the environment list opens.

7. Copy the key off this host ([Copy the key](#copy-the-key)).

8. Add the Docker host as an environment ([Add a local socket environment](Usage-Environments#add-a-local-socket-environment)). While no environment exists, the environment list shows the panel `Add your first environment`. Its `Test and add` button adds the mounted socket, and the panel names the missing mount or group when the socket is not usable ([First-run panel](Usage-Environments#first-run-panel)).

## Certificate warning

The browser warns because no certificate authority signed the certificate dockmin generated. Compare the certificate in the browser's certificate viewer with the fingerprint in the log before accepting.

1. Read the fingerprint.

```bash
docker logs dockmin | grep tls_certificate_self_signed
```

Success: one line.

```text
{"timestamp": "…", "level": "INFO", "logger": "app.serving.launcher", "message": "tls_certificate_self_signed", "request_id": null, "sha256_fingerprint": "<fingerprint>", "not_after": "<date>", "generated": true, "reason": "missing"}
```

2. Open the certificate in the browser's certificate viewer and compare its SHA-256 fingerprint with `sha256_fingerprint`.
3. Accept the warning when the two match.

| Item | Value |
|---|---|
| Names | `localhost`, `127.0.0.1` and `::1`. For any other host name or address the browser reports a name mismatch in addition to the missing authority |
| Renewal | A start replaces the pair when a file is missing, the pair is unusable, or the certificate is expiring (within 30 days), expired or not yet valid. The fingerprint changes and browsers warn again. Renew with `docker restart dockmin` |
| HSTS | Not sent for the generated certificate |

The `Certificate` page under `Administration` shows the source, the fingerprint and the expiry ([Certificate](Administration-Certificate)). Files, key and validity of the generated pair: [Self-signed certificate](Administration-Certificate#self-signed-certificate). To remove the warning, supply a certificate ([Own certificate](Getting-Started-Installation#own-certificate)) or terminate TLS at a reverse proxy ([Behind a reverse proxy](Getting-Started-Installation#behind-a-reverse-proxy)). Other symptoms: [Certificate warning](Troubleshooting#certificate-warning).

## Copy the key

The key is generated at the first start. Copy it to a place outside this host and outside every backup.

1. Copy the key out of the container.

```bash
docker cp dockmin:/key/secret.key ./dockmin-secret.key
```

Success: the file `dockmin-secret.key` exists in the current directory. Failure: `Could not find the file /key/secret.key in container dockmin` means the container uses a supplied key (`DOCKMIN_SECRET_KEY` or `DOCKMIN_SECRET_KEY_FILE`) and generated none.

2. Under `sudo`, the copy belongs to root. Before a command reads it with `<` or `$(cat …)`, give it to the current user.

```bash
sudo chown "$USER" dockmin-secret.key
```

3. Open `Administration > Backups` and note the fingerprint under `Encryption key`.

<!-- screenshot: Getting-Started-Quick-Start-2: the Backups page, the Encryption key section with Fingerprint and Held in; instance started with the Quick Start command -->

4. Move `dockmin-secret.key` to storage outside this host, such as a password manager, and keep the fingerprint beside it.
5. Delete the file on this host.

**A backup archive never contains the key.** A restore asks for it, and without it the archive and the credentials in the database cannot be read ([Backups](Administration-Backups)). Keep the copy apart from every archive. Anyone who holds both can read every stored secret.

| Item | Value |
|---|---|
| Format | Base64 of 32 random bytes. Whitespace around the value is ignored |
| Location in the container | `/key/secret.key`, on the volume `dockmin-key`, owned by uid 10001 |
| Mode | `0400` for the key generated at the first start. `0600` for a key written by a [re-key](Administration-Encryption) or by [Restoring the key volume](Administration-Backups#restoring-the-key-volume) |
| Copy | Belongs to the user who ran `docker cp`, with the mode of the file it copies |
| Fingerprint | Logged as `key_fingerprint` by `encryption_key_generated` at the first start, and shown on the Backups page |
| Never replaced | An existing key file is never overwritten. A key is generated only for a new database |

To put a copy back into a key volume: [Restoring the key volume](Administration-Backups#restoring-the-key-volume). Removing the volume `dockmin-key` deletes the only key on this host once the copy is moved off it ([Removing](Getting-Started-Installation#removing)). Key theory and re-key: [Encryption](Administration-Encryption).
