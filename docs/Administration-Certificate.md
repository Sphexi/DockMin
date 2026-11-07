# Certificate

`Administration > Certificate` shows the certificate dockmin serves HTTPS with, or states that TLS is terminated in front of dockmin.

<!-- screenshot: Administration-Certificate-1: the Server certificate page with the Source panel, the Certificate loaded at start list and the Listeners list; an instance running with the generated self-signed certificate -->

## Access

| Item | Fact |
|---|---|
| Who | Administrators. A standard user receives `403` |
| Where | `Administration > Certificate`, `GET /server/certificate`. The page is titled `Server certificate` |
| Caching | Every response carries `Cache-Control: no-store` |
| Reads | The certificate file on each view. The key file is never opened |
| Actions | None. The page offers no upload, no edit and no regeneration. Viewing writes no activity row |

## Certificate page

| Section | Content |
|---|---|
| TLS is not configured | `dockmin serves plain HTTP. TLS is terminated in front of it.` `HTTP port`, `Trusted proxies` and `Trusted origins` (`None` when empty). Shown only when TLS is off. No other section is shown then |
| Restart notice | `Restart dockmin to serve the replaced certificate.` and `The certificate file on disk is not the certificate loaded at start.` Shown when the file's fingerprint differs from the loaded certificate's |
| Source | `Source`: `Generated self-signed certificate` or `Supplied file`. `File`: the certificate file's path |
| Self-signed notice | Shown for a generated certificate: `Browsers warn about this certificate until it is trusted or replaced.`, the fingerprint comparison, `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE` as the way to remove the warning, and `dockmin generates a new certificate at start when this one is expiring or expired. Restart dockmin before the expiry date below.` |
| No certificate loaded | `This process does not serve HTTPS.` and `No certificate was loaded by this process.` Shown when TLS is on but this process loaded no certificate |
| Expiry notices | `The certificate expires in N days.` (`less than a day` under 1 day) at 30 days or less; `The certificate expired on <date>.` after expiry; `The certificate is not valid until <date>.` before the start of validity. The first two add `Restart dockmin to generate a new certificate.` for a generated certificate and `Replace the certificate file, then restart dockmin.` for a supplied one |
| Certificate loaded at start | `Status`, `Subject`, `Issuer`, `Self-signed`, `Subject alternative names`, `Valid from`, `Valid until`, `SHA-256 fingerprint`, `Serial number`, `Public key`, `Certificates in the file` |
| Certificate file on disk now | `Subject`, `Valid until`, `SHA-256 fingerprint`. Shown when the file's fingerprint differs from the loaded certificate's, or when no certificate is loaded |
| File notice | `The certificate file cannot be read now.` Shown when the file cannot be read and no on-disk details are shown |
| Listeners | `HTTPS port` (with `(published as N)` when `DOCKMIN_HTTPS_PUBLIC_PORT` is set) and `HTTP port` with its mode |

| `Status` | Condition |
|---|---|
| `Valid` | More than 30 days of validity left |
| `Expires within 30 days` | 30 days or fewer left |
| `Expired` | The end of validity has passed |
| `Not yet valid` | The start of validity has not been reached |

| `HTTP port` mode | `DOCKMIN_HTTP_MODE` |
|---|---|
| `Serves the application` | `serve` |
| `Redirects to HTTPS` | `redirect` |
| `Not listening` | `off` |

- `Subject alternative names` lists at most 256 entries, as `DNS:`, `IP:`, `email:` and `URI:` forms.
- `Public key` reads the type and size: `RSA, 2048 bits`, `EC secp256r1, 256 bits`; `Ed25519` and `Ed448` carry no size.
- Only the first certificate of the file is inspected; `Certificates in the file` counts all of them.

## HTTPS

dockmin serves HTTPS itself with the certificate named by `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE`, or, when neither file is set, with a self-signed certificate it generates.

### Certificate source

