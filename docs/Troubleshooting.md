# Troubleshooting

Short answers to common questions: [FAQ](Troubleshooting-FAQ).

## Startup self-check failures

A failed startup precondition writes one log line whose `message` is `startup_check_failed`, with the fields `check`, `problem`, `remedy`, `fatal` and `during` ([Startup self-check](Reference-Deployment#startup-self-check)). Read it. For a container named `dockmin`, started with `docker run`:

```bash
docker logs dockmin
```

With Compose:

```bash
docker compose logs dockmin
```

```text
{"timestamp": "…", "level": "ERROR", "logger": "app.selfcheck", "message": "startup_check_failed", "request_id": null, "check": "<check>", "problem": "<problem>", "remedy": "<remedy>", "fatal": true, "during": "startup"}
```

Exit statuses: [Serving](Reference-Deployment#serving).

A configuration error in a `problem` column below is logged with the prefix `invalid configuration: `.

### A container that restarts repeatedly

| Symptom | Cause | Action |
|---|---|---|
| `docker ps` alternates between `Restarting (2)` or `Restarting (3)` and `Up N seconds (health: starting)`. `Restarting` shows only between attempts | The container was started with `--restart unless-stopped` and exits at every start because a check failed. The number is the exit status | Read the log line with `docker logs dockmin`, correct the cause and recreate the container. `docker stop dockmin` ends the restart loop first |

```bash
docker logs dockmin
docker stop dockmin
```

The exit status of a stopped container:

```bash
docker inspect --format '{{.State.ExitCode}}' dockmin
```

### `encryption_key`

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `at most one of DOCKMIN_SECRET_KEY or DOCKMIN_SECRET_KEY_FILE may be set` | Both variables are set | Unset one. With neither set, the key is generated in `DOCKMIN_SECRET_KEY_DIR` ([Key sources](Administration-Encryption#key-sources)) | `2` |
| `at most one of DOCKMIN_PREVIOUS_SECRET_KEY or DOCKMIN_PREVIOUS_SECRET_KEY_FILE may be set` | Both previous-key variables are set | Unset one ([Supplying the previous key](Administration-Encryption#supplying-the-previous-key)) | `2` |
| `DOCKMIN_SECRET_KEY_DIR must be an absolute path` | The variable holds a relative path, and no key variable is set | Set an absolute path | `2` |
| `DOCKMIN_SECRET_KEY_DIR must be a different directory from DOCKMIN_DATA_DIR, neither inside it nor containing it` | The key directory is the data directory, inside it or above it, after symbolic links are resolved | Mount the key volume at a path outside the data directory. The default is `/key` | `2` |
| `DOCKMIN_SECRET_KEY is not valid base64`, `DOCKMIN_SECRET_KEY must be base64 of exactly 32 bytes`, and the same for `DOCKMIN_SECRET_KEY_FILE`, `DOCKMIN_PREVIOUS_SECRET_KEY` and `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` | The value, or the content of the file, is not the base64 text of 32 bytes | Generate a key ([Key sources](Administration-Encryption#key-sources)), or supply the key the stored data was sealed with. Whitespace around the value is ignored | `2` |
| `DOCKMIN_SECRET_KEY_FILE does not point to a readable file`, `DOCKMIN_PREVIOUS_SECRET_KEY_FILE does not point to a readable file` | The file is missing, is not UTF-8 text, or is unreadable by uid 10001, or the Docker secret or bind mount is not in place | Check the path, the mount and the file's mode. The file must be readable by uid 10001 ([Create the encryption key](Getting-Started-Installation#create-the-encryption-key)) | `2` |
| `DOCKMIN_PREVIOUS_SECRET_KEY must differ from DOCKMIN_SECRET_KEY`, or the same with `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` as the first name | The previous key equals the supplied current key | Unset the previous-key variable, or give it the key being replaced | `2` |
| `<source> does not match this database, which expects key version N; if the key was rotated, supply the old one as DOCKMIN_PREVIOUS_SECRET_KEY or DOCKMIN_PREVIOUS_SECRET_KEY_FILE`. `<source>` is `the configured DOCKMIN_SECRET_KEY`, `the key in <path> (DOCKMIN_SECRET_KEY_FILE)`, or, for a generated key, `the key in <file>` | The key named in the problem is not the one the database was created with | Start with the original key. After a rotation, supply the old key as the previous key and recreate the container ([Re-key](Administration-Encryption#re-key)). Otherwise restore a backup taken with the current key | `3` |
| `The previous key, from <previous source>, opens this database at key version N but <source> does not. Starting would rotate the database to key version M and rewrite its canary under <source>; stored values would stay at version N until a re-key runs, and restarting with the old key as the current key would no longer work. Set DOCKMIN_ALLOW_KEY_ROTATION=1 to proceed`. `<previous source>` is `the configured DOCKMIN_PREVIOUS_SECRET_KEY` or `the key in <path> (DOCKMIN_PREVIOUS_SECRET_KEY_FILE)`; `<source>` is as in the row above | A rotation was started without permission | Set `DOCKMIN_ALLOW_KEY_ROTATION` to `true` for this start and recreate the container ([Re-key](Administration-Encryption#re-key)), or restore the original key | `3` |
| `stored values exist at key version N (M values) that no configured key can read; supply the key that reads them as DOCKMIN_PREVIOUS_SECRET_KEY or DOCKMIN_PREVIOUS_SECRET_KEY_FILE`. More than one version is listed as `N (M values), N2 (M2 values)` | Values from an earlier version remain and their key is not supplied | Supply that key as the previous key (Compose: through an override file; `docker run`: `-v` and `-e`) and recreate the container ([Re-key](Administration-Encryption#re-key)) | `3` |
| `cannot rotate to a new key while values remain at key version N; restart with the key that reads them as DOCKMIN_PREVIOUS_SECRET_KEY or DOCKMIN_PREVIOUS_SECRET_KEY_FILE and finish the re-key` | A rotation was started before the values of an earlier one were re-encrypted | Start with the key that reads those values as the previous key, and re-encrypt them on the Encryption page ([Re-encrypt stored secrets](Administration-Encryption#re-encrypt-stored-secrets)) | `3` |
| `cannot rotate to a new key while secret columns hold values that are not valid ciphertext, which no key can read: <table> row <id>, ...; clear or re-enter them first` | A secret column is damaged | Clear or re-enter the values the problem names. The Encryption page lists them ([Failed values](Administration-Encryption#failed-values)) | `3` |
| `the crypto state rows are missing from the setting table while encrypted values exist; restore the database rather than starting with a new key` | The database is damaged | Restore a backup ([Restore](Administration-Backups#restore)) | `3` |
| `the stored database instance id is malformed` | The database is damaged | Restore a backup | `3` |

The `remedy` of every row above with exit `3` reads: `Start with the key this database was created with, or restore a backup taken with the current key`.

The `remedy` of a configuration error in this check reads: `Set at most one of DOCKMIN_SECRET_KEY or DOCKMIN_SECRET_KEY_FILE, or set neither and mount a volume at DOCKMIN_SECRET_KEY_DIR; a supplied key is base64 of 32 random bytes, for example the output of ` followed by the command `openssl rand -base64 32`.

#### Generated key

Neither key variable is set and the key is read from `secret.key` in `DOCKMIN_SECRET_KEY_DIR` (`/key`). These refusals are found at start, exit `3`. No key file is written by any of them; the data directory may already hold the database and `server-tls/` from the same start. `<file>` is `<dir>/secret.key`, `<data>` is the data directory, `<n>` is the uid the process runs as. The directory refusals apply only while `secret.key` does not exist.

| Symptom (`problem`) | `remedy` | Cause and action |
|---|---|---|
| `No encryption key is configured and <file> does not exist, but the database already holds encrypted data` | `Mount the key volume this instance was created with at <dir>, or supply its key with DOCKMIN_SECRET_KEY_FILE. A new key cannot read the stored credentials` | The key volume is missing, empty, or not the one the instance was created with. Mount the original volume, `-v dockmin-key:/key`, or put the key back from its copy ([Restoring the key volume](Administration-Backups#restoring-the-key-volume)). Without a copy: [Key loss](Administration-Encryption#key-loss) |
| `No encryption key is configured and <file> does not exist, but <data>/restore/<id>/previous holds data kept by an earlier restore`, or `…<data>/restore/<id>/rejected holds…` | `Mount the key volume this instance was created with at <dir>, or supply its key with DOCKMIN_SECRET_KEY_FILE. A new key cannot read the stored credentials; if that data is no longer needed, remove <data>/restore/<id>/previous` (`rejected` when that is the directory) | A restore left its previous or rejected data, sealed with the original key, and the key volume is missing or empty. Mount the original key volume. When that data is no longer needed, remove the directory the remedy names |
| `The previous key, from <previous source>, is set but <file> does not exist`, with `<previous source>` as in the rotation row above | The same as the first row | A re-key was started without the key volume that holds the new key. Mount it ([Re-key](Administration-Encryption#re-key)) |
| `The key directory <dir> does not exist`, `The key directory <dir> is not a directory`, `The key directory <dir> is a symbolic link` | `Mount a volume at <dir>, for example -v <volume-name>:<dir>, or set DOCKMIN_SECRET_KEY_FILE` | `DOCKMIN_SECRET_KEY_DIR` names a path that is not a directory. Mount a volume at it |
| `The key directory <dir> cannot be opened`, `The key directory <dir> changed while opening` | The same | The directory could not be opened, or was replaced while the start opened it. Check its owner and mode, and that nothing replaces it |
| `The key directory <dir> is the same directory as the data directory <data>`, `The key directory <dir> is inside the data directory <data>` | `Give <dir> its own volume, for example -v <volume-name>:<dir>` | One volume is mounted at both paths, or the key directory is a path inside the data directory. Mount a separate volume at the key directory |
| `The key directory <dir> is not a mounted volume; a key written there is lost when the container is replaced` | `Mount a volume at <dir>, for example -v <volume-name>:<dir>, or set DOCKMIN_SECRET_KEY_FILE` | The command mounts no volume at the key directory, also with a read-only root filesystem. Add `-v dockmin-key:/key` and recreate the container |
| `The key directory <dir> is a tmpfs mount; a key written there is lost when the container stops` (`ramfs` when that is the type) | `Mount a volume at <dir>, for example -v <volume-name>:<dir>, or set DOCKMIN_SECRET_KEY_FILE` | The key directory is a `--tmpfs` mount that the process user can open. Mount a volume instead. A `--tmpfs` mount the process user cannot open gives the owner text below |
| `The key directory <dir> must be owned by uid <n> and writable only by it` | `A named volume takes this from the image, which runs as uid 10001. For a bind mount, give the host directory to uid <n>, the user this process runs as, with mode 0700` | A bind mount with another owner, a directory writable by its group or others, or a directory the process user cannot open, enter or write. Fix the bind mount as root: the first command below |
| `The key file <file> is a symbolic link`, `The key file <file> is not a regular file`, `The key file <file> cannot be read`, `The key file <file> does not hold base64 of 32 bytes` | `Restore the key file from its copy. dockmin never replaces an existing key file` | The file is damaged, replaced, or unreadable by uid 10001. Put the copy back ([Restoring the key volume](Administration-Backups#restoring-the-key-volume)) |
| `The key file <file> could not be used: <strerror>` | `Check the permissions and free space of <dir>` | The operating system refused a read, the create or the hard link that publishes the key. Check the volume's free space, that it is not mounted read-only, and that it supports hard links |
| `The previous key, from <previous source>, must differ from the key in <file>` | `Remove <variable>, or set it to the key the stored data was sealed with before the current one`, with `<variable>` the previous-key variable that is set | The previous key equals the key in `secret.key` |

Give a bind-mounted host directory to uid 10001, as root:

```bash
chown 10001:10001 <host-directory>
chmod 0700 <host-directory>
```

A key file with group or other permission bits is read, and the start continues with the warning `encryption_key_file_permissions` ([Key directory](Administration-Encryption#key-directory)).

A key that cannot be found is not recoverable: [Key loss](Administration-Encryption#key-loss).

### `database`

Applied migrations are matched to the image's migration files by version and name.

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `The database schema check failed: This database was written by a newer dockmin (schema N; this build supports M). Run that version or restore a backup.` | The image is older than the database. The database is not modified. `remedy`: `The database has not been modified` | Run the image version that wrote it, or restore a backup taken before the upgrade | `3` |
| `The database schema check failed: migration NNNN_<name>.sql was applied but its file is missing` | The image lacks a migration file that the database applied | Reinstall the image the database was created with, or restore a backup | `3` |
| `The database schema check failed: migration NNNN was applied as NNNN_<name>.sql but the file is now NNNN_<other-name>.sql` | The image names a migration differently from the database's record | The same | `3` |
| `The database schema check failed: the ledger has no record of migration NNNN` | The database's migration record has a gap | The same | `3` |
| `The database schema check failed: migration file <file> does not match NNNN_name.sql`, `migration version NNNN is used by <file> and <file>`, `migration NNNN is missing before <file>` | The image's own migration files are malformed | Reinstall the image | `3` |
| `The database schema check failed: migration <file> would leave a row in <table> referring to a row that does not exist` | A migration failed its foreign-key check. The migration is rolled back | Reinstall the image, or restore a backup | `3` |
| `The database could not be opened: ...` | The file is damaged or is not a dockmin database, or a migration statement failed. A migration runs in one transaction, so a failed one is rolled back and the database stays at the earlier schema | Check that the data directory holds the dockmin database. Restore a backup if the file is damaged | `3` |
| `The database could not be read while checking the encryption key: ...` | The database opened and migrated, then failed while the key was checked against it | Check that the data directory holds the dockmin database and that the file is not damaged. Restore a backup if it is | `3` |
| `The database file could not be created or read: ...` | Permissions or free space | Check the data directory's permissions and free space | `3` |

The `remedy` of every `database schema check failed` row except the newer-schema row reads: `Reinstall the image the database was created with, or restore a backup taken with it`. The `remedy` of the other rows reads: `Check that the data directory holds the dockmin database and that the file is not damaged; restore a backup if it is`, and for the last row `Check the permissions and free space of the data directory`.

### `data_directory`

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `The data directory <dir> does not exist`, `<dir> is not a directory` | No volume is mounted at the data directory | Create the directory or mount a volume there, writable by uid 10001 | `3` |
| `The data directory <dir> is a symbolic link` | The path is a link | Point `DOCKMIN_DATA_DIR` at the directory itself, or mount a volume there | `3` |
| `The data directory <dir> is not writable` | The process user cannot write | Make the directory writable by uid 10001 | `3` |
| `Another dockmin process is using the data directory` | A second process, or a second container on the same volume, holds the instance lock | Stop the other process, or give this one its own data directory ([Single instance](Reference-Deployment#single-instance)) | `3` |
| `The data directory's lock file cannot be used` | `dockmin.lock` is not a regular file owned by the process user | Check the lock file in the data directory | `3` |
| `The SQLite temporary directory <path> cannot be created` | `SQLITE_TMPDIR` names a path the process user cannot create | Change `SQLITE_TMPDIR` or its parent's permissions | `3` |
| `The Docker connection work directory under the data directory cannot be prepared` | A link or a file stands where the directory should be | Check that it is a real directory owned by the process user | `3` |
| `A Compose work or stacks directory under the data directory cannot be prepared` | A link or a file stands where `compose-work` or `stacks` should be, or `stacks` belongs to another user | Check that they are real directories owned by the process user | `3` |

### `compose_cli`

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `The configured Docker Compose binary must be an absolute path to an executable file owned by root or the server's user, with no group or world write access to the file or its directory.` `remedy` reads `Set DOCKMIN_COMPOSE_BINARY to an absolute path to an executable that only root or the process user can write, or unset it to search PATH` | The configured binary is not an absolute path to an executable owned by root or the process user, or its file or directory is writable by group or others | Set an absolute path to a binary that meets [Binary location](Reference-Deployment#binary-location), or unset the variable | `3` |
| `The Docker Compose binary was not found; stacks are unavailable`, `The Docker Compose version could not be read; stacks are unavailable`, `Docker Compose <version> is older than v2.32.0; stacks are unavailable` | Not fatal: the process starts. See [Compose missing or too old](#compose-missing-or-too-old) | | none |

### `buildx`

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `The buildx binary was not found or is not trusted; stack builds are unavailable`. `remedy` reads `The image carries buildx; otherwise install it, or set DOCKMIN_BUILDX_BINARY to an absolute path to an executable that only root or the process user can write` | No buildx binary was found, or the one found or configured is not owned by root or the process user, or its file or directory is writable by group or others. A rejected binary is logged as `buildx_binary_untrusted` or `buildx_binary_unusable`, with the path | Use the image, which carries `/usr/local/libexec/docker/cli-plugins/docker-buildx`. Outside the image, install buildx or set `DOCKMIN_BUILDX_BINARY` to a binary that meets [Buildx binary](Reference-Deployment#buildx-binary). Restart | none |

- The check is never fatal. The process starts and only a deploy that would build an image is refused (`buildx_unavailable`).

### `tls_material`

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `DOCKMIN_TLS_CERT_FILE and DOCKMIN_TLS_KEY_FILE must be set together` | Only one of the two variables is set | Set both, or neither | `2` |
| `The certificate file <path> cannot be read: <reason>`, `The private key file <path> cannot be read: <reason>`, `The certificate or key file cannot be read: <reason>` | The file is missing or unreadable by uid 10001 | Check the path, the mount and the owner. Files are read by uid 10001 | `3` |
| `The certificate file <path> holds no PEM certificate` | The file is not PEM | Supply a PEM certificate chain, leaf first | `3` |
| `The certificate in <path> has fields that cannot be read` | The certificate is malformed | Issue the certificate again | `3` |
| `The private key in <path> does not match the first certificate in <path>` | Key and certificate are from different pairs, or the chain starts with a different certificate | Put the leaf certificate first and use its key | `3` |
| `The private key in <path> is not an unencrypted PEM key that can be loaded` | The key is encrypted or malformed | Supply an unencrypted PEM key. A passphrase is removed as described in [TLS material](Usage-Environments#tls-material) | `3` |
| `The certificate directory <dir> is a symbolic link`, `The certificate directory <dir> is not a directory` | `<dir>` is `<DOCKMIN_DATA_DIR>/server-tls`, where the self-signed pair is kept, and it is a link or a file | Remove the link or file. dockmin creates the directory and the pair at the next start | `3` |
| `The self-signed certificate in <dir> cannot be stored: <strerror>` | The data directory is read-only, full, or not writable by the process user | Make `<DOCKMIN_DATA_DIR>` writable by uid 10001, free space, or set `DOCKMIN_TLS_SELF_SIGNED=false` and supply a certificate or terminate TLS in front of dockmin | `3` |

The `remedy` of a configuration error and of a file failure with a supplied certificate reads: `Check that DOCKMIN_TLS_CERT_FILE and DOCKMIN_TLS_KEY_FILE are readable by the user the process runs as (uid 10001 in the image) and hold a PEM certificate and its key`.

The `remedy` of a self-signed pair failure reads: `Make <data_dir> writable by the user the process runs as (uid 10001 in the image), or set DOCKMIN_TLS_SELF_SIGNED=false and supply a certificate or terminate TLS in front of dockmin`, with the data directory's path in place of `<data_dir>`.

### `jwt_settings`

The `remedy` reads: `Set DOCKMIN_JWT_HEADER, DOCKMIN_JWT_ISSUER, DOCKMIN_JWT_AUDIENCE and exactly one of DOCKMIN_JWT_JWKS_URL (an https: URL) or DOCKMIN_JWT_KEY_FILE (a PEM public key or a JWKS document), or remove every DOCKMIN_JWT_* setting to turn access platform sign-in off`.

| Symptom (`problem`) | Cause | Action | Exit |
|---|---|---|---|
| `DOCKMIN_JWT_HEADER must be set when <variable> is set` | `DOCKMIN_JWT_ISSUER`, `DOCKMIN_JWT_AUDIENCE`, `DOCKMIN_JWT_JWKS_URL`, `DOCKMIN_JWT_KEY_FILE` or `DOCKMIN_JWT_CREATE_USERS` is set without the header | Set `DOCKMIN_JWT_HEADER`, or remove the variable | `2` |
| `<variable> must be set when DOCKMIN_JWT_HEADER is set` | The issuer or the audience is missing | Set it | `2` |
| `exactly one of DOCKMIN_JWT_JWKS_URL or DOCKMIN_JWT_KEY_FILE must be set when DOCKMIN_JWT_HEADER is set` | Neither key source is set, or both | Keep one | `2` |
| `DOCKMIN_JWT_KEY_FILE does not point to a readable file` | The path is missing, or the file is unreadable by uid 10001 | Check the path, the mount and the owner | `2` |
| `DOCKMIN_JWT_KEY_FILE is not usable: <reason>` | The file is neither a PEM public key nor a JWKS document | Supply a PEM public key or a JWKS document | `2` |
| `DOCKMIN_JWT_KEY_FILE holds no key that can verify a permitted algorithm` | Every key is private, symmetric, not for signing, or declares an algorithm outside `DOCKMIN_JWT_ALGORITHMS` | Supply a public signing key that fits the algorithms | `2` |
| `DOCKMIN_JWT_JWKS_URL: ... must be an https: URL without credentials or a fragment` | The URL is not `https:`, is over 2,048 characters, or carries credentials or a fragment | Correct the URL | `2` |
| `DOCKMIN_JWT_ALGORITHMS: ... entries must be among ES256, ES384, ES512, RS256, RS384, RS512, PS256, PS384, PS512, EdDSA` | An entry is not one of the ten permitted algorithms. `none` and `HS*` are not | Correct the list | `2` |
| `DOCKMIN_JWT_HEADER: ... must be a header name of letters, digits and hyphens` | The header name has another character, or is over 64 characters | Correct the name | `2` |
| `DOCKMIN_JWT_IDENTITY_CLAIM: ... must be a claim name, or dotted names for a nested claim` | The claim path has another character, an empty name, or more than 8 names. The same message names `DOCKMIN_JWT_USERNAME_CLAIM` | Correct the path | `2` |
| `DOCKMIN_JWT_ISSUER: ... must be text without control characters`, and the same for `DOCKMIN_JWT_AUDIENCE` | The value is empty or holds a control character | Correct the value | `2` |

The settings are described in [Settings](Usage-Teams-and-Users#settings).

| Symptom while running | Cause | Action |
|---|---|---|
| `jwt_refused` lines with one `reason`, and nobody is signed in | The token fails a verification step ([Verification](Usage-Teams-and-Users#verification)). `bad_issuer`, `bad_audience`, `alg_not_allowed`, `key_type_mismatch`, `bad_signature` and `missing_claim` point at a setting or a key; `expired`, `not_yet_valid`, `iat_future` and `too_old` at the clock or `DOCKMIN_JWT_MAX_AGE`; `malformed` and `too_long` at the header's content | Compare the token's claims with the settings |
| `jwt_refused` with `keys_unavailable`, `unknown_kid` or `kid_required`, and `jwks_fetch_failed` lines | The keys cannot be fetched or do not hold the token's `kid` ([JWKS URL](Usage-Teams-and-Users#jwks-url)). `address_refused` means the URL's address is internal | Read the `reason` of `jwks_fetch_failed`. For `address_refused`, set `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` ([Address policy](Reference-Security#address-policy)) |
| A verified user is sent to the login page, and the `login` row reads `jwt_unlinked` | No account is linked to the identity | Enter the identity on the user's page, or turn on `DOCKMIN_JWT_CREATE_USERS` ([Linking accounts](Usage-Teams-and-Users#linking-accounts)) |
| A signed-in user is signed out at every request, and `logout` rows read `platform_assertion_ended` | The platform does not send the header on every request, or the token fails | Make the platform send the header on every request it forwards |

### Other startup failures

| Symptom | Cause | Action | Exit |
|---|---|---|---|
| `configuration_invalid` with a `problem` naming a variable | A variable is outside its range or form ([Configuration](Reference-Configuration)), or two listeners share a port (`DOCKMIN_HTTPS_PORT must differ from DOCKMIN_PORT`), or `DOCKMIN_HTTP_MODE` is `redirect` or `off` with TLS off (`DOCKMIN_TLS_SELF_SIGNED=false` and no certificate files), or `DOCKMIN_TLS_SELF_SIGNED` is not a boolean (`invalid configuration: DOCKMIN_TLS_SELF_SIGNED: Input should be a valid boolean, unable to interpret input`) | Correct the named variable | `2` |
| uvicorn reports an error binding an address | The port is in use, or `DOCKMIN_HOST` is not an address of the container | Change `DOCKMIN_PORT` or `DOCKMIN_HTTPS_PORT`, or stop the process that holds it | `3` |
| `Application startup failed. Exiting.` after a `startup_check_failed` line | The check above it | Act on that line | `3` |

## Certificate warning

| Symptom | Cause | Action |
|---|---|---|
| The browser shows a certificate warning on the first visit to `https://<server-host>:8443/` | dockmin serves a self-signed certificate that no browser trusts. Expected ([Self-signed certificate](Administration-Certificate#self-signed-certificate)) | Read the fingerprint from the log, compare it with the fingerprint in the browser's certificate viewer, then accept the certificate for this address |
| The warning names the address or host name as a mismatch | The certificate holds only `localhost`, `127.0.0.1` and `::1` | Compare the fingerprint and accept, or supply a certificate for the name in use |
| The warning returns after the certificate was accepted | A new certificate was generated: the log line `tls_certificate_self_signed` has `generated` `true` and a `reason` (`missing`, `unusable`, `expiring`, `expired` or `not_yet_valid`), or the data volume is new | Compare the new fingerprint with the log and accept it again |
| A browser warning is not acceptable for this deployment | A browser removes the warning only for a certificate that a trusted authority issued for the name in use | Supply a certificate with `DOCKMIN_TLS_CERT_FILE` and `DOCKMIN_TLS_KEY_FILE`, or terminate TLS at a reverse proxy ([Behind a reverse proxy](Getting-Started-Installation#behind-a-reverse-proxy)) |

Read the fingerprint, for a container named `dockmin`:

```bash
docker logs dockmin | grep tls_certificate_self_signed
```

The certificate page shows the same fingerprint after sign-in ([Certificate page](Administration-Certificate#certificate-page)).

## A proxy receives a redirect

| Symptom | Cause | Action |
|---|---|---|
| A reverse proxy, or a check that calls the plain port, receives `308` answers with `Location: https://…:8443/…`. A request other than `GET` or `HEAD` receives `400` with the body `Use HTTPS` | The plain listener redirects because `DOCKMIN_TLS_SELF_SIGNED` is at its default `true` | Set `DOCKMIN_TLS_SELF_SIGNED=false` and recreate the container ([Behind a reverse proxy](Getting-Started-Installation#behind-a-reverse-proxy)) |

## Unreachable environment

An administrator's environment page shows the status and, for `unreachable`, the stage that failed. Run `Test connection` for the current result ([Connection test](Usage-Environments#connection-test)). Messages are quoted by their first sentence; the full texts are in [Failures by stage](Usage-Environments#failures-by-stage).

### Local socket

| Stage | Symptom | Cause | Action |
|---|---|---|---|
| Socket | `Nothing exists at this path.` | The socket is not mounted at that path | Mount `/var/run/docker.sock` into the container, or enter the mounted path |
| Socket | `Permission denied. The account this product runs as cannot open the socket; check its owner, group and mode.` The environment list reads `Failed at Socket`, and the Docker Engine stage reads `Not run.` | The process user (uid 10001) is not in the socket's group | Recreate the container with the socket's group: add `--group-add "$(stat -c %g /var/run/docker.sock)"` to the `docker run` command. With Compose, set `DOCKER_GID` to the socket's group id and recreate the container |
| Socket | `The socket exists but nothing is listening on it.` | The Docker daemon is not running | Start the daemon |
| Socket | `The path exists but is not a Unix socket.`, `The path is too long to be a Unix socket address.` | The path names a file or directory, or exceeds 107 bytes | Enter the socket's path |
| Docker Engine | Any message | The daemon answered unexpectedly | [Docker Engine stage](#docker-engine-stage) |

```bash
docker exec dockmin ls -l /var/run/docker.sock
docker exec dockmin id
stat -c %g /var/run/docker.sock
```

The first two commands show the socket's owner and group and the container user's groups. The third, run on the Docker host, prints the group id for `--group-add` or `DOCKER_GID`. With Compose, run the first two as:

```bash
docker compose exec dockmin ls -l /var/run/docker.sock
docker compose exec dockmin id
```

### Remote host over TLS

| Stage | Symptom | Cause | Action |
|---|---|---|---|
| DNS lookup | `The name does not exist.`, `The DNS server did not answer.`, `The DNS server reported a permanent failure for this name.`, `The name resolved, but to no usable address.` | The name is wrong, has no address record, or the resolver in the container fails | Check the name. Resolve it from the container with the first command below. Enter the IP address instead if DNS is the problem |
| TCP connection | `The connection was refused.`, `There is no route to the host.`, `The connection timed out after 5 seconds.` | Nothing listens on the port, no network path, or a firewall drops the packets | Check that `dockerd` listens on the port and that the firewall admits the dockmin server |
| TCP connection | `This server's network policy blocked the connection.` | An egress policy on the dockmin server | Allow the connection |
| TCP connection | `This address is on this server or a link-local network.` | Every address the name resolves to is internal | Use an address that is not internal, or permit it with `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` ([Address policy](Reference-Security#address-policy)) |
| TLS handshake | `The server does not speak TLS on this port.` | The port is plain TCP (`2375`) or another service | Use the TLS port, normally `2376` ([Configure the daemon](Usage-Environments#configure-the-daemon)) |
| TLS handshake | `The server does not offer TLS 1.2 or later` | The daemon or a proxy is limited to older protocols | Allow TLS 1.2 or later |
| TLS handshake | `The server rejected the client certificate.`, `The server closed the connection during the TLS handshake.`, `The server closed the connection after the handshake without answering.`, `The TLS handshake completed, but the server did not answer a request within 10 seconds.` | The daemon's CA does not trust the client certificate's issuer, the certificate is expired, or the service is not a Docker daemon | Check the client certificate against the daemon's `tlscacert` with the second command below |
| TLS handshake | `The server rejected the handshake.` | The daemon does not accept the client certificate, or shares no cipher suite with the dockmin server | Check the client certificate as in the row above, and the cipher suites the daemon accepts |
| TLS handshake | `The stored TLS material could not be loaded.` | Stored material is unreadable | Edit the environment and supply all three items again |
| Server certificate | `The server's certificate was not issued by the supplied CA certificate.` | The CA certificate given to dockmin did not sign the daemon's certificate | Supply the CA that signed it |
| Server certificate | `The server's certificate does not cover the host name or IP address used to reach it.` | The Subject Alternative Name list lacks the exact value in `Host` | Issue a server certificate with that name ([Generate TLS material](Usage-Environments#generate-tls-material)), or enter a name the certificate holds |
| Server certificate | `The server's certificate has expired.`, `The server's certificate is not valid yet.` | The certificate's dates, or clock skew between the hosts | Renew the certificate. Correct the clocks |
| Server certificate | `The server's certificate is not valid for server authentication.`, `The server's certificate, or its issuer, uses a key or digest that is too weak.` | The certificate lacks `serverAuth`, or uses a weak key or signature | Issue a new certificate |
| Docker Engine | Any message | The daemon answered unexpectedly | [Docker Engine stage](#docker-engine-stage) |

```bash
docker compose exec dockmin getent hosts <host-name>
openssl s_client -connect <host-name>:2376 -CAfile ca.pem -cert client-cert.pem -key client-key.pem -verify_hostname <host-name> < /dev/null
openssl x509 -in server-cert.pem -noout -dates -ext subjectAltName
```

The first command resolves the name from the container. The second, run from any machine that reaches the daemon, prints `Verify return code: 0 (ok)` when the daemon's certificate chain and name verify, and `Verify return code: 62 (hostname mismatch)` when the name is missing from the certificate. An `alert` line after the handshake means the daemon rejected the client certificate. For a host given as an IP address, replace `-verify_hostname <host-name>` with `-verify_ip <host-address>`. The third prints the server certificate's dates and names. For a container started with `docker run`, write `docker exec dockmin` in place of `docker compose exec dockmin`.

### Docker Engine stage

| Symptom | Cause | Action |
|---|---|---|
| `The daemon's API version is <version>; this product supports 1.44 to 1.51.` Status `version_unsupported` | Docker is too old | Upgrade the daemon to API 1.44 or later ([Supported Engine API versions](Usage-Environments#supported-engine-api-versions)) |
| `The daemon only accepts API versions newer than the 1.44 to 1.51 this product supports.` Status `version_unsupported` | dockmin is older than the daemon requires | Upgrade dockmin |
| `The Engine refused the request as unauthorised or forbidden (HTTP <code>).`, `A gateway in front of the Engine answered HTTP <code>.` | An authorisation plugin, gateway or proxy sits in front of the daemon | Allow dockmin's requests through it |
| `The connection to the Engine was lost or timed out.` | A network fault or an overloaded daemon | Retry. Check the daemon's load and the network |
| `The endpoint answered, but not like a Docker Engine API`, `The endpoint sent far more data than a Docker Engine API replies with.` | The address and port belong to another service | Correct the host and port |

## Compose missing or too old

| Symptom | Cause | Action |
|---|---|---|
| Stack pages render and every Compose operation is refused. The log has `compose_binary_not_found` and a non-fatal `startup_check_failed` with `check` `compose_cli` | No Compose binary was found | In the image the binary is at `/usr/local/libexec/docker/cli-plugins/docker-compose`. Outside the image, install Compose v2.32.0 or later, or set `DOCKMIN_COMPOSE_BINARY` to it. Restart |
| The log has `compose_below_floor`. The environment page shows the badge `Below the required v2.32.0` | The binary is older than v2.32.0 | Install a newer binary. The image carries v5.5.1. Restart: the version is read once at start |
| The log has `compose_version_unreadable` | The binary does not print a version | Check the binary. The next page load or operation tries again, at most once a minute |
| The log has `compose_binary_untrusted` with a path | The binary or its directory is not owned by root or the process user, or is writable by group or others | Correct ownership and mode. The search continues and skips that path |
| Stack operations fail with `compose_unavailable` (`Docker Compose was not found on this server.`), `compose_version_unsupported` (`Docker Compose <version> is installed on this server; dockmin requires <floor> or later.`) or `compose_busy` (`The server is busy. Try again shortly.`) | No binary, a version below the floor, or no Compose process slot free | See the rows above. For `compose_busy`, retry when running operations end ([Docker Compose requirement](Usage-Stacks#docker-compose-requirement)) |

```bash
docker compose exec dockmin /usr/local/libexec/docker/cli-plugins/docker-compose version
```

The command prints the version of the binary the image carries. For a container started with `docker run`, write `docker exec dockmin` in place of `docker compose exec dockmin`.

## `credential_missing` binding

| Symptom | Cause | Action |
|---|---|---|
| An image reference shows `Credential set deleted` or `Registry address changed`, with `was <set name>`. The binding filter `Credential set deleted` (`credential_missing`) lists it | The registry credential set the image was pulled with was deleted, or its registry address was edited | An administrator pulls the reference again, choosing another set or `Anonymous` (`Repair` opens the pull form). The binding is then rebound ([When a set is deleted or its address changes](Usage-Images#when-a-set-is-deleted-or-its-address-changes)) |
| A pull or recreate answers `409`: `The credential set this image was pulled with has been deleted. Choose another set.` | The same | The same. A standard user cannot choose a set |
| The update check reports `unknown` with reason `credential_missing` | Automatic updates for the reference stopped | Repair the binding |
| A `container-updater` run fails with reason `credential_missing`, an `image_update_refused` event is written, and later runs count the object `held` | The rule's object has a reference bound to the lost set | Repair the binding. The hold ends when the cause changes or a user acts on the object ([Refusals and holds](Operations-Automation#refusals-and-holds)) |

## dockmin could not identify its container

How the container is found: [How the container is identified](Administration-Instance#how-the-container-is-identified).

| Symptom | Cause | Action |
|---|---|---|
| The Instance page reads `dockmin could not identify a container of its own.` No container is marked `This dockmin instance` | dockmin does not run in a container, or neither `/proc/self/mountinfo` nor `/proc/self/cgroup` names a 64-digit container id | Expected outside a container. In a container, check that the runtime mounts `/etc/hostname`, `/etc/hosts` and `/etc/resolv.conf` from a `/containers/{id}/` path, or names the container in the cgroup path |
| The Instance page names the container and reads `No environment reports this container.` | No environment's Engine reports a container with this id and this process's host name | Add an environment for the Docker host dockmin runs on ([Add a local socket environment](Usage-Environments#add-a-local-socket-environment)). The id is protected on every environment; volumes, the Compose project and the Swarm service are not protected until a home is found |
| The Instance page lists no environment with the badge `Used for restart`, and the restart is refused with `dockmin could not confirm its container through an environment on the local Docker socket, so it cannot restart itself.` | Only environments reached over TLS report the container | Add an environment of type `Local Docker socket` for the host, then reload the page |
| A notice reads `Some environments could not be read, so the protection of volumes, Compose projects and Swarm services may be incomplete there. dockmin keeps trying.` and lists them | Those environments did not answer when dockmin looked | [Some environments could not be read](#some-environments-could-not-be-read) |

## The restart did not happen

| Symptom | Cause | Action |
|---|---|---|
| The restart is refused with `A restore is in progress. Restart is unavailable until it finishes.` | A restore is running | Wait for the restore to end ([Restore](Administration-Backups#restore)) |
| The restart is refused with `Stored secrets are being re-encrypted. Restart is unavailable until it finishes.` | A [re-encryption](Administration-Encryption#offline-gate) holds the instance offline | Wait for it to end |
| The restart is refused with `An update is in progress, or an earlier one left files in the data directory. Restart is unavailable until they are gone.` | An update helper is running, or an earlier update left files under `update/` in the data volume | Wait, or follow [Manual recovery](Administration-Instance#manual-recovery) |
| `dockmin's container is no longer on the host that was used to find it.` | The container was removed or recreated after dockmin found it | Reload the Instance page. Restart the container from the host; dockmin identifies its new container at start |
| `dockmin's container could not be read from the host.` | The container's inspect reply is larger than dockmin reads | Restart the container from the host |
| `The restart could not be recorded, so it was not sent. Try again.` | The activity row could not be written | Try again. Read the server log for a database error |
| `dockmin has restarted since this page was shown. Review the instance again.` | The process started again between the page and the confirmation | Open the Instance page again |
| The restarting page reads `dockmin did not restart within N seconds.` and `instance_restart_failed` is in the log | The Engine refused the restart call or it failed. The call is made after the response, so the browser is not told | Read `docker logs dockmin` and `docker inspect dockmin` on the host, and restart the container there |
| The restarting page reads `dockmin did not restart within N seconds.` and `instance_restart_unconfirmed` is in the log | The Engine did not answer the restart call, so its outcome is unknown | Read `docker ps` on the host; restart the container there if it did not restart |
| The restarting page reads `dockmin did not restart within N seconds.` and the container is stopped | The container stopped and did not start | Read the log line of the failed start ([Startup self-check failures](#startup-self-check-failures)). Without a restart policy, start the container from the host |
| The activity log holds `instance_restart_requested` with result `success` and nothing restarted | `success` records that the restart was requested, before it is sent | Look for `instance_restart_failed` or `instance_restart_unconfirmed` in the log |

`N` is the container's stop timeout, 45 s when unset, plus 60 s.

## Some environments could not be read

| Symptom | Cause | Action |
|---|---|---|
| The Instance page lists environments under the notice `Some environments could not be read, so the protection …` | When dockmin looked for its own container, those environments did not answer within 8 s, and none had been read before | [Unreachable environment](#unreachable-environment). dockmin looks again after 5 s, then 10 s, doubling up to 5 minutes, until every environment has answered. Creating, deleting or testing an environment, or changing its connection, looks again at once |

Until every environment has answered, a volume, a Compose project or a Swarm service of dockmin's container may be removable from those environments. The container's own id is protected everywhere.

## Backup refused: a stored secret cannot be read

| Symptom | Cause | Action |
|---|---|---|
| A download answers `503` with `A stored secret could not be read with the loaded key, so no backup was made. Affected values: <table>.<column> row <id>, ...`. A `backup-syncer` run ends `failure` with reason `secret_unreadable`. The log line `backup_secret_unreadable` carries `locations` | A stored secret fails to open: no loaded key opens a value below the current key version, a value at the current version does not open, or a value is not shaped like ciphertext. The message names at most 10 locations ([Production failures](Administration-Backups#production-failures)) | Open `Administration > Encryption` and choose `Check stored secrets`. The run lists every such location, up to 100, with a link to the page that owns each value. Enter the secret again on that page, then produce the backup ([Failed values](Administration-Encryption#failed-values)) |
| The same message without `Affected values:` | After the secrets were re-sealed in the copy, a value remained at another key version | Retry. If it recurs, run `Check stored secrets` and correct the values it lists |
| `Check stored secrets` lists a value under `No page of its own` | The value belongs to a stack variable or a stack file, which have no page of their own | Edit the stack and enter the variable or the file's content again |

## Re-encrypting stored secrets

The steps and the refusals: [Re-encrypt stored secrets](Administration-Encryption#re-encrypt-stored-secrets).

| Symptom | Cause | Action |
|---|---|---|
| Every request that changes something answers `503` with `This instance is re-encrypting stored secrets. Changes are refused until it finishes.` The page reads `Offline while stored secrets are re-encrypted` | A re-encryption holds the offline gate | Reload in a few seconds. Pages can still be read, and an administrator follows the run from `Administration > Encryption`, `Latest run`. The gate reopens when the run ends |
| The Encryption page answers `409` with `A check or re-encryption is already running.` | Another run holds the claim | Wait for it to end |
| The review answers `409` with `N jobs are running; wait for them to finish.` | Image or stack jobs are running | Wait for them to end, then review again |
| The review answers `409` with `Every stored secret is already at key version N.` | Nothing is below the current key version | None. The re-key is complete; remove the previous key ([After the re-encryption](Administration-Encryption#after-the-re-encryption)) |
| The start answers `409` with `Changed since it was reviewed; nothing was rewritten.` | A secret was added, changed or removed after the review page was loaded | Review again |
| The start answers `409` with `A restore is in progress. Try again when it has finished.`, `A backup is still being produced. Try again shortly.` or `An update is in progress. Restore is unavailable until it finishes.` | A restore, a backup download or scheduled backup, or an update holds the instance | Wait for it to end, then review again |
| The run ends `Finished with values that did not open` | Values remain that no loaded key opens, that do not open with the current key, or that are not shaped like ciphertext | Enter each listed value again on the page named, then re-encrypt again ([Failed values](Administration-Encryption#failed-values)) |
| A re-encryption ends `Interrupted`. The log line `encryption_rekey_failed` names the error class when an error ended it | The application was stopping, or an error ended the run. A re-encryption never ends `Failed` | Every stored secret is still readable. Start the re-encryption again; it resumes |
| A check ends `Interrupted` or `Failed`. The log line `encryption_check_failed` names the error class for `Failed` | The application was stopping (`Interrupted`), or an error ended the check (`Failed`). Nothing was changed | Start the check again. Read the server log when it fails again |
| The run page answers `404` | A finished run is kept for 30 minutes, and a restart of the process drops it | Open `Administration > Encryption` |

## Restore refusals and failures

Every restore step and reason: [Restore](Administration-Backups#restore). The progress page shows the failing step and its reason. Exit statuses and messages of the command-line restore: [Restore from the command line](Administration-Backups#restore-from-the-command-line).

### Before the key

| Symptom | Cause | Action |
|---|---|---|
| `403` on a restore page | The request used an API token or had no browser session | Sign in with a browser |
| `409`: `A restore is already in progress.` | Another restore is engaged | Open `/restore`. Its owner, or another administrator before the key is confirmed, cancels it |
| `409`: `N jobs are running; wait for them to finish.` | Image or stack jobs are running | Wait for them to end, then begin again |
| `409`: `A backup is still being produced. Try again shortly.` | A download or scheduled backup held the production lock for more than 30 s | Retry |
| `409`: `Stored secrets are being re-encrypted. Try again when it has finished.` | A re-encryption on the Encryption page holds the instance offline | Wait for it to end ([Re-encrypting stored secrets](#re-encrypting-stored-secrets)) |
| `409`: `A stored-secret check is running. Try again when it has finished.` | A check on the Encryption page is running | Wait for it to end |
| `409`: `An update is in progress. Restore is unavailable until it finishes.` | An update helper is running, or an earlier update left files under `update/` in the data volume | Wait, or follow [Manual recovery](Administration-Instance#manual-recovery) |
| Upload refused, `archive_invalid` | The file is not a dockmin archive, or is damaged or malformed | Download the archive again and compare its SHA-256 with the value on the `Backups` page |
| Upload refused, `format_unsupported` | The manifest's `format_version` is not `1` | Use an archive made by dockmin |
| Upload refused, `insufficient_space` | The data volume has less than 256 MiB free | Free space on the volume |
| Upload ends `408` | The body sent no data for 120 s | Retry on a stable connection |
| `From object storage` is empty and reads `No object storage target is configured.` | The restore lists archives through an existing target. Targets cannot be added while the instance is offline | Cancel the restore, add the target, and begin again ([Restore from object storage](Administration-Backups#restore-from-object-storage)) |
| `The archives could not be listed` | The listing was refused or failed. The page shows the reason's message | Check the target ([Connection test](Administration-Object-Storage#connection-test)). `refused` means the credential cannot list the backup folder |
| `The target's credential cannot be read; enter it again.` | The target's stored secret does not open with the running key | Cancel the restore and enter the secret again on the target's page |
| `The object storage target no longer exists.` | The target was deleted | Cancel, add the target again, and begin again |
| `Choose an archive from the list.` | The key is not in the listing the page holds | List the archives again, then choose |
| `The archive is larger than the size limit.` | The object is over 7 GiB | Use another archive |
| `There is not enough free space on the data volume.` | Free space is below the archive's size plus 256 MiB | Free space on the volume |
| `The archive changed in the bucket since it was listed.` | The object's size differs from the listing | Refresh the list and choose again |
| `The archive could not be fetched.` or a reason's message, shown above the upload form on `/restore` | The fetch failed: the server answered with an error, the connection ended, or the archive failed the checks an upload also meets | Read the message, correct the cause and choose the archive again |
| `An upload is already in progress.` | An upload or a fetch is running | Wait for it to end, or cancel the restore |
| Upload fails with `413` or a reset connection | A reverse proxy limits the request body | Raise the proxy's body limit above the archive's size ([Reverse proxy limits for backups](Reference-Deployment#reverse-proxy-limits-for-backups)) |
| The review reads `The archive's schema version is N; this build supports up to M.` | The archive was made by a newer version | Run a version that supports schema N |
| The review reads `The archive has no enabled administrator with your user name.` | The signed-in username is not among the archive's administrators | Cancel, sign in as an administrator listed in the archive, and begin again. An archive lists at most 100 |
| The restore was cancelled, or ends `timed_out` or `shutdown` | An administrator cancelled, 15 minutes passed before the key was confirmed, or the application stopped | Begin again |

### The key step

| `reason` | Message | Cause | Action |
|---|---|---|---|
| (none) | `Confirm that this instance's data will be replaced.` | The confirmation box is unticked | Tick it |
| `key_missing` | `Enter the encryption key to continue.` | The field is empty | Enter the key |
| `key_invalid` | `The key must be the base64 text of exactly 32 bytes, as configured.` | The text is not a key | Enter the key exactly as configured |
| `fingerprint_mismatch` | `This key does not match the archive. It was made with key version N, fingerprint F.` | A different key | Find the key whose fingerprint is `F`. Without it the archive cannot be restored ([Key loss](Administration-Encryption#key-loss)) |
| `manifest_tampered` | `The archive manifest does not carry a valid signature for this key.` | The manifest was altered or damaged after the archive was made | Use an unaltered copy |
| (none) | `The uploaded archive changed since the review.` | Another upload replaced the archive | Review the archive again |

A rejected key leaves the instance offline with the archive in place. Retry, upload another archive or cancel.

### After the key

The live data is untouched unless the failure is at step 8 or 9.

| `reason` | Step | Cause | Action |
|---|---|---|---|
| `digest_mismatch` | 4, 5 | A member is truncated, damaged, out of order, or differs from the manifest | Download the archive again |
| `archive_invalid` | 4 | The archive's structure breaks the format | Use another archive |
| `insufficient_space` | 5 | Free space is below twice the members' size plus 64 MiB | Free space on the data volume |
| `database_invalid` | 6 | The staged database is not a dockmin database or fails its integrity checks. The log line `restore_database_findings` lists findings | Use another archive |
| `schema_newer` | 6 | The staged schema is newer than this build | Run a newer version |
| `migration_failed` | 6 | A migration of the older schema failed: a migration file is missing or named differently from the archive's record, or a statement failed (`The staged database could not be migrated.`) | Restore the archive with the version that made it, then upgrade that instance |
| `schema_mismatch` | 6 | After migration the schema differs from a fresh database's. The log line `restore_schema_mismatch` names the objects | Use another archive |
| `operator_not_in_archive` | 6 | The restored `user` table has no enabled administrator with your username | Sign in as an administrator that exists in the archive |
| `stack_files_inconsistent` | 6 | A stack file and its record disagree | Use another archive |
| `decrypt_failed` | 7 | The key does not open the archive's secrets. The message names a column and row, never a value | Use the key the archive was made with. If that key was entered, the archive is damaged |
| `reseal_failed` | 7 | A secret could not be re-encrypted under the running key | Use another archive |
| `swap_failed` | 8 | A move failed, or the running services did not stop within 15 s. Completed moves are reversed and the old data starts again. The log has `restore_swap_interrupted`, `restore_stop_failed` or `restore_requests_not_drained` | Retry when no request is in flight |
| `restart_failed` | 9 | The services did not start on the restored data. The restored files move to `restore/<restore-id>/rejected/` and the previous data returns. The log has `startup_check_failed` with `during` `restore` | Read that line ([Startup self-check failures](#startup-self-check-failures)), correct the cause and restore again |

### Failed gate

| Symptom | Cause | Action |
|---|---|---|
| Every page and `/api/health` answer `503`. The message reads `A restore failed and the data could not be put back. Restore the files by hand from the paths below, then restart the container.` The log has `restore_failed_unrecoverable` | A swap or restart could not be reversed | Follow the recovery in [Failed gate](Administration-Backups#failed-gate) |
