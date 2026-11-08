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
| Owner assignment review | `/-/backfill` | `GET` |
| Owner assignment | `/-/backfill` | `POST` |
| Save or delete the access control | `/{volume_name}/access`, `/{volume_name}/access/delete` | `POST` |

- `{volume_name}` is 1 to 255 characters: letters, digits, `_`, `.`, `-`; the first is a letter or digit. Any other form answers `404` for every role, before any call to the host.
- The fixed paths sit under `/-/`. A volume name cannot begin with `-`, so a volume called `new` or `prune` is still reached at its own path.
- `{tab}` is `overview` (default), `containers` or `access`. Any other value answers `404`. `access` is shown to administrators; other roles receive `403`.
- `{mode}` is `anonymous` (default) or `all`.
- `/-/remove` takes 1 to 25 distinct `volume` values. Outside that, an administrator receives `400`. A standard user's request with no `volume` value answers `404`.
- Every `POST` redirects (`303`) on success. Create redirects to the new volume, remove to the list. A forced removal and a prune redirect to a result page, `/results/{token}` ([Result pages](Contributing-Frontend-Conventions#result-pages)), which lists each target and outcome; reloading it repeats nothing.
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
| Select column | A checkbox on each volume the user may remove, for [bulk removal](#bulk-removal). Disabled on a volume marked `In use by dockmin` |
| Actions column | `Details` for everyone; `Remove` for users with write access to the volume, except on a volume marked `In use by dockmin` |
| Order | Name, ignoring case |
| Page size | 25 by default; `limit` 1 to 500. Outside the range answers `422` |
| Paging | `cursor`; an invalid cursor answers `400` |
| Count | `N volumes`, or `N volumes match these filters, of M you can see` |
| Used by | Names of up to 10 containers the viewer may see, linked. Containers beyond the limit, and containers the viewer may not see, are counted and not named |
| Buttons | `Create volume` for users who may create; `Assign owners` for administrators when a volume is uncontrolled ([Assign owners](#assign-owners)); `Prune volumes` for administrators; `Remove selected...` for users who may remove |

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
| Stack deploy, redeploy | The name of each volume the stack's file creates |
| Stack remove with volumes | Each volume the remove deletes |

Message: `{Who} started a {action} on it {N} seconds ago and it has not finished. Wait for it to complete, then try again.` (`409`). `{Who}` is `You`, `Another user` or, for administrators, the holder's name. The holders follow the table in [Busy](Usage-Containers#busy).

## Recorded actions

Every action records one activity row, for success and for failure. A refusal on access leaves no row: a volume the user may not see, a user who lacks the access level required, a name that is not a valid volume name, and a standard user's request whose access could not be decided because the host could not be read. A refused force run leaves no row. So does an owner assignment refused because its plan changed.

| Action | Target |
|---|---|
| `volume_created` | The volume's name. None when the form was refused |
| `volume_removed` | The volume's name |
| `resource_control_created` | The volume's name. Written by an owner assignment, one row for each volume assigned, and by [Adopt everything on this host](Usage-Environments#adopt-everything-on-this-host), which records `onboarding` `true` and `candidate_source` and not `backfill` |

| Detail key | Present when |
|---|---|
| `driver` | `volume_created`, `volume_removed` |
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
| `changed_since_review` | The creation time or the use differs from the confirmation |
| `self_protected` | The container dockmin runs in has the volume mounted |
| `object_busy` | Another action holds the volume |
| `volume_gone` | The volume no longer exists |
| `engine_rejected` | Docker refused the request |
| `volume_changed` | `container_created`: a volume changed while a standard user's container was being created |
| `unreachable`, `unsupported`, `credentials`, `engine`, `outcome_unknown`, `too_large` | Host failures, as in [Host failures](Usage-Containers#host-failures) |
