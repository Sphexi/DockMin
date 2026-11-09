# Volumes

Volumes are listed and managed per environment. The list and the detail page are read from the host when the page loads. Nothing is cached. Volume contents are not browsable.

## Pages and routes

All paths are under `/environments/{id}/volumes`.

| Page | Path | Method |
|---|---|---|
| Volume list | The base path | `GET` |
| Volume | `/{volume_name}?tab={tab}` | `GET` |
| Create form | `/-/new` | `GET` |
| Create, or add rows | `/-/new` | `POST` |
| Prune selection and confirmation | `/-/prune?mode={mode}` | `GET` |
| Prune | `/-/prune` | `POST` |
| Remove confirmation | `/{volume_name}/remove` | `GET` |
| Forced remove confirmation | `/{volume_name}/remove?force=1` | `GET` |
| Remove, or remove with containers | `/{volume_name}/remove` | `POST` |
| Bulk removal confirmation | `/-/remove?volume={volume_name}`, repeated | `GET` |
| Bulk removal | `/-/remove` | `POST` |
| Export confirmation | `/{volume_name}/export` | `GET` |
| Export download | `/{volume_name}/export` | `POST` |
| Import upload form | `/-/import` | `GET` |
| Import upload | `/-/import` | `POST` |
| Import review | `/-/import/{upload_id}` | `GET` |
| Import, or add option rows | `/-/import/{upload_id}` | `POST` |
| Import discard | `/-/import/{upload_id}/cancel` | `POST` |
| Owner assignment review | `/-/backfill` | `GET` |
| Owner assignment | `/-/backfill` | `POST` |
| Save or delete the access control | `/{volume_name}/access`, `/{volume_name}/access/delete` | `POST` |

