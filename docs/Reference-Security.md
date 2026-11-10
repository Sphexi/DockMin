# Security

Roles, the authorisation model and the restrictions on standard users: [Access control](Usage-Access-Control).

## Authentication

| Item | Fact |
|---|---|
| Browser | A session cookie. Lifetime and idle timeout: `DOCKMIN_SESSION_LIFETIME`, `DOCKMIN_SESSION_IDLE_TIMEOUT` ([Configuration](Reference-Configuration#accounts-and-sessions)). Cookie attributes: [Cookies](#cookies) |
| Forms | Every state-changing request from a browser carries a CSRF token ([Frontend conventions](Contributing-Frontend-Conventions)) |
| API | A bearer token that acts as its user ([JSON API](Reference-JSON-API#authentication)) |
| Access platform | Off by default. A signed token in a configured header, verified against an operator-supplied key and mapped to a linked account, opens a session that is checked against the token on every request ([Teams and users](Usage-Teams-and-Users), [Access platform tokens](#access-platform-tokens)) |
| Webhook calls | A webhook token, in the URL path or as the bearer credential. It starts one run of one automation, with that automation's owner's authority, and reads nothing else. Stored as a SHA-256 hash and shown once; revoked by a password change, a password reset and `End all sessions`; refused while the owner is disabled ([JSON API](Reference-JSON-API#webhook-calls)) |
| Passwords | Hashed with Argon2id. Length and character-class rules and hash costs: [Configuration](Reference-Configuration#accounts-and-sessions) |
| Sign-in lock | After repeated failures a username is locked ([Teams and users](Usage-Teams-and-Users#sign-in-lock)) |
| Rate limit | Requests with no signed-in user are limited per address when the limit is enforced ([Rate limit](Reference-Deployment#rate-limit)) |
| Outbound addresses | Endpoints an administrator names may not be on this server or a link-local network unless `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` permits it ([Address policy](#address-policy)) |

## Outbound contacts

| Contact | Destination | Started by | Detail |
|---|---|---|---|
| Template catalogue fetch | The address an administrator saved for the catalogue, and its redirects | An administrator choosing `Enable and fetch` or `Refresh`. Never at start, on a page load or on a schedule | [Template catalogues](Administration-Template-Catalogues) |
| Registry connection test, Amazon ECR token, image update check | The registry address of a credential set or image, and its token service | An administrator's connection test, a token needed for an ECR set, a user's update check, or a run of a `container-updater` automation | [Registries](Administration-Registries#registry-connections) |
| Adoption review | Manifest `HEAD` requests to addresses that have a credential set granted to the environment, and the token service of each | An administrator starting the review. Never at start, on a page load or on a schedule | [Environments](Usage-Environments#adopt-everything-on-this-host) |
| Bind unbound images review | Manifest `HEAD` requests to addresses that have a credential set granted to the environment, with each granted set's login, then without a login when no set matched, and the token service of each | An administrator starting the review. Never at start, on a page load or on a schedule | [Images](Usage-Images#bind-unbound-images) |
| dockmin update check | The registry of dockmin's own image reference: a manifest `HEAD` and a tag list `GET` | An administrator's update check ([Instance](Administration-Instance)) | [Registries](Administration-Registries#registry-connections) |
| Object storage, notification receivers, environment hosts, access platform key URL | The address an administrator saved | The feature that names it | [Address policy](#address-policy) |

- A fresh installation contacts no catalogue.
- The dockmin server contacts the registry of an image reference only for an update check: a user's request, an administrator's review, or a `container-updater` run. An image bound to a credential set is checked with it; any other image is checked anonymously. An anonymous check reaches only globally routable addresses, for the registry and for its token service, unless a registry entry (any credential set, authentication off included) is granted to the environment at the registry address; the [address policy](#address-policy) then applies. Without an entry, a private, carrier-grade NAT (`100.64.0.0/10`), unique-local, loopback or link-local address, or an IPv4 address embedded in an IPv6 form of these, is refused after name resolution and before connecting, with the update-check reason `private_address`. The check for a dockmin update is not restricted. A new instance holds two entries with authentication off, `docker.io` and `ghcr.io`, granted to each environment created afterwards ([Registries](Administration-Registries#entries-a-new-instance-starts-with)). Pulls are made by the Docker host. Nothing contacts a registry at start, at setup, when an environment is created or when a page loads.
- Every destination passes the [address policy](#address-policy).

## Address policy

Connections to operator-named hosts are refused when the address is internal, unless `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` permits it.

| Applies to | Where |
|---|---|
| Object storage endpoints | Save, connection test, backup upload and retention, activity export, container log export |
| Notification receivers | Save, test message and delivery, for webhook, ntfy, Gotify, Discord, Slack, Teams and email servers ([Notifications](Administration-Notifications)) |
| Registry addresses | Save, connection test, update checks |
| Template catalogue addresses | Save, each fetch and each redirect ([Template catalogues](Administration-Template-Catalogues)) |
| A registry's token service | The `realm` the registry names |
| The Amazon ECR endpoint | Token exchange |
| Environment TCP hosts | Save, connection test, Engine requests, container terminals and other raw streams |
| Access platform key URL | Each fetch of `DOCKMIN_JWT_JWKS_URL` ([Configuration](Reference-Configuration#access-platform)) |
| Compose's Docker host | Checked before Compose starts, for a TCP environment |
| Not applied to | The local Unix socket |

| Internal address class | Range |
|---|---|
| Unspecified | `0.0.0.0/8`, `::` |
| Loopback | `127.0.0.0/8`, `::1` |
| Link-local | `169.254.0.0/16`, `fe80::/10` |
| AWS IPv6 metadata | `fd00:ec2::/64` |
| Embedded IPv4 | An internal IPv4 address inside an IPv4-mapped (`::ffff:0:0/96`), IPv4-compatible (`::/96`), IPv4-translated (`::ffff:0:0:0/96`), NAT64 (`64:ff9b::/96`), NAT64 local-use (`64:ff9b:1::/48`, every prefix placement) or 6to4 (`2002::/16`) address |
| Names | `localhost`, `*.localhost`, `metadata`, `metadata.goog`, `metadata.google.internal`, `instance-data`, `instance-data.ec2.internal`, and numeric spellings of an internal IPv4 address. Names are compared in lower case, without brackets or a trailing dot |

Private ranges (RFC 1918, `fc00::/7` outside `fd00:ec2::/64`) are not internal.

| Setting | Value |
|---|---|
| `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` | IP addresses or networks, such as `127.0.0.1/32` or `10.0.0.0/8`. Empty permits nothing. Format: [Configuration](Reference-Configuration#listeners-and-network) |

- An embedded address is allowed when the address itself is permitted, or when every internal IPv4 address it embeds is permitted.
- Each entry lets an administrator's session reach that address from the server, including a cloud metadata service on `169.254.169.254`.
- Checked twice: at save, for an IP literal or one of the internal names, as a field error; at connect, after name resolution, on every address dialled.

| Connection | Resolved addresses |
|---|---|
| Object storage, registry, token service, ECR, Engine requests, notification receivers (HTTP and SMTP) | Refused addresses are skipped. The connection fails as refused only when no permitted address is left |
| Compose's Docker host | Every resolved address must be permitted, else the operation fails before Compose starts. A name that does not resolve within 5 s fails the same way, with `The environment's host name could not be resolved from this server.` |

| Surface | Result |
|---|---|
| Field error | `This address is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` |
| Connection test, TCP stage | The same text |
| Registry token service | `The token service is not used.` followed by the same text |
| Update check | Unknown, reason `address_refused`: `The registry or its token service is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` |
| Update check, anonymous, no entry granted at the registry address | Unknown, reason `private_address`: `The registry is on a private network. Add a registry entry for its address, with authentication off if it needs none.` |
| Object storage | Failure reason `address_refused`. Not retried |
| Notification receiver | Field error at save for an IP literal or an internal name. At send, the delivery fails with `address_refused` and the text above, and is tried again on the [retry schedule](Administration-Notifications#delivery). A test message fails with the same text |
| Amazon ECR | The set's token exchange fails with the same text |
| Engine requests | The environment is unreachable with the same text. The event stream reconnects with its longest delay |
| Stack operations | Failure reasons `address_refused` and `host_unresolved` |
| Log | `outbound_address_refused` with `address`, cut to 64 characters |

What the policy does not cover:

- An address that is not internal. Cloud metadata services at `100.100.100.200` (Alibaba Cloud), `168.63.129.16` (Azure) and `192.0.0.192` (Oracle Cloud) are reachable; restrict them at the network.
- AWS instance metadata version 2 answers a container only when the instance's hop limit is 2 or more.
- A host that is reachable through an address the policy does not classify as internal: a Docker bridge gateway address, or a LAN address that fronts a service bound to loopback.
- Teredo and ISATAP addresses are not decoded.
- A host name that resolves to a different address inside the Compose process than at the check.

## Access platform tokens

| Item | Fact |
|---|---|
| Verification | Every token is verified before any claim is read: algorithm allow-list (`none` and `HS*` never), key type matching the algorithm, signature, `iss`, `aud`, `exp`, `nbf`, and `iat` not more than 30 seconds in the future and no older than `DOCKMIN_JWT_MAX_AGE`. `exp` and `nbf` allow 30 seconds of skew |
| Failure | A token that does not verify is treated as absent. Nothing falls back to another method |
| Identity | One claim, matched to an account an administrator linked. Usernames are never matched. Roles, teams and groups are never read from a claim |
| New accounts | Only with `DOCKMIN_JWT_CREATE_USERS`: a standard user with an unusable password, never an administrator |
| Sessions | A session opened from a token ends at the first request whose token is absent, refused, or names another identity. Revocation at the platform applies at the next request |
| Exchange | `GET` only |
| Sign-out | Sets a marker cookie until the user chooses `Continue as` |
| Password sign-in | Administrators can always sign in with a password. `DOCKMIN_JWT_PASSWORD_SIGN_IN` can refuse it for standard users and ends their existing password sessions |
| `/api` | Never reads the header. A JWT in `Authorization` is an invalid API token |
| Stored | No token is stored. A verified token is remembered in memory by its SHA-256 digest for at most 5 minutes, and never past its `exp` or `iat` plus `DOCKMIN_JWT_MAX_AGE`. At most 1,000 are remembered |
| Logs | A token value is not written to a log line, an activity row or a page. The log scrubber replaces text shaped like a compact JWS, three base64url segments with the first beginning `eyJ`, with `[REDACTED]` |
| Key fetch | `https` only, under the address policy, no redirects, 5 seconds, 64 KiB, 20 keys. Nothing is fetched while the feature is off |
| Replay | A captured token works against dockmin until `exp` or `DOCKMIN_JWT_MAX_AGE` after `iat`, when presented directly to it |

dockmin cannot tell which peer sent the header. Make it reachable only through the platform, over HTTPS or a network no other host shares, and shorten `DOCKMIN_JWT_MAX_AGE` where tokens live longer than needed ([Teams and users](Usage-Teams-and-Users)).

## Cookies

| Cookie | Name with `DOCKMIN_COOKIE_SECURE` at its default | Name with it `false` | Attributes |
|---|---|---|---|
| Session | `__Host-dockmin_session` | `dockmin_session` | `HttpOnly`, `SameSite=Lax`, path `/`, `Secure` by the setting, lifetime `DOCKMIN_SESSION_LIFETIME` |
| CSRF | `__Host-dockmin_csrf` | `dockmin_csrf` | `HttpOnly`, `SameSite=Lax`, path `/`, `Secure` by the setting, a browser-session cookie |
| Notice | `__Host-dockmin_flash` | `dockmin_flash` | `HttpOnly`, `SameSite=Lax`, path `/`, `Secure` by the setting, 120 seconds. Signed with a key generated at process start, so a restart drops any pending notice |
| Access platform sign-out marker | `dockmin_signed_out` | `dockmin_signed_out` | `HttpOnly`, `SameSite=Lax`, path `/`, `Secure` by the setting. No `__Host-` prefix |
| Theme | `dockmin_theme` | `dockmin_theme` | Written by the page script, one year, `SameSite=Lax`, `Secure` on an HTTPS page. Holds `light` or `dark` |

The `__Host-` prefix and the `Secure` attribute make a browser send the cookie only over HTTPS and refuse a copy set by a sibling subdomain.

- Serve HTTPS directly (a supplied or a self-signed certificate) or terminate TLS at the proxy, and leave `DOCKMIN_COOKIE_SECURE` at its default.
- `DOCKMIN_COOKIE_SECURE` is the configured value, not the scheme the process sees: behind a TLS-terminating proxy the cookies still carry `Secure`.
- `compose.yaml` passes `DOCKMIN_COOKIE_SECURE` and `DOCKMIN_SESSION_LIFETIME` through from `.env`.

| While `DOCKMIN_COOKIE_SECURE` is `false` | Detail |
|---|---|
| Cookie attribute and prefix | The session, CSRF and notice cookies drop the `Secure` attribute and the `__Host-` prefix |
| Passwords and session cookies | Travel over plain HTTP, readable by anyone on the network path |
| Published address | Do not change the address `compose.yaml` publishes (`127.0.0.1`) |
| Remote access | Forward the port over SSH ([Installation](Getting-Started-Installation)) |
| Log | `cookie_secure_disabled`, at warning level, once at start |

## dockmin's own container

dockmin finds the container it runs in at start ([Instance](Administration-Instance)). The container, its volumes, its Compose project and its Swarm service stay visible. What would stop it, or take away what it runs on, is refused for every role, administrators included. The refusal is not a role restriction: no grant lifts it.

| Surface | Result |
|---|---|
| `stop`, `restart`, `kill`, `pause`, `remove`, recreate, batch of these | Refused. `start` and `unpause` are allowed |
| Terminal, attach | Refused |
| Detach from a network | Refused |
| Create, duplicate or recreate a container with `PidMode`, `IpcMode` or `NetworkMode` of the form `container:{name or id}` naming it, `VolumesFrom` naming it, or a named volume it has mounted | Refused, in every environment that reports the container |
| Remove a volume it has mounted: single, with containers, bulk, prune, stack removal with volumes | Refused, or the volume is kept |
| Export a volume it has mounted, or a `local` volume bound to a path it mounts | Refused. While its mounts on the host are unknown, every volume of that host is refused |
| Export a Compose or Swarm stack that holds it, or the container itself | Refused. While its mounts on the host are unknown, every stack and container export on that host is refused |
| Import into a `local` volume whose `device` option names a path it mounts | Refused. While its mounts on the host are unknown, every `device` path is refused |
| Deploy, redeploy, stop, restart or remove its Compose project | Refused |
| Change or remove its Swarm service; change the Swarm stack that holds it; drain its node | Refused |
| Restart | Only by an administrator, from the Instance page, through an environment on the local Docker socket |
| Automation | The object is skipped |

- A shell in the container can read the encryption key and the data volume and can stop the process. A terminal or attach to dockmin's container is refused to administrators too, and a second container may not join its namespaces or inherit its volumes.
- A refused request answers `409` and records a failure with the reason `self_protected` where the refused action is recorded ([Activity](Administration-Activity#self-protection-refusals)).
- The protection of the container's own id holds from start. The protection of its volumes, Compose project, Swarm service and node needs an environment that reports the container; until every environment has been read it may be incomplete ([Instance](Administration-Instance)).
- An environment reached over TLS can report the container, and is then protected as well, but is never used to send a restart.
- Per-object detail: [Containers](Usage-Containers#dockmins-own-container), [Volumes](Usage-Volumes#dockmins-volumes), [Stacks](Usage-Stacks#dockmins-own-stack), [Swarm](Usage-Swarm#dockmins-own-service), [Networks](Usage-Networks#detach).

## Files read from a host

Reading a Compose project's files from its host makes dockmin bind-mount a host directory into a helper container ([Stacks](Usage-Stacks#the-helper-container)).

| Item | Value |
|---|---|
| Who | Administrators only. A standard user receives `404` |
| Path | Only the working directory the project's containers record. It is refused when it is `/`, is relative or not normalised, holds control characters or `:`, or holds, lies inside or equals the source of a mount of dockmin's own container ([Stacks](Usage-Stacks#where-the-files-are)) |
| Mount | The directory, read-only, at `/dockmin-read`. Nothing else is mounted |
| Helper | Created from an image already on the host, never started, no network, read-only root file system, all capabilities dropped. Removed after the read |
| Reads | Regular files only, each under a size limit, inside the working directory. Symbolic links, directories and archives that are not exactly one file are refused |
| Output | A secret file's content and a secret variable's value are held in memory for 15 minutes, at most 8 reads for each user and 32 in all, stored encrypted when adopted, and never shown or recorded |
| Recorded | `stack_files_read` names the project, the helper container and the reasons files were refused ([Activity](Administration-Activity#stack-rows)) |

## Volume export

An administrator can download a volume's files as one archive ([Volumes](Usage-Volumes#export)).

| Item | Value |
|---|---|
| Who | Administrators only. Any other user receives `404` on the confirmation and on the download, and nothing is recorded |
| Contents | Plaintext. The archive holds every file of the volume as it is stored, unencrypted and without a passphrase. Anyone who holds the file can read everything in the volume, including files whose mode allows the owner only |
| Where the plaintext goes | The response to the administrator's browser. dockmin writes nothing to its own disk and does not log or parse file content |
| dockmin's own volumes | Refused: a volume mounted in the container dockmin runs in, and a `local` volume bound to a path that container mounts. The data volume holds the database and the key volume holds the encryption key. While dockmin's mounts on a host are unknown, every volume of that host is refused |
| Plugin drivers | Refused. Only the `local` driver is exported |
| Driver options | An option whose key names a credential, or whose value carries one, is removed whole from the manifest and listed by key. The match is on key names and on the forms `user%password` and `user:password@host` inside a value. An option that holds a credential in another form is written as found |
| Labels | `com.docker.compose.*`, `com.docker.stack.namespace` and `dockmin.*` are not written |
| Host access | A helper container created from an image already on the host, never started, with the volume read-only, no network, a read-only root file system and all capabilities dropped. An image that declares a volume at or below the helper's mount point is never used. The image the confirmation showed is the one used, and it is checked again when the download starts. The helper is removed with its anonymous volumes when the export ends |
| Recorded | `volume_export_started` and `volume_exported`, naming the volume and never an option value, a label value or a path ([Activity](Administration-Activity#volume-export-rows)) |

## Volume import

An administrator can create a volume from an export archive ([Volumes](Usage-Volumes#import)).

| Item | Value |
|---|---|
| Who | Administrators only. Any other user receives `404` on every import route, before a request body is read, and nothing is recorded |
| Archives accepted | Only archives written by a volume export. The first entry must be `manifest.json` with a valid manifest. A plain tar file is refused. The archive is not signed or authenticated: any archive in the format is accepted |
| Upload | A multipart `POST` whose first part is the CSRF token, verified before any byte of the file is read. At most 7 GiB and 64 KiB, within 3,600 seconds. Written to `{data directory}/volume-import/` as a `0600` file in a `0700` directory, with at least 256 MiB left free at every write |
| Held upload | Usable only by the user, the session and the environment that made it, under a random 128-bit identifier. One for each user, four in all, 30 minutes. A new upload is refused while the holds of other users fill the four; no hold is evicted. Deleted on every way out, and the whole directory at start |
| Validation | The whole archive is read and checked before any volume is created, and checked again while it is sent. A refusal names the first entry at fault |
| Path rules | Absolute names, `..` components, empty or `.` components, control characters, names that are not UTF-8, paths over 4,096 bytes, components over 255 bytes, more than 128 components below `data/`, names that together take more than 64 MiB, repeated names, a name used as both a file and a directory, and anything outside `data/` are refused |
| Symbolic links | Written as links, never followed. An entry below a symbolic link of the archive is refused. A relative target that resolves outside the volume's root is refused. An absolute target is written as stored and resolves in the root of whatever container mounts the volume |
| Hard links | Written only when the target is a regular file that comes earlier in the archive under `data/` |
| Devices, FIFOs, sockets | Refused, with sparse files (including PAX sparse records) and every other entry type that is not a file, directory or link |
| Modes | The setuid and setgid bits are cleared. The sticky bit is kept |
| Owners | Numeric uid and gid are kept as archived, within 0 to 4294967295. A negative size, uid or gid is refused, and a negative time is read as 0. Owner names are emptied. Files are written with the owners the archive names, including `0:0` |
| Extended attributes | Only `user.` attributes are applied |
| Resource limits | 2,000,000 entries, 64 GiB of file data, and a ceiling on decompressed bytes of the file data limit plus 8 KiB for each allowed entry plus 2 MiB, so a compression bomb is refused. Extension headers are held at most 16 and 1 MiB for one entry, and each counts toward the entry limit. At most 2 archives are validated at once |
| Labels | `com.docker.compose.*`, `com.docker.stack.namespace` and `dockmin.*` are never applied, so an import cannot place a volume under a stack's access. `com.docker.volume.anonymous` is never applied. The import adds `dockmin.import`, with a value unique to the import, to the volume it creates |
| Driver | Only `local`. A plugin driver is refused at upload and on confirmation |
| Driver options | Shown and editable on the review. A `device` option that is, holds or lies inside a source of a mount of dockmin's own container is refused, as is every `device` while dockmin's mounts on the host are unknown. The path is compared with repeated slashes and `.` components collapsed; a path with `..` or a control character is treated as protected, and `/` is above every source. Any other `device` path receives the archive's files when the volume is mounted by the helper |
| Host path or share | A `device` option, or a `type` other than empty or `tmpfs`, needs a confirmation naming the type and device, bound to the archive's digest, type and device. Without it the import is refused before anything is created (`mount_not_confirmed`) |
| Empty target | The new volume is read through the helper before any write. A volume that holds any file is refused (`volume_not_empty`), the volume the import created is removed and no file is written. Files already written to a host path or share stay there when the volume is removed |
| Name | An existing volume is never written to, replaced or adopted. The name is checked under the volume lock, and the volume the host answers the create with must carry this import's `dockmin.import` label: a volume that does not is refused as a name in use and left as it is |
| Host access | A helper container created from an image already on the host, never started, with the volume mounted read-write, no network, a read-only root file system and all capabilities dropped. An image that declares a volume at or below the helper's mount point is never used; the image the review showed is the one used and is checked again. It is removed with its anonymous volumes when the import ends |
| Failure | The volume the import created is removed. A volume that existed before is never removed. Files already written to a host path or share are not removed |
| Contents | Not scanned. A container that mounts the volume runs with whatever the files contain, including executables |
| Recorded | `volume_imported`, naming the volume and the option keys, and never an option value, a label value, a path or file content ([Activity](Administration-Activity#volume-import-rows)) |

## Stack and container export

An administrator can download a managed stack or a container as one archive ([Stacks](Usage-Stacks#export), [Swarm stacks](Usage-Swarm#export-swarm-stacks), [Containers](Usage-Containers#export)).

| Item | Value |
|---|---|
| Who | Administrators only. Any other user receives `404` on the confirmation and on the download, and nothing is recorded |
| Where the archive goes | The response to the administrator's browser. dockmin writes nothing to its own disk |
| dockmin's own objects | Refused: a stack that holds the container dockmin runs in, that container, and a volume it has mounted or whose `device` path it mounts. While dockmin's mounts on a host are unknown, every stack and container export on that host is refused |
| Not in the archive | dockmin's encryption key, registry credential sets, image bindings, resource controls, automation rules and webhooks, activity rows, dismissals, and images |
| Secret values | Sealed under a passphrase, or left out ([Export sealing](#export-sealing)). A value that is left out is never decrypted |
| Integrity | The manifest records the size and SHA-256 of the stack file, each plain companion file and each build context. Volume data has no digest. A sealed archive carries an HMAC-SHA-256 over the whole manifest except `sealed.mac`, checked on unlock. A modified manifest is refused with the message of a wrong passphrase. An archive whose `secrets` is `omitted` or `none` has no MAC and no integrity protection: the digests detect damage and do not detect tampering. Each sealed value is also authenticated ([Export sealing](#export-sealing)) |
| Volumes | Only `local` volumes, each chosen by the administrator, read through a helper container as for a [volume export](#volume-export). A driver option that holds a credential is removed and listed by key |
| Bind mounts | A bind source is written as it is. The directory's contents are not read |
| Network options | A user-defined network's driver options that name a credential, and credential entries inside option values, are removed and listed by key. Labels `com.docker.compose.*`, `com.docker.stack.namespace` and `dockmin.*` are not written |
| Recorded | `stack_export_started`, `stack_exported`, `container_export_started` and `container_exported`, with counts and the secret mode, and never a name, a value, a path or a passphrase ([Activity](Administration-Activity#stack-export-rows)) |

| Plain part of the archive | Content |
|---|---|
| `manifest.json` | The object's name, the names of its secret variables, files and environment variables, the size of each secret file, the values of every variable that is not secret, the container's create form (command, entrypoint, labels, healthcheck command, log options), network options, image references and digests, and each volume's name, driver, labels and options |
| `stack/compose.yaml` | The stack file as stored |
| `stack/files/{name}` | Each plain companion file |
| `stack/contexts/{index}` | Each build context archive as uploaded |
| `volumes/{index}/data/...` | The files of each chosen volume, including files whose mode allows the owner only |
| Container environment values | Every value not marked secret on the confirmation |

- A container's environment has no secret marking of its own. A value the administrator does not mark is written as plain text.
- A credential in a container's command, entrypoint, label value, healthcheck command or log option is written as it is.
- A stack's secret variables and secret companion files are the only stack values the archive seals.
- The confirmation states which values are written as plain text, and names every variable and file by name, before anything is read.
- Build contexts are stored unencrypted ([Stack builds](#stack-builds)), and are exported as stored.

## Export sealing

Secret values of a stack export and the environment values marked secret in a container export are encrypted with AES-256-GCM under a key derived from a passphrase the administrator enters.

| Item | Value |
|---|---|
| Passphrase | 12 to 1024 characters after Unicode NFKC normalisation, entered twice. It is held in memory for the request. It is not stored, logged, recorded or put back into a page |
| Key derivation | Argon2id, version 19, 64 MiB of memory, 3 iterations, 4 lanes, a 32-byte key |
| Keys | The Argon2id output is expanded with HKDF-SHA-256 into a separate AES-256 key and a MAC key |
| Salt | 16 random bytes for each archive |
| Cipher | AES-256-GCM, a random 12-byte nonce for each value |
| Bound to | The archive's id and the value's name. A value moved to another name, a record taken from another archive, and an altered nonce, ciphertext or salt are refused |
| Names | `variable:{NAME}`, `file:{name}` and `environment:{position}` |
| Recorded in the manifest | `sealed.cipher`, `sealed.kdf` (`algorithm`, `version`, `memory_kib`, `iterations`, `lanes`, `salt`), `sealed.records` (`name`, `nonce`, `ciphertext`), sorted by name, and `sealed.mac` |
| Accepted when reading | 8192 to 262144 KiB of memory, 1 to 8 iterations, 1 to 16 lanes. Anything else is refused, so a damaged manifest cannot ask for more |
| Failure | A wrong passphrase, an altered value, a modified manifest, and a name that is not sealed give one message, `The sealed values could not be opened.`, and name neither the value nor the cause |
| Lost passphrase | The sealed values cannot be recovered by any other means. The rest of the archive is readable and importable without it |
| Secret values in memory | Decrypted in a worker thread and sealed in the same call, then discarded. They are never logged or recorded |
| Passphrase with no secrets | An object with no secret entries seals nothing. The passphrase is not used |

| What a holder of the archive can read without the passphrase | Detail |
|---|---|
| Names | Every secret variable, file and environment variable by name |
| Sizes | The stored size of each secret file |
| Counts | The number of sealed values |

| Residual | Detail |
|---|---|
| A weak passphrase | Anyone who holds the archive can try passphrases offline. Each try costs one Argon2id derivation at the recorded parameters |
| A passphrase kept beside the archive | Opens every sealed value |
| Plain values | Not sealed. They are readable by anyone who holds the archive ([Stack and container export](#stack-and-container-export)) |

## Stack import

An administrator can create a managed stack from a stack export archive ([Compose stacks](Usage-Stacks#import-compose), [Swarm stacks](Usage-Swarm#import-swarm-stacks)).

| Item | Value |
|---|---|
| Who | Administrators only. Any other user receives `404` on every import route, before a request body is read, and nothing is recorded |
| Archives accepted | Only archives written by a stack export of the route's kind. Any archive in the format is accepted. A sealed archive's manifest is verified against its MAC when the passphrase is entered; an archive without a MAC (`secrets` `omitted` or `none`) is not authenticated, and its digests detect damage and do not detect tampering. The review states when an archive has no integrity protection |
| Upload | A multipart `POST` whose first part is the CSRF token, verified before any byte of the file is read. At most 7 GiB and 64 KiB, within 3,600 seconds. Written to `{data directory}/stack-import/` as a `0600` file, with at least 256 MiB left free at every write |
| Held upload | Usable only by the user, the session and the environment that made it, under a random 128-bit identifier. One for each user and kind, four in all, 30 minutes. A stack upload and a container upload by one user are held together. Deleted on every way out, and the whole directory at start |
| Validation | The whole archive is read and checked before anything is created. Every declared document has its size and SHA-256 checked, and a member the manifest does not declare is refused |
| Tar rules | The rules of [volume import](#volume-import), applied to each volume of the archive under its own prefix, and shared with it by one implementation: entry types, names, symbolic links, hard links, owner ids, repeated names. Devices, FIFOs, sockets, absolute names, `..` components and entries below a symbolic link are refused |
| Resource limits | 2,000,000 entries and 64 GiB of file data across every volume, and a ceiling on decompressed bytes, so a compression bomb is refused. At most 2 archives are validated at once |
| Modes and owners | The setuid and setgid bits are cleared. Numeric owner ids are kept. Only `user.` extended attributes are applied |
| Passphrase | Entered on the review page. Used once, in a worker thread, and never stored, logged, recorded or put back into a page. A sealed value that was altered or moved is refused ([Export sealing](#export-sealing)) |
| Attempt limit | 5 attempts for each sign-in session, shared with container import and counted for the life of the process. An attempt is taken before the key derivation starts; a success returns it, and a failure or a cancelled request keeps it. One key derivation runs at a time in the instance. Further attempts answer `429` until the administrator signs in again. The secret values can then be entered by hand |
| Opened values | Held in memory with the upload until the import, the discard or the expiry. Stored encrypted by the create. Never in a log line, an activity row or a response |
| Plan binding | The confirmation carries a keyed SHA-256 over what the review showed of the archive and of the host. The key is random for each process and never stored. A plan that differs, including after a restart, is refused (`changed_since_review`) and nothing is changed |
| Credential choice | Registry credentials are not in an archive. Each image the deployment pulls needs a credential set, or anonymous, chosen on the review. Nothing is preselected, and a matching registry address does not select a set. Without a choice the import is refused (`credential_choice_required`) |
| Binding | The image is bound to the set that pulled it, by the deployment ([Images](Usage-Images)) |
| Labels | The import sets `com.docker.compose.project` and `com.docker.compose.volume`, or `com.docker.stack.namespace`, on each volume it creates, from the stack name. For a Compose stack the labels and driver options the stack file declares for the volume are applied. No label and no driver option recorded in the archive is applied |
| Names | An existing stack, project, namespace, container, volume, network, service, config or secret is never overwritten, adopted or written to. A collision refuses the import until the stack is given another name |
| Volumes | Created and written before the stack is stored. Each carries the label `dockmin.import`, with a value unique to the import, and a volume the host answers the create with that lacks it is refused as a name in use and left untouched. The first deployment accepts a volume the import created only by name and creation time. A project volume the import did not create is judged by the project label, so a failed first deployment can be retried. After a restart, and before the first deployment, a volume carrying the project label and no access control of its own is accepted ([Stacks](Usage-Stacks#import-volumes)) |
| Host paths | Every bind source and every volume `device` of the resolved stack, listed with a required choice for each. The import creates no directory on a host. A kept path is stored as archived; a replaced path becomes a new named volume, and a replaced volume device becomes a plain named volume. The stack file is rewritten only at the nodes that change |
| Host path or share volumes | A volume the import writes into whose options carry a `device`, or a `type` other than empty or `tmpfs`, needs a confirmation bound to the archive's digest, the volume's name, the type and the device (`mounts_unconfirmed`), must read empty through the helper before the write (`mount_not_empty`), and is refused when its `device` is a path dockmin's own container mounts (`self_protected`) |
| Host access | A helper container created from an image already on the host, never started, with the volume mounted read-write, no network, a read-only root file system and all capabilities dropped. It is removed when its volume has been written |
| Failure | Every volume the import created is removed, without force, and no stack record is written. A volume that existed before is never removed |
| Contents | Not scanned. A container that mounts a volume runs with whatever the files contain, including executables. A stack file passes the checks of a create |
| Owner | The importing administrator. No team or other grant is carried |
| Recorded | `stack_imported`, with counts and never a value, a passphrase, a variable or file name, or a path ([Activity](Administration-Activity#stack-import-rows)) |

## Container import

An administrator can prepare a container from a container export archive: the import creates its networks and volumes and opens the create form filled from the archive ([Containers](Usage-Containers#import)).

| Item | Value |
|---|---|
| Who | Administrators only. Any other user receives `404` on every import route, before a request body is read, and nothing is recorded |
| Archives accepted | Only archives written by a container export. Any archive in the format is accepted. Integrity is as for [stack import](#stack-import): a MAC verified on unlock for a sealed archive, none otherwise. A stack archive is refused at upload |
| Upload, held upload | As [stack import](#stack-import): the CSRF token is the first multipart part and is verified before any byte of the file is read; at most 7 GiB and 64 KiB within 3,600 seconds; a `0600` file under `{data directory}/stack-import/`; held for one user, session and environment under a random 128-bit identifier; one for each user and kind, four in all, 30 minutes, shared with stack import |
| Validation, tar rules, limits | As [stack import](#stack-import): the whole archive is read and checked before anything is created, with the rules of [volume import](#volume-import) applied to each volume under its own prefix. Devices, FIFOs, sockets, absolute names, `..` components and entries below a symbolic link are refused. 2,000,000 entries, 64 GiB of file data, a ceiling on decompressed bytes |
| Modes and owners | The setuid and setgid bits are cleared. Numeric owner ids are kept. Only `user.` extended attributes are applied |
| Passphrase, attempt limit | As [stack import](#stack-import): used once, in a worker thread, and never stored, logged, recorded or put back into a page. 5 attempts for each sign-in session, shared with stack import and counted as stated there. Further attempts answer `429` until the administrator signs in again |
| Values in a URL | None. The create form opens at `?imported={id}`, where `{id}` is a random 128-bit identifier. No environment value, secret value, passphrase, name or path is in the URL, a redirect or a notice |
| Held form values | The form's field values, with the opened and entered secret values, are held in memory only, usable only by the user, the session and the environment that made them. 30 minutes, one for each user and environment, eight in all. Released when the create starts. Never in a log line, an activity row or a URL. They appear in the response only as the value of the form's own fields |
| Volumes | Created with the `local` driver, without the archive's driver options and labels, so an archive cannot create a volume backed by a path on the host. Each carries one label, `dockmin.import`, with a value unique to the import. An archived volume with another driver is not written. A volume that exists is never written to: the import refuses until another name is chosen. Each name is checked absent under the volume's lock, and a volume the host answers the create with that lacks the label is refused as a name in use and left untouched |
| Networks | Created only when the environment has none of that name, with the default address management driver, after the checks of the network create form, the check for labels dockmin reserves and a comparison with the address pools of every network on the host. An invalid, overlapping or unsupported network refuses the whole import. A network that exists is used as it is and never changed |
| Host access | A helper container created from an image already on the host, never started, with the volume mounted read-write, no network, a read-only root file system and all capabilities dropped. It is removed when its volume has been written |
| Host paths | Listed with a required choice for each, none preselected. The import creates no directory and reads no content. A kept bind mount stays a [restricted option](Usage-Containers#options-restricted-to-administrators), checked on the final request of the create form. A replaced path becomes a named volume that the create form creates |
| Container creation | By the create form, never by the import. The restriction set runs on the final request of every submission: an administrator sees the restricted options in use under `Administrator options in use`, and a label dockmin sets is refused for every role ([Refused labels](Usage-Containers#refused-labels)); the checks against reaching dockmin's own container run for every role; the credential set for the pull is chosen on the form and binds the image ([Images](Usage-Images)). The import neither pre-checks nor bypasses any of them |
| Plan binding | The confirmation carries a keyed SHA-256 over what the review showed of the archive and of the host. The key is random for each process and never stored. A plan that differs, including after a restart, is refused (`changed_since_review`) and nothing is changed |
| Failure | Every network and volume the import created is removed, in reverse order, volumes without force. An object that existed before is never removed. The failure row names the objects removed and any that could not be removed |
| Owner | The importing administrator owns each network and volume the import creates. No team or other grant is carried |
| Contents | Not scanned. A container that mounts a volume runs with whatever the files contain, including executables |
| Recorded | `container_imported`, with counts and never a value, a passphrase, a variable name or a path ([Activity](Administration-Activity#container-import-rows)) |

## Stack builds

An administrator's Compose stack can build images on its host ([Stacks](Usage-Stacks#builds)). A `RUN` step of a Dockerfile executes on that host while the image builds.

| Item | Value |
|---|---|
| Who | Administrators only. A build can use any image on the host as a base, and every build shares the host's build cache. Neither is covered by the access rules of images or stacks |
| Residual | A `RUN` step can do anything an unprivileged container with default capabilities and the default network can do on that host, while it runs |
| Confirmation | The redeploy confirmation names each service that builds, the image tag that is replaced and the base images, and states that the `RUN` steps execute on the host |
| Context | Only an archive uploaded to the stack. It is read as a stream that accepts regular files and directories, at most 10,000 entries and 256 MiB, and is written to a private directory under the data directory, deleted when the build ends. Symbolic links, hard links, devices, absolute names, `..` and a name used as both a file and a directory are refused. At most 2 archives are processed at once |
| Dockerfile | Read statically before any build. `ONBUILD` is refused. `ADD` of a URL or a Git repository needs `--checksum`. A quote, a backslash or a backtick in the flags of `RUN`, `COPY`, `ADD` and `FROM` is refused, so that no `--mount from=` is hidden from the check |
| Server files | No `build` key names a path on the server: `context` names a stored archive and `dockerfile` a path inside it. `additional_contexts`, `secrets`, `ssh`, `cache_from`, `cache_to` and `dockerfile_inline` are refused, and so are `RUN --mount=type=ssh` and `type=secret` |
| Credentials | buildx holds none: `DOCKER_CONFIG` holds `{}`, no stack variable and no proxy variable is in its environment, and `--pull=false` is set. dockmin pulls each base through the credential set the image is bound to, then checks that the base is on the host |
| Frontend | `# syntax=` is refused. A frontend image would be pulled outside the binding and its code would run as the parser |
| Sandbox | `RUN --network=host`, `RUN --security`, `build.network`, `build.privileged`, `build.entitlements` and `build.extra_hosts` are refused. Builds run in BuildKit's default sandbox with the daemon's default network |
| Bases with triggers | A base image with `ONBUILD` triggers is refused, because its instructions are not read |
| Bases replaced | The image id of each base is recorded when it is checked and compared after each build. A different id fails the build before `up` (`base_changed`) |
| Concurrency | At most 2 builds run at once across the server, apart from the Compose process slots |
| Build arguments | A resolved argument that holds a secret variable's value or a secret file's scrubbed term is refused. Build arguments are recorded in the image history |
| Image names | A tag owned by another project is refused, never moved |
| Binary | The buildx binary is held to the trust check of the Compose binary: owned by root or the dockmin user, writable by neither group nor others ([Buildx binary](Reference-Deployment#buildx-binary)) |
| Output | Build output is scrubbed of secret values and of the data directory path, cut to 1 MiB, and never interpreted |
| Automation | Never builds ([Stacks](Usage-Stacks#builds-and-automation)) |
| Contexts at rest | Not encrypted. They are in the database and in every backup. Keep credentials in secret variables |

## Revealing stack variables

An administrator can show the value of a secret stack variable on the Variables tab of a Compose or a Swarm stack ([Stacks](Usage-Stacks#reveal-a-secret-variable)).

| Item | Value |
|---|---|
| Who | Administrators on a session sign-in, password or access platform. A standard user and an API token cannot reveal. No JSON route returns a secret value |
| Credential | The administrator's own dockmin password, entered again. A miss counts toward the sign-in lock ([Teams and users](Usage-Teams-and-Users#sign-in-lock)) |
| Window | 5 minutes, held on the session, covering every secret variable of every stack the administrator can open |
| Reveals | 20 for each window. Entering the password again opens a new window with none spent |
| Display | The value is shown for 60 seconds. The page script hides it sooner when the page is left. Every answer is sent with `Cache-Control: no-store` and the page's history cache is off |
| Standard users | See 16 `•` characters for every secret variable |
| Access platform accounts | An account created at access platform sign-in has no dockmin password, so it cannot open a window until an administrator sets one ([Teams and users](Usage-Teams-and-Users)) |

A window ends at the first of:

| Cause | Mechanism |
|---|---|
| 5 minutes pass | The window carries its own end time |
| The 20th reveal is spent | The count is kept on the session and the check refuses past 20 |
| The session ends | Expiry, idle timeout, sign-out and revocation each remove the session from use |
| The user's stored password hash changes | A password change, an administrator's reset, or a rehash of the password at sign-in |
| The user's role changes | The same database trigger as the password hash |
| The account is disabled or enabled | The same database trigger |

The trigger closes the window of every session of the user in the same transaction that changes the user.

| What the mask discloses | To whom |
|---|---|
| Whether a value has at least 24 printable characters on one line, with no leading or trailing whitespace, and when it does, its first 4 and last 4 characters | Administrators, on the Variables tab and on a notification channel's page ([Notifications](Administration-Notifications)). A value that does not qualify shows only 16 `•` characters |

| What is recorded | Detail |
|---|---|
| `reveal_window_opened` | The user, the sign-in method and when the window ends. A refused password is a failure row with its reason |
| `stack_variable_revealed` | The user, the stack, the variable's name, a random token that identifies the stored value, when the window ends and the reveals left. Never the value, and never the password ([Activity](Administration-Activity#reveal-and-dismissal-rows)) |
| Notification | The event type `stack_variable_revealed` ([Notifications](Administration-Notifications)) |

| Residual | Detail |
|---|---|
| An administrator's password and session | Together they read every secret stack variable. Each window allows 20, a further window needs the password, and every reveal is recorded |
| A standard user with write access to a stack | They can deploy a file that copies a secret variable into a label, a readable service field or a Compose error, where they can read it ([Stacks](Usage-Stacks#secret-variables)) |
| A browser without script | The reveal form posts to a page that holds the answer, not to a redirect. The browser keeps the posted password with that page so that it can offer to resend it. The value stays on the page until it is left |
| Anyone with access to the host's Engine API | A value interpolated into a container's configuration is readable there ([Stacks](Usage-Stacks#secret-variables)) |

## Reporting a vulnerability

1. Open the repository at `https://github.com/Sphexi/DockMin` and choose the Security tab.
2. Choose `Report a vulnerability`. The report goes to the repository's private vulnerability reporting, not to a public issue.
3. Fill in the form with:

   | Include | Detail |
   |---|---|
   | Version | The product version and the image tag |
   | Description | What was done and what happened |
   | Reproduction | The steps, or a minimal configuration |
   | Impact | What an attacker gains, and what access they need |

4. Submit the report. GitHub keeps it private and creates no public issue.

- Do not open a public issue for a vulnerability.
- Do not include a real encryption key, credential or archive in a report.
