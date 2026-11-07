# Encryption

`Administration > Encryption` shows the key in use and how many stored secrets each key version holds, checks that the stored secrets open, and re-encrypts them under the current key.

<!-- screenshot: Administration-Encryption-1: the Encryption overview with the Key block, the Stored secrets by key version table and the Status line; an instance that completed the rotation stage of a re-key, so a previous key is configured and some secrets sit at version 1 while the current version is 2 -->

## Access

| Item | Fact |
|---|---|
| Who | Administrators, on every page and action. Any other role receives `403` |
| Where | `Administration > Encryption`, `/server/encryption` |
| Caching | Every answer carries `Cache-Control: no-store` |
| Secret values | Never shown. Pages name locations (table, column, row) and counts |

| Page or action | Route |
|---|---|
| Overview | `GET /server/encryption` |
| Start a check | `POST /server/encryption/check` |
| Review a re-encryption | `GET /server/encryption/rekey` |
| Start a re-encryption | `POST /server/encryption/rekey`. Fields `expected_counts`, `confirm_rekey` = `yes` |
| Run page | `GET /server/encryption/runs/{run id}`. The id is 32 hexadecimal digits; any other form answers `404`. An HTMX request receives the fragment, polled every 2 s while the run goes |

## Overview

| Section | Content |
|---|---|
| Key | `Held in`, `Key version`, `Fingerprint` (16 hexadecimal digits), `Previous key` |
| Stored secrets by key version | One row per secret column, one column per key version, the current version marked `(current)` |
| Status | `Below the current key` with the number of secrets sealed under an older key version, or `Current` when every stored secret is at the current version |
| Check stored secrets | The button that starts a check |
| Re-encrypt stored secrets | `Review and re-encrypt` while any secret is below the current version, otherwise `Nothing to re-encrypt.` |
| Latest run | A link to the newest check or re-encryption still kept (`Check of stored secrets` or `Re-encryption of stored secrets`), and whether it is running or finished |

| `Held in` reads | Key source |
|---|---|
| `the configured DOCKMIN_SECRET_KEY` | The variable |
| `the key in <path> (DOCKMIN_SECRET_KEY_FILE)` | The file |
| `the key in <key-directory>/secret.key` | The generated key |

| `Previous key` reads | Meaning |
|---|---|
| `the configured DOCKMIN_PREVIOUS_SECRET_KEY, fingerprint <digits>` | Supplied by variable |
| `the key in <path> (DOCKMIN_PREVIOUS_SECRET_KEY_FILE), fingerprint <digits>` | Supplied by file |
| `None configured` | No previous key is loaded |

Columns counted:

| Column |
|---|
| `registry_credential.secret_ciphertext` |
| `registry_credential.derived_secret_ciphertext` |
| `environment_tls_material.client_key_ciphertext` |
| `object_storage_target.secret_access_key_ciphertext` |
| `stack_variable.value_ciphertext` |
| `stack_file.content_ciphertext` |
| `notification_channel.secrets_ciphertext` |