| Configuration | TLS | Certificate | Default `DOCKMIN_HTTP_MODE` |
|---|---|---|---|
| `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE` set | On | The supplied files | `redirect` |
| No files, `DOCKMIN_TLS_SELF_SIGNED` true (the default) | On | A generated self-signed certificate ([Self-signed certificate](#self-signed-certificate)) | `redirect` |
| No files, `DOCKMIN_TLS_SELF_SIGNED=false` | Off | None | `serve` |

- Set `DOCKMIN_TLS_SELF_SIGNED=false` when a reverse proxy reaches the plain listener ([Deployment](Reference-Deployment#behind-a-reverse-proxy)). The shipped `compose.yaml` sets it.
- A blank `DOCKMIN_TLS_SELF_SIGNED` is `true`.
- `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE` are set together or not at all.
- `DOCKMIN_TLS_SELF_SIGNED` is ignored when both files are set.
- `DOCKMIN_HTTPS_PORT` must differ from `DOCKMIN_PORT` when TLS is on.
- `redirect` and `off` for `DOCKMIN_HTTP_MODE` are refused when TLS is off.

### Supplied certificate

| Item | Value |
|---|---|
| Files | Paths, never values. Mount them as Docker secrets or bind mounts. The key is never stored in the database, never held in an environment variable and never returned by any route |
| Certificate | PEM, leaf first, then the chain. Only the first certificate is inspected |
| Key | Unencrypted PEM. A passphrase-protected key is refused |
| Permissions | The process runs as uid 10001 in the image and reads both files. A file mounted from the host keeps the host file's owner and mode |
| Protocols | TLS 1.2 and TLS 1.3 |
| TLS 1.2 suites | `ECDHE+AESGCM:ECDHE+CHACHA20` |
| TLS 1.3 suites | The TLS library's defaults |
| Client certificates | Not requested |
| Application protocol | HTTP/1.1 |
| Renewal | The files are read at start. A replaced certificate is served after a restart |
| Expired or not yet valid certificate | The start proceeds. Logged as `tls_certificate_invalid_now` at warning level with `state`, `subject`, `not_before` and `not_after`, and shown on the [certificate page](#certificate-page) |
| Expiring certificate | Within 30 days of expiry. Logged at every start as `tls_certificate_expiring` with `days_left` |
| Unusable files | The process does not start (check `tls_material`, [Troubleshooting](Troubleshooting#tls_material)) |
| HSTS | Sent ([Deployment](Reference-Deployment#security-headers)) |

The protocol rows apply to a self-signed certificate as well.

#### Supply a certificate

1. Place the certificate chain and the key in a directory the container can mount, such as `secrets/tls_fullchain.pem` and `secrets/tls_privkey.pem`. The certificate is PEM with the leaf first. The key is unencrypted PEM.
2. Make both files readable by uid 10001 only. Run as root:

```bash
chown 10001:10001 secrets/tls_fullchain.pem secrets/tls_privkey.pem
chmod 0400 secrets/tls_fullchain.pem secrets/tls_privkey.pem
```

   Result: no output.

3. Mount the files into the container and set both variables to their paths inside the container: `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE`. For a container started with `docker run`, the options are in [Own certificate](Getting-Started-Installation#own-certificate). For Compose, the direct HTTPS variant is in [Direct HTTPS](Getting-Started-Installation#direct-https).
4. Recreate the container.

   Result: no `startup_check_failed` line with `check` `tls_material` in the log. The certificate page shows `Source`: `Supplied file`, the file path, and `Status`: `Valid` while the certificate is inside its validity period and more than 30 days from its end.

   If the container exits with status `2` or `3`, read the `startup_check_failed` line ([Troubleshooting](Troubleshooting#tls_material)).

#### Renew a supplied certificate

1. Replace the certificate file, and the key file when the key changed. The two must match.
2. Restart dockmin: `Administration > Instance`, `Restart dockmin` ([Instance](Administration-Instance#restart-dockmin)), or from the host:

```bash
docker restart <container-name>
```

   Result: the certificate page shows the new `SHA-256 fingerprint` and `Valid until`. Before the restart it shows `Restart dockmin to serve the replaced certificate.`

## Self-signed certificate

Generated at start when TLS is on and no certificate files are set.

| Item | Value |
|---|---|
| Directory | `<DOCKMIN_DATA_DIR>/server-tls/` (`/data/server-tls/` in the image), mode `0700`, owned by the process user |
| Files | `cert.pem` (mode `0644`) and `key.pem` (mode `0600`) |
| Key | EC P-256, unencrypted PKCS#8 PEM. Read by the TLS library at start. Never stored in the database, never logged, never returned by a route, in no backup archive |
| Subject and issuer | `CN=dockmin` |
| Subject alternative names | `DNS:localhost`, `IP:127.0.0.1`, `IP:::1`. The host's own name and address are not included, so a browser reaching dockmin by any other name or address reports a name mismatch as well as an untrusted issuer |
| Extensions | Basic constraints `CA:FALSE` (critical), key usage `digitalSignature` (critical), extended key usage `serverAuth`, subject alternative names, subject key identifier |
| Signature | SHA-256 |
| Validity | 825 days in total, starting one hour before generation |
| Trust | No browser or client trusts the certificate until a user accepts it. The browser warns, and a client that verifies certificates refuses it |
| In a backup archive | No. The archive is built from the database, and a restore leaves `server-tls/` as it is |
| HSTS | [Security headers](Reference-Deployment#security-headers) |
| Before the first administrator exists | The listener answers on every published interface. Browser requests are redirected to `/setup`, which needs the one-time value written to the log ([Quick start](Getting-Started-Quick-Start)). API paths answer `503` and WebSocket handshakes are refused. `/api/health`, `/api/ready` and the favicon are exempt |

The pair is checked at every start, before the instance lock is taken:

| Pair in the directory | Result |
|---|---|
| Both files are regular files, the key matches the certificate, and the certificate is valid for more than 30 days | Reused |
| Either file is missing | A new pair is written. `reason` is `missing` |
| A file is unreadable or malformed, or the key does not match the certificate | A new pair is written. `reason` is `unusable` |
| The certificate expires within 30 days | A new pair is written. `reason` is `expiring` |
| The certificate has expired | A new pair is written. `reason` is `expired` |
| The certificate is not yet valid | A new pair is written. `reason` is `not_yet_valid` |

- A running process never replaces its certificate. Renewal is a restart within the last 30 days of validity, or any restart after expiry.
- A new pair has a new fingerprint. A browser that accepted the previous certificate warns again.
- Each file is written under a temporary name and renamed into place, the key first, then the certificate, and the directory is flushed. A pair left mismatched by an interruption is replaced at the next start.
- A second process started on the same data directory can replace an expiring pair before it fails to take the instance lock. The running process keeps the pair it loaded, and the [certificate page](#certificate-page) shows the file on disk as replaced.
- The start is refused when `server-tls` is a symbolic link or is not a directory, or when the directory or its files cannot be created or written: check `tls_material`, status `3`.
- A trusted certificate: supply one with `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE` ([Supply a certificate](#supply-a-certificate)), or terminate TLS at a reverse proxy.

Each start in this mode logs `tls_certificate_self_signed`:

| Field | Value |
|---|---|
| `sha256_fingerprint` | SHA-256 of the certificate, upper-case hexadecimal pairs separated by colons. The certificate page shows the same value |
| `not_after` | End of validity, ISO 8601 |
| `generated` | `true` when this start wrote a new pair |
| `reason` | Why a new pair was written ([above](#self-signed-certificate)), or `null` |

#### Verify the certificate before accepting the warning

1. Read the line for a container named `<container-name>`:

```bash
docker logs <container-name> | grep tls_certificate_self_signed
```

   Result: one line per start, with `sha256_fingerprint`.

2. Open dockmin in the browser. At the certificate warning, open the browser's certificate viewer and read the SHA-256 fingerprint.
3. Compare the two values. When they are equal, accept the warning for the host. When they differ, do not accept it.

   The same fingerprint is on the certificate page after sign-in. The browser warning itself: [Troubleshooting](Troubleshooting#certificate-warning).

## Related settings

| Variable | Use |
|---|---|
| `DOCKMIN_TLS_CERT_FILE`, `DOCKMIN_TLS_KEY_FILE` | The supplied certificate and its key |
| `DOCKMIN_TLS_SELF_SIGNED` | Generates the self-signed certificate when no files are set |
| `DOCKMIN_HTTPS_PORT`, `DOCKMIN_HTTPS_PUBLIC_PORT`, `DOCKMIN_PORT`, `DOCKMIN_HTTP_MODE`, `DOCKMIN_HOST` | The listeners the page reports |
| `DOCKMIN_TRUSTED_PROXIES`, `DOCKMIN_TRUSTED_ORIGINS` | Shown when TLS is off |
| `DOCKMIN_DATA_DIR` | Holds `server-tls/` |

Types, defaults and ranges: [Configuration](Reference-Configuration#listeners-and-network).

## Failure states

| Symptom | Cause | Action |
|---|---|---|
| The page answers `403` | The user is not an administrator | Sign in as an administrator |
| The container exits with status `3`, check `tls_material` | A TLS file is unreadable, holds no PEM certificate, is not an unencrypted PEM key, or the key does not match the first certificate; or the self-signed pair cannot be stored | [Troubleshooting](Troubleshooting#tls_material) |
| The container exits with status `2`, check `tls_material` | Only one of `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE` is set | Set both or neither |
| The page shows `Restart dockmin to serve the replaced certificate.` | The file on disk differs from the certificate loaded at start | Restart dockmin |
| The page shows `Expired`, `Expires within 30 days` or an expiry notice | The loaded certificate is past or near its end of validity | Generated: restart dockmin. Supplied: replace the file, then restart |
| The page shows `This process does not serve HTTPS.` | TLS is on but this process loaded no certificate | Read the log of the start for `tls_material` |
| The page shows `The certificate file cannot be read now.` | The file is missing or unreadable by the process user | Restore the file and its permissions |
| The page shows `TLS is not configured` | `DOCKMIN_TLS_SELF_SIGNED=false` with no certificate files | None when a proxy terminates TLS ([Deployment](Reference-Deployment#behind-a-reverse-proxy)) |
| The browser warns about the certificate | The certificate is generated, or its issuer or name is not trusted | [Troubleshooting](Troubleshooting#certificate-warning) |