- `{volume_name}` is 1 to 255 characters: letters, digits, `_`, `.`, `-`; the first is a letter or digit. Any other form answers `404` for every role, before any call to the host.
- The fixed paths sit under `/-/`. A volume name cannot begin with `-`, so a volume called `new` or `prune` is still reached at its own path.
- `{tab}` is `overview` (default), `containers` or `access`. Any other value answers `404`. `access` is shown to administrators; other roles receive `403`.
- `{mode}` is `anonymous` (default) or `all`.
- `/-/remove` takes 1 to 25 distinct `volume` values. Outside that, an administrator receives `400`. A standard user's request with no `volume` value answers `404`.
- `{upload_id}` is 32 lowercase hexadecimal digits. Any other form answers `404` for every role.
- Every `POST` except the export download and the import run redirects (`303`) on success. Create redirects to the new volume, remove to the list, the import upload to its review, the import discard to the list. The export download answers `200` with the archive ([Export](#export)). The import run answers `200` with its result page ([Import](#import)). A forced removal and a prune redirect to a result page, `/results/{token}` ([Result pages](Contributing-Frontend-Conventions#result-pages)), which lists each target and outcome; reloading it repeats nothing.
- A refusal renders a page at the real status code, with a `Review again` link for `409`, `502` and `503`.
- Volume routes have no JSON form.

## Access

Viewing needs sight of the environment ([Access](Usage-Environments#access)). A standard user without it receives `404` on every route.

A volume is identified by its name. Visibility follows the order in [Visibility](Usage-Containers#visibility): the volume's own resource control, then the control of its stack, then the default access policy for uncontrolled objects. A volume belongs to a stack when it carries the label `com.docker.compose.project` or `com.docker.stack.namespace`.

| Action | Administrators | Standard users |
|---|---|---|
| List, volume page | All volumes | Volumes they may see; `404` for any other name |
| Create | Any driver and driver options | Remote environments they hold a grant on; driver `local` and no driver options. `403` on a local socket; `404` without a grant |
| Remove | Yes | Write access to the volume. `403` on a local socket |
| Bulk remove | Yes | Volumes they hold write access to. The others are counted and left out. On a local socket environment none qualifies |
| Remove with its containers | Yes | Write access to the volume and to every container that uses it. `403` on a local socket |
| Prune | Yes | Refused (`403`) |
| Export | Yes, except a volume dockmin's own container mounts | `404` on both routes |
| Import | Yes | `404` on every import route |
| Assign owners | Yes | `404` on both routes |
| Access tab | Yes | Refused (`403`) |

- A volume that does not exist, and a volume the user may not see, answer identically (`404`).
- A volume the user may read but not change shows no `Remove` button. Posting a removal answers `403`.
- On a local socket environment a standard user has read access at most ([Local socket environments](Usage-Containers#local-socket-environments)).

| Withheld from standard users | Shown to administrators |
|---|---|
| Mount point (`Not shown for your role`) | Yes |
| Driver options (`Not shown for your role`) | Yes. Values are scrubbed of embedded secrets, including `user%password` and `user:password@host` forms; a secret-named key shows `Redacted` |
| Values of secret-named label keys (`Redacted`) | Yes |
| Names of containers they may not see | Yes. The `Used by` count includes every container on the host |

Secret-named keys follow the rule in [Withheld from standard users](Usage-Containers#withheld-from-standard-users).

## Volume list

| Item | Value |
|---|---|
| Columns | Select (users who may remove a volume on the page), Name, Driver, Created, Stack, Used by, Actions |
| Name | An anonymous volume shows its first 12 characters, with the full name as the cell's title |
| Badge | `Anonymous` for an anonymous volume. `In use by dockmin` for a volume the container dockmin runs in has mounted ([dockmin's volumes](#dockmins-volumes)) |
| Select column | A checkbox on each volume the user may remove, for [bulk removal](#bulk-removal). Disabled on a volume marked `In use by dockmin`. Shift-click ticks a range of enabled boxes ([Range selection](Usage-Containers#range-selection)) |
| Actions column | `Details` for everyone; `Remove` for users with write access to the volume, except on a volume marked `In use by dockmin` |
| Order | Name, ignoring case. The Name, Driver, Created and Stack headers sort the list ([Sorting](#sorting)) |
| Page size | 25 by default; `limit` 1 to 500. Outside the range answers `422` |
| Paging | `cursor`; an invalid cursor answers `400` |
| Count | `N volumes`, or `N volumes match these filters, of M you can see` |
| Used by | Names of up to 10 containers the viewer may see, linked. Containers beyond the limit, and containers the viewer may not see, are counted and not named |
| Buttons | `Create volume` for users who may create; `Assign owners` for administrators when a volume is uncontrolled ([Assign owners](#assign-owners)); `Import volume` and `Prune volumes` for administrators; `Remove selected...` for users who may remove |

Every volume the viewer may see is listed. A container uses a volume when it has a mount of type `volume` with the volume's name, in any state. Bind mounts and tmpfs mounts do not count.

A volume is anonymous when it carries the label `com.docker.volume.anonymous` or its name is 64 hexadecimal digits. Every other volume is named.

### Filters

| Filter | Parameter | Match |
|---|---|---|
| Name contains | `q` | At most 128 characters. Case-insensitive substring of the name |
| Driver | `driver` | `local`, or `other` for any other driver |
| Use | `usage` | `used` or `unused` |
| Kind | `kind` | `named` or `anonymous` |
| Stack | `stack` | At most 128 characters. Case-insensitive exact match on the stack name |

An invalid filter value replaces the table with the message for the field and answers `400`. Filters survive paging.

### Sorting

The rules of [Sorting](Usage-Containers#sorting) apply: `sort` and `dir`, the header links, the cursor, and the `400` answers (`Sort by one of the listed columns.`, `Choose ascending or descending.`, `The page link is not valid.`).

| Column | `sort` | Orders by | Empty value |
|---|---|---|---|
| Name | `name` | Name, ignoring case | None |
| Driver | `driver` | Driver name, ignoring case | None |
| Created | `created` | Creation time | A volume whose creation time is absent or unreadable |
| Stack | `stack` | Stack name, ignoring case | A volume with no stack |

Default order: `name` ascending, then id. Empty values come first ascending and last descending. Filters apply before the sort.

### Host failures

The list is replaced by an error, and the answer is `503`. The messages are those of [Host failures](Usage-Containers#host-failures). A host whose volume list exceeds 16 MiB answers `This host reports more volume data than can be listed here (limit 16 MiB).` and no partial list is shown. The container list read beside it is limited to 32 MiB.

## Volume page

| Tab | Content | Shown to |
|---|---|---|
| Overview | Name, driver, scope, kind (`Named`, or `Anonymous: created by a container without a name`), created, stack, `Used by` (count of containers), mount point, driver options, labels | Everyone. Mount point and driver options: administrators |
| Containers | Name, short id and state of the containers using the volume: at most 10 the viewer may see, then counts of the rest and of those the viewer may not see | Everyone |
| Access | Resource control | Administrators |

- Label values are cut at 4096 characters. A volume whose labels were cut while reading shows `Only the first labels are shown.`
- Values of driver options that look like secrets are redacted.
- The page offers `Remove` to users with write access, and the badge `In use by dockmin` in its place for a volume dockmin's own container mounts.
- The page offers `Export` to administrators, except on a volume marked `In use by dockmin` ([Export](#export)).
- A raw inspect document is never shown.

## Create

1. Open `Create volume` on the volume list.
2. Fill `Name`. Administrators may fill `Driver` and driver options; any user may add labels.
3. Choose `Create volume`. The volume page opens. When a field is refused, the form returns with the message beside the field.

| Field | Administrators | Standard users |
|---|---|---|
| Name | Required. 2 to 255 characters: letters, digits, `_`, `.`, `-`; the first is a letter or digit. Not 64 hexadecimal digits | Same |
| Driver | `[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}`, such as `local` or `vieux/sshfs:latest`. Empty is `local` | Not offered. `local` |
| Driver options | Up to 32 key and value rows. Key: 1 to 128 characters of letters, digits, `.`, `_`, `-`; the first is a letter or digit. Value: at most 1024 characters, no control characters. A repeated key is refused | Not offered. None are accepted |
| Labels | Up to 64 key and value rows, 64 KiB in all. Key at most 255 characters. No NUL in a value | Same |

- The `local` options `type`, `device` and `o` bind a host path or mount a remote filesystem; a standard user may set no option.
- Labels with the keys `com.docker.compose.project` and `com.docker.stack.namespace` are refused, whatever their case and surrounding whitespace. The label `com.docker.volume.anonymous` is refused: `This label is set by Docker.`
- A name that exists is never adopted: dockmin checks first and refuses.
- If a volume of the name appears between that check and the create, and differs from the request in driver, options or labels, no ownership is recorded for it. The result page shows `A volume with this name appeared just before the request. It was left as it is and no ownership was recorded for it.` and the row carries `preexisting`. A plain volume with identical settings that another client creates in that interval is claimed by the creator.
- A resource control left on the name by a volume that was removed outside dockmin is replaced by the new owner's. The row records `stale_control_replaced`.
- The new volume is put under a resource control owned by its creator ([Ownership](#ownership)).

| Result | Status | Message | Shown |
|---|---|---|---|
| Invalid field | 400 | The field's message | Beside the field |
| Standard user set a driver or an option | 400 | `Only administrators can choose a volume driver.` or `Only administrators can set the option <key>.` | Beside the field |
| Name taken | 409 | `A volume with this name already exists.` | Name field |
| Docker refused the configuration | 502 | `Docker refused this volume configuration.` | Banner |
| Host unavailable | 503 | The host message ([Host failures](Usage-Containers#host-failures)) | Banner |
| Host did not answer in time | 503 | `The Docker host did not answer in time. The action may still complete.` | Banner. Check the volume list before submitting again |

Docker's own error text is shown only to administrators, under the message, in a block headed `Docker said`. It is scrubbed, reduced to one line and cut to 300 characters, appears only when the Engine refused with status `400` or `404` and sent a message, and is not written to any log line or activity row. A standard user sees the message in the table only. The block appears on the create form for a refused configuration and on the page of any other `Docker refused the action` answer.

## Remove

Removal deletes a volume and all of the data in it. It needs write access to the volume.

1. Open `Remove` on the volume list or the volume page.
2. Read the confirmation. When it reads `This volume is in use`, stop the containers it names and remove them, or choose `Continue to force removal` ([Remove with its containers](#remove-with-its-containers)).
3. Choose `Remove volume`. The list opens with `Volume removed.`

| Item | Value |
|---|---|
| Confirmation | The environment, the volume, its driver, kind, stack, and the containers that use it |
| In use | A volume is in use when a container in any state mounts it. Stopped containers count |
| Not in use | The confirmation carries the volume's creation time and the number of containers using it (`0`). A different creation time, or any user at removal, refuses the removal |
| In use: the confirmation | Reads `This volume is in use`. Names the containers the viewer may see, counts the rest, and offers only `Continue to force removal` |
| Lock | The volume is held for the removal ([Busy](#busy)). The use is counted again before the volume is removed |
| Access control | Deleted with the volume |

| Result | Status | Message |
|---|---|---|
| Removed | 303 | Redirect to the list with `Volume removed.` |
| In use | 409 | `The volume is used by one or more containers.` |
| Creation time or use changed | 409 | `This volume changed since you reviewed it; nothing was changed.` |
| Another action holds the volume | 409 | [Busy](#busy) |
| Volume gone | 404 | `The volume no longer exists.` |
| Docker refused | 502 | `Docker refused the action.` |
| Host unavailable | 503 | The host message |

### Remove with its containers

`Continue to force removal` opens `?force=1`, a separate confirmation titled `Force remove volume`. Docker does not remove a volume that any container mounts. The force path removes those containers first, then the volume.

| Item | Value |
|---|---|
| Needs | Write access to the volume and to every container that uses it |
| Page | Lists under `Containers that would be removed first` every container that uses the volume (name, short id, state) and under `What forcing does` states what happens |
| Order | The containers in name order, one at a time, then the volume. Button: `Force remove N containers and the volume` (`container` for one) |
| Each container | Removed through the container remove action: without force and without its anonymous volumes. Each writes its own `container_removed` row |
| Stops at | The first container that is not removed, because the volume removal depends on all of them. The volume is kept. Containers already removed stay removed |
| Reviewed state | The confirmation carries the volume's creation time, a SHA-256 digest of the ids of all containers that use it, and each container's name. Any difference refuses the whole run |
| Locks | Each container, then the volume, as in a single removal |
| Limit | 25 containers |
| Page with nothing to force | A volume with no users redirects to the plain confirmation |

The force path does not:

- stop, kill or pause a container. A container in state `running`, `paused` or `restarting` refuses the run until it is stopped
- remove a container the user cannot change. The page counts such containers and names none
- remove the anonymous volumes of the containers it removes
- send Docker's `force` flag to the volume removal. That flag does not override a volume in use; for a volume plugin it makes the daemon drop the volume's record when the plugin fails to remove it, which can leave data on the host that is no longer listed
- undo a container removal when a later step fails

| Refusal | Status | Message |
|---|---|---|
| A using container is `running`, `paused` or `restarting` | 409 | `Containers that are running must be stopped first.` The page lists them |
| The user cannot change a using container | 409 | The page counts the containers, and names none |
| More than 25 containers use the volume | 409 | `More than 25 containers use this volume.` The page asks for some to be removed first |
| Containers, their names or the creation time differ from the confirmation | 409 | `This volume changed since you reviewed it; nothing was changed.` |

A refused run writes no row and removes nothing. The page titled `This removal cannot go ahead` shows the refusal with a `Review again` link.

The result page lists each container with its outcome, then the volume.

| Outcome per container | Meaning |
|---|---|
| `Removed` | Gone |
| `Already gone` | It was removed before the step |
| `Not found` | The container is not visible any more |
| `Not permitted` | The user's access to it ended |
| `Busy` | Another action holds the container |
| `Refused: the container is not in a state that allows it` | The container's state changed |
| `The host could not be reached` | |
| `Failed` | Docker refused the removal |

The run answers `303` to the result page whether or not the volume was removed. The page states which, and for a run that ended early, why.

## Bulk removal

Bulk removal deletes several volumes in one reviewed step. It needs write access to each volume.

1. On the volume list, tick the `Select` checkbox of each volume to remove.
2. Choose `Remove selected...`. The confirmation lists each volume and whether it is removed or kept. Nothing changes yet.
3. Choose `Remove N volumes`. The result page shows the count removed out of the count submitted and one row per volume.

| Item | Value |
|---|---|
| Selection | The `Select` column of the list, then `Remove selected...`. 1 to 25 volumes: `Select at least one volume.` and `Select at most 25 volumes.` (`400`) |
| Confirmation | `GET /environments/{id}/volumes/-/remove?volume={volume_name}`. Lists each volume: name, driver, kind, stack, and whether it is removed or kept. Nothing changes |
| Left out | A selected volume that does not exist, that the user may not see, or that the user may read but not change is counted and reported as left out. Its name is not given |
| In use | A volume that any container mounts is named and kept, under `Volumes in use are kept`. The confirmation names the containers the viewer may see and counts the rest. `Remove with its containers...` opens the forced confirmation of that volume |
| Force | Not available in bulk |
| dockmin's volumes | Listed under `Left out` as kept, never removed ([dockmin's volumes](#dockmins-volumes)) |
| Posted | For each removable volume: its name, creation time and the number of containers mounting it (`target`) |
| Run | Each volume is removed on its own, in the order submitted, as [Remove](#remove) does. The creation time and the use count are checked again for each. A refusal or failure on one volume is that volume's result and does not stop the others. Nothing is rolled back |
| Result | `303` to `/results/{token}` ([Result pages](Contributing-Frontend-Conventions#result-pages)): the count removed out of the count submitted, and one row per volume |

| Result per volume | Message |
|---|---|
| `Removed` | `Removed.` |
| `Kept: in use` | `In use by a container. Stop and remove the container first.` |
| `Skipped: changed since you reviewed it` | `This volume changed since you reviewed it; nothing was changed.` |
| `Skipped: busy` | `Another action on this volume is in progress.` |
| `Already gone` | `Not found, or already removed.` |
| `Read only` | `You can read this volume but not change it.` |
| `Protected` | `The volume {name} is mounted in the container dockmin runs in. It cannot be removed from dockmin.` |
| `Unreachable` | The host message |
| `Failed` | `Docker refused the action.` or `The volume could not be removed.` |

Each removal writes a `volume_removed` row with `batch` set. The access control of a removed volume is deleted with it, as for a single removal.

## Export

Export downloads one volume as a gzip archive holding its files and a manifest. Administrators only. The archive is not encrypted.

1. Open `Export` on the volume page.
2. Read the confirmation.
3. Choose `Download archive`. The browser saves `volume-{name}-{YYYYMMDDTHHMMSSZ}.tar.gz`.

| Item | Value |
|---|---|
| Who | Administrators. A standard user receives `404` on both routes and nothing is recorded |
| Volume | Driver `local` only, with or without driver options. Any size |
| Read | A helper container is created on the host and never started; the volume is mounted into it read-only and the files are streamed from the Engine's archive call ([The helper container](#the-helper-container)) |
| Output | `application/gzip`, `Content-Disposition: attachment`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` |
| Writes | Nothing on the host is changed except the helper container, which is created and removed |
| Disk | Nothing is written to dockmin's disk. The archive is produced while the data is read |
| Lock | The volume is held until the download ends ([Busy](#busy)) |
| Concurrency | One export at a time on an environment, two in all ([Limits](#limits)) |
| Import | An export archive is imported as a new volume ([Import](#import)) |

### Confirmation

`GET /environments/{id}/volumes/{volume_name}/export`. Nothing is read, created or recorded.

| Element | Text |
|---|---|
| Heading | `Export volume {name}?` |
| Subtitle | `Review the volume below. Nothing is read until you confirm.` |
| Facts | Driver, Created, Environment. The volume's size is not shown |
| `What the archive holds` | `Every file of the volume under data/, with its mode, numeric owner ids, timestamps and symbolic links.` `manifest.json: the volume's name, driver, labels and driver options. Credentials in driver options are left out and listed by key.` `Device files and FIFOs are left out and listed in skipped.json. Sockets are not read.` |
| Alert `The archive is not encrypted` | `The archive holds the volume's files as they are. Anyone who holds the file can read everything in the volume.` |
| Alert `The volume is in use` | Shown when a container in state `running`, `paused` or `restarting` mounts the volume: `{N} running containers mount this volume. If they write while the archive is read, the archive can be inconsistent. A database's files are the usual case. Nothing is stopped.` One container: `1 running container mounts this volume. If it writes while the archive is read, the archive can be inconsistent. ...`. Names up to 10 containers and counts the rest |
| Line when none runs | `No running container mounts the volume.` |
| `The helper container` | `dockmin creates a container named dockmin-read- followed by twelve characters from the image {first 12 digits of the image id}, which is already on the host. The container is never started. It mounts the volume read-only, has no network and no capabilities, and appears in the container list until the export ends. It is removed when the export ends, however it ends.` |
| Buttons | `Download archive` (a form that posts the CSRF token and the volume's creation time), `Cancel` |
| No image on the host | The alert `The host has no image to use`: `The helper container is created from an image that is already on the host, and nothing is pulled. Pull or load an image on the host, then review the export again.` No download button |

- The download is a plain form `POST`. It works without script.
- A running container does not stop the export.

### Archive

A tar archive in PAX format, compressed with gzip (level 6, no timestamp in the gzip header). Members in this order:

| Member | Content |
|---|---|
| `manifest.json` | JSON, keys sorted, indented two spaces ([Manifest](#manifest)) |
| `data/` | The volume's root directory, then every entry below it as `data/{relative path}`, in the order the Engine lists them |
| `skipped.json` | Entries left out ([skipped.json](#skippedjson)). Always present, last |

| Entry in the volume | In the archive |
|---|---|
| Regular file | Copied, with content, mode, numeric uid and gid, and modification time |
| Directory | Copied, with mode, ids and modification time |
| Symbolic link | Copied as a link with its target as written. Targets are not rewritten or followed |
| Hard link | Copied as a hard link; the target is rewritten to its path under `data/` |
| Extended attributes | `SCHILY.xattr.*` records are kept. Other PAX records are rebuilt from the member, not copied |
| FIFO, character device, block device | Left out, listed in `skipped.json` |
| Unix socket | Not in the archive and not in `skipped.json`. The Engine's archive stream does not carry sockets |
| Any other member type | Left out, listed in `skipped.json` with type `other` |

- Owner names are not recorded. Ownership is the numeric uid and gid.
- The archive holds the files as they are on the host at the time of the read.
- A stream that is not the complete tar of one directory fails the export: no end marker, data after the end marker, a header over 1 MiB, a member outside the directory, a member with a `..` segment. The response then ends without the gzip trailer, so the file does not decompress cleanly.

#### Manifest

| Field | Type | Value |
|---|---|---|
| `format_version` | integer | `1` |
| `kind` | string | `volume` |
| `exported_at` | string | UTC time of the export, ISO 8601, ending in `Z` |
| `instance_version` | string | The dockmin version that wrote the archive; `unknown` when it cannot be read |
| `name` | string | The volume's name |
| `driver` | string | `local` |
| `labels` | object | The volume's labels, keys sorted, without the excluded labels below |
| `options` | object | The volume's driver options, keys sorted, with credentials removed ([Credentials in driver options](#credentials-in-driver-options)) |
| `removed_option_keys` | array of strings | What was removed from `options`, as keys and `key.name` entries. Never a value |
| `data_path` | string | `data` |

- A field not listed is refused by a reader. The volume's resource control is not exported.
- Labels left out, matched without regard to case: `com.docker.stack.namespace`, and every label beginning `com.docker.compose.` or `dockmin.`.

#### Credentials in driver options

| Case | Result | Listed in `removed_option_keys` |
|---|---|---|
| An option whose key names a credential: `password`, `passwd`, `pass`, `pwd`, `passphrase`, `secret`, `secretfile`, `token`, `key`, `apikey`, `credential`, `credentials`, `cred`, `creds`, or a key the log scrubber treats as secret | The option is removed | `{key}` |
| A comma-separated entry inside a value whose name is one of those, such as `password=...` in `o` | The entry is removed | `{key}.{name}`, for example `o.password` |
| `user=name%password` or `username=name%password` inside a value | The text from `%` is cut | `{key}.password` |
| `user:password@host` inside a value | The password is cut | `{key}.password` |

Every other option is written as found.

#### skipped.json

| Field | Value |
|---|---|
| `format_version` | `1` |
| `count` | The number of entries left out, exact |
| `truncated` | `true` when `count` exceeds the 10,000 entries listed |
| `skipped` | Objects with `path` (relative to `data/`) and `type`: `fifo`, `character_device`, `block_device` or `other` |

### Refusals

A refusal before the download starts renders a page at the real status code. Nothing is read from the volume and no helper container is created, except where the table says otherwise.

| Condition | Status | Page | Message | Reason |
|---|---|---|---|---|
| Not an administrator, a name that is not a valid volume name, a volume that does not exist or is not visible | 404 | | Not found | No row |
| The volume is mounted in the container dockmin runs in | 409 | `Protected` | `The volume {name} is mounted in the container dockmin runs in. It cannot be exported from dockmin.` | `self_protected` |
| The volume is a `local` volume whose `device` option is, holds or lies inside a source of a mount of dockmin's container | 409 | `Protected` | The same message | `self_protected` |
| dockmin's own mounts on the host are not known | 409 | `Protected` | `dockmin cannot establish which volumes its own container mounts on this host, so no volume here can be exported until it can.` No volume on that host can be exported | `self_protected` |
| The driver is not `local` | 409 | `The volume cannot be exported` | `Only volumes of the local driver can be exported. A plugin driver can run code on the host when its volume is mounted.` | `plugin_driver` |
| The creation time differs from the confirmation's | 409 | `The volume changed` | `This volume changed since you reviewed it; nothing was changed.` | `changed_since_review` |
| Another action holds the volume | 409 | `This volume is busy` | [Busy](#busy) | `object_busy` |
| An export already runs on the environment | 429, `Retry-After: 5` | `The export was not started` | `A volume export is already running on this environment. Try again when it has finished.` | `export_busy` |
| Two exports run in the instance | 429, `Retry-After: 5` | `The export was not started` | `Too many volume exports are running. Try again shortly.` | `export_busy` |
| The volume was removed after the review | 404 | `The volume no longer exists` | `The volume no longer exists.` | `volume_gone` |
| The host has no image | 409 | `The volume cannot be exported` | `The host has no image to create the helper container from. Pull or load an image on the host, then try again.` | `helper_image_missing` |
| Docker refused to create the helper | 409 | `The volume cannot be exported` | `Docker refused to create the helper container.` | `helper_refused` |
| Docker could not read the volume | 409 | `The volume cannot be exported` | `Docker could not read the volume's contents.` | `unreadable` |
| The host is unreachable or answers unexpectedly | 503 | The host message | [Host failures](Usage-Containers#host-failures) | The host failure reason |

- `unreadable` is known only after the helper exists. The helper is removed before the page is returned.
- The confirmation applies the same checks for dockmin's own volumes, mounts and plugin drivers. It does not check the creation time, the lock or the limits.
- A volume with driver options (NFS, CIFS) is not refused. The Docker host mounts the storage when the volume is read.

### What is left out

| Left out | Where it is recorded |
|---|---|
| Device files and FIFOs | `skipped.json`, and the count in `skipped` on the `volume_exported` row |
| Unix sockets | Nowhere. They are absent from the archive and from `skipped.json` |
| Entries of any other member type | `skipped.json` with type `other` |
| The volume's resource control (owner, teams) | Not exported |
| Credential option keys and values | `removed_option_keys` in the manifest |
| Labels `com.docker.compose.*`, `com.docker.stack.namespace`, `dockmin.*` | Not recorded |

### The helper container

| Item | Value |
|---|---|
| Image | An image already on the host. Nothing is pulled. Images are taken smallest first, at most 16 are inspected, and the first that declares no `VOLUME` is used. When every one inspected declares a volume, the smallest is used |
| Name | `dockmin-read-{12 hexadecimal digits}` |
| Label | `dockmin.role=file-reader` |
| State | `created`, always. The container is never started and no code from the image runs |
| Body | `NetworkDisabled` true. `HostConfig`: `Mounts` `[{"Type": "volume", "Source": "{volume}", "Target": "/dockmin-read", "ReadOnly": true}]`, `NetworkMode` `none`, `ReadonlyRootfs` true, `CapDrop` `["ALL"]`, `AutoRemove` false |
| Read | The Engine's container archive call for `/dockmin-read`. The Engine serves the mounts of a created container. The archive's top directory `dockmin-read/` becomes `data/` |
| Visible | The helper is listed with the other containers on the host while it exists |
| Removed | The Engine's container delete with `v` true, when the download ends by completion, failure, client disconnect or cancellation. The removal is shielded from cancellation and has 30 seconds. Anonymous volumes the image declared are removed with it. The exported volume is never removed |
| Removal refused | The row records `helper_removed` `false`. A helper in state `created` for over 10 minutes is removed, with its anonymous volumes, when the next helper container is created on that environment, unless an export is still using it |
| Recorded | `helper_container`: the full id, on `volume_export_started` |

### Limits

| Limit | Value |
|---|---|
| Size | None |
| Total time | None. The download lasts as long as data flows |
| Stall | The export fails when the host sends no byte for 60 seconds |
| Concurrent exports | 1 for each environment, 2 in the instance, counted in the process |
| Memory | Bounded: at most four 64 KiB output chunks are queued, and no tar member is held whole |
| Directory entries | `skipped.json` lists at most 10,000 |

### Format version

| Item | Value |
|---|---|
| Written | `format_version` `1` |
| Reading | A later release reads every earlier `format_version` |
| Newer archive | An archive whose `format_version` is above the version the release knows is refused |

## Import

Import creates one new volume from a volume export archive and writes the archive's files into it. Administrators only.

1. Open `Import volume` on the volume list.
2. Choose the archive and `Upload and check`. The archive is read in full. Nothing is created.
3. Read the review. Change the name, driver or driver options if needed.
4. Choose `Import volume`. The result page opens.

| Item | Value |
|---|---|
| Who | Administrators. A standard user receives `404` on every import route and nothing is recorded |
| Accepts | Only an archive written by a volume export ([Archive](#archive)). A plain tar file, or any archive that does not start with `manifest.json`, is refused |
| Creates | A new volume on the host the environment connects to. On a Swarm environment that is the manager the environment reaches, no other node. An existing volume is never written to, replaced or adopted |
| Owner | The importing administrator ([Ownership](#ownership)) |
| Write | A helper container is created on the host and never started; the new volume is mounted into it read-write and the files are sent to the Engine's archive call ([Import helper](#import-helper)) |
| Disk | The upload is kept in a private directory under the data directory until the import ends ([Held uploads](#held-uploads)) |
| Lock | The volume name is held from the absence check until the owner is saved ([Busy](#busy)) |
| Recorded | One `volume_imported` row for each import request that reaches the import. The upload, the review and the discard record nothing ([Recorded actions](#recorded-actions)) |

### Pages

#### Upload

`GET /environments/{id}/volumes/-/import`.

| Element | Text |
|---|---|
| Heading | `Import volume`, with the environment name and badge |
| Panel `Upload an archive` | `The archive must have been written by a volume export. It is checked in full before anything is created, and you review what it holds before the volume is created.` |
| Field `Archive` | Required file input, `.gz`, `.tgz` or `application/gzip`. Help: `A .tar.gz file written by a volume export, up to 7 GiB as uploaded. Devices, FIFOs, sockets, absolute names, names containing .. and entries below a symbolic link are refused.` |
| Buttons | `Upload and check`, `Cancel` |

The upload is a multipart `POST` with the fields in this order: `csrf_token`, then `archive`. The token is verified before any byte of the file is read. The form works without script.

| Result | Status | Shown |
|---|---|---|
| Archive accepted | `303` | The review |
| Archive refused, upload refused for size or space, or archive of a driver other than `local` | `422` | The upload page, with the message beside `Archive` ([Refusals of the archive](#refusals-of-the-archive)) |
| Token missing or wrong, or Origin not accepted | `403` | |
| The body is not a multipart form of the token followed by one file | `400` | |
| No body data for 15 seconds, or the body takes over 3,600 seconds | `408`, `Connection: close` | |
| The body exceeds 7 GiB plus 64 KiB | `413` | |

#### Review

`GET /environments/{id}/volumes/-/import/{upload_id}`. Nothing is created or recorded. The page reads the held upload, never the request body.

| Section | Content |
|---|---|
| Heading | `Review the import`. Subtitle: `Nothing has been created yet. Check what the archive holds, then confirm.` Button `Back to volumes` |
| `The archive` | Environment; Format version; Exported (time); Source volume (the name in the manifest); Size as uploaded; Entries (`{N}: {n} files, {n} directories, {n} symbolic links, {n} hard links`); File data (total size of file content); Left out when exported (`{N} devices or FIFOs`, from `skipped.json`); SHA-256 of the uploaded file |
| `The volume this creates` | `Name` (required, default the source name), `Driver` (default the archive's), `Driver options` rows (default the archive's), and the note on removed credential keys |
| `Labels` | The labels that will be applied, and the labels that will not |
| `What happens` | Five statements ([What happens](#what-happens)) and the helper container description |
| Buttons | `Import volume` (absent when the host has no image), `Back to volumes`, and a separate form `Discard the upload` |

| Element | Text |
|---|---|
| Alert when the name exists | `A volume named {name} already exists on this host. An existing volume is never written to. Choose another name.` |
| `Name` help | `At least two characters: letters, digits, underscore, period and hyphen, starting with a letter or digit. The name must not exist on the host.` |
| `Driver` help | `Only the local driver can be imported. Leave empty for local.` |
| `Driver options` help | `Passed to the driver as they are. For the local driver, type, device and o bind a host path or mount a remote filesystem.` |
| Removed option keys present | `Left out when the volume was exported, because they can hold credentials: {keys}. Enter them above if the volume needs them.` |
| No option key removed | `No driver option was left out of the archive.` |
| Labels applied | `Applied as archived:` and a table of `Label` and `Value`. Values are cut to 256 characters with `...` |
| No label to apply | `The archive carries no labels to apply.` |
| Labels dropped | `Not applied, because they grant access to a stack, belong to dockmin or are set by Docker:` and the keys |
| No label dropped | `No label was dropped.` |
| No image on the host | The alert `The host has no image to use`: `The helper container is created from an image that is already on the host, and nothing is pulled. Pull or load an image on the host, then review the import again.` |

- The review reads the host to show whether the name exists and whether a helper image exists. A host that cannot be read answers a page at `503`.
- The option rows are added by a form post that imports nothing.
- Credentials are shown nowhere. The review lists the keys the export removed and never a value.

##### What happens

| Statement |
|---|
| `The volume becomes yours, as when you create one.` |
| `Files are written as archived: their numeric owner ids and modes are kept, except that setuid and setgid bits are cleared. A container that mounts the volume runs with whatever the files contain.` |
| `The data directory's own owner and mode are not applied; the new volume's root keeps Docker's defaults.` |
| `Extended attributes other than user. ones are not applied` and, when entries carried others, `({N} entries carried some).` |
| `If the data cannot be written, the volume this import created is removed.` |

The helper paragraph reads: `dockmin creates a container named dockmin-read- followed by twelve characters from the image {first 12 digits of the image id}, which is already on the host. The container is never started. It mounts the new volume, has no network and no capabilities, and appears in the container list until the import ends. It is removed when the import ends, however it ends.`

#### Result

`POST /environments/{id}/volumes/-/import/{upload_id}` answers `200` with the result page. The upload is removed once the volume has been created, so a second post of the same `{upload_id}` answers `The upload is gone`.

| Element | Text |
|---|---|
| Heading | `Volume imported`, with the environment name. Buttons `Open the volume`, `Back to volumes` |
| Status | `The volume {name} was created and holds the archived data.` |
| Facts | Environment; Driver; Entries written; File data; Labels applied (count); Labels dropped (the keys, or `None`); Owner |
| Owner | `You`, or `Not recorded. An administrator can assign access to the volume.` when the owner could not be saved |

#### Discard

`POST /environments/{id}/volumes/-/import/{upload_id}/cancel` removes the held upload and redirects to the list with the message `The upload was discarded. Nothing was changed.` An upload whose import is running is not discarded.

### Accepted and refused entries

An archive is read twice: when it is uploaded, and again while it is sent to the Engine. Both reads apply every rule below.

| Rule | Value |
|---|---|
| Compression | gzip only |
| First entry | A regular file named `manifest.json` |
| Last entry | `skipped.json` when present. An entry after it is refused |
| Every other entry | `data`, or a name below `data/` |
| Data directory entry | `data/` may appear once, as a directory. It is not written |
| Empty archive | An archive with no entry below `data/` is refused |

| Entry type | Result |
|---|---|
| Regular file | Written with its content |
| Directory | Written |
| Symbolic link | Written as a link with its target as stored. An absolute target is stored as written. A relative target that resolves outside the volume's root is refused |
| Hard link | Written as a hard link. The target must be a regular file that comes earlier in the archive, named relative to `data/` |
| Character device, block device, FIFO, socket, sparse file, any other type | Refused |

| Name rule | Result |
|---|---|
| Absolute name | Refused |
| A `..` component | Refused |
| Empty or `.` component below `data/` (`data//x`, `data/./x`) | Refused |
| Control character, or a name that is not valid UTF-8 | Refused |
| Path over 4,096 bytes, or a component over 255 bytes | Refused |
| A name that appears twice | Refused |
| A name used as both a file and a directory | Refused |
| An entry below a symbolic link of the archive | Refused |
| Owner uid or gid outside 0 to 4294967295 | Refused |
| A directory entry with a size | Refused |

A hard link target that fails the name rules, or that is not an earlier regular file, is refused as a bad hard link target.

#### Refusals of the archive

The message is shown beside `Archive` with ` Entry: {name}.` appended when the refusal concerns one entry. The name is shown with unprintable characters replaced by `?`, cut to 80 characters.

| Condition | Message |
|---|---|
| The first two bytes are not a gzip header | `The file is not a gzip archive.` |
| A truncated or corrupt gzip or tar stream, a `skipped.json` that is not an object with an integer `count` of 0 or more, a `skipped.json` over 16 MiB, a directory entry with a size, or an unreadable modification time | `The file is not a readable volume export archive.` |
| The first entry is not `manifest.json` | `The archive was not written by a volume export: it does not start with manifest.json.` |
| `manifest.json` over 1 MiB, not JSON, a `format_version` that is missing, not an integer or below 1, a field missing or not in the [manifest](#manifest), `kind` other than `volume`, or `data_path` other than `data` | `The archive's manifest.json is not a valid volume manifest.` |
| `format_version` above the reader's | `The archive's format version is {N}; this build reads up to 1.` |
| An entry of a type that is refused | `The archive holds an entry that is not a file, directory or link, such as a device, FIFO or socket.` |
| An absolute name | `The archive holds an entry with an absolute name.` |
| A `..` component | ``The archive holds an entry whose name contains `..`.`` |
| A name or component over the limit | `The archive holds an entry with a name that is too long.` |
| An unusable name, or an unusable symbolic link target (empty, a control character, over 4,096 bytes) | `The archive holds an entry with an unusable name or link target.` |
| An entry that is not below `data/`, or follows `skipped.json` | `The archive holds an entry outside its data directory.` |
| A repeated name | `The archive holds the same name twice.` |
| A name used as a file and a directory | `The archive uses a name as both a file and a directory.` |
| An entry below a symbolic link | `The archive holds an entry below a symbolic link.` |
| A symbolic link target that leaves the volume | `The archive holds a symbolic link whose relative target leaves the volume.` |
| A hard link whose target is not an earlier regular file | `The archive holds a hard link to a file that does not come earlier in the archive.` |
| An owner id out of range | `The archive holds an entry with an owner id out of range.` |
| More than 2,000,000 entries | `The archive holds more entries than the limit allows.` |
| File data over 64 GiB, or more decompressed bytes than the ceiling ([Limits](#import-limits)) | `The archive extracts to more than the size limit.` |
| No entry below `data/` | `The archive holds no data.` |
| The manifest's driver is not `local` | `Only volumes of the local driver can be imported. A plugin driver can run code on the host when its volume is mounted.` |
| The upload exceeds 7 GiB | `The archive is larger than the upload limit.` |
| The data volume has less than 256 MiB free after the upload's size | `There is not enough free space on the data volume for this upload.` |

A refused upload is removed from disk at once. The refusal names the first entry that broke a rule.

### Modes and owners

| Property | Result |
|---|---|
| Owner uid and gid | Kept as archived, numerically. Owner names are not applied |
| Permission bits | Kept as archived, except the setuid and setgid bits, which are cleared. The sticky bit is kept |
| Modification time | Kept as archived, clamped to the range 0 to 17179869184 seconds |
| Extended attributes | Only `user.` attributes are applied. Others are dropped and counted on the review as entries that carried some |
| A directory with no entry of its own in the archive | Created by the Engine with owner `0:0` and mode `0755` |
| The volume's root | The `data/` entry's owner and mode are not applied. The root keeps Docker's defaults for a new volume |
| Existing content | None. The volume is new |

- The files are written as archived. A container that mounts the volume runs with whatever the files contain.
- An application that runs as a non-root user reads its data only if the archived uid and gid match the user it runs as on the target.

### Labels

| Label | Result |
|---|---|
| `com.docker.stack.namespace`, and every label beginning `com.docker.compose.` or `dockmin.`, matched without regard to case | Not applied. Listed as dropped |
| `com.docker.volume.anonymous` | Not applied. Listed as dropped |
| Every other label | Applied as archived. Not editable on the review |

- A label set that fails the create rules ([Create](#create)) refuses the import with `invalid`.
- The review and the result page list each dropped key. The row records their count.

### Driver and options

| Item | Result |
|---|---|
| Defaults | The archive's driver and options |
| Editable | `Driver` and the option rows, on the review. Rules as [Create](#create) for an administrator |
| Driver | `local`, or empty. Any other driver is refused: at upload when it is the archive's, and on confirmation when it is typed. Message: `Only volumes of the local driver can be imported. A plugin driver can run code on the host when its volume is mounted.` |
| Options | Up to 32 rows. Written to the volume as entered |
| Removed credentials | Options the export removed are not restored. The review lists their keys. Enter them in the option rows if the volume needs them |
| `device` option | Refused when the path is, holds or lies inside a source of a mount of dockmin's own container, or while dockmin's own mounts on the host are unknown. Reason `self_protected` |

- A volume with the options `type`, `device` and `o` binds a host path or mounts a remote filesystem. The files are written to that path when the volume is mounted by the helper.
- A `device` path that is not a source of dockmin's own container is not otherwise checked.

### Name collision

| Item | Result |
|---|---|
| Review | When the name exists, the review shows the alert above the `Name` field. `Import volume` is still offered |
| Confirmation | The name is checked on the host while the volume name is held. A name that exists answers `409`: the review again, with `A volume with this name already exists.` at `Name`, the typed values kept, and the upload still held |
| Existing volume | Never written to, replaced or adopted. The import does not call the create route that returns an existing volume |
| Appears between the check and the create | A volume of identical driver, options and labels that another client creates in that interval cannot be told apart from the new one. It is written to, and removed if the write fails |
| Stale access control | A control left on the name by a volume removed outside dockmin is replaced by the new owner's. The row records `stale_control_replaced` |

### Held uploads

| Item | Value |
|---|---|
| File | `{data directory}/volume-import/{32 hexadecimal digits}.tar.gz`, mode `0600` in a `0700` directory. Written under a `.partial` name and renamed when complete |
| Usable by | The user, the session and the environment that uploaded it. Any other use answers `The upload is gone` |
| Count | One for each user. A newer upload by the same user replaces the earlier one. Four in all; the oldest is dropped past four. An upload whose import is running is never dropped |
| Lifetime | 30 minutes from the upload. An upload whose import is running does not expire |
| Removed | On a refused archive, a failed upload, a newer upload by the same user, discard, expiry, shutdown, and at start, when the whole directory is deleted. When the volume has been created, whether the import then succeeds or fails |
| Kept | After a refusal that created nothing: name in use, invalid fields, a changed digest, no helper image, an unreachable host, a busy volume, a protected path |
| Digest | SHA-256 of the file, shown on the review and sent back with the confirmation. A different digest is refused |
| Backups | Not part of a backup archive |

### Import helper

| Item | Value |
|---|---|
| Image | An image already on the host, chosen as for [export](#the-helper-container). Nothing is pulled. A host with no image cannot import |
| Name, label, state | As [export](#the-helper-container): `dockmin-read-{12 hexadecimal digits}`, `dockmin.role=file-reader`, state `created`, never started |
| Body | `NetworkDisabled` true. `HostConfig`: `Mounts` `[{"Type": "volume", "Source": "{volume}", "Target": "/dockmin-read", "ReadOnly": false}]`, `NetworkMode` `none`, `ReadonlyRootfs` true, `CapDrop` `["ALL"]`, `AutoRemove` false |
| Write | One Engine archive call for `/dockmin-read`, with `noOverwriteDirNonDir` true, sent as a chunked tar body of `application/x-tar` |
| Source of the tar | The held file, re-validated and rewritten as it is sent: setuid and setgid bits cleared, `uname` and `gname` emptied, names relative to the volume, the `data/` entry and `manifest.json` and `skipped.json` left out |
| Visible | The helper is listed with the other containers while it exists |
| Removed | When the import ends by completion, failure, client disconnect or cancellation, with its anonymous volumes. The volume is not removed by this step. Leftover helpers are swept as for export |
| Recorded | `helper_container` (the full id) and `helper_removed` on the row |

### Import limits

| Limit | Value |
|---|---|
| Upload | 7 GiB as uploaded, with 64 KiB of multipart framing |
| Upload time | 3,600 seconds for the whole body; 15 seconds without a body message |
| Free space | At least 256 MiB free on the data volume before the first byte, plus the declared length when the request has one, and after every write |
| Entries | 2,000,000 |
| File data | 64 GiB in all |
| Decompressed bytes read | The file data limit, plus 8 KiB for each allowed entry, plus 2 MiB. Past it, the archive is refused and no entry is named |
| Path, component | 4,096 bytes, 255 bytes |
| `manifest.json`, `skipped.json` | 1 MiB, 16 MiB |
| Validation at once | 2 archives across the process, shared with build context validation |
| Held uploads | 1 for each user, 4 in all, 30 minutes |
| Write stall | The import fails when the host accepts no byte for 60 seconds. The host's reply is waited for 600 seconds |
| Write time | No total limit |
| Memory | The tar is rebuilt in a worker thread in steps of at most 256 KiB. No entry is held whole |

The reverse proxy limits for the upload are in [Deployment](Reference-Deployment#reverse-proxy-limits-for-volume-imports).

### Import refusals

A refusal at the confirmation renders a page at the real status code. A page for a refusal that created nothing keeps the upload held, and carries a `Review again` link for `409`, `502` and `503`.

| Condition | Status | Page | Message | Reason |
|---|---|---|---|---|
| Not an administrator, an environment id or upload id of the wrong form | 404 | | Not found | No row |
| The upload is not held for this session, user and environment | 409 | `The upload is gone` | `This upload is no longer held. Upload the archive again; nothing was changed.` Link `Upload the archive again` | `upload_gone` |
| The digest differs from the review's | 400 | The review | `The archive changed since you reviewed it; nothing was changed.` | `changed_since_review` |
| A field fails validation | 400 | The review | The field's message | `invalid` |
| The typed driver is not `local` | 400 | The review | `Only volumes of the local driver can be imported. A plugin driver can run code on the host when its volume is mounted.` at `Driver` | `invalid` |
| The `device` option names a path dockmin's container mounts, or dockmin's mounts are unknown | 409 | `Protected` | `The volume's device option names a path that the container dockmin runs in has mounted, or dockmin cannot establish which paths it mounts. Data cannot be imported into it from dockmin.` | `self_protected` |
| The name exists | 409 | The review | `A volume with this name already exists.` at `Name` | `name_in_use` |
| Another action holds the volume | 409 | `This volume is busy` | [Busy](#busy) | `object_busy` |
| The host has no image | 409 | `The volume cannot be imported` | `The host has no image to create the helper container from. Pull or load an image on the host, then try again.` | `helper_image_missing` |
| Docker refused the volume's configuration | 502 | `Docker refused this configuration` | `Docker refused this volume configuration.` For administrators, the Engine's scrubbed message under `Docker said`, as for [Create](#create) | `engine_rejected` |
| The host is unreachable, or did not answer in time | 503 | The host message | [Host failures](Usage-Containers#host-failures) | `unreachable`, `outcome_unknown` |
| The data could not be written | 502 | `The import failed` | [Failure behaviour](#failure-behaviour) | [Failure reasons](#recorded-actions) |

### Failure behaviour

| Stage | Result |
|---|---|
| Before the volume is created | Nothing is changed. The upload stays held, except after `upload_gone` |
| The data cannot be written after the volume was created (host unreachable, the Engine refused the archive, the archive no longer passes validation, the helper cannot be created) | The helper is removed, then the volume this import created. The page answers `502`: `The archive could not be written to the new volume. The volume this import created was removed.` The upload is removed |
| That volume cannot be removed | The same page: `The archive could not be written to the new volume. The volume this import created could not be removed; remove it from the volumes list.` The row records `volume_removed` `false` |
| The request is cancelled or the client leaves during the create or the write | The helper is removed, then the volume. A failure row with reason `interrupted` is written |
| The request is cancelled while the owner is being saved | The volume and its data stay. A success row with `interrupted` `true` and `control_created` `false` is written |
| The owner cannot be saved | The volume and its data stay. The result page shows `Owner: Not recorded.` and the row records `control_created` `false` |

- Only the volume the import created is removed, without force. A volume that existed before the import is never removed.
- The owner is saved last, after the data is written, so a failed import changes no access control.
- A volume removed this way leaves no access control behind.

### Format versions

| Item | Value |
|---|---|
| Read | `format_version` 1, and every earlier version a later release reads |
| Newer archive | Refused: `The archive's format version is {N}; this build reads up to 1.` |
| Plain tar, or an archive with no manifest first | Refused: `The archive was not written by a volume export: it does not start with manifest.json.` |

## dockmin's volumes

A volume mounted in the container dockmin runs in ([dockmin's own container](Usage-Containers#dockmins-own-container)) is listed and its page opens. It is never removed from dockmin.

| Where | Behaviour |
|---|---|
| List | Badge `In use by dockmin`. No `Remove` link. The checkbox is disabled |
| Remove, remove with containers | Refused: `409`, the page `Protected`: `The volume {name} is mounted in the container dockmin runs in. It cannot be removed from dockmin.` Recorded as a `volume_removed` failure with reason `self_protected` |
| Bulk removal | Listed under `Left out` on the confirmation; reported `Protected` if a run reaches it |
| Prune | Listed under `Kept` on the confirmation and not removed |
| Stack removal with volumes | Kept. The stack's remove-volumes page gives the reason `The container dockmin runs in has it mounted.` to an administrator |
| Mounting it in another container | Refused when a container is created or recreated ([dockmin's own container](Usage-Containers#dockmins-own-container)) |
| Export | No `Export` button. The confirmation and the download are refused: `409`, the page `Protected`. Recorded as a `volume_export_started` failure with reason `self_protected` ([Refusals](#refusals)) |

## Prune

Pruning removes unused volumes in bulk. Administrators only.

1. Open `Prune volumes` on the volume list.
2. Under `What to prune`, choose `Anonymous volumes` or `All unused volumes`, then `List the volumes`.
3. Read the list `N volumes would be removed`. For `All unused volumes`, the page states that named volumes hold data no container mounts now and that removing one deletes the data.
4. Choose `Remove N volumes`. The result page lists each volume and its outcome.

| Mode | Selects |
|---|---|
| `anonymous` | Unused volumes with the label `com.docker.volume.anonymous` and a 64-digit hexadecimal name |
| `all` | Every unused volume, named volumes included |

| Item | Value |
|---|---|
| Listing | `GET /environments/{id}/volumes/-/prune?mode={mode}` lists exactly the volumes the prune removes: name, driver, stack, created, and the anonymous badge. Without `mode` it lists the `anonymous` volumes |
| Unused | No container in any state mounts the volume |
| `all` | The confirmation states that named volumes hold data no container mounts now, and that removing one deletes the data |
| Order and limit | By name, at most 100. The list, its count and the button name only the objects in this prune; a notice gives the rest: `N more volumes are not in this prune. Run it again afterwards for the rest.` (`1 more volume is not in this prune.` for one) |
| Posted | The mode, and each listed volume with its creation time. A posted list of fewer than 1 or more than 100 volumes answers `400`: `Select between 1 and 100 volumes.` |
| Run | The candidates are read again for the posted mode. A volume that is no longer a candidate, or whose creation time changed, is skipped. Each volume is removed on its own, without force, and a failure does not stop the others |
| Docker prune | Docker's prune endpoint is never called |
| Invalid mode | `400`: `Choose anonymous or all.` |

An anonymous volume made by a daemon that does not label them is treated as named, and is selected only by `all`.

| Result per volume | Meaning |
|---|---|
| `Removed` | Gone |
| `Skipped: changed since you reviewed it` | No longer a candidate, or recreated |
| `Skipped: a container mounts it now` | |
| `Skipped: another operation is running on it` | |
| `Already gone` | |
| `Failed` | Docker refused or the host failed |

## Assign owners

Assign owners puts volumes that have no control under a control owned by the user their containers point to. Administrators only.

1. Open `Assign owners` on the volume list.
2. Read the review page. `N volumes to assign` lists each volume with its owner and the containers mounting it; the `left alone` sections list the rest.
3. Choose `Assign N volumes`. The result page shows the count assigned out of the count reviewed and one row per volume.

| Item | Value |
|---|---|
| Entry | `Assign owners` on the volume list, shown when the host has a volume with no control of its own and none inherited from a stack. Not shown when the host cannot be read |
| Review | `GET /environments/{id}/volumes/-/backfill` |
| Run | `POST /environments/{id}/volumes/-/backfill`, with `expected_plan` |
| Standard users | `404` on both routes |
| Reads | The volume list and the container list of the host, now. A host that cannot be read answers `503` with the host message ([Host failures](#host-failures)) |

### Candidate volumes

| Item | Value |
|---|---|
| Listed | Every volume with no control of its own whose stack, found by the label `com.docker.compose.project` or `com.docker.stack.namespace`, has no control. A volume that inherits a stack's control is not listed |
| Order and limit | By name, ignoring case, at most 500. A longer host shows `Showing the first N of M volumes without access settings, by name. Run the assignment again for the rest.` |
| Mounting containers | Containers in any state with a mount of type `volume` and the volume's name |

### Candidate owner

Each mounting container points to at most one user.

| Container | Points to |
|---|---|
| Has a control with an owner | The owner of the control |
| Has a control with no owner | No user. The control states that the container has no owner |
| Has no control | The user of the newest successful `container_created` row whose target is the container's id and whose actor is a user, unless a successful `resource_control_deleted` row for the container is newer. Failure rows and automation rows do not count |

A recreate row targets the replaced container, so it never names a creator. A recreated container is found through the control the recreate carried over. A control an administrator removed from a container is not undone by this fallback.

| Outcome | Condition | Action |
|---|---|---|
| `assign` | At least one container mounts the volume, every mounting container points to a user, and they are all the same user | Assigned to that user |
| `ambiguous` | Mounting containers point to two or more distinct users, whatever the others show | Left alone. The page names the candidates |
| `no candidate` | No container mounts the volume, or no mounting container points to a user, or some point to one user and others to none | Left alone |
| `unreadable` | The name cannot hold a control, or the volume's labels were cut short, which could hide a stack label | Left alone |

### Review page

| Section | Content |
|---|---|
| `N volumes to assign` | Each volume with its owner, the containers mounting it, and where the owner came from: `container ownership`, or `creation record` when any mounting container's owner came from the activity log. Names every target, and carries the confirmation to assign |
| `left alone: more than one candidate` | Each volume with its candidates and containers |
| `left alone: no candidate` | Each volume with its containers, or `no container mounts it` |
| `left alone: unreadable` | Each volume's name |

The form posts `expected_plan`: the SHA-256 digest of the lines `<volume> <user_id>` of the volumes to assign, sorted, each ending in a newline.

### Run

| Step | Behaviour |
|---|---|
| Recompute | The plan is read again. A digest that differs from `expected_plan` refuses the whole request before any write: `409`, `The volumes or their owners changed since you reviewed them; nothing was changed.` The page offers `Review again`. No row is written |
| Each volume | In name order, one at a time, under the volume's lock. The containers are listed once, when the plan is recomputed. Under the lock only the volume is inspected and its control looked up again: the volume must still exist, carry the creation time it had in the recomputed plan, and have no control |
| Write | A control owned by the user, access level `write`, granting nothing to anyone else and not public. The volume is then visible to its owner and to administrators |
| Result | `303` to `/results/{token}` ([Result pages](Contributing-Frontend-Conventions#result-pages)): the count assigned out of the count reviewed, and one row per volume |

| Result per volume | Message |
|---|---|
| `Assigned` | `Assigned to {owner}.` |
| `Skipped: has access settings now` | `Skipped: the volume has an owner or access settings now.` |
| `Skipped: gone` | `Skipped: the volume no longer exists.` |
| `Skipped: changed since you reviewed it` | `Skipped: the volume was replaced since you reviewed it.` |
| `Skipped: busy` | `Skipped: another action on this volume is in progress.` |
| `Skipped: host unreachable` | `Skipped: the host could not be read.` |
| `Failed` | `Not assigned: the owner could not be recorded.` |

A skipped volume writes no row and does not stop the others. Nothing is rolled back. Each assignment is recorded as `resource_control_created` with the detail keys in [Recorded actions](#recorded-actions).

## Ownership

| Event | Access control |
|---|---|
| Create | The volume is put under a resource control owned by its creator, administrators included, in the same operation. The control's access level is `write`, and the owner holds the control's level ([Ownership](Usage-Stacks#ownership)). No other user or team is granted anything |
| Saving fails | The volume is kept. The page shows `Volume created, but its ownership could not be recorded. An administrator can assign access to it.` The row records `control_created` as `false`. The default access policy for uncontrolled objects applies |
| Import | The new volume is put under a control owned by the importing administrator, after the data is written. The row records `control_created`. Saving fails: the volume is kept and the result page shows `Owner: Not recorded.` |
| Remove | The control is deleted. The row records `control_released` as `false` when it could not be deleted |
| Volume made by a stack | It carries the stack's label and is visible to whoever may see the stack. A control on the volume itself overrides the stack's. Removing the stack without its volumes copies the stack's control onto each such volume that has none ([Volumes of a stack](#volumes-of-a-stack)) |
| Volume with no control | The default access policy for uncontrolled objects applies. Administrators assign a control on the Access tab, or to many volumes at once with [Assign owners](#assign-owners) |

Administrators see every volume whatever its control.

### Volumes made by a container create

A create or duplicate of a container can make volumes: anonymous volumes for mounts with no source, and named volumes that did not exist. Each such volume gets its own control, owned by the user who created the container. The control is not a copy of the container's. A later grant on the container does not extend to the volume.

| Volume | Claimed when |
|---|---|
| Anonymous (mount with no source) | Always, unless the container takes volumes from another container |
| Named, absent when the form was checked | It now exists, uses the `local` driver, has no driver options and no labels, and has no control or still has the control it had when it was shown absent. A stale control is replaced |
| Any other volume that appeared in that interval | Never. It is left as it is and counted in `volumes_preexisting` on the container's row |

- Claims are saved in the same operation as the container's control. Each claimed volume writes a `volume_created` row of its own, linked to the container.
- A recreate claims nothing. Its volumes are the original container's.
- A standard user's create is undone when a volume it mounts changed while the container was being created: [Volumes and networks](Usage-Containers#volumes-and-networks).
- Volumes created before they were controlled follow the row *Volume with no control*.

### Volumes of a stack

A [stack](Usage-Stacks) names the volumes it creates in its Compose file. Compose reuses a volume whose name matches.

| Event | Behaviour |
|---|---|
| A deployment declares a volume that already exists | Refused unless the volume carries the label `com.docker.compose.project` with the stack's name (any case) and has no control of its own: `Volume {name} already exists and does not belong to this stack.` (`409`, reason `preexisting_objects`). A volume made on this page under a name a stack declares is refused until it is removed |
| A deployment declares a volume that does not exist | Compose creates it with the project label. It has no control of its own and is visible to whoever may see the stack |
| A control is left on a name whose volume no longer exists | The deployment that creates the volume releases it. The row records `stale_controls_released` |
| The create form is used for a name a deployment is checking | Busy until the deployment's `up` returns |
| Stack removed without volumes | The volumes stay with their data. Each volume that carries the project label and has no control of its own gets a copy of the stack's control: the same owner, visibility, access level and grants. The stack row records `controls_copied` |
| Stack removed with volumes | Deletes only volumes that the stack's file creates, that carry the project label and that have no control of their own. A standard user needs write access to each. Every volume is read again just before it is removed. A volume that a container outside the stack mounts is kept. External volumes are never deleted ([Remove with volumes](Usage-Stacks#remove-with-volumes)) |
| Anonymous volumes of a stack's containers | Anonymous volumes that an image declares are not listed or removed by a stack removal, with or without volumes. They remain and can be removed from this page or by [prune](#prune) |
| External volume in a stack file | Not created or removed by the stack. A standard user needs write access to it, or read access when every mount of it is read-only, and it must use the `local` driver with no driver options ([External volumes and networks](Usage-Stacks#external-volumes-and-networks)) |

### Volumes of a Swarm stack

A [Swarm stack](Usage-Stacks#swarm-stacks) names the volumes its services mount. dockmin never creates or removes them.

| Event | Behaviour |
|---|---|
| A service mounts a project volume | The first task that runs on a node creates `{stack}_{key}` there, labelled `com.docker.stack.namespace` through the mount's options. It has no control of its own and is visible to whoever may see the stack, on the manager dockmin is connected to. A volume that already exists on the node keeps its own labels |
| A deployment names a volume that exists on the connected manager | Refused unless it carries the stack's label (any case) and has no control of its own: `Volume {name} already exists and does not belong to this stack.` (`409`, reason `preexisting_objects`). Other nodes are not checked |
| Stack removed or redeployed | No volume is removed, on any node. The remove confirmation lists the stack's volumes visible on the manager and states that volumes on other nodes stay |
| Stack removed | Each volume of the namespace visible on the manager that has no control of its own gets a copy of the stack's control, when the stack has one. The stack row records `controls_copied` |
| Anonymous volumes of a stack's tasks | Created by the nodes, without the stack's label. Not listed or removed by a stack operation. Remove them from this page where visible, or by [prune](#prune) |
| Standard user | A named volume, project or external, is mounted only by a service pinned to the connected manager. An external volume needs write access, or read access for a read-only mount, and the `local` driver with no driver options ([Swarm stack restrictions for standard users](Usage-Stacks#swarm-stack-restrictions-for-standard-users)) |

## Busy

One action runs on a volume at a time. A second is refused at once; it does not wait.

| Action | Held |
|---|---|
| Create | The volume name |
| Remove | The volume |
| Remove with containers | Each container in turn, then the volume |
| Export | The volume, from the start of the download until it ends |
| Import | The new volume's name, from the absence check until the owner is saved |
| Stack deploy, redeploy | The name of each volume the stack's file creates |
| Stack remove with volumes | Each volume the remove deletes |

Message: `{Who} started a {action} on it {N} seconds ago and it has not finished. Wait for it to complete, then try again.` (`409`). `{Who}` is `You`, `Another user` or, for administrators, the holder's name. The holders follow the table in [Busy](Usage-Containers#busy).

## Recorded actions

Every action records one activity row, for success and for failure. A refusal on access leaves no row: a volume the user may not see, a user who lacks the access level required, a name that is not a valid volume name, and a standard user's request whose access could not be decided because the host could not be read. A refused force run leaves no row. So does an owner assignment refused because its plan changed.

| Action | Target |
|---|---|
| `volume_created` | The volume's name. None when the form was refused |
| `volume_removed` | The volume's name |
| `volume_export_started` | The volume's name. Written once the export is cleared and the helper container exists, before any data is read, or when the export is refused after the request reached the service |
| `volume_exported` | The volume's name. Written when the download ends, for success and for failure |
| `volume_imported` | The volume's name, cut to 255 characters. Written once for each import request that reaches the import, for success and for failure. The upload, the review and the discard write none |
| `resource_control_created` | The volume's name. Written by an owner assignment, one row for each volume assigned, and by [Adopt everything on this host](Usage-Environments#adopt-everything-on-this-host), which records `onboarding` `true` and `candidate_source` and not `backfill` |

| Detail key | Present when |
|---|---|
| `driver` | `volume_created`, `volume_removed`, `volume_export_started`, `volume_exported` |
| `option_keys` | `volume_created`: the option keys, comma-separated. Values are never recorded |
| `labels` | `volume_created`: the number of labels. Keys and values are never recorded |
| `restricted` | `volume_created` by an administrator: `driver` for a driver other than `local`, `options` when options were set, comma-separated |
| `control_created` | `volume_created`: whether the access control was saved |
| `stale_control_replaced` | `volume_created`: a control left on the name was replaced |
| `preexisting` | `volume_created`: a volume of the name appeared before the create and differs from the request. No control was placed |
| `via_container`, `anonymous` | `volume_created` written by a container create: the container's id, and whether the volume is anonymous |
| `in_use_containers` | `volume_removed`: the number of containers using the volume when it was removed |
| `force` | `volume_removed`: `true` when it ended a removal with containers |
| `containers_removed` | `volume_removed`: the number of containers removed first. `0` otherwise |
| `pruned` | `volume_removed`, `true` when it ran from a prune. A prune writes one row per volume and none for the run |
| `batch` | `volume_removed`, `true` when it ran from a bulk removal. A bulk removal writes one row per volume and none for the run |
| `control_released` | `volume_removed`: whether the access control was deleted |
| `format_version`, `containers`, `running_containers`, `helper_container` | `volume_export_started`. Keys: [Activity](Administration-Activity#volume-export-rows) |
| `name`, `bytes`, `source_bytes`, `skipped`, `completed`, `helper_removed` | `volume_exported`. Keys: [Activity](Administration-Activity#volume-export-rows) |
| `name`, `driver`, `format_version`, `members`, `bytes`, `option_keys`, `labels`, `labels_dropped`, `helper_container`, `helper_removed`, `bytes_written`, `control_created`, `stale_control_replaced`, `volume_removed` | `volume_imported`. Keys: [Activity](Administration-Activity#volume-import-rows) |
| `backfill` | `resource_control_created` from an owner assignment: `true` |
| `candidate_source` | `resource_control_created` from an owner assignment: `container_control`, or `activity` when any mounting container's owner came from the activity log |
| `containers` | `resource_control_created` from an owner assignment: the short ids of the mounting containers, comma-separated |
| `owner_user_id`, `access_level` | `resource_control_created` from an owner assignment: the owner, and `write` |
| `fields` | Reasons `invalid` and `restricted`: the names of the refused fields, comma-separated |
| `option_keys` | Reason `restricted`: the refused option keys |
| `holder_action` | Failure row for a busy object |
| `interrupted` | The request ended before the Engine answered. The row is written when the Engine call settles, or as `outcome_unknown` if it does not settle |
| `reason`, `message` | Failure rows |

A mountpoint, a driver option value and a label value are never recorded.

A container create that makes volumes records `volumes_claimed` (the number claimed) and, when any were left alone, `volumes_preexisting` on its `container_created` row ([Create and duplicate](Usage-Containers#create-and-duplicate)).

A removal with containers writes a `container_removed` row for each container it removes and a `volume_removed` row for the volume. Its rows carry `force` and `containers_removed`.

| Failure reason | Cause |
|---|---|
| `invalid` | A field failed validation |
| `restricted` | A standard user chose a driver or set an option |
| `name_in_use` | A volume with the name exists |
| `volume_in_use` | Removal of a volume that containers mount |
| `changed_since_review` | The creation time or the use differs from the confirmation. For an import: the held archive's digest differs from the review's |
| `self_protected` | The container dockmin runs in has the volume mounted. For an export: also while dockmin's own mounts on the host are unknown, and for a volume bound to a path dockmin's container mounts. For an import: a `device` option naming such a path |
| `object_busy` | Another action holds the volume |
| `export_busy` | `volume_export_started`: the export limit is reached |
| `plugin_driver`, `helper_image_missing`, `helper_refused`, `unreadable` | `volume_export_started`: [Refusals](#refusals). `helper_image_missing` and `helper_refused` also on `volume_imported` |
| `upload_gone` | `volume_imported`: the upload is not held for this session, user and environment |
| `archive_invalid`, `archive_rejected` | `volume_imported`: the held archive no longer passed validation while it was sent; the Engine refused the archive or the path |
| `interrupted`, `malformed_stream` | `volume_exported`: the client left before the archive was complete; the host's stream was not the tar of one directory. `interrupted` also on `volume_imported`: the request ended during the create or the write |
| `volume_gone` | The volume no longer exists |
| `engine_rejected` | Docker refused the request |
| `volume_changed` | `container_created`: a volume changed while a standard user's container was being created |
| `unreachable`, `unsupported`, `credentials`, `engine`, `engine_error`, `outcome_unknown`, `too_large` | Host failures, as in [Host failures](Usage-Containers#host-failures) |
