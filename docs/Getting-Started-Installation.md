# Installation

| Path | Use | Section |
|---|---|---|
| `docker run` | The default install, with a generated key and certificate | [Quick start](Getting-Started-Quick-Start) |
| `docker run` with a supplied certificate | Browsers that trust the issuer do not warn | [Own certificate](#own-certificate) |
| `docker run` behind a proxy | TLS ends at a reverse proxy | [Behind a reverse proxy](#behind-a-reverse-proxy) |
| `docker run` with container hardening | Read-only root filesystem, dropped capabilities, resource limits | [Hardened variant](#hardened-variant) |
| Docker Compose | The key as a Docker secret, plain HTTP on loopback, or the HTTPS and proxy examples | [Compose](#compose) |
| Image built from source | No access to `ghcr.io` | [Build from source](#build-from-source) |

Requirements: [Prerequisites](Getting-Started-Prerequisites). The commands below keep the volumes `dockmin-data` and `dockmin-key` of the [Quick start](Getting-Started-Quick-Start).

## Own certificate

A supplied certificate replaces the generated one. Browsers that trust its issuer do not warn, and dockmin sends HSTS ([HTTPS](Administration-Certificate#https)). The generated key volume is still used.

| Item | Value |
|---|---|
| Certificate | PEM, leaf first, then the chain |
| Key | Unencrypted PEM |
| Reader | uid 10001, which also needs search permission on the directory |
| Names | The certificate's subject alternative names must list the host name browsers use |
| Ignored | `DOCKMIN_TLS_SELF_SIGNED`. No `server-tls` directory is created |
| Variables | `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE`, set together ([Configuration](Reference-Configuration#listeners-and-network)) |

Replace a running instance in this order. Both volumes are kept.

1. Put both files in one host directory and make them readable by uid 10001.

```bash
sudo chown 10001:10001 <certificate-directory>/<certificate-file> <certificate-directory>/<key-file>
sudo chmod 0400 <certificate-directory>/<certificate-file> <certificate-directory>/<key-file>
```

2. Stop and remove the container.

```bash
docker stop --time 45 dockmin
docker rm dockmin
```

3. Start it with the files mounted.

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  -v <certificate-directory>:/certs:ro \
  -e DOCKMIN_TLS_CERT_FILE=/certs/<certificate-file> \
  -e DOCKMIN_TLS_KEY_FILE=/certs/<key-file> \
  ghcr.io/sphexi/dockmin:latest
```

4. Wait for `healthy`, then open `https://<host-name>:8443/`. `<host-name>` is a name the certificate lists.

```bash
docker ps --filter name=dockmin
```

Success: the status is `(healthy)`, the log has no `tls_certificate_self_signed` line, and `Administration > Certificate` shows the source `Supplied file` ([Certificate](Administration-Certificate)). Failure: a `startup_check_failed` line with `check` `tls_material` means the files are unreadable by uid 10001 or are not a PEM certificate and its key ([tls_material](Troubleshooting#tls_material)).

A replaced certificate is served after a restart.

```bash
docker restart dockmin
```

## Behind a reverse proxy

A proxy that reaches dockmin's plain listener needs the self-signed certificate turned off. dockmin then serves the plain listener on `DOCKMIN_PORT` (default `8000`) and no HTTPS, so publish that port to the proxy only. Requirements and settings: [Behind a reverse proxy](Reference-Deployment#behind-a-reverse-proxy).

1. Stop and remove the container.

```bash
docker stop --time 45 dockmin
docker rm dockmin
```

2. Start it with the self-signed certificate off. `<proxy-facing-address>` is the host address the proxy connects to, such as `127.0.0.1` for a proxy on the same host. `<proxy-address>` is the address the proxy connects from, as dockmin sees it. A connection through a published Docker port can show the Docker gateway as the peer.

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p <proxy-facing-address>:8000:8000 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  -e DOCKMIN_TLS_SELF_SIGNED=false \
  -e DOCKMIN_TRUSTED_PROXIES=<proxy-address> \
  ghcr.io/sphexi/dockmin:latest
```

3. Point the proxy at `http://<proxy-facing-address>:8000`. It must send `X-Forwarded-For` and `X-Forwarded-Proto: https`, preserve `Host`, pass WebSocket upgrades and not buffer streams ([Behind a reverse proxy](Reference-Deployment#behind-a-reverse-proxy), [Terminals and streams](Reference-Deployment#terminals-and-streams-behind-a-reverse-proxy)).
4. Open the proxy's public address.

Success: the sign-in page, or `Set up dockmin` before the first administrator exists. `Administration > Certificate` shows `TLS is not configured`. Failure: the proxy receives a redirect to port 8443, because the container still serves the self-signed certificate. Check `DOCKMIN_TLS_SELF_SIGNED=false` ([A proxy receives a redirect](Troubleshooting#a-proxy-receives-a-redirect)).

## Hardened variant

The quick-start command with the hardening that `compose.yaml` applies.

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  --read-only \
  --tmpfs /tmp:size=64m,mode=1777,noexec,nosuid,nodev \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --memory 1g \
  --cpus 2 \
  --pids-limit 512 \
  --stop-timeout 45 \
  --log-opt max-size=10m \
  --log-opt max-file=5 \
  ghcr.io/sphexi/dockmin:latest
```

| Added flag | Effect |
|---|---|
| `--read-only` | Root filesystem read-only. `/data`, `/key` and `/tmp` stay writable. Without a mounted data or key volume the container refuses to start |
| `--tmpfs /tmp:…` | A 64 MB `/tmp` that does not allow executables, set-user-id files or devices |
| `--cap-drop ALL` | No Linux capabilities |
| `--security-opt no-new-privileges:true` | A process cannot gain privileges through `exec` |
| `--memory 1g`, `--cpus 2`, `--pids-limit 512` | Resource limits |
| `--stop-timeout 45` | Seconds Docker waits for the shutdown sequence before killing the container |
| `--log-opt max-size=10m`, `--log-opt max-file=5` | Five log files of 10 MB each |

Success: `docker ps --filter name=dockmin` reports `(healthy)`. Failure: a `startup_check_failed` line in `docker logs dockmin` ([Troubleshooting](Troubleshooting#startup-self-check-failures)).

The Docker API grants effective root on the host to anything that can reach it. These flags limit what a compromised dockmin process can do to the container, not what the socket grants.

## Upgrading

An administrator can also update a container started with the Quick Start command from `Administration > Instance`, with `Check for updates` ([Instance](Administration-Instance)). The steps below update it by hand.

1. Download a backup from the Backups page and confirm that the key copy exists outside this host ([Copy the key](Getting-Started-Quick-Start#copy-the-key), [Backups](Administration-Backups)).
2. Pull the new image.

```bash
docker pull ghcr.io/sphexi/dockmin:latest
```

Success: the output ends with `Status: Downloaded newer image` or `Status: Image is up to date`.

3. Remove the container. Both volumes are kept.

```bash
docker stop --time 45 dockmin
docker rm dockmin
```

4. Run the command the container was created with. For the quick start, that is the command under [Start dockmin](Getting-Started-Quick-Start#start-dockmin).
5. Read the log.

```bash
docker logs dockmin
```

Success: no `startup_check_failed` line, and `docker ps --filter name=dockmin` reports `(healthy)`.

| Item | Behaviour |
|---|---|
| Migrations | Run at startup, before the listeners accept requests |
| Data | The volume `dockmin-data` is kept |
| Key | The volume `dockmin-key` is kept and the same key file is read |
| Certificate | Reused while valid |
| Version tag | A container created from a version tag stays on that version. Pull and run the new tag |
| Database written by a newer version | The start is refused: `This database was written by a newer dockmin`. The database is not modified. The `database` check fails and the exit status is `3` ([database](Troubleshooting#database)) |
| Going back | Restore the backup taken before the upgrade, with the older image ([Restore](Administration-Backups#restore)) |

## Removing

Run every command in this section in the same shell.

1. Read the image reference, because the next commands remove the container it is read from.

```bash
image="$(docker inspect --format '{{.Config.Image}}' dockmin)"
```

2. Remove the container, keeping the data and the key.

```bash
docker stop --time 45 dockmin
docker rm dockmin
```

Success: `docker ps -a --filter name=dockmin` prints only the header. The volumes `dockmin-data` and `dockmin-key` remain.

3. Optional. **Remove the data.** The next command deletes the volume `dockmin-data`: the database, every stored credential, the stack files, the generated certificate and any restore leftovers. Without a backup and its key the data cannot be recovered.

```bash
docker volume rm dockmin-data
```

4. Optional. **Remove the key.** The next command deletes the volume `dockmin-key`, the only copy of the key on this host once the copy made by `docker cp` is moved off it. Without a copy of `secret.key`, the stored credentials in any surviving data volume and every archive made with this key can never be read.

```bash
docker volume rm dockmin-key
```

5. Remove the image.

```bash
docker image rm "$image"
```

Containers, stacks, networks and volumes created through dockmin on managed hosts are not removed. Key copies made with `docker cp` remain where they were written. Failure at `docker volume rm`: `volume is in use` means a container still mounts it. Remove that container first.

## Compose

Compose runs the same image with the key as a Docker secret and plain HTTP on the host's loopback. Every command in this part runs from the repository root, which [Get the source](#get-the-source) creates.

| Item | Value |
|---|---|
| Key | The file `secrets/dockmin_secret_key`, passed as `DOCKMIN_SECRET_KEY_FILE`. No key is generated and no key volume is used |
| HTTPS | `compose.yaml` sets `DOCKMIN_TLS_SELF_SIGNED` to `false`. Compose serves plain HTTP on `127.0.0.1`. The direct HTTPS example supplies a certificate |

### compose.yaml

The shipped [`compose.yaml`](https://github.com/Sphexi/DockMin/blob/main/compose.yaml) runs the image. It has no `build:` section.

| Setting | Value |
|---|---|
| Image | `${DOCKMIN_IMAGE:-dockmin:local}` |
| Published port | `127.0.0.1:${DOCKMIN_PORT:-8000}`: the plain listener on the host's loopback |
| Encryption key | A file secret from `./secrets/dockmin_secret_key`, read as `DOCKMIN_SECRET_KEY_FILE=/run/secrets/dockmin_secret_key`. A key is never generated, and no `/key` volume is mounted |
| Self-signed certificate | Off: `DOCKMIN_TLS_SELF_SIGNED` is fixed to `false`, so the published port serves the application |
| Data | The Compose volume `dockmin-data`, named `<project>_dockmin-data` by Docker (`dockmin_dockmin-data` for a directory named `dockmin`), at `/data` |
| Docker socket | `/var/run/docker.sock`, mounted read-write, with `group_add` of `${DOCKER_GID:-999}` |
| Root filesystem | `read_only: true` |
| `/tmp` | `tmpfs`, `size=64m,mode=1777,noexec,nosuid,nodev` |
| Capabilities | `cap_drop: [ALL]` |
| Privileges | `security_opt: [no-new-privileges:true]` |
| Limits | `cpus: "2"`, `memory: 1g`, `pids: 512` |
| Stop grace period | `45s` |
| Logging | `json-file`, `max-size: 10m`, `max-file: "5"` |
| Restart | `unless-stopped` |
| Variables passed | [Variables passed by `compose.yaml`](Reference-Configuration#variables-passed-by-composeyaml) |

- [`.env.example`](https://github.com/Sphexi/DockMin/blob/main/.env.example) lists every variable the file and the examples read, with its default.
- The Docker API grants effective root on the host to anything that reaches it. The socket is the dockmin container's reach.
- Compose has no egress policy. Outbound access is the default bridge network's; restrict it at the host firewall ([Registry connections](Administration-Registries#registry-connections)).

### Get the source

1. Clone the repository.

```bash
git clone https://github.com/Sphexi/DockMin.git dockmin
cd dockmin
```

Success: the directory `dockmin` holds `compose.yaml`, `.env.example` and `examples/`. Failure: `git: command not found` means `git` is not installed.

### Get the image

Build the image from source.

```bash
docker build -t dockmin:local .
```

Success: the output ends with `naming to docker.io/library/dockmin:local`. The build produces `dockmin:local`, which is the default of `DOCKMIN_IMAGE`. To run an image from a registry instead, skip the build and follow [An image from a registry](#an-image-from-a-registry). Failure: the build needs Docker with BuildKit and network access to Docker Hub and the Python package index.

### Create the encryption key

dockmin encrypts stored credentials with one key. Create it once.

1. Create the key file.

```bash
install -d -m 0700 secrets
(umask 077 && openssl rand -base64 32 | tr -d '\r\n' > secrets/dockmin_secret_key)
```

2. Check its size.

```bash
wc -c secrets/dockmin_secret_key
```

Success: `44 secrets/dockmin_secret_key`.

3. **Copy the key to a place outside this host and outside every backup before going on.** A backup archive never contains the key. A restore asks for it, and without it the archive and the credentials in the database cannot be read ([Backups](Administration-Backups)).
4. Give the file to the container user. Run as root, after copying the key.

```bash
chown 10001:10001 secrets/dockmin_secret_key
chmod 0400 secrets/dockmin_secret_key
```

Success: `ls -ln secrets/dockmin_secret_key` shows mode `-r--------` and owner `10001 10001`. After this only root and uid 10001 can read the file. A file that other host users can read gives them the key and, with any archive, every stored secret in it.

5. Check whether a host account uses uid 10001.

```bash
getent passwd 10001
```

No output means no host account uses the id. A line names an account that can read the key.

| Item | Value |
|---|---|
| Format | Base64 of 32 random bytes. Whitespace around the value is ignored |
| Location | `secrets/dockmin_secret_key`, mounted by `compose.yaml` as the Docker secret `dockmin_secret_key` |
| Reader | The container runs as uid 10001. The mounted file keeps the host file's owner and mode |
| Unreadable by uid 10001 | The container exits with `startup_check_failed`, check `encryption_key`, problem `DOCKMIN_SECRET_KEY_FILE does not point to a readable file` ([encryption_key](Troubleshooting#encryption_key)) |

### Configure

1. Copy the example environment file.

```bash
cp .env.example .env
```

| Variable | Set to |
|---|---|
| `DOCKER_GID` | The numeric group that owns the Docker socket. The container user needs it to open the socket |
| `DOCKMIN_PORT` | Port published on `127.0.0.1` and used by the plain HTTP listener ([Configuration](Reference-Configuration#listeners-and-network)) |
| `DOCKMIN_COOKIE_SECURE` | `false` only to sign in over plain HTTP on this host. Leave `true` when HTTPS is served |

2. Find the group and write it to `.env`.

```bash
stat -c %g /var/run/docker.sock
sed -i "s/^DOCKER_GID=.*/DOCKER_GID=$(stat -c %g /var/run/docker.sock)/" .env
```

Success: `grep ^DOCKER_GID= .env` prints the number `stat` printed.

3. To sign in over plain HTTP at `http://127.0.0.1:8000/`, turn the `Secure` attribute off.

```bash
sed -i 's/^DOCKMIN_COOKIE_SECURE=.*/DOCKMIN_COOKIE_SECURE=false/' .env
```

The session and CSRF cookies carry the `Secure` attribute by default, so a browser signs in only over HTTPS ([Cookies](Reference-Security#cookies)). Every variable: [Configuration](Reference-Configuration).

While `DOCKMIN_COOKIE_SECURE` is `false`:

- Passwords and session cookies travel over plain HTTP, and dockmin logs `cookie_secure_disabled` at start.
- Do not change the address `compose.yaml` publishes (`127.0.0.1`).

From another machine, forward the port over SSH and open `http://127.0.0.1:8000/` on that machine. Use the value of `DOCKMIN_PORT` for both ports when it is not `8000`.

```bash
ssh -L 8000:127.0.0.1:8000 <user>@<server-host>
```

### An image from a registry

Set `DOCKMIN_IMAGE` to the reference of an image pushed to a registry, then pull it. `<dockmin-image>` is that reference, for example the image in the [Quick start](Getting-Started-Quick-Start#start-dockmin) command.

```bash
sed -i 's|^DOCKMIN_IMAGE=.*|DOCKMIN_IMAGE=<dockmin-image>|' .env
docker compose pull
```

Success: `docker compose pull` ends with the service `Pulled`.

### Start

1. Start the service.

```bash
docker compose up -d
docker compose ps
```

Success: the `STATUS` column reports `Up <time> (healthy)` and `PORTS` shows `127.0.0.1:8000->8000/tcp`.

| Item | Value |
|---|---|
| Published port | `127.0.0.1:8000`, the plain HTTP listener |
| Data | The Compose volume `dockmin-data`, named `<project>_dockmin-data` by Docker, at `/data` |
| Restart policy | `unless-stopped` |

2. On failure, read the log. A failed start logs one `startup_check_failed` line and exits.

```bash
docker compose logs dockmin
```

| Symptom | Cause |
|---|---|
| `pull access denied for dockmin` | The image `dockmin:local` was not built ([Get the image](#get-the-image)) |
| Compose names a missing file under `secrets/` | The key file does not exist ([Create the encryption key](#create-the-encryption-key)) |
| `startup_check_failed`, check `encryption_key` | The key file is unreadable by uid 10001 or does not hold base64 of 32 bytes |

Other causes and actions: [Troubleshooting](Troubleshooting#startup-self-check-failures).

### First administrator on Compose

At the first start no user exists. Every request is redirected to `/setup` until an administrator is created. dockmin logs a one-time value once, at `WARNING`.

1. Read the one-time value.

```bash
docker compose logs dockmin | grep "First-run setup"
```

```text
dockmin-1  | {"timestamp": "…", "level": "WARNING", "logger": "app.auth.bootstrap", "message": "First-run setup is required. Open /setup and submit this one-time value: <value>", "request_id": null}
```

2. Open `http://127.0.0.1:8000/setup`.
3. Enter the one-time value in `Setup token`, an `Administrator username`, and a `Password` twice (`Confirm password`).
4. Choose `Create administrator`. The user is created as an administrator and the browser is sent to the sign-in page.
5. Sign in with that username and password.

The one-time value, username and password rules, and the failure messages are those of [Start dockmin](Getting-Started-Quick-Start#start-dockmin), steps 3 to 5. The environment list then shows the panel `Add your first environment` ([First-run panel](Usage-Environments#first-run-panel)); with Compose, a socket the container user cannot open is fixed through `DOCKER_GID`.

### Direct HTTPS

dockmin serves HTTPS itself from a certificate and key. Port 443 serves the application and port 80 redirects to it ([HTTPS](Administration-Certificate#https)).

| Item | Value |
|---|---|
| Override file | `examples/direct-https/compose.yaml` |
| Certificate | `secrets/tls_fullchain.pem`: PEM, leaf first, then the chain. Another path: `DOCKMIN_TLS_CERT_SOURCE`. File secret `dockmin_tls_cert`, read as `DOCKMIN_TLS_CERT_FILE=/run/secrets/dockmin_tls_cert` |
| Key | `secrets/tls_privkey.pem`: unencrypted PEM. Another path: `DOCKMIN_TLS_KEY_SOURCE`. File secret `dockmin_tls_key`, read as `DOCKMIN_TLS_KEY_FILE=/run/secrets/dockmin_tls_key` |
| Reader | uid 10001 |
| Published ports | `${DOCKMIN_HTTPS_PUBLIC_PORT:-443}` to the HTTPS port (`DOCKMIN_HTTPS_PORT`), and `80` to `DOCKMIN_PORT` |
| Names | The certificate's subject alternative names must list the host name browsers use |
| Self-signed certificate | Not generated: the supplied files take precedence over `DOCKMIN_TLS_SELF_SIGNED` |

| Variable | Value in the example |
|---|---|
| `DOCKMIN_HTTPS_PUBLIC_PORT` | `443`. Port a redirect names |
| `DOCKMIN_HTTPS_PORT` | `8443`. Port of the HTTPS listener inside the container |
| `DOCKMIN_HTTP_MODE` | `redirect`. What port 80 does: `redirect`, `serve` or `off` ([Configuration](Reference-Configuration#listeners-and-network)) |

The override uses the `!override` YAML tag of Compose files and is tested with Compose v2.33.0 and v5.5.1.

1. For a trial, create a self-signed certificate. Browsers warn about it. For production, place the certificate and key from a certificate authority at the two paths.

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout secrets/tls_privkey.pem -out secrets/tls_fullchain.pem \
  -subj "/CN=<host-name>" -addext "subjectAltName=DNS:<host-name>"
```

2. Make both files readable by the container user, and set the cookies back to `Secure`.

```bash
chown 10001:10001 secrets/tls_fullchain.pem secrets/tls_privkey.pem
chmod 0400 secrets/tls_fullchain.pem secrets/tls_privkey.pem
sed -i 's/^DOCKMIN_COOKIE_SECURE=.*/DOCKMIN_COOKIE_SECURE=true/' .env
```

3. Start with the override file.

```bash
docker compose -f compose.yaml -f examples/direct-https/compose.yaml up -d
docker compose -f compose.yaml -f examples/direct-https/compose.yaml ps
```

Success: `STATUS` reports `(healthy)`. Failure: a `startup_check_failed` line with check `tls_material` means the files are unreadable by uid 10001 or are not a PEM certificate and its key ([tls_material](Troubleshooting#tls_material)).

4. Open `https://<host-name>/`. `http://<host-name>/` redirects to it. Pass the same `-f` options to every later Compose command for this deployment.

A replaced certificate is served after a restart. `Administration > Certificate` shows what is loaded ([Certificate](Administration-Certificate)).

### Reverse proxy

nginx terminates TLS and forwards to dockmin on a private network. dockmin publishes no port ([Behind a reverse proxy](Reference-Deployment#behind-a-reverse-proxy)).

| Item | Value |
|---|---|
| Override file | `examples/reverse-proxy/nginx/compose.yaml` |
| dockmin | Publishes no port. `DOCKMIN_TRUSTED_PROXIES` is `${DOCKMIN_PROXY_ADDRESS}/32`, default `172.29.250.2/32`. `DOCKMIN_TLS_SELF_SIGNED` stays `false`, as in `compose.yaml` |
| Certificate | `secrets/proxy_fullchain.pem`. Another path: `DOCKMIN_PROXY_TLS_CERT_SOURCE` |
| Key | `secrets/proxy_privkey.pem`. Another path: `DOCKMIN_PROXY_TLS_KEY_SOURCE` |
| Reader | uid 101, the nginx user |
| Published port | `443` on nginx, to nginx's port 8443. Port 80 is not published |
| Network | Subnet `${DOCKMIN_PROXY_SUBNET:-172.29.250.0/24}`, automatic assignment limited to `${DOCKMIN_PROXY_IP_RANGE:-172.29.250.128/25}`. `DOCKMIN_PROXY_ADDRESS` (default `172.29.250.2`) must lie inside the subnet and outside the range |
| nginx image | `nginx:1.31.8-alpine`, pinned by digest, run as `101:101` with the image's entrypoint. Pulled on the first start |
| nginx address | The fixed `${DOCKMIN_PROXY_ADDRESS:-172.29.250.2}` on the network |
| Upstream | `server dockmin:${DOCKMIN_PORT:-8000};`, rendered by the image's template mechanism from `dockmin-upstream.conf.template` (read-only bind) to `/tmp/dockmin-upstream.conf` with `NGINX_ENVSUBST_OUTPUT_DIR=/tmp` |
| Start order | After dockmin is healthy |
| Hardening | Read-only root filesystem, `tmpfs` `/tmp`, all capabilities dropped, `no-new-privileges`, limits of 1 CPU, 256 MiB and 128 processes |

| `nginx.conf` | Value |
|---|---|
| Listener | `8443 ssl`, TLS 1.2 and 1.3 |
| Request headers to dockmin | `Host $http_host`, `X-Forwarded-For $remote_addr`, `X-Forwarded-Proto $scheme`, `Upgrade` and `Connection` for WebSocket |
| Body size | `client_max_body_size 1m` |
| Terminal WebSocket | `proxy_buffering off`, 3600 s read and send timeouts |
| Streams, log downloads, `/activity/-/export`, `/backups/download` | `proxy_buffering off`, 3600 s read timeout |
| `/restore/archive` | `client_max_body_size 7g`, `client_body_timeout 600s`, `proxy_request_buffering off`, 3600 s read and send timeouts |
| Stack context uploads | `client_max_body_size 17m`, `client_body_timeout 300s`, `proxy_request_buffering off` |
| Access log | Method and path without the query string |
| Temporary paths | Under `/tmp` |

The override uses the `!reset` YAML tag of Compose files and is tested with Compose v2.33.0 and v5.5.1. A proxy other than nginx must meet the requirements in [Behind a reverse proxy](Reference-Deployment#behind-a-reverse-proxy), [Terminals and streams](Reference-Deployment#terminals-and-streams-behind-a-reverse-proxy) and [Reverse proxy limits for backups](Reference-Deployment#reverse-proxy-limits-for-backups).

1. For a trial, create a self-signed certificate. Browsers warn about it. For production, place the certificate and key from a certificate authority at the two paths.

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout secrets/proxy_privkey.pem -out secrets/proxy_fullchain.pem \
  -subj "/CN=<host-name>" -addext "subjectAltName=DNS:<host-name>"
```

2. Check whether a host account uses uid 101. A line names an account that can read the files.

```bash
getent passwd 101
```

3. Make both files readable by uid 101, and set the cookies back to `Secure`.

```bash
chown 101:101 secrets/proxy_fullchain.pem secrets/proxy_privkey.pem
chmod 0400 secrets/proxy_fullchain.pem secrets/proxy_privkey.pem
sed -i 's/^DOCKMIN_COOKIE_SECURE=.*/DOCKMIN_COOKIE_SECURE=true/' .env
```

4. Start with the override file.

```bash
docker compose -f compose.yaml -f examples/reverse-proxy/nginx/compose.yaml up -d
docker compose -f compose.yaml -f examples/reverse-proxy/nginx/compose.yaml ps
```

Success: both services run and dockmin reports `(healthy)`. nginx starts after dockmin is healthy.

5. Read the one-time setup value with the same `-f` options, then open `https://<host-name>/`.

```bash
docker compose -f compose.yaml -f examples/reverse-proxy/nginx/compose.yaml logs dockmin | grep "First-run setup"
```

Success: the page `Set up dockmin`. Failures:

| Symptom | Check |
|---|---|
| nginx answers `502` | dockmin is not running or not healthy ([Troubleshooting](Troubleshooting#startup-self-check-failures)) |
| nginx exits at start | Read its log with `docker compose -f compose.yaml -f examples/reverse-proxy/nginx/compose.yaml logs nginx`. A certificate or key that uid 101 cannot read is a cause |
| The browser is redirected to port 8443 | `DOCKMIN_TLS_SELF_SIGNED` is not `false` ([A proxy receives a redirect](Troubleshooting#a-proxy-receives-a-redirect)) |

Pass the same `-f` options to every later Compose command for this deployment.

### Upgrading a Compose deployment

The Instance page offers no update to a Compose deployment ([Instance](Administration-Instance)).

1. Download a backup from the Backups page and keep the encryption key ([Backups](Administration-Backups)).
2. Update the source to the new release.

```bash
git fetch --tags
git checkout <release-tag>
```

3. Build the new image.

```bash
docker build -t dockmin:<new-version> .
```

4. Point `DOCKMIN_IMAGE` at the new image.

```bash
sed -i 's|^DOCKMIN_IMAGE=.*|DOCKMIN_IMAGE=dockmin:<new-version>|' .env
```

5. Recreate the container. Use the same `-f` options as the deployment.

```bash
docker compose up -d
```

6. Read the log.

```bash
docker compose logs dockmin
```

Success: no `startup_check_failed` line, and `docker compose ps` reports `(healthy)`.

| Item | Behaviour |
|---|---|
| Migrations | Run at startup, before the listeners accept requests |
| Data | The volume `<project>_dockmin-data` is kept |
| Key | The same key file is read |
| `.env` and `secrets/` | Only the `DOCKMIN_IMAGE` line of `.env` changes. `secrets/` is unchanged |
| Database written by a newer version | The start is refused: `This database was written by a newer dockmin`. The database is not modified. The `database` check fails and the exit status is `3` ([database](Troubleshooting#database)) |
| Going back | Restore the backup taken before the upgrade, with the older image ([Restore](Administration-Backups#restore)) |

For an image from a registry, step 2 still applies: `compose.yaml` and `examples/` must come from the same release as the image. Skip step 3, set `DOCKMIN_IMAGE` to the new reference in step 4, and pull it before step 5.

```bash
docker compose pull
```

### Removing a Compose deployment

1. Stop and remove the container, keeping the data. Pass the same `-f` options used to start.

```bash
docker compose down
```

Success: `docker compose ps` lists no service. The volume `<project>_dockmin-data` and the files in `secrets/` remain.

2. Optional. **Remove the data.** The next command deletes the volume `<project>_dockmin-data`: the database, every stored credential, the stack files and any restore leftovers. Without a backup and its key the data cannot be recovered.

```bash
docker compose down --volumes
```

3. Remove the image.

```bash
docker image rm dockmin:local
```

The files in `secrets/` stay until deleted by hand, including the key.

## Build from source

Build the image as in [Get the image](#get-the-image), from the source in [Get the source](#get-the-source). Requirements: Docker with BuildKit, and network access to Docker Hub and the Python package index.

To run the built image with `docker run`, use the [Quick start](Getting-Started-Quick-Start#start-dockmin) command with `dockmin:local` as its last argument. The same substitution applies to every other `docker run` command on these pages.