- Counts cover well-formed values only. A value that is not shaped like ciphertext is reported by a check.
- The fingerprint of a key identifies it and reveals no part of it. The Backups page shows the same fingerprint for the key an archive was made with ([Backups](Administration-Backups#the-key-is-not-in-the-archive)).

## Check stored secrets

`Check stored secrets` starts a check at once. There is no confirmation.

| Item | Fact |
|---|---|
| Effect | Opens each well-formed stored secret sealed at the current key version and lists the ones that do not open, and the values not shaped like ciphertext, by table, column and row. Nothing is changed and the instance stays online |
| Not opened | Secrets below the current key version. The overview counts them; a re-encryption opens and rewrites them |
| Result | The browser opens the run page ([Run page](#run-page)) |
| Refused with `409` | `A check or re-encryption is already running.`, `A restore is in progress. Try again when it has finished.`, `The instance is shutting down.` The overview reloads with the message under `The check did not start` |
| Recorded | `encryption_values_checked` ([Activity log](#activity-log)). A check refused before it starts writes no row |

## Re-encrypt stored secrets

`Review and re-encrypt` opens a review page that names every column and count a re-encryption would rewrite. Nothing changes until the box `Re-encrypt the stored secrets listed above` is ticked and `Re-encrypt stored secrets` is chosen.

<!-- screenshot: Administration-Encryption-2: the review page "Re-encrypt N stored secrets?" with the column list, the ticked box and the Re-encrypt stored secrets button; an instance with secrets at key version 1 and current version 2 -->

| Item | Fact |
|---|---|
| Review | `GET /server/encryption/rekey` answers `409` with `Every stored secret is already at key version N.` when nothing is below the current version |
| Confirmation | The box `Re-encrypt the stored secrets listed above`. Not ticked: `422`, `Confirm that stored secrets will be re-encrypted.` |
| Review is current | The review carries a digest of the counts in `expected_counts`. A count that changed since the review is refused: `Changed since it was reviewed; nothing was rewritten.` |
| Rewrites | Every secret below the current key version, in batches of 100 rows. Each batch is its own transaction. A value is rewritten only if it is unchanged since it was read |
| Then | Every value already at the current version is opened and checked |
| Stopped half way | Every value stays readable at the version it holds. Starting again resumes where the run stopped |
| Gate | The offline gate is engaged for the whole run ([Offline gate](#offline-gate)) |
| Recorded | `encryption_rekey_requested` when it starts, `encryption_rekey_completed` when it ends ([Activity log](#activity-log)) |

| Refusal at the start | Message |
|---|---|
| Box not ticked (`422`) | `Confirm that stored secrets will be re-encrypted.` |
| Nothing below the current version | `Every stored secret is already at key version N.` |
| The instance is stopping | `The instance is shutting down.` |
| A check or re-encryption is running | `A check or re-encryption is already running.` |
| A restore is engaged | `A restore is in progress. Try again when it has finished.` |
| An update directory exists | `An update is in progress. Restore is unavailable until it finishes.` |
| Image or stack jobs are running | `N jobs are running; wait for them to finish.` |
| A backup is in production for more than 30 s | `A backup is still being produced. Try again shortly.` |
| The counts changed | `Changed since it was reviewed; nothing was rewritten.` |

Refusals other than the box answer `409`. Nothing is taken when a start is refused.

### Offline gate

| While the gate is engaged | Behaviour |
|---|---|
| Banner | `This instance is re-encrypting stored secrets. Changes are refused until it finishes.` |
| Requests that change something | `503` with `Retry-After: 30`, page `Offline while stored secrets are re-encrypted`. `/api` paths receive a JSON error with status `503`. `/restore` requests are refused too |
| Admitted | `GET`, `HEAD`, `OPTIONS`, `TRACE`, `POST /login`, `POST /login/continue` and `POST /logout`. Pages can be read |
| WebSocket handshakes | Closed with code `1013` |
| Scheduler | Suspended. Scheduled jobs stop, and runs in progress end `interrupted` with reason `suspended` |
| Backups | Downloads and scheduled backups are refused: `Stored secrets are being re-encrypted. Try again when it has finished.` |
| Restore | Refused: `Stored secrets are being re-encrypted. Try again when it has finished.` |
| Update | Refused: `Stored secrets are being re-encrypted. Updating is unavailable until it finishes.` |
| Restart | Refused: `Stored secrets are being re-encrypted. Restart is unavailable until it finishes.` |
| End | The gate reopens and the scheduler resumes, whatever the outcome |

### Run page

`GET /server/encryption/runs/{run id}` follows a check or a re-encryption. It refreshes every 2 s while the run goes, and has a `Refresh status` link.

| Section | Content |
|---|---|
| Progress panel | Title, outcome badge, and for a re-encryption a bar of rewritten against total |
| Counts | `Key version`, `Started`, `Finished` (when it has ended), for a re-encryption `Rewritten` and `Still below key version N`, then `Opened and checked`, `No key opens it`, `Does not open with the current key`, `Not well formed` |
| Values that did not open | The failed values ([Failed values](#failed-values)). Shown when there are any |
| After the re-encryption | Shown when a re-encryption completed: the variables to remove ([After the re-encryption](#after-the-re-encryption)) |

| Outcome | Message | Action |
|---|---|---|
| `Complete` | Re-encryption: `Every stored secret is now sealed under key version N.` Check: `Every stored secret opened.` | [After the re-encryption](#after-the-re-encryption) for a re-encryption |
| `Finished with values that did not open` | `N stored secrets are still below key version V.` and `N stored secrets did not open. Enter each of them again on the page named below.` | Enter each value listed again on the page named, then re-encrypt or check again |
| `Interrupted` | Re-encryption: `Stopped before it finished. Every stored secret is still readable. Re-encrypting again resumes where this stopped.` Check: `Stopped before it finished. Nothing was changed.` | Start again |
| `Failed` | `The run failed before it finished. Every stored secret is still readable. The server log names the reason; start it again when it is resolved.` | Read the server log, then start again |

- A check ends `Failed` when an error ends it, logged as `encryption_check_failed`. A re-encryption that an error ends reads `Interrupted` and is logged as `encryption_rekey_failed`.
- A run is kept in memory for 30 minutes after it ends. A process restart loses it, and its page answers `404`.

### Failed values

Both kinds of run list the values that did not open, at most 100 locations, with `Showing the first N of M locations.` beyond that.

| Column | Content |
|---|---|
| Location | `<table>.<column> row <id>` |
| Problem | `No key opens it`: no loaded key opens a value below the current version. `Does not open with the current key`: a value at the current version does not open. `Not a well-formed stored secret`: the value is not shaped like ciphertext |
| Page | A link to the page that owns the value: `Open the credential set`, `Open the object storage target` or `Open the notification channel`; `Open the environments` for an environment's TLS material. `No page of its own` for stack variables and stack files |

- Fix a value by entering the secret again on the page named. The new value is sealed at the current version.
- A value that no key opens stays below the current version after a re-encryption.

## Encryption at rest

Secret values are encrypted one field at a time before they are written to the database. The key is never stored in the database.

| Item | Value |
|---|---|
| Encrypted fields | Registry credential secret and derived secret; environment TLS client key; object storage secret access key; the value of a secret stack variable; the content of a secret stack file; the secret fields of a notification channel: the webhook signing key and secret header value, the ntfy and Gotify tokens, the Discord, Slack and Teams webhook addresses, and the email password |
| Cipher | AES-256-GCM with a random 96-bit nonce per value |
| Key | 32 bytes, base64 text, supplied or generated ([Key sources](#key-sources)) |
| Stored form | `v<key version>:<nonce>:<ciphertext and tag>`, base64 |
| Binding | Each value is authenticated against its table, column, row id and the instance id. A ciphertext copied to another row or another database does not decrypt |
| Instance id | 32 hexadecimal digits, created at the first start and stored in the database |
| Key version | A whole number starting at 1, stored with every value. It rises by one at each rotation ([Re-key](#re-key)) |
| Canary | An encrypted check value, stored in the database and opened at every start |
| Not encrypted | Password hashes (Argon2id), API token hashes, webhook token hashes, usernames, endpoints, access key ids, CA certificates, client certificates, Compose files, plain companion files and non-secret variable values ([Backups](Administration-Backups#sensitive-content)) |
| Logs | Every spelling of the loaded keys is registered with the log scrubber. Log lines name findings and locations, never values |

### Key sources

| Configuration | Key source |
|---|---|
| `DOCKMIN_SECRET_KEY` set | The variable |
| `DOCKMIN_SECRET_KEY_FILE` set | The file it names |
| Both set | Refused: status `2`, self-check `encryption_key` |
| Neither set | The generated key: `secret.key` in `DOCKMIN_SECRET_KEY_DIR` (`/key` in the image) |

- A value that is not base64 of 32 bytes, or an unreadable `DOCKMIN_SECRET_KEY_FILE`, ends the start with status `2` and the self-check `encryption_key` ([Troubleshooting](Troubleshooting#encryption_key)).
- Variables, format and defaults: [Configuration](Reference-Configuration#encryption-key).
- The key file for the shipped `compose.yaml`, with its owner and mode: [Create the encryption key](Getting-Started-Installation#create-the-encryption-key).

Generate a key to supply:

```bash
openssl rand -base64 32
```

Result: one line of 44 characters ending in `=`.

### Generated key

| Item | Fact |
|---|---|
| Created | At the first start, when no key variable is set, `secret.key` does not exist and the database is new. The full rule: [Key directory](#key-directory) |
| Never over existing data | With no `secret.key`, a start is refused when the database holds encrypted data or any `crypto.*` setting row, when `<DOCKMIN_DATA_DIR>/restore/<restore-id>/previous` or `rejected` exists, or when `DOCKMIN_PREVIOUS_SECRET_KEY` or `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is set. No key is generated and no file is written |
| Never replaced | An existing `secret.key` is read as it is. A damaged file is refused, not replaced |
| Never with a supplied key | No key is generated while `DOCKMIN_SECRET_KEY` or `DOCKMIN_SECRET_KEY_FILE` is set |
| Mode and owner | `0400`, owned by the user the process runs as (uid 10001 in the image). Created without following a symbolic link, and flushed to disk before the database holds anything sealed with it. A key written by a re-key or a key volume restore is `0600` |
| Volume | To create the key: a mounted volume or bind mount at the key directory, outside the data directory, and not a `tmpfs` or `ramfs`. Creating the key without one is refused. An existing `secret.key` is read without these checks. An anonymous volume (`-v /key`) passes |
| Apart from the data | The key volume and the data volume are separate. The data volume and every backup archive hold ciphertext and no key |
| Who can read it | Anyone who can read the key volume: a user who can run Docker commands on the host, which includes an administrator's write access in a local socket environment ([Local socket read ceiling](Usage-Access-Control#local-socket-read-ceiling)) |
| Logs | `encryption_key_generated` (warning level) carries `key_file`, `key_fingerprint` and a note. Every spelling of the key is registered with the log scrubber before the file is written |
| Identified by | The key fingerprint: 16 hexadecimal digits in `encryption_key_generated`, in `encryption_key_loaded` at every start, and on the Backups page |

Copy the key off the host after the first start, apart from every archive: [Copy the key](Getting-Started-Quick-Start#copy-the-key). To put a copy back into a key volume: [Restoring the key volume](Administration-Backups#restoring-the-key-volume).

### Key directory

With neither `DOCKMIN_SECRET_KEY` nor `DOCKMIN_SECRET_KEY_FILE` set, the encryption key is the file `secret.key` in `DOCKMIN_SECRET_KEY_DIR` (`/key` in the image). With either variable set, nothing is generated and the directory is not read.

| Item | Value |
|---|---|
| Setting | `DOCKMIN_SECRET_KEY_DIR` ([Configuration](Reference-Configuration#encryption-key)) |
| Setting rule | Absolute, and not equal to, inside or containing `DOCKMIN_DATA_DIR`, compared after symbolic links are resolved. Checked only when the key is generated; a violation ends the start with status `2`, check `encryption_key` |
| File | `<DOCKMIN_SECRET_KEY_DIR>/secret.key`: the base64 text of 32 random bytes and a newline. The same format as `DOCKMIN_SECRET_KEY`, so its text pastes into the restore page |
| Mode and owner | `0400` when generated, `0600` when written by a re-key or a key volume restore. Owned by the user the process runs as (uid 10001 in the image) |
| Existing file | Read, never replaced, truncated or rewritten. A file that is a symbolic link, is not a regular file, cannot be read or does not hold base64 of 32 bytes is refused |
| File with group or other permission bits | Read. Logged as `encryption_key_file_permissions` at warning level with `key_file` and `mode`; the start continues |
| Two starts at once | The file is created exclusively. The start that did not create it reads the file the other wrote |
| Activity log | No row. `encryption_key_generated` is the record |

The file is created only when all of these hold, checked in this order after the database is migrated:

| # | Condition |
|---|---|
| 1 | `DOCKMIN_SECRET_KEY` and `DOCKMIN_SECRET_KEY_FILE` are both unset |
| 2 | `secret.key` does not exist |
| 3 | Neither `DOCKMIN_PREVIOUS_SECRET_KEY` nor `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` is set |
| 4 | The database is new: it has no crypto state row, no `crypto.*` setting row and no encrypted value |
| 5 | `<DOCKMIN_DATA_DIR>/restore/<restore-id>/previous` and `rejected` do not exist: no data kept by an earlier restore |
| 6 | The key directory passes the checks below |

When `secret.key` does not exist and condition 3, 4, 5 or 6 fails, the start is refused ([Check at every start](#check-at-every-start)). No key file is written; the database and `server-tls/` may already exist in the data directory from the same start.

- The directory checks run only when `secret.key` does not exist. An existing `secret.key` is read without them.
- A leftover of a restore is removed by deleting the directory the refusal names, when that data is no longer needed.
- The file and its directory are flushed to disk before the first encrypted check value is written to the database. A crash between the two leaves a readable key and a new database, which the next start completes.

| Key directory check, in order, before creating the key | Refused when |
|---|---|
| Exists | Missing |
| Type | Not a directory, or a symbolic link |
| Data directory | The same directory as `DOCKMIN_DATA_DIR`, or inside it |
| Mount point | Not a mount point: a volume or bind mount is required |
| Filesystem | `tmpfs` or `ramfs`, read from `/proc/self/mountinfo`. Not checked when that file cannot be read |
| Owner | Not owned by the user the process runs as |
| Permissions | Writable by its group or others, or not writable by the process user. A directory the process user cannot open is refused with the owner text |

| Mount | Result |
|---|---|
| Named volume | Takes the owner and mode of `/key` in the image (uid 10001, `0700`) when it is first mounted |
| Bind mount | Keeps its host owner and mode. The host directory must be owned by uid 10001 with mode `0700`; a root-owned directory is refused |
| Anonymous volume (`-v /key`) | Passes the mount point check, and the key is not reachable from a replacement container. Mount a named volume |
| `tmpfs` | Refused as `tmpfs` when the process user can open the directory; the `tmpfs` check runs before the owner check. A mount the process user cannot open, such as mode `0700` owned by root, is refused with the owner text |
| A volume that is also mounted at `/data` | Refused: the key directory is the same directory as the data directory |
| A bind mount of a subdirectory of the data volume | Not detected |
| No mount | Refused |

- A start with a generated key and a data directory that is not a mount point logs `data_directory_not_mounted` at warning level, with `path` and a `note`, once per process start. Data kept in the container's writable layer is lost when the container is replaced.
- The key file is published by creating a temporary file in the key directory, then linking it to `secret.key`, so `secret.key` never exists half written and an existing file is never replaced. A temporary file left by a crash is removed at the next key creation and never read as the key. A filesystem without hard links refuses with `The key file <file> could not be used: <strerror>`.
- A restore's restart resolves the key through the same code and reads the existing file.
- `secret.key` is in no backup archive ([Backups](Administration-Backups#the-key-is-not-in-the-archive)).

### Check at every start

| Situation | Result |
|---|---|
| First start with a generated key | `secret.key` is created and flushed to disk. Then the instance id and the canary are created at key version 1 |
| First start with a supplied key | The instance id and the canary are created at key version 1 |
| The key opens the canary | The start continues. Stored values at another version need the previous key ([Re-key](#re-key)) |
| The previous key opens the canary, the current key does not | A rotation. It needs `DOCKMIN_ALLOW_KEY_ROTATION` ([Re-key](#re-key)) |
| No configured key opens the canary | The start is refused: self-check `encryption_key`, status `3` |
| The crypto state rows are missing but encrypted values exist | The start is refused |
| The database cannot be read while the key is checked | The start is refused: self-check `database`, status `3` |
| A secret column holds a value that is not ciphertext | Logged at error level as `unreadable_secret_values` with `locations` (table and row); the start continues. A check lists such values |
| Generated key refused at start | `encryption_key`, status `3`. Rules: [Key directory](#key-directory). Texts: [Troubleshooting](Troubleshooting#generated-key). No file is written, and an existing `secret.key` is never replaced |
| Generated key: `secret.key` has group or other permission bits | Logged as `encryption_key_file_permissions` with `mode`; the start continues |
| Every start that passes | One `encryption_key_loaded` line for each key version held, with `key_version` and `key_fingerprint` |

The exact texts: [Troubleshooting](Troubleshooting#encryption_key). The startup self-check as a whole: [Deployment](Reference-Deployment#startup-self-check).

## Key loss

| Situation | Result | Action |
|---|---|---|
| The key is lost, the database and archives remain | The instance refuses to start. No archive made with that key can be restored. The encrypted fields cannot be decrypted by any means | Find a copy of the key. Otherwise start a new instance with a new data directory and key, and enter every credential again |
| The key is lost, a copy exists | The instance starts once the copy is supplied | Supply it as `DOCKMIN_SECRET_KEY` or `DOCKMIN_SECRET_KEY_FILE`, or put it back into the key volume ([Restoring the key volume](Administration-Backups#restoring-the-key-volume)) |
| A generated key: the key volume is lost or not mounted, the data volume remains, no copy exists | The instance refuses to start: `No encryption key is configured and <file> does not exist, but the database already holds encrypted data`. No new key is generated. The stored credentials, and every archive made with the key, cannot be read by any means | Mount the key volume the instance was created with. Otherwise start a new instance with new volumes and enter every credential again |
| A generated key: the key volume is lost, a copy exists | The same refusal until the copy is back | [Restoring the key volume](Administration-Backups#restoring-the-key-volume) |
| A generated key: the data volume is lost, the key volume remains | A start with the key volume and a new data volume reads the existing key and seals a new database with it. An archive made with that key restores with it ([Backups](Administration-Backups#restoring-into-a-fresh-instance)) | Restore the latest archive |
| The key is exposed | Anyone with the key and a copy of the database or an archive reads every stored secret in it | [Re-key](#re-key), re-encrypt the stored secrets, and replace the credentials the database held. Until the re-encryption finishes, stored values stay sealed under the exposed key. Copies of the database and archives made before the re-key stay readable with the exposed key |

- Each archive records the fingerprint of its key ([Backups](Administration-Backups#the-key-is-not-in-the-archive)).

## Re-key

A re-key replaces the key without losing the stored secrets. It has two stages.

| Stage | What happens |
|---|---|
| 1. Rotate | The instance starts with the new key as the current key, the old key as the previous key and `DOCKMIN_ALLOW_KEY_ROTATION` set. The start opens the encrypted check value with the previous key and seals it again under the new key at the next key version. Stored values stay at the old version and remain readable. A value written after the rotation is sealed at the new version |
| 2. Re-encrypt | An administrator rewrites every stored secret under the new key ([Re-encrypt stored secrets](#re-encrypt-stored-secrets)) |

Then the previous key and `DOCKMIN_ALLOW_KEY_ROTATION` are removed ([After the re-encryption](#after-the-re-encryption)). Variables: [Configuration](Reference-Configuration#encryption-key).

### Supplying the previous key

| Source | Variable | Notes |
|---|---|---|
| File (recommended) | `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` | Path, inside the container, of a file that holds the base64 old key. The key is not in the container's environment. The file must be readable by uid 10001 |
| Variable | `DOCKMIN_PREVIOUS_SECRET_KEY` | The base64 old key. Visible to anyone who can inspect the container while it is set |

- At most one of the two may be set. Both ends the start with status `2`: `at most one of DOCKMIN_PREVIOUS_SECRET_KEY or DOCKMIN_PREVIOUS_SECRET_KEY_FILE may be set`.
- The previous key must differ from the current key.
- The Encryption page names the source and the fingerprint of the previous key the instance loaded.

| Key source | Steps |
|---|---|
| Generated key, container started with `docker run` | [Re-key with a generated key](#re-key-with-a-generated-key) |
| Key file or variable, Compose | [Re-key with a supplied key](#re-key-with-a-supplied-key) |

### Re-key with a supplied key

When a start is refused, read `docker compose logs dockmin` for the `startup_check_failed` line and match it in [Troubleshooting](Troubleshooting#encryption_key).

1. Download a backup. It needs the old key to restore; keep the old key, stored apart from the archive ([Backups](Administration-Backups#download)).

   Result: the archive downloads and the Backups page lists it.

2. Give the old key a file of its own. Run as root in the repository root. The copy keeps the owner and mode the container needs:

```bash
install -m 0400 -o 10001 -g 10001 secrets/dockmin_secret_key secrets/dockmin_secret_key_previous
```

   Result: no output. `ls -l secrets/dockmin_secret_key_previous` shows mode `-r--------` and owner `10001`.

3. Create `compose.override.yaml` beside `compose.yaml`. It mounts the copy as a Docker secret, names it as the previous key and allows the rotation. Compose reads this file only when no `-f` option is given; with `-f` options, add `-f compose.override.yaml` to every Compose command ([Configuration](Reference-Configuration#setting-a-variable-composeyaml-does-not-pass)).

```yaml
services:
  dockmin:
    environment:
      DOCKMIN_PREVIOUS_SECRET_KEY_FILE: /run/secrets/dockmin_secret_key_previous
      DOCKMIN_ALLOW_KEY_ROTATION: "true"
    secrets:
      - dockmin_secret_key_previous
secrets:
  dockmin_secret_key_previous:
    file: ./secrets/dockmin_secret_key_previous
```

   Result: `docker compose config` prints the merged file with both variables under `dockmin`.

4. Write the new key to the key file. Run as root, so that the file keeps its owner and mode. Where the key is passed in the environment instead, set the new key as `DOCKMIN_SECRET_KEY`.

```bash
openssl rand -base64 32 | tr -d '\r\n' > secrets/dockmin_secret_key
```

   Result: no output.

5. Store a copy of the new key outside this host and outside every backup ([Backups](Administration-Backups#the-key-is-not-in-the-archive)). Then recreate the container. A restart does not apply changed environment values.

```bash
docker compose up -d
```

   Result: the log holds two `encryption_key_loaded` lines, one for the new key version and one for the old, and no `startup_check_failed`. The Encryption page shows `Key version` raised by one, a `Previous key` fingerprint, and status `Below the current key` while any secret is stored.

6. The start rotated the check value to the next key version. Delete the `DOCKMIN_ALLOW_KEY_ROTATION` line from `compose.override.yaml` and recreate the container:

```bash
docker compose up -d
```

   Result: the start passes with the previous key and without the rotation setting.

7. Re-encrypt the stored secrets on the Encryption page ([Re-encrypt stored secrets](#re-encrypt-stored-secrets)).

   Result: the run ends `Complete` and the overview reads `Current`. If it ends otherwise, see [Run page](#run-page).

8. Remove the previous key from the deployment. Delete the override file and recreate the container:

```bash
rm compose.override.yaml
docker compose up -d
```

   Result: the Encryption page shows `Previous key`: `None configured`.

9. Delete `secrets/dockmin_secret_key_previous` when no archive made before the re-key may be restored. Otherwise keep a copy of the old key outside this host.

- To pass the old key as a variable, replace the two `secrets` blocks and the `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` line of the override with `DOCKMIN_PREVIOUS_SECRET_KEY: <old-key>`, and skip step 2. The override file then holds the old key: delete it at step 8.
- The old key still opens every archive made before the re-key.
- With `-f` options, recreate with the same options.

### Re-key with a generated key

For a container named `dockmin` with the volumes `dockmin-data` and `dockmin-key`, started with the Quick Start command ([Start dockmin](Getting-Started-Quick-Start#start-dockmin)). A container started with other options is recreated with those options. When a start is refused, read `docker logs dockmin` for the `startup_check_failed` line and match it in [Troubleshooting](Troubleshooting#generated-key).

1. Download a backup ([Backups](Administration-Backups#download)).

   Result: the archive downloads.

2. Copy the old key off the container, under a name that keeps it apart from the new key, and check that the copy equals the key in the volume. Under `sudo`, the copy belongs to root: give it to the current user first ([Copy the key](Getting-Started-Quick-Start#copy-the-key)). `cmp` prints nothing when the two are equal:

```bash
docker cp dockmin:/key/secret.key ./dockmin-secret-previous.key
docker exec dockmin cat /key/secret.key | cmp - dockmin-secret-previous.key
```

   Result: `cmp` prints nothing. Any output means the copy is wrong: do not continue.

3. Give the copy to the container user, as root, so that the container can read it when the file is mounted:

```bash
sudo chown 10001:10001 dockmin-secret-previous.key
sudo chmod 0400 dockmin-secret-previous.key
```

4. Stop the container and remove it. The volumes stay:

```bash
docker stop --time 45 dockmin
docker rm dockmin
```

   Result: both commands print `dockmin`.

5. Replace `secret.key` in the key volume with a new key. This overwrites the only key in the volume. Run it only once `dockmin-secret-previous.key` from step 2 exists and `cmp` printed nothing. Without that file, the stored values and every archive made before the re-key can never be read. The command writes `secret.key.new` and renames it over `secret.key`. The new file has mode `0600`:

```bash
docker run --rm -v dockmin-key:/key --entrypoint python ghcr.io/sphexi/dockmin:latest -c 'import base64, os, pathlib, secrets; os.umask(0o077); pathlib.Path("/key/secret.key.new").write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n"); os.replace("/key/secret.key.new", "/key/secret.key")'
```

   Flush the host's filesystems, so that a host crash cannot leave an empty `secret.key`, which the next start refuses:

```bash
sync
```

   Result: no output from either command.

6. Start the container with the old key mounted as a file, named as the previous key, and the rotation allowed. The start rotates the check value to the next key version:

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  -v "$PWD/dockmin-secret-previous.key:/run/previous-key:ro" \
  -e DOCKMIN_PREVIOUS_SECRET_KEY_FILE=/run/previous-key \
  -e DOCKMIN_ALLOW_KEY_ROTATION=true \
  ghcr.io/sphexi/dockmin:latest
```

   Result: `docker logs dockmin` holds two `encryption_key_loaded` lines and no `startup_check_failed`. The Encryption page shows `Key version` raised by one and a `Previous key` fingerprint.

7. Copy the new key off the container, apart from every backup ([Backups](Administration-Backups#the-key-is-not-in-the-archive)). The copy has mode `0600`:

```bash
docker cp dockmin:/key/secret.key ./dockmin-secret-current.key
```

8. Remove `DOCKMIN_ALLOW_KEY_ROTATION`: stop and remove the container, then start it again with the previous key only.

```bash
docker stop --time 45 dockmin
docker rm dockmin
```

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  -v "$PWD/dockmin-secret-previous.key:/run/previous-key:ro" \
  -e DOCKMIN_PREVIOUS_SECRET_KEY_FILE=/run/previous-key \
  ghcr.io/sphexi/dockmin:latest
```

   Result: the start passes; the Encryption page still names the previous key.

9. Re-encrypt the stored secrets on the Encryption page ([Re-encrypt stored secrets](#re-encrypt-stored-secrets)).

   Result: the run ends `Complete` and the overview reads `Current`. If it ends otherwise, see [Run page](#run-page).

10. Stop and remove the container, then start it again without the previous key:

```bash
docker stop --time 45 dockmin
docker rm dockmin
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  ghcr.io/sphexi/dockmin:latest
```

   Result: the Encryption page shows `Previous key`: `None configured`.

11. Delete `dockmin-secret-previous.key` from this host when no archive made before the re-key may be restored. Otherwise move it outside this host.

- To pass the old key as a variable, prefix the commands of steps 6 and 8 with `DOCKMIN_PREVIOUS_SECRET_KEY="$(cat dockmin-secret-previous.key)"`, replace the `-v "$PWD/dockmin-secret-previous.key:…"` and `-e DOCKMIN_PREVIOUS_SECRET_KEY_FILE=…` lines with `-e DOCKMIN_PREVIOUS_SECRET_KEY`, and skip step 3. The old key is passed by name, so it is not in the arguments of the `docker` process, which other users of the host can read. `sudo` resets the environment by default and drops the variable: run the command as a user that can run `docker` without `sudo` ([Installation](Getting-Started-Installation)).
- A start with the previous key and without `DOCKMIN_ALLOW_KEY_ROTATION` is refused while the check value is still at the old key version. Once the rotation has happened, the same start passes.
- A start with a previous key set and no `secret.key` in the key volume is refused.

### After the re-encryption

| Do | Consequence of not doing it |
|---|---|
| Remove `DOCKMIN_PREVIOUS_SECRET_KEY` or `DOCKMIN_PREVIOUS_SECRET_KEY_FILE`, and `DOCKMIN_ALLOW_KEY_ROTATION`, from the container's configuration, and recreate the container | A variable is visible to anyone who can inspect the container while it is set. A start that still names the previous key keeps it loaded |
| Keep the old key outside this host while an archive made before the re-key may be restored | The old key opens every archive made before the re-key. A restore asks for the key the archive was made with |

### Ways a value reaches the new version

| Way | Detail |
|---|---|
| Re-encrypt on the Encryption page | Every stored secret below the current key version is rewritten under the current key ([Re-encrypt stored secrets](#re-encrypt-stored-secrets)) |
| The secret is entered again | Editing a registry credential set, an object storage target, an environment's TLS material, a notification channel's secrets, a stack variable or a secret stack file seals the new value at the current version |
| A backup is restored with the current key | An archive holds every secret sealed at the key version current when it was made. Restoring an archive made after the rotation, with the running key, replaces the database with one whose values are all at the new version. Changes made after the backup are lost ([Backups](Administration-Backups#changes-the-restore-makes)) |

A backup produced while some values are below the current version holds them re-sealed at the current version, and needs the current key to restore.

### Refusals at start during a re-key

| Refusal at start | Cause |
|---|---|
| A start with the previous key but without `DOCKMIN_ALLOW_KEY_ROTATION` | The message names the previous key's source, the version the rotation would create, and `DOCKMIN_ALLOW_KEY_ROTATION=1` |
| Values at a version no configured key reads | The message names the version and the count, and asks for the key as `DOCKMIN_PREVIOUS_SECRET_KEY` or `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` |
| A rotation while values remain at a version other than the stored one, or while a secret column holds a value that is not ciphertext | Finish the earlier re-key, or clear or re-enter the values first |
| The previous key equals the current key | The start ends with status `2` for a supplied key, and `3` for a generated key |

The exact texts: [Troubleshooting](Troubleshooting#encryption_key).

## Activity log

Actor: the administrator. Target type `instance`, no target id. Category `user_action`. Row fields: [Activity](Administration-Activity).

| Action | Result | Written | Detail keys |
|---|---|---|---|
| `encryption_values_checked` | `success`, `failure` | A check ended | `checked`, `corrupt`, `unparseable`; on failure `reason` |
| `encryption_rekey_requested` | `success`, `failure` | A re-encryption was admitted, before any value is rewritten; or was refused | `key_version`, `values_below_current`; on failure `reason`, `message` |
| `encryption_rekey_completed` | `success`, `failure` | A re-encryption ended | `key_version`, `rewritten`, `checked`, `unreadable`, `corrupt`, `unparseable`, `remaining_below_current`, `duration_ms`; on failure `reason` |

- `checked` counts the values at the current key version that opened. `corrupt` counts values that do not open with the current key, `unparseable` values that are not shaped like ciphertext, `unreadable` values below the current version that no loaded key opens.
- `encryption_rekey_completed` is a `success` when every stored secret is at the current version and every value opened. Otherwise it is a `failure` with `reason` `values_unreadable` or `interrupted`.
- A refused start of a re-encryption records `reason` `EncryptionConflict` (another operation holds the instance, nothing needs rewriting, or the counts changed) or `ValidationFailed` (the box was not ticked), and `message` as the text shown to the administrator.
- No row holds a key, a secret or a value's text. Locations of values that did not open are on the run page only.
- Key generation writes no row: `encryption_key_generated` is a log line.

| Log line | Level | When |
|---|---|---|
| `encryption_key_generated` | warning | A key file was created |
| `encryption_key_loaded` | info | Every start, once for each key version held |
| `encryption_key_file_permissions` | warning | The generated key file has group or other permission bits |
| `unreadable_secret_values` | error | A secret column holds a value that is not ciphertext |
| `data_directory_not_mounted` | warning | A generated key and a data directory that is not a mount point |
| `encryption_check_failed` | error | An error ended a check |
| `encryption_rekey_failed` | error | An error ended a re-encryption |
| `encryption_rekey_finished` | info | A re-encryption ended, with `outcome`, `rewritten`, `remaining` |

## Related settings

| Variable | Use |
|---|---|
| `DOCKMIN_SECRET_KEY`, `DOCKMIN_SECRET_KEY_FILE`, `DOCKMIN_SECRET_KEY_DIR` | The current key ([Configuration](Reference-Configuration#encryption-key)) |
| `DOCKMIN_PREVIOUS_SECRET_KEY`, `DOCKMIN_PREVIOUS_SECRET_KEY_FILE` | The previous key during a re-key |
| `DOCKMIN_ALLOW_KEY_ROTATION` | Allows the start that rotates the check value |

## Failure states

| Symptom | Cause | Action |
|---|---|---|
| The page answers `403` | The user is not an administrator | Sign in as an administrator |
| The overview answers `409` `A check or re-encryption is already running.` | Another run holds the claim | Wait for it to end ([Troubleshooting](Troubleshooting#re-encrypting-stored-secrets)) |
| The review answers `409` `N jobs are running; wait for them to finish.` | Image or stack jobs are running | Wait for them, then review again |
| The start answers `409` `Changed since it was reviewed; nothing was rewritten.` | A secret was added, changed or removed after the review loaded | Review again |
| A run ends `Finished with values that did not open` | Values no key opens, that do not open, or are not ciphertext | [Failed values](#failed-values) |
| A run ends `Interrupted` or `Failed` | The process stopped, or an error ended the run | Every stored secret is still readable. Read the server log, then start again |
| The run page answers `404` | The run ended more than 30 minutes ago, or the process restarted | Open the overview |
| A backup is refused because a stored secret cannot be read | A value does not open | Check stored secrets, then enter the listed values again ([Troubleshooting](Troubleshooting#backup-refused-a-stored-secret-cannot-be-read)) |
| The instance does not start after a key change | The configured keys do not open the database | [Troubleshooting](Troubleshooting#encryption_key) |
