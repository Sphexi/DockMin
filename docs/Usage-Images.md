# Images

Images are listed and managed per environment. The list and the detail page are read from the host when the page loads. The update status shown beside each reference is the stored result of its last update check ([Stored status](#stored-status)).

![Image list filtered by reference](images/Usage-Images-1.webp)

## Pages and routes

All paths are under `/environments/{id}/images`.

| Page | Path | Method |
|---|---|---|
| Image list | The base path | `GET` |
| Image total | `/disk-usage` | `GET`, a fragment the list page requests ([Total size](#total-size)) |
| Image | `/{image_id}?tab={tab}` | `GET` |
| Pull form | `/pull?reference={reference}` | `GET` |
| Credential picker for a reference | `/pull/choices` | `POST`, htmx only; other requests redirect (`303`) to the pull form |
| Start a pull | `/pull` | `POST` |
| Pull or push progress | `/jobs/{job_id}` | `GET` |
| Progress stream (server-sent events) | `/jobs/{job_id}/events` | `GET` |
| Cancel a pull or push | `/jobs/{job_id}/cancel` | `POST` |
| Push form | `/{image_id}/push?reference={reference}` | `GET` |
| Start a push | `/{image_id}/push` | `POST` |
| Tag review | `/{image_id}/tag?reference={reference}` | `GET` |
| Tag | `/{image_id}/tag` | `POST` |
| Untag confirmation | `/{image_id}/untag?reference={reference}` | `GET` |
| Untag | `/{image_id}/untag` | `POST` |
| Remove confirmation | `/{image_id}/remove` | `GET` |
| Forced remove confirmation | `/{image_id}/remove?force=1` | `GET` |
| Remove | `/{image_id}/remove` | `POST` |
| Bulk removal confirmation | `/remove?image={image_id}`, repeated | `GET` |
| Bulk removal | `/remove` | `POST` |
| Prune selection and confirmation | `/prune?mode={mode}` | `GET` |
| Prune | `/prune` | `POST` |
| Orphaned bindings | `/bindings/reap` | `GET` (list), `POST` (reap) |
| Change credential set | `/bindings/rebind?reference={reference}` | `GET` (page), `POST` (verify and change, or remove) |
| Update check | `/updates` | `POST` |
| Start a bind unbound images review ([Bind unbound images](#bind-unbound-images)) | `/anonymous-binding` | `POST`, `303` to the job page |
| Review progress | `/anonymous-binding/jobs/{job_id}` | `GET`; an htmx request for the panel answers `HX-Redirect` to the review once it is ready |
| Cancel a review | `/anonymous-binding/jobs/{job_id}/cancel` | `POST`, `303` to the job page |
| Review | `/anonymous-binding/reviews/{token}` | `GET` |
| Apply | `/anonymous-binding/reviews/{token}/apply` | `POST`, `303` to the review, which shows the summary of the apply |

- `{image_id}` is the full 64-character lower-case hexadecimal id. Any other form answers `404` for every role, before any call to the host.
- `{job_id}` is 22 characters from `A-Za-z0-9_-`. Any other form answers `404`. `{token}` of a review follows the same rule.
- `{tab}` is `overview` (default), `configuration` or `history`. Any other value answers `404`.
- `{mode}` is `dangling` or `unused`. Any other value answers `400`: `Choose dangling or unused.`
- `/remove` takes 1 to 25 distinct `image` values. Outside that, the confirmation answers `400`.
- Every `POST` redirects (`303`) on success. A refusal renders a page at the real status code, with a `Review again` link for `409`, `502` and `503`.

## Access

Viewing needs sight of the environment ([Environments](Usage-Environments#access)). A standard user without it receives `404` on every route.

| Action | Administrators | Standard users |
|---|---|---|
| List, image page, history, update check | Yes | Yes |
| Pull, push | Yes | Remote environments they hold a grant on; `403` on a local socket; `404` without a grant |
| Watch, refresh or cancel a pull or push | Any job | Their own jobs only; another user's job answers `404` |
| Change the credential binding of an image | Yes | No. `403`: `Only an administrator can change which credential set this image uses.` |
| Tag, untag, remove, bulk remove, force, prune, reap bindings | Yes | Refused (`403`) |
| Change a binding without a pull ([Rebind without a pull](#rebind-without-a-pull)) | Yes | `404`, as for an unknown path. The `Repair` and `Change credential set` links are not shown |
| Bind unbound images without a pull ([Bindings without a pull](#bindings-without-a-pull)) | Yes | `404` |
| Bind unbound images ([Bind unbound images](#bind-unbound-images)) | Yes | `404` on every route, and no row is written |

| Withheld from standard users | Shown to administrators |
|---|---|
| Entrypoint, command, environment variables (`Not shown for your role`) | Yes. Values of secret-named variables are redacted for everyone |
| Build command of each layer in the History tab | Yes |
| Values of secret-named label keys (`Redacted`) | Yes |
| Names of containers they may not see | Yes. The `Used by` count includes every container on the host |
| Name of the credential set an image is bound to, unless they hold a grant on the set | Yes. Otherwise `a credential set` |

Secret-named keys follow the rule in [Containers](Usage-Containers#withheld-from-standard-users).

## Image references

A reference is parsed before any call to the host. Its canonical form keys the image's credential binding.

| Part | Rule |
|---|---|
| Whole | Trimmed. At most 512 characters. ASCII, no whitespace, no control characters |
| Form | `[host[:port]/]path[:tag][@sha256:digest]` |
| Host | Present when the first segment contains `.` or `:`, or is `localhost`, and a `/` follows. It follows the [address rules](Administration-Registries#address-rules), so a plain-HTTP, numeric-shorthand or malformed host is refused |
| Name | Host and path together: 1 to 255 characters. Each path component is lower-case letters and digits separated by `.`, `_`, `__` or hyphens |
| Tag | 1 to 128 characters: letters, digits, `_`, `.`, `-`; the first is not `.` or `-` |
| Digest | `sha256:` followed by 64 lower-case hexadecimal digits |
| Default tag | `latest`, added when there is neither a tag nor a digest |

### Docker Hub

| Input | Canonical form |
|---|---|
| `busybox` | `docker.io/library/busybox:latest` |
| `nginx:1.27` | `docker.io/library/nginx:1.27` |
| `myorg/app` | `docker.io/myorg/app:latest` |
| `index.docker.io/myorg/app:2`, `registry-1.docker.io/myorg/app:2` | `docker.io/myorg/app:2` |

- A name without a host is a Docker Hub name. A single path component gets the `library/` namespace.
- Hub names are shown without `docker.io/`, and without `library/` when that is the only namespace.

### Other registries and digests

| Input | Canonical form |
|---|---|
| `registry.example.com:5000/team/app` | `registry.example.com:5000/team/app:latest` |
| `Registry.Example.com/team/app:2` | `registry.example.com/team/app:2` |
| `ghcr.io/org/app@sha256:{64 hex digits}` | `ghcr.io/org/app@sha256:{64 hex digits}` |
| `busybox:1@sha256:{64 hex digits}` | `docker.io/library/busybox@sha256:{64 hex digits}` |

- A reference with a digest is pinned. Its canonical form leaves the tag out, because the host pulls by digest and ignores it.
- A pinned reference has its own binding. It is never the binding of the same repository's tag.

### Refused references

Messages quote no input.

| Cause | Message |
|---|---|
| Blank | `Image is required.` |
| Too long, not ASCII, contains whitespace or control characters, or otherwise malformed | `Image is not a valid image reference.` |
| Digest is not `sha256:` and 64 hexadecimal digits | `Image digest must be sha256: followed by 64 hexadecimal digits.` |
| Invalid tag | `Image tag may use letters, digits, underscores, periods and hyphens.` |
| Name empty or over 255 characters | `Image name must be between 1 and 255 characters.` |
| Invalid host | `Image registry host is not valid.` |
| Invalid path component | `Image name must use lower-case letters, digits, periods and hyphens.` |
| Push of a digest | `Push a tag, not a digest.` |
| Push of a tag the image does not carry | `That reference is not on this image.` |
| Tag target with a digest | `A tag is needed; a digest cannot be assigned.` |

## Image list

| Item | Value |
|---|---|
| Columns | Select (administrators), Image, Size, Created, Used by, Actions |
| Select column | A checkbox per image, for [bulk removal](#bulk-removal). Administrators only. Shift-click ticks a range ([Range selection](Usage-Containers#range-selection)) |
| Actions column | Shown to users who may pull or remove. `Details` for everyone; `Pull again` when the image has a tag and the user may pull; `Remove` for administrators |
| Order | The lowest reference in the form shown, ignoring case, then id. Images with no tag sort last. The Image, Size and Created headers sort the list ([Sorting](#sorting)) |
| Page size | 25 by default; `limit` 1 to 500 |
| Paging | `cursor`; an invalid cursor answers `400` |
| Count | `{N} images`; with a filter, `{N} images match these filters` (`1 image matches these filters`) |
| Total | `{size} for all images on this host.` after the count ([Total size](#total-size)) |
| Reference shown | The tags; the digests when the image has no tag. At most 100 per image, then `and N more` |
| Used by | Names of up to 10 containers the viewer may see, linked; `and N others` for the rest |
| Buttons | `Pull image` for users who may pull; `Prune images`, `Remove selected...` and, while the stored status is `reachable`, `Bind unbound images` ([Bind unbound images](#bind-unbound-images)) for administrators |
| Empty list | `This host has no images.` (administrators); `You cannot see any images on this host. Ask an administrator for access to more images.` (standard users). With a filter: `No matching images` and `Change the filters, or clear them to see every image.` |

Every image on the host is listed. The list is not filtered by the viewer's access.

### Filters

| Filter | Values | Match |
|---|---|---|
| `q` (`Reference contains`) | At most 128 characters | Case-insensitive substring of a reference, or a prefix of the image id |
| `dangling` | `yes`, `no` | Dangling: the image has no tag |
| `usage` | `used`, `unused` | A container, running or not, uses the image |
| `binding` | `bound`, `credential_missing`, `unbound` | At least one reference shown for the image has the state. `anonymous` is accepted and filters as `unbound` |
| `update` (`Updates`) | `available` | At least one reference shown for the image has the stored status `Update available`, as the list shows it ([Stored status](#stored-status)): a result that is out of date does not count |

An invalid filter value answers `400` and lists the problems; for `update` the problem reads `Choose available.`; for `dangling`, `Choose yes or no.` Filters survive paging.

The `Updates available` card of the [environment page](Usage-Environments#environment-page) links to `?update=available`.

### Sorting

The rules of [Sorting](Usage-Containers#sorting) apply: `sort` and `dir`, the header links, the cursor, and the `400` answers (`Sort by one of the listed columns.`, `Choose ascending or descending.`, `The page link is not valid.`).

| Column | `sort` | Orders by |
|---|---|---|
| Image | `image` | The lowest tag in the form shown, ignoring case |
| Size | `size` | Size in bytes |
| Created | `created` | Creation time |

| Rule | Value |
|---|---|
| Default order | The lowest tag ascending, then id. Images with no tag come last |
| With `sort=image` | An image with no tag has an empty value, the lowest: first ascending, last descending |
| Ties | Broken by the lowest tag (ignoring case), then id, in the sort direction |
| Filters | The sort applies to the images that match |

### Total size

The line above the table gives the bytes the host's images occupy.

| Item | Value |
|---|---|
| Text | `25 images. 2.5 GiB for all images on this host.` With a filter: `3 images match these filters. 2.5 GiB for all images on this host.` |
| Figure | `LayersSize` of the Engine's disk usage for images (`/system/df` with `type=image`): every layer on the host, each shared layer counted once. It is not the sum of the sizes in the Size column, which counts a shared layer once for each image |
| Covers | Every image on the host. A filter does not change it, and a standard user sees the same figure |
| Read | Once per full page load, by a request the page makes after it loads: `GET /environments/{id}/images/disk-usage`. Paging, sorting and filtering leave the figure as it is and make no new read |
| Answer | `200`, the figure as a fragment, `Cache-Control: no-store`. An environment the user cannot see answers `404` |
| Reply limit | 16 MiB, decoded off the event loop |
| Absent | The count shows without a total when the host does not answer, answers too much, or reports a figure that is missing or negative. The fragment is empty and no message is shown. The failure is logged as `image_disk_usage_unavailable` |
| Without JavaScript | The total is absent |

### Binding states

| State | Shown as | Meaning |
|---|---|---|
| `bound` | `Bound to {set name}` | The reference was pulled or pushed with that credential set, or bound to it without a pull |
| `credential_missing` | `Bound set deleted` or `Registry address changed`, with `was {set name}; updates use anonymous access` | The set was deleted, or its registry address changed. The binding stays as a record and behaves as `unbound` |
| `unbound` | `Not bound` | No binding exists for the reference. Update checks and automatic updates use anonymous access to the registry in the reference. An image that runs on the host can be bound without a pull ([Bindings without a pull](#bindings-without-a-pull)) |

- Each reference of an image has its own state.
- Administrators see a link beside a reference whose binding can be changed, on the list and on the image page:

  | Binding | Link | Opens |
  |---|---|---|
  | `credential_missing` | `Repair` | The [rebind page](#rebind-without-a-pull); for a reference pinned by digest, the pull form |
  | `bound`, reference not pinned by digest | `Change credential set` | The rebind page |
  | `bound`, reference pinned by digest | None | |
  | `unbound` | None | |

  Each link carries the reference.
- On the first page, administrators see a notice when bindings refer to images that are no longer on the host ([Orphaned bindings](#orphaned-bindings)).

### Stored status

Each reference in the image list shows the result of its last update check and when it was made. The image page does not show it.

| State shown | When |
|---|---|
| `Up to date`, `Update available`, `Unknown`, `Pinned` | The stored result, with `checked {time}` |
| `Not checked` | No result is stored, the stored state is `not_bound` (no check produces it), or the result is out of date |

A stored result is out of date when the image holds other digests for the reference's repository now:

| Stored result against the image's digests now | Shown |
|---|---|
| The stored local digest is among them, or none was stored | The stored state |
| The stored local digest is not among them, and the stored registry digest is | `Up to date` |
| Neither is among them | `Not checked` |

| Item | Value |
|---|---|
| Written | After every [update check](#update-check), whoever started it: `Check for updates`, and each `container-updater` run, dry runs included, for containers and Compose stacks |
| Describes | The image the host holds locally for the reference, compared with the registry. A Swarm service's image is not a host image, and two services of one reference can run different digests, so `container-updater` checks Swarm services against the digest in each service and stores nothing for them |
| Row | One per environment and reference, the latest only: state, reason, local digest, registry digest, check time |
| Credentials | None stored |
| Retention | A row checked more than 30 days ago is deleted when a later result is stored for the same environment. Deleting the environment deletes its rows |
| Older result | A result with an earlier check time than the stored one is dropped |
| Not stored | `busy`, `invalid_reference` and `image_id_reference` results. `credential_not_available` and `credential_mismatch` from a check a user starts. `credential_not_available` from an automation whose owner holds no grant on the bound set |
| Write failure | Logged as `image_update_status_write_failed`. The check still answers |
| Visible to | Everyone who can open the list |

### Image page

| Tab | Content |
|---|---|
| Overview | Id, size, created, platform, `Used by`, the references with their binding state and actions (`Pull again`, `Untag`), and the update check |
| Configuration | Entrypoint, command, working directory, user, exposed ports, environment variables, labels |
| History | The layers, newest first: layer id, created, created by, size, tags |

- The summary offers `Pull again`, `Tag` (administrators), `Push` and `Remove` (administrators).
- `Pull again`, `Push` and `Untag` apply to tags. Digest references show the chip `digest`.
- At most 500 labels, variables and exposed ports are read; a notice reads `Only the first entries are shown.`
- An image whose configuration exceeds 4 MiB, or whose history exceeds 1 MiB, is not shown: `This image's configuration is too large to show.`

### Host failures

The list and the image page are replaced by an error. The messages are those of [Containers](Usage-Containers#host-failures), and the answer is `503`. A host whose image list exceeds 16 MiB answers `This host reports more image data than can be listed here (limit 16 MiB).` and no partial list is shown. An image id the host does not know answers `404`. Troubleshooting: [Troubleshooting](Troubleshooting).

## Credential bindings

A binding records which credential set pulled a reference in one environment. Re-pulling uses the same set.

| Item | Value |
|---|---|
| Key | The environment and the canonical reference |
| Value | One credential set |
| Written | After a pull or push ends cleanly, when a container create or recreate pulls the image, when a Swarm service create or edit changes the service's image ([Swarm services](#swarm-services)), when a Swarm stack deploy or redeploy sends an image to a service ([Swarm stacks](#swarm-stacks)), when an administrator applies `Adopt everything on this host` to a reference with no binding ([Bindings without a pull](#bindings-without-a-pull)), and when an administrator applies [Bind unbound images](#bind-unbound-images) to a registry. A failed or cancelled transfer writes none |
| Survives | Removal, untagging and pruning of the image; a restart. Deleted with the environment |
| Created by | Whoever pulls or pushes a reference that has no binding, with a set chosen or the only set that fits. An administrator, without a pull, through [Bindings without a pull](#bindings-without-a-pull) or [Bind unbound images](#bind-unbound-images) |
| Changed by | Administrators only, by an explicit credential choice on a pull or push, or on the [rebind page](#rebind-without-a-pull). A `credential_missing` binding is repaired by any pull that names or is offered a set |
| Not written | A pull or push with `Anonymous`. An unbound reference pulled anonymously stays unbound |
| Removed by | An administrator's `Anonymous` choice on a pull, a push or the rebind page; a reap ([Orphaned bindings](#orphaned-bindings)) |
| Tag | Tagging pulls nothing and binds nothing |

A pull, push, create, recreate, Swarm service change or Swarm stack deployment reports what it did to the binding:

| Result | Meaning |
|---|---|
| `created` | The reference had no binding; one was written |
| `rebound` | An administrator's explicit choice on a pull or push changed the set, or a pull that named or was offered a set repaired a `credential_missing` binding |
| `removed` | An administrator chose `Anonymous` against a binding that names a set; the binding was deleted |
| `kept` | The existing binding already names the set used, or it differs and was not changed because the caller did not choose against the binding as it stands |
| `skipped` | The set no longer exists, a `credential_missing` binding was not repaired, or an anonymous pull had no binding to remove |

### Which set a pull uses

The first rule that applies decides.

| Order | Condition | Result |
|---|---|---|
| 1 | The caller made an explicit choice | That set, or no credentials for `Anonymous`. A standard user whose choice differs from a binding that names a set is refused (`403`). An administrator's `Anonymous` removes that binding |
| 2 | The reference is bound to a set | That set. A standard user is offered only that option |
| 3 | No binding, or the binding is `credential_missing`; no set fits the registry | An anonymous pull |
| 3 | No binding, or the binding is `credential_missing`; one set fits | That set, which is then bound |
| 3 | No binding, or the binding is `credential_missing`; several sets fit | Refused (`400`): `Choose which credential set to pull with.` |
| 3 | No binding, or the binding is `credential_missing`; automation (no user) | An anonymous pull |

- A set fits when its address equals the reference's registry address, it is granted to the environment, and, for a standard user, it is granted to the user or one of their teams ([Registries](Administration-Registries#access)). An entry that [reads as anonymous](Administration-Registries#anonymous-entries) does not count at order 3: it neither turns an anonymous pull into a pull bound to the entry nor makes a pull ask for a choice. At most 200 sets are offered. With more, the choice is refused: `Too many credential sets fit this registry to choose between.`
- The [credential picker](Administration-Registries#the-credential-picker) of a form sends an explicit choice, so the rule at order 1 decides for every pull a form starts. Order 3 applies to callers that send no choice, such as the JSON routes.
- The picker leaves out an entry that reads as anonymous unless the reference is bound to it. It preselects the bound set; for a reference with no binding or a `credential_missing` binding, the [default set](Administration-Registries#default-for-an-address) for the address, else `Anonymous`. The push form and the recreate confirmation preselect the bound set; with no binding or a `credential_missing` binding, the default set, else the only fitting set, else `Anonymous` when none fits, and nothing when several fit.
- A bound set is used as bound, whatever the address match. A bound image is never matched to another set by its registry host.
- A bound set that is no longer granted to the environment or the user, or whose secret cannot be read, ends the job as `credential_unavailable`, with the messages in [Registries](Administration-Registries#access).
- A chosen set for another registry than the reference's answers `400`: `That credential set is for a different registry.`
- Automation (no user) uses the bound set and needs only its environment grant. An unbound reference, and a `credential_missing` binding, are pulled anonymously. Automation never chooses a set by registry host.
- The default-for-an-address flag only preselects a form.

| Picker state | Text shown |
|---|---|
| Administrator, binding is `bound` | `Choosing a different set changes the binding for every user of this image. Choosing Anonymous removes the binding.` The `Anonymous` option reads `Remove the binding. The image is then pulled and updated without signing in.` |
| Administrator, binding is `credential_missing` | `The credential set this image was bound to was deleted, so updates use anonymous access. Choose a set to bind the image to it.` |
| Reference not bound | `The set that pulls the image is bound to it, so updates use the same set. An anonymous pull binds nothing.` |
| Standard user, binding is `bound` | The bound set's name as text, with `Only an administrator can change which credential set this image uses.` |
| Standard user, bound set not granted to them | `This image is bound to a credential set you cannot use.`, followed by `Only an administrator can change which credential set this image uses.` No button |
| No credential set fits the registry | `No credential set is available for this registry.` The pull is anonymous |
| Binding `credential_missing`, no other set at the registry | `The credential set this image was bound to was deleted, and no other set is available for this registry. The image is pulled anonymously.` |

### Swarm services

A Swarm service create, and an edit that changes the image, choose credentials for the reference and pin it before the service is sent to Docker ([Swarm](Usage-Swarm#images-and-registry-credentials)). The nodes pull the image.

| Item | Value |
|---|---|
| Set used | The rules of [Which set a pull uses](#which-set-a-pull-uses). The create form and the review of an edit offer the choice when several sets fit; an explicit choice is carried to the request |
| Standard user | A choice that differs from the existing binding is refused (`400`, reason `restricted`) beside the image field: `Only an administrator can change which credential set this image uses.` |
| Digest | dockmin asks the registry, with the same credentials, for the digest of the reference. The service is stored with the reference and `@sha256:` and the digest, so every node runs one image. A reference that already names a digest is not looked up |
| Registry did not answer | The service is stored with the image as typed. The page warns that nodes may run different images of the tag, and the row records `digest_pinned` as `false` |
| Binding written | In the transaction that settles the service's access control, for the reference as typed, with the digest observed. The row records `binding` (`created`, `rebound`, `kept`, `skipped`, or `not_recorded`) |
| Not written | An image id; scale; force update; roll back; an edit that keeps the image |
| Credential sent to Docker | A create sends a non-anonymous set. An edit that changes the image always sends a credential: the chosen set, or the empty credential for an anonymous pull, so credentials stored with the earlier image are not reused. An edit that keeps the image, scale and force update send none and Swarm reuses the stored credential. A roll back uses the credential stored with the previous configuration |
| Stored by Swarm | Docker keeps the credential with the service and gives it to the nodes that pull. It is not returned by Docker and is not shown, stored or logged by dockmin |
| Lifetime | dockmin refreshes a credential stored with a service only through `container-updater`, for a service bound to an Amazon ECR set ([Swarm](Usage-Swarm#credential-renewal-by-container-updater)). Deleting a set does not remove it from services |
| Updates by `container-updater` | The reference's bound set, resolved with no user and no choice; anonymous for a reference with no binding or a `credential_missing` binding. The nodes pull. A binding is kept and its observed digest is updated ([Automation](Operations-Automation#swarm-services)) |

### Swarm stacks

A Swarm stack chooses credentials for each distinct image reference of its file before anything is sent to Docker ([Stacks](Usage-Stacks#swarm-images-and-credentials)).

| Item | Value |
|---|---|
| Set used | The rules of [Which set a pull uses](#which-set-a-pull-uses), per reference. When several sets fit and none is chosen the form lists each reference with its sets (`409`, reason `credential_choice_required`) |
| Standard user | A choice that differs from the existing binding is refused (`400`, reason `restricted`) |
| Digest | Looked up once per reference and operation with the same credentials. Every service of the reference runs the pinned image. A failed lookup deploys the reference as written and lists it as not pinned |
| Binding written | In the transaction that records the deployment, for each reference sent to a service by a create, by an update that changes the image string, or by an update that carries a different set than the binding. When an operation stops, for the references already sent. The row records `binding` |
| Not written | An image id; a service that already holds the specification |
| Credential sent to Docker | A create sends a non-anonymous set. An update that changes the image string always sends one: the chosen set, or the empty credential for an anonymous pull. Any other update sends none and Swarm reuses the stored credential |
| Lifetime | dockmin refreshes a credential stored with a service only through `container-updater`, for a service bound to an Amazon ECR set ([Swarm](Usage-Swarm#credential-renewal-by-container-updater)) |

### When a set is deleted or its address changes

| Event | Effect on its bindings |
|---|---|
| Set deleted | Every binding becomes `credential_missing`, `Bound set deleted`. None is removed |
| Registry address of the set edited | Every binding becomes `credential_missing`, `Registry address changed`. The set's default flag is cleared |

- The name of the lost set is kept with the binding.
- A `credential_missing` binding behaves as `unbound`. The update check and `container-updater` use anonymous access to the registry in the reference. The update check shows `Checked anonymously; the bound set was deleted.` beside the result, and the run detail of `container-updater` counts `anonymous_fallback` ([Automation](Operations-Automation#updater-run-detail)). A registry that refuses the anonymous request gives `authentication_required`; a registry on a private network with no entry granted at its address gives `private_address` ([Update check](#update-check)).
- Repair: any pull that names a set, or is offered the only fitting set, binds the reference to it (`rebound`). An administrator uses `Repair` to choose a set without a pull ([Rebind without a pull](#rebind-without-a-pull)). A reference pinned by digest is repaired by pulling it.
- An administrator's `Anonymous` choice on the rebind page deletes the record.

| Bound set | State | Update check and `container-updater` |
|---|---|---|
| Deleted, or its address changed | `credential_missing` | Anonymous access to the registry in the reference |
| Exists at the same address, no longer granted to the environment | `bound` | Refused: `credential_not_available` in a check, `credential_unavailable` in an automation. No fallback to anonymous access |
| Exists at the same address, secret cannot be read | `bound` | The same refusal |

Withdrawing an environment grant ([Remove a grant](Administration-Registries#remove-a-grant)) changes no binding.

### Rebind without a pull

`/environments/{id}/images/bindings/rebind?reference={reference}`. Administrators only. The page changes which credential set a binding names, or removes the binding. It pulls nothing.

1. On the image list or image page, select `Repair` or `Change credential set` beside the reference.
2. Choose a credential set, or `Anonymous`, in the picker.
3. Select `Verify and change binding`. For a set, dockmin sends one manifest `HEAD` to the registry with it. `Anonymous` sends nothing.
4. The list opens with `The binding now uses the chosen credential set.` or, for `Anonymous`, `The binding was removed.` If the page shows a refusal instead, match it in the table below; the binding is unchanged.

| Item | Value |
|---|---|
| Page shows | The reference, its registry address, the current binding, and the credential picker ([Registries](Administration-Registries#the-credential-picker)) with the sets that fit the registry and `Anonymous` |
| Preselected | The bound set, and nothing when it is not among the sets offered. For a `credential_missing` binding, as for a reference with no binding ([Which set a pull uses](#which-set-a-pull-uses)) |
| Buttons | `Verify and change binding`, `Pull the image instead` (the pull form with the reference), `Cancel` |
| Reference | Must be bound, as `bound` or `credential_missing`, and not pinned by digest. Otherwise `400` |
| Verification | A `HEAD` of the reference's manifest, as in [Registry request](#registry-request), made with the chosen set. The binding changes only if the registry answers with a digest |
| Anonymous choice | Deletes the binding, `bound` or `credential_missing`. No registry request is made. The reference is then `unbound` |
| Unchanged choice | Choosing the set the binding already names (state `bound`) changes nothing, makes no registry request and writes no activity row |
| Review | The form carries the binding it was built on. If the binding is no longer that one when the request is applied, nothing changes |
| Digest | The stored observed digest is not changed |
| Success | Redirect (`303`) to the list with `The binding now uses the chosen credential set.`, or `The binding was removed.` for `Anonymous` |

| Refusal | Status | Message |
|---|---|---|
| Reference pinned by digest | `400` | `A reference pinned by digest is rebound by pulling it.` |
| Reference not bound | `400` | `Pull the image to bind it.` |
| No set chosen | `400` | `Choose one of the listed credential sets.` |
| The registry refuses the choice, or cannot be reached | `400` | The product-written message of the failed check, beside the credential picker |
| Set not available to the environment, or for another registry | `409` | The messages in [Registries](Administration-Registries#access), on the page |
| Binding changed since the page was shown | `409` | `The binding changed since you reviewed it. Review it again.` With a `Review again` link |

- A failed check leaves the binding as it was.
- The rows written are listed under [Recorded actions](#recorded-actions).

### Bindings without a pull

An image pulled outside dockmin has no binding. `Adopt everything on this host` ([Environments](Usage-Environments#adopt-everything-on-this-host)) binds it to the credential set whose registry reports the digest the host holds. Nothing is pulled.

| Item | Value |
|---|---|
| Who | Administrators. A standard user cannot start the review |
| References checked | The configured image of every container and, on a Swarm manager, the image of every service, with any digest suffix removed. Each reference in canonical form, less those that already have a binding |
| Containers created by image id | As in [Orphaned bindings](#orphaned-bindings): at most 100 are read, 8 at a time. When one cannot be read, or there are more, the review shows `Some containers or services could not be read, so an image in use may be missing.` |
| Local digests | The `RepoDigests` of the local image whose tags carry the reference. A Swarm service's digest suffix is not used |
| Reads | Nothing is written during the check. The binding is written when the confirmed plan is applied |

#### Candidates

| Rule | Value |
|---|---|
| Sets tried | Each credential set whose registry address equals the reference's registry address and that is granted to the environment. At most 10 are tried |
| Anonymous | Tried only when no set matched, and when a set at the address is of a provider that allows anonymous access. Never for Amazon ECR |
| Entry that reads as anonymous | Not tried as a set. It enables the anonymous check, so an entry for a public registry and no account gives `anonymous_only` ([Anonymous entries](Administration-Registries#anonymous-entries)). An entry with authentication off and a CA bundle or verification off is tried as itself |
| Request | One manifest `HEAD` for each candidate, as in [Registry request](#registry-request): the same client, the OCI and Docker manifest and index media types, the set's TLS settings. A `Bearer` challenge adds the token request of that registry. No manifest is read with `GET` |
| Contacted | Only addresses that have a credential set granted to the environment, entries with authentication off included. A reference at an address with no set is not contacted |
| Amazon ECR | The set's token is obtained before its request, outside any database transaction |
| Match | The digest the registry returns is one of the local `RepoDigests` |
| Failed candidate | Not a match. It is not retried, and the review shows it beside the reference with the cause: `refused the credentials`, `has no such repository or tag`, `did not answer in time`, `could not be resolved`, `could not be reached`, `failed the TLS handshake`, `certificate not accepted`, `is limiting requests`, `reply not understood`, `credential set unavailable` |
| Digest differs | The candidate is shown as `a different image` |

A match shows that the set can read the image. It does not show which set pulled it, so one match binds and several are never resolved by order.

#### Outcomes of the digest check

| Outcome | Shown as | Meaning | Binding |
|---|---|---|---|
| `binds` | `Binds to {set}.` | Exactly one set matched, and at most 10 sets are at the address | Offered, ticked |
| `ambiguous` | `More than one set can read this image. Choose one.` | Several sets matched, or one matched and more than 10 sets are at the address, so the match could hide another | Offered, not ticked, and a select of the matching candidates with nothing preselected. Ticking without a choice is refused |
| `anonymous_only` | `The registry answered without credentials; the image needs no binding.` | No set matched and the anonymous check did | None. The box is disabled |
| `no_match` | `No credential set could be matched to the image on the host.` | No candidate matched, or no set is granted to the environment at the address | None |
| `no_local_digest` | `The host holds no registry digest for it; it was built or tagged locally.` | The local image has no `RepoDigests`. No request is sent | None |
| `pinned` | `It names a digest, so it needs no binding.` | The reference names a digest. No request is sent | None |
| `not_checked` | `Not checked: the time or reference limit was reached.` | The time limit or the reference limit was reached before every candidate was tried, or more than 200 sets are at the address | None |

A reference is `not_checked` as a whole when any of its candidates was not checked.

| Bound | Value |
|---|---|
| References checked | 200 for each review. Counted are the references that need a request: `pinned` and `no_local_digest` do not count. The rest, in reference order, are `not_checked` and no candidate is tried |
| Sets tried for a reference | 10, then anonymous only when none matched. More than 10 sets give `ambiguous` when one matched; more than 200 give `not_checked` |
| Requests in flight | 5 |
| One request | 10 s, or the time left of the 120 s |
| Whole check | 120 s. A request not started by then is `not_checked` |

#### Binding

| Item | Value |
|---|---|
| Written | For each ticked image whose choice is a set, when the confirmed plan is applied. The binding names the set chosen and keeps the matching digest as its observed digest. An image whose choice is anonymous writes nothing and is reported `It is read without a binding.` |
| Checks at the write | The environment may use the set, the set is at the reference's registry address, the reference is not pinned and the digest is a `sha256:` digest. The host and the registry are not contacted |
| Binding exists | A reference that has a binding, in any state, is skipped. No row is written and the binding is not changed |
| Refusals | `missing`, `credential_mismatch` and `anonymous_choice` (reasons of [Recorded actions](#recorded-actions)) are recorded as failures and the result page shows the product message |
| Not done | No pull, no Engine call and no change to a container |
| Recorded | `image_bound` with `verified` `true` and `pulled` `false` ([Recorded actions](#recorded-actions)) |

### Bind unbound images

`Bind unbound images` on the image list binds images that have no binding to the credential set that can read them. One manifest `HEAD` for each granted set shows which sets return the digest the host holds. Nothing is pulled and nothing on the host changes.

| Item | Value |
|---|---|
| Who | Administrators. A standard user receives `404` on every route and no row is written |
| Start | `Bind unbound images` on the image list, shown while the stored status is `reachable`. Not reachable: `409`, `The environment is not reachable`: `Test the connection, then start the review again.` |
| Flow | Review job, review page with one form, apply back to the same review page |
| Contacted before the start | Nothing |
| Job | Kind `bulk_binding_review`. It shares the limits of pull, push and container jobs: 2 running per user and 8 in the process. Over a limit: `429`, `Too many jobs are running`. A cancelled review ends with `The review was cancelled. Nothing was written.` and an unexpected failure with `The review failed.` Visible to the administrator who started it |
| Job page | `Refresh status` and `Cancel the review` while running, `Open the review` once it succeeded. While the review runs, the panel is fetched again every 2 seconds and the poll that finds the review ready opens it (`HX-Redirect`). Without JavaScript the page reloads every 5 seconds |
| Held review | In memory, for 30 minutes after the review finished, for the user and the environment that started it. 2 for each user and 16 in all; the oldest is dropped first. Gone: `409`, `The review has expired` |
| Applied twice | While an apply runs, a second apply is refused: `409`, `Already being applied` |

#### Which references are checked

| Item | Value |
|---|---|
| References | Every tag of every image on the host that has no binding row, in any state. A binding that is `credential_missing` counts as bound |
| Checked | A reference that is tagged, unbound, has a local registry digest, and names an address where at least one credential set is granted to the environment |
| Credential set | A set granted to the environment that does not read as anonymous: authentication on, or certificate verification off, or a CA bundle. An entry that [reads as anonymous](Administration-Registries#anonymous-entries) is not a set here. Up to 200 granted sets are read |
| Not read | Swarm services |

| Left out | Shown as |
|---|---|
| `pinned` | `Pinned by digest.` |
| `no_local_digest` | `The image has no recorded registry digest.` |
| `no_credential_set` | `No credential set at this address; updates use anonymous access.` |

- Left-out references are listed under `Not checked ({N})` with `Reference` and `Why`. The first 200 are listed, with the total.
- No request is sent for a left-out reference.

#### Checks and limits

| Item | Value |
|---|---|
| Order | Each granted set at the address first, in parallel, one manifest `HEAD` each, as in [Registry request](#registry-request): the set's TLS settings, the `Bearer` token request of that registry, and for Amazon ECR the set's token before its request. Anonymous only when no set matched |
| Sets tried | 10 for each reference. With more than 10 sets at the address, a reference that a tried set read is offered with a picker. More than 200 sets give `not_checked` |
| Anonymous | One manifest `HEAD` with no login, sent when no set matched and a granted set at the address is of a provider that allows anonymous access. Never for Amazon ECR |
| Match | The digest the registry returns is one of the local `RepoDigests` |
| Not done | No manifest `GET`, no pull, no Engine write |

| Bound | Value |
|---|---|
| References checked | 200 for each review. Pinned references and references with no local digest do not count. The rest are not checked and are counted: `{N} not checked; start another review to check the next ones.` |
| Requests in flight | 5 |
| One request | 10 s, or the time left of the 120 s |
| Whole review | 120 s. A request not started by then is `not_checked` |
| Order | References used by a container first, then the rest, each in reference order |
| Resume point | The review starts after the last reference checked by the same user's previous review of the environment, and continues from the start of the order when it reaches the end. Held in memory; lost at a restart. Cleared by a review that leaves nothing unchecked |

#### Outcomes for each image

| Outcome | Shown as | Offered |
|---|---|---|
| One set matched | The reference with the set's name and digest | Yes, bound to that set |
| Several sets matched | The reference with a select of the matching sets, `Leave unbound` selected, and each set's digest | Yes, bound to the set chosen. Nothing is preselected: an image with no set chosen stays unbound |
| No set matched, the anonymous request matched | `Works without binding.` under `Not offered` | No. The image needs no binding ([Bind unbound images anonymously](#bind-unbound-images-anonymously)) |
| Nothing matched | `Needs a credential set.` under `Not offered`, with one line `{set name}: {reason}` for each set that failed or returned another digest | No |
| Not reached | `Not checked: the limit on references or the time budget was reached.` under `Not offered` | No |

| Reason of a set | Text |
|---|---|
| Another digest | `The registry holds a different digest for this tag.` |
| A failed request | The registry client's text for the failure: [Results](#results) lists the stages and kinds |
| A set that could not be used | `The check failed.` |

#### Review page

| Part | Content |
|---|---|
| Latest apply | After an apply: the [summary](#apply) at the top |
| Not checked | `{N} not checked; start another review to check the next ones.` while any reference was not checked |
| Form | One form for every registry with an image to bind. Text: `Each image offered below is unbound, and a credential set granted to this environment returned the digest the host holds. Confirming a registry binds its images to the sets shown, so their updates use those sets. Nothing is pulled and nothing on the host changes. Each registry is confirmed on its own.` |
| `Apply all bindings` | Shown when more than one registry has an image to bind. It applies every registry in the form |
| Registry | One section for each address: the credential sets granted there, the table `Reference`, `Credential set`, `Digest` of the images to bind, and its own button |
| Registry button | `Bind these {N} images at {address}`, or `Bind the chosen images at {address}` when the registry has an image with a picker. It applies that registry only |
| Picker | A select for each image that several sets read: `Leave unbound`, then each matching set. Help: `An image that more than one set can read stays unbound unless you choose a set for it.` |
| Not offered | `Not offered ({N})`: `Reference` and `Outcome` of the checked references that are not offered, with the reason lines. At most 200 for each registry, with the total |
| Registry with nothing to bind | `No image at this address can be bound.` |
| Nothing to bind at all | `No unbound image can be bound to a credential set.` and `An image with no credential set at its address updates with anonymous access.`, with `Back to the images`. After an apply that left nothing: `No image is left to bind.` |
| Left out | `Not checked ({N})`, as in [Which references are checked](#which-references-are-checked) |

#### Apply

| Item | Value |
|---|---|
| Posted | `registry` and `expected_plan`, one pair for each registry with an image to bind; `choice`, repeated, as `{reference} {set id}` or `{reference} none`; `only`, the one registry a registry button applies. Without `only`, every posted registry is applied. At most 50 registries and 200 choices |
| Confirmed | One or more registries. Refused before anything is written, `400`, `Check the selection`: `Confirm at least one registry.`, `That registry is not in the review.` for an address the review does not hold, or `The confirmed plan is not the one the review offered. Reload the review.` for a plan that is not the one offered |
| Choice | Limited to the sets that matched for that image in the review. A choice for another set, for an image with no picker, or for a reference the review does not hold is refused, `400`, `Check the selection`: `That credential set is not one the review offered for the image.` A malformed or repeated choice is refused: `A credential set choice is malformed.`, `An image was chosen twice.` |
| Plan | The SHA-256 of the sorted lines of the registry, one for each image to bind: `image <reference> set <id> <digest>`, or for a picker image `image <reference> sets <id>:<digest>,<id>:<digest>` with every set offered. The posted choice is not in the plan |
| Plan check | For each confirmed registry on its own, before it is written: the local digests, the binding rows and the credential sets granted at the address are read again and compared with the review. No request goes to a registry. A difference makes that registry `changed`: nothing is bound there and it stays on the review. The other registries apply |
| Write | Each image is bound as its own operation to the set it is offered with, or the set chosen. The checks are those of [Binding](#binding) |
| Left unbound | A picker image with no set chosen is `Left unbound`: `No credential set was chosen; it stays in the review.` |
| Bound meanwhile | A reference that has a binding when it is written is `Skipped`: `It was bound in the meantime.` The rest of its registry applies |
| One fails | `Failed`, with the product message, or `The operation failed.` for an error that is not a product error. The others proceed |
| Host not readable | The unavailable page. The review is kept |
| Result | `303` to the review page, with `Latest apply` above the list |
| Review afterwards | Images that were bound or skipped leave the review. Failed and left-unbound images and changed registries stay and can be applied again. A registry with nothing left to show is dropped. The review keeps its first expiry |

| `Latest apply` | Content |
|---|---|
| Counts | `{N} bound, {N} skipped, {N} failed, {N} changed at {registries}.`, with `, {N} left unbound` when any |
| Changed registry | `{address}: Changed since the review; nothing was bound at this address. Start a new review to see the current images.` |
| Table | `Reference`, `Result` (`Bound`, `Skipped`, `Failed` or `Left unbound`) and `Detail` |
| Bound detail | `Bound to {set name}; nothing was pulled.` |

#### Recorded

| Item | Value |
|---|---|
| Row | One `image_bound` row for each binding, with the set as `credential_set_id`, `anonymous` `false`, `digest`, `verified` `true`, `pulled` `false` and `bulk` `true` ([Recorded actions](#recorded-actions)) |
| Failure | A binding the write refuses records a failure row with its reason |
| No row | Starting the review, a refused apply, a changed registry, a skipped reference and a left-unbound image |
| Request id | The rows of one apply share a request id ([Request id](Administration-Activity#request-id)) |

#### Bind unbound images anonymously

The tool writes no anonymous binding. An image that no granted set can read but its registry serves without credentials is listed as `Works without binding.` and is checked and updated anonymously like any unbound image ([Update check](#update-check)). An image at an address with no granted credential set is not checked and reads `No credential set at this address; updates use anonymous access.`

A bound image takes part in update checks at once: [Update check](#update-check).

### Orphaned bindings

A binding is orphaned when its reference is on no image and in no container's configured image.

1. On the first page of the image list, select `Review and remove` in the notice about bindings that refer to images no longer on the host. The page `Image bindings` lists them.
2. Review the list. With no orphans the page reads `No bindings to remove`.
3. Select `Remove {N} bindings`. The list opens with `Removed N bindings.`

| Rule | Value |
|---|---|
| Who | Administrators |
| Where | The notice on the first page of the list links to `/environments/{id}/images/bindings/reap` |
| Listed | At most 200 bindings, in id order; `Showing the first 200. Run this again for the rest.` when cut |
| Containers created by image id | Their configured reference is read from the container. At most 100 such containers are read, 8 at a time. When one cannot be read, or there are more, no binding is offered |
| Reap | Deletes each listed binding that is still orphaned and still names the reviewed reference. A binding that changed, or whose image returned, is left |
| Result | Redirect to the list with `Removed N bindings.` |
| Effect | The next pull of the reference asks for a credential set again |
| Refusal when containers could not be checked | `400`: `Some containers could not be checked, so no binding was removed.` |

## Pull

A pull runs in the background as a [job](#pull-and-push-jobs). The pull form is `GET /environments/{id}/images/pull`.

1. Select `Pull image` on the image list, or `Pull again` on a tag.
2. Enter the reference in `Image reference` and select `Continue`. With JavaScript, changing the field loads the next step without leaving the page, and `Continue` stays visible.
3. Read the canonical reference, its registry, its binding, and the [credential picker](Administration-Registries#the-credential-picker): `Anonymous (no credentials)` and the sets that fit. Choose one. `Anonymous` binds nothing.
4. Select `Pull image`. The job page opens (`303`) and shows the layers.
5. When the job ends, `View image` opens the image. If it failed, match the message in [Outcomes](#outcomes) or see [Troubleshooting](Troubleshooting).

| Result | Status | Message |
|---|---|---|
| Invalid reference or choice | 400 | The messages in [Refused references](#refused-references) |
| Several sets fit and none was chosen | 400 | `Choose which credential set to pull with.` |
| Standard user would change a binding | 403 | `Only an administrator can change which credential set this image uses.` |
| Chosen set not offered for this environment and user | 409 | `That registry credential set is not available here.` |
| Chosen set is for another registry | 400 | `That credential set is for a different registry.` |
| Job limit reached | 429 | The limit message ([Limits](#limits)) |
| Host unavailable | 503 | The host message |

- A standard user whose reference is bound to a set they hold no grant on sees `This image is bound to a credential set you cannot use. Only an administrator can change which credential set this image uses.` and no button.
- A reference already on the host is pulled again.
- A reference with neither a tag nor a digest is pulled as `latest`. A pull never fetches every tag of a repository.
- A failed start renders the form with the heading `The image was not pulled`.

## Push

Push sends one tag of an image to the registry its reference names.

1. On the image page, select `Push`.
2. Choose the tag in `Reference to push` and select `Continue`. The only tag is preselected.
3. Choose the credential set. The same [precedence](#which-set-a-pull-uses) applies.
4. Select `Push image`. The job page opens.

| Item | Value |
|---|---|
| Access | Users who may pull ([Access](#access)) |
| Form | The image's tags; the only tag is preselected. Then the same credential choice as a pull |
| Reference | A tag the image carries. A digest is refused |
| Credential set | A set for the registry in the tag. The same [precedence](#which-set-a-pull-uses) applies |
| Binding | Written when the push ends cleanly |
| No tag | `This image has no tag to push.` Administrators are offered `Tag image`; others are told an administrator has to tag it |
| Failed start | The form is headed `The image was not pushed` |

The tag is checked again when the job runs. A tag moved to another image refuses the push: `This image changed since you reviewed it; nothing was changed.`

## Pull and push jobs

| Item | Value |
|---|---|
| Visible to | The user who started the job; administrators |
| Retention | Finished jobs for 15 minutes, at most 64. Jobs are held in memory and are lost on restart; a job page then answers `404` |
| Overall time limit | 3600 seconds. A job that reaches it ends as `outcome_unknown` |
| Cancel | The starter or an administrator. The transfer to the host is stopped |
| Lock | The reference is held for the whole job. Tag, untag, remove, pull, push and create on the same reference answer busy ([Busy](#busy)) |

### Limits

| Limit | Value | Message |
|---|---|---|
| Running jobs per user | 2 | `You have 2 transfers running. Wait for one to finish.` |
| Running jobs per instance | 8; standard users fill at most 7, the last slot is kept for administrators | `This server is running as many transfers as it allows. Try again shortly.` |
| Progress streams | One live view each; the pools are those of [live stats](Usage-Containers#stats) | `Too many live views are open` (`429`) |

- Container creates and recreates that pull count toward both running-job limits ([Containers](Usage-Containers#job-limits)).

### Progress

| Item | Value |
|---|---|
| State | `Running`, `Succeeded`, `Failed`, `Cancelled` |
| Summary | `N of M layers done`, bytes transferred of bytes known, and the share |
| Layers | At most 128 tracked, then `and N more layers`. Each shows its id (12 characters), stage and bytes |
| Stages | Queued, Waiting, Downloading, Verifying, Downloaded, Extracting, Complete, Already on the host, Preparing, Pushing, Pushed, Mounted from another repository, Working |
| Not shown | The host's own status text |
| Result | A message, and for a success a `View image` link |

- With JavaScript, the page opens the event stream while the job runs and swaps in each state. `Progress updates live while this page is open.` The stream sends a comment line every 15 seconds without change and ends after 300 seconds with a retry hint of 3 seconds. Each connection is authorised again.
- Without JavaScript, or when the stream is refused or lost, the page shows the state at load and a `Refresh status` link. `Live updates stopped. Use Refresh to see the current state.`
- `Cancel pull` and `Cancel push` post a form and work without JavaScript.

### Outcomes

| Reason | Message |
|---|---|
| Success | `Pulled {reference}.` or `Pushed {reference}.`, with the binding result: `A binding was created for this reference.`, `The binding for this reference now names the credential set used.` or `The existing binding was kept.` |
| `not_found_or_denied` | `The registry has no such image, or refused access to it.` (pull); `The registry refused the push, or the repository does not exist.` (push) |
| `pull_failed`, `push_failed` | `The image could not be pulled.`; `The image could not be pushed.` |
| `outcome_unknown` | The host message, or after a cancel `The transfer was cancelled. What it left on the host is not known.` The page adds: `Check the image list before trying again. Layers or the tag may have changed on the host.` |
| `unreachable`, `unsupported`, `credentials`, `engine` | Host failures, as in [Containers](Usage-Containers#host-failures) |
| `object_busy` | The busy message |
| `credential_missing`, `credential_unavailable`, `credential_choice_required`, `credential_mismatch` | The messages in [Pull](#pull) |
| `forbidden` | `You no longer have permission for this action.`, or the rebind refusal |
| `not_found` | `The image or environment was not found.` |
| `changed` | `This image changed since you reviewed it; nothing was changed.` |
| `image_gone` | `The image no longer exists.` |
| `invalid` | The message of the refused field |
| `image_in_use` | `The image is used by one or more containers.` |
| `image_has_dependents` | `Other images are built on this one. Remove them first.` |
| `too_large` | `This object's configuration is too large to read.` |
| `failed` | The product's message for an error not listed above, or `The transfer failed.` for an unexpected one. The reason is never a class name |

The registry's own text is never shown.

### Busy

`{Who} started a {action} on this image {N} seconds ago and it has not finished. Wait for it to complete, then try again.` (`409`). `{Who}` is `You`, `Another user` or, for administrators, the holder's name.

## Tag

Tagging adds a reference to an image. Administrators only.

1. On the image page, select `Tag`.
2. Enter the reference in `New reference` and select `Review`.
3. Read the review. It states when the reference already belongs to another image.
4. Select `Add reference`, or `Move the reference to this image` when it moves. The image page opens with `Reference added.`

| Item | Value |
|---|---|
| Form | New reference. Without a tag, `latest` |
| Review | Names the image and the reference. States when the reference already belongs to another image, with that image's references. The other image is not removed |
| Already on this image | Nothing changes and nothing is recorded |
| Binding | None is written |
| Reviewed holder | The review carries the id of the image that holds the reference now, or `none`. A different holder when the tag runs refuses it: `This image changed since you reviewed it; nothing was changed.` (`409`) |

## Untag

Untag removes one tag from an image. Administrators only. The page lists `Untag` for tags.

| Situation | Effect |
|---|---|
| The image keeps another reference | The reference is removed. The image stays |
| The reference is the last one, no container uses the image | The reference and the image are removed. The button reads `Remove reference and image` |
| The reference is the last one, containers use the image | Refused: `The image is used by one or more containers.` (`409`). A separate force confirmation follows |
| Forced | The reference is removed and the image stays on the host without a reference. Containers keep using it. The button reads `Force remove reference` |

- The last reference is counted as Docker counts it: removing the last tag of a repository also drops that repository's digest references.
- The confirmation binds whether the reference is the last and the number of containers using the image. A change in either refuses the untag: `This image changed since you reviewed it; nothing was changed.` (`409`).
- The confirmation for a reference that is not on the image answers `404`.
- Success redirects to the image with `Reference removed.`, or to the list with `Image removed.` when the image went.

## Remove

Removal deletes an image and every reference to it. Administrators only.

1. On the image page or list, select `Remove`.
2. Read the confirmation: the environment, the image, every reference, the size and the containers using it.
3. Select `Remove image`. For an image a container uses, select `Continue to force removal` and then `Force remove image`.
4. The list opens with `Image removed.`

| Item | Value |
|---|---|
| Confirmation | The environment, the image, every reference (at most 100, then `and N more`), the size and the containers using it |
| Not in use | Each reference is removed by name, only while the name still belongs to this image, then the image by id and the layers no other image shares. Containers are not affected |
| In use | The first page only leads to a second. `Continue to force removal` opens `?force=1` |
| Force | The image is removed by id in one call, whatever its references. Docker refuses a forced removal while a running container uses it |
| Bindings | Kept |
| Reviewed state | The confirmation carries a digest of the references and the number of containers using the image. A difference refuses the removal |

| Result | Status | Message |
|---|---|---|
| Removed | 303 | Redirect to the list with `Image removed.` |
| In use, not forced | 409 | `The image is used by one or more containers.` |
| Other images are built on it | 409 | `Other images are built on this one. Remove them first.` |
| References or container count changed | 409 | `This image changed since you reviewed it; nothing was changed.` |
| Changed while the removal ran | 409 | `This image changed while the action ran, and part of it may already be done. Review the image and try again.` |
| Another action holds the image | 409 | [Busy](#busy) |
| Image gone | 404 | `The image no longer exists.` |
| Docker refused | 502 | `Docker refused the action.` |
| Host unavailable | 503 | The host message |

Docker's own error text is never shown.

## Bulk removal

Bulk removal deletes several images in one reviewed step. Administrators only.

1. Tick the `Select` checkbox of each image on the list (1 to 25).
2. Select `Remove selected...`. The confirmation lists each image and whether it is removed or kept.
3. Select `Remove {N} images`. The result page lists one row per image.

| Item | Value |
|---|---|
| Selection | The `Select` column of the list, then `Remove selected...`. 1 to 25 images: `Select at least one image.` and `Select at most 25 images.` (`400`) |
| Confirmation | `GET /environments/{id}/images/remove?image={image_id}`. Lists each image: its first reference, short id, size, the other references (at most 100, then `and N more references`) and whether it is removed or kept. Nothing changes |
| Left out | A selected id that is not an image of the host is counted and reported as left out |
| In use | An image a container uses is named and kept: the confirmation lists the containers the viewer may see, counts the rest, and links `Force remove...` to the forced confirmation of that image |
| Force | Not available in bulk |
| Posted | For each removable image: its id, the digest of its references and the number of containers using it (`target`), and the name shown (`display_name`). In-use images are not posted |
| Run | Each image is removed on its own, in the order submitted, as [Remove](#remove) does without force. A refusal or failure on one image is that image's result and does not stop the others. Nothing is rolled back |
| Reviewed state | The references and the use count are read again for each image. A difference skips it |
| Bindings | Kept |
| Result | `303` to `/results/{token}` ([Frontend conventions](Contributing-Frontend-Conventions#result-pages)): `{N} of {M} images removed`, `Up to {size} freed` (layers shared with other images are not counted twice by Docker, so the real figure can be lower), and one row per image |

| Result per image | Message |
|---|---|
| `Removed` | `Removed.` |
| `Kept: in use` | `In use by a container. Remove it on its own page to force.` Links to the forced confirmation |
| `Skipped: changed since you reviewed it` | `This image changed since you reviewed it; nothing was changed.` |
| `Skipped: busy` | `Another action on this image is in progress.` |
| `Already gone` | `Not found, or already removed.` |
| `Unreachable` | The host message |
| `Failed` | `Other images are built on this one. Remove them first.`, `Docker refused the action.` or `The image could not be removed.` |

Each removal writes an `image_removed` row with `batch` set ([Recorded actions](#recorded-actions)).

## Prune

Pruning removes unused images in bulk. Administrators only.

1. Select `Prune images` on the image list.
2. Choose `Dangling images` or `Unused images` under `What to prune` and select `List the images`.
3. Read the heading `{N} images would be removed` and the list of exactly those images.
4. Select `Remove {N} images`. The result page shows each image's outcome and `Up to {size} freed`.

![Prune confirmation for unused images](images/Usage-Images-2.webp)

| Mode | Selects |
|---|---|
| `dangling` | Images with no tag that no container uses |
| `unused` | Every image no container uses, tagged or not |

| Item | Value |
|---|---|
| Listing | `GET /environments/{id}/images/prune?mode={mode}` lists exactly the images the prune removes. With no candidates the page reads `Nothing to prune.` |
| Limit | At most 100 images, largest first. The list, its count and the button name only the objects in this prune; a notice gives the rest: `{N} more images are not in this prune. Run it again afterwards for the rest.` (`1 more image is not in this prune.`) |
| Posted | Each listed image with a digest of its references |
| Run | The candidates are read again. An image that changed since the listing is skipped. Each image is removed on its own, without force, and a failure does not stop the others |
| Freed | `Up to {size}`. Layers shared with images that stay are not freed |
| Bindings | Kept |

| Result per image | Meaning |
|---|---|
| `Removed` | Gone |
| `Skipped: changed since you reviewed it` | References changed, or no longer a candidate |
| `Skipped: a container uses it now` | |
| `Skipped: another operation is running on it` | |
| `Already gone` | |
| `Failed` | Docker refused or the host failed |

## Update check

The check compares the digest an image holds locally for a reference with the digest its registry reports now.

1. Select `Check for updates` on the image list (it checks the tags of the page) or on the image page (its tags).
2. Read the result. With JavaScript the status of each reference updates in place; without it a result page opens and the list or image page is the back link.
3. For `Unknown`, read the reason in [Results](#results) and see [Troubleshooting](Troubleshooting).

| Item | Value |
|---|---|
| Started by | `Check for updates` on the list (the tags of the page) and on the image page (its tags), `POST /environments/{id}/images/updates`. Also `container-updater` runs ([Automation](Operations-Automation#detection)) |
| Access | Sight of the environment |
| References | 1 to 25 distinct references per request; more answers `400`: `Check between 1 and 25 references at once.` |
| Stored | The latest result of each reference ([Stored status](#stored-status)) |
| Result cache | The server keeps each answer in memory for 30 seconds, `Unknown` included, for every caller. A check inside that time returns it. `busy` is not kept |
| Swarm services | `container-updater` compares the digest in a service's image with the registry's, not the host's images, and stores nothing ([Automation](Operations-Automation#swarm-detection)) |
| Run from | The dockmin server. A registry only the Docker host can reach reads as `Unknown` |
| Without JavaScript | A page with the result; the list or image page is the back link |

### Registry request

| Item | Value |
|---|---|
| Request | `HEAD` of `/v2/{repository}/manifests/{tag}` over HTTPS, accepting OCI and Docker manifest and index media types, `User-Agent: dockmin-image-update-check` |
| Authentication | A `401` with a `Bearer` challenge sends one `GET` to the `https` token service, with the set's login when there is one, and repeats the `HEAD` with the token. A `Basic` challenge sends the set's login to the registry |
| Compared | The `Docker-Content-Digest` header, which must be one `sha256:` value |
| Connect to | The address of the bound set, with the set's TLS settings; for a reference with no binding or a `credential_missing` binding, the registry in the reference, with verified certificates and no login. `docker.io` is contacted at `registry-1.docker.io` |
| Limits | 5 s name lookup, 5 s per connection, 10 s per exchange, 30 s in all. Redirects are not followed. Replies are read with bounds |
| Proxy variables | Not read ([Reference-Deployment](Administration-Registries#registry-connections)) |
| Credentials | Never sent without a verified certificate |

A reference with no usable binding is checked anonymously at the registry its reference names. No registry entry is needed when the registry and its token service resolve to globally routable addresses. Otherwise an entry for the registry address, with authentication off if it needs none, must be granted to the environment; any credential set counts, an entry with authentication off included. Without one, the check refuses every address that is not global (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, IPv6 `fc00::/7`, loopback, link-local, and IPv4 addresses embedded in IPv6 forms of these), after name resolution and before connecting, for the registry and for the token service it names. The result is `Unknown` with `private_address`. With an entry granted, only the [address policy](Reference-Security#address-policy) applies. The Docker Hub and GitHub Container Registry entries a new instance starts with ([Registries](Administration-Registries#entries-a-new-instance-starts-with)) are not required for these public registries. The check for a dockmin update is not restricted. Nothing contacts a registry without an administrator's pull or check, a user's update check or a `container-updater` run.

### Results

| State | Meaning |
|---|---|
| `Up to date` | A local digest equals the registry's |
| `Update available` | The registry's digest differs from every local digest |
| `Pinned` | The reference names a digest, so it does not change |
| `Unknown` | The check could not decide. The reason follows |

| Reason | Cause |
|---|---|
| `no_local_digest` | The host holds no registry digest for the image; it was built or tagged locally |
| `credential_not_available` | The user holds no grant on the bound set, or the set is not granted to the environment, or its secret cannot be read |
| `credential_mismatch` | The bound set is for a different registry |
| `invalid_reference` | The reference is not valid |
| `image_id_reference` | The text is an image id, not a registry reference. No registry is contacted |
| `busy` | No check slot came free within 10 seconds. Not kept |
| `authentication_required` | The anonymous check was refused with `401` or `403`. Bind the image to a credential set |
| `private_address` | No registry entry is granted to the environment at the registry address, and the registry or its token service resolves to an address that is not globally routable: `The registry is on a private network. Add a registry entry for its address, with authentication off if it needs none.` |
| `no_digest` | The registry reported no digest |
| `dns`, `tcp`, `tls`, `certificate`, `authentication` | The connection stage that failed |
| `address_refused` | The registry or its token service is on this server or a link-local network and no permit covers it: `The registry or its token service is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` ([Reference-Deployment](Reference-Security#address-policy)) |
| `not_found` | The registry has no such repository or tag |
| `rate_limited` | The registry answered `429` |
| `protocol` | The reply was not a valid registry response |
| `timeout` | The registry did not answer in time |

A failure is the reference's `Unknown`, never `Up to date`. For a reference whose binding is `credential_missing`, the result carries `Checked anonymously; the bound set was deleted.`

| `authentication_required` | Behaviour |
|---|---|
| Update check | `Unknown` with the reason, each time the check runs. Binding the image to a credential set clears it |
| `container-updater`, update mode | The first run that meets it writes `image_update_refused` for the image and fails the object. Later runs count the object `held` and fail nothing, until the image is bound to a credential set or the reason changes ([Refusals and holds](Operations-Automation#refusals-and-holds)) |
| `container-updater`, report mode | Failed object, reason `authentication_required`. Holds are ignored |
| Compose stack | A reference whose check fails, for this or any other reason, is a failed object. The other services of the stack still update |

| `private_address` | Behaviour |
|---|---|
| Update check | `Unknown` with the reason, each time the check runs. Granting a registry entry at the registry address clears it |
| `container-updater`, update mode | As for `authentication_required`: written once as `image_update_refused`, then held until an entry is granted at the address or the reason changes ([Refusals and holds](Operations-Automation#refusals-and-holds)) |
| `container-updater`, report mode | Failed object, reason `private_address`. Holds are ignored |
| Entry granted | Only the loopback, link-local and metadata classes still need `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` |

| Limit | Value | Message |
|---|---|---|
| Update check requests per user at once | 2 | `You have update checks running. Wait for them to finish.` (`429`) |
| Checks against registries at once | 4 per instance, shared by users and `container-updater` runs | Further references wait up to 10 s, then read `busy` |
| Update check requests of `container-updater` | Not counted against the per-user limit | |
| Identical checks | Share one request to the registry | |

## Recorded actions

Every action records one activity row, for success and for failure. A refusal on access leaves no row. A tag that changes nothing leaves no row.

| Action | Target |
|---|---|
| `image_pulled` | The image id once the pull ended. None for a failure |
| `image_pushed`, `image_tagged`, `image_untagged`, `image_removed` | The image id |

| Detail key | Present when |
|---|---|
| `reference` | `image_pulled`, `image_pushed`, `image_tagged`, `image_untagged`: the canonical reference |
| `credential_set_id`, `anonymous`, `credential_source` | Pull and push: the set used (`null` for none), whether the pull was anonymous, and how it was chosen: `choice`, `binding`, `only_match` or `no_match` |
| `digest`, `binding` | Pull and push that ended cleanly: the digest observed, and the binding result (`created`, `rebound`, `removed`, `kept`, `skipped`) |
| `trigger`, `remote_digest` | `image_pulled` by `container-updater`: `update`, and the digest the registry reported at the check |
| `replaced_image_id` | Tag: the image the reference moved from |
| `trigger`, `replaced_at`, `replaced_target_type`, `replaced_target` | `image_removed` by `container-updater` after a grace period: `update_prune`, when the update was recorded, `container` or `stack`, and the id of the new container or the name of the stack ([Automation](Operations-Automation#removal-of-replaced-images)) |
| `references`, `force`, `in_use_containers`, `size` | Remove |
| `force`, `in_use_containers`, `untagged`, `deleted` | Untag |
| `untagged`, `deleted` | Remove |
| `pruned` | Remove, `true` when it ran from a prune. A prune writes one row per image and none for the run |
| `batch` | Remove, `true` when it ran from a bulk removal. A bulk removal writes one row per image and none for the run |
| `unreviewed_untagged` | Remove, when the host untagged references that were not reviewed |
| `holder_action` | Failure row for a busy reference |
| `interrupted` | The request ended before the operation settled |
| `reason`, `message` | Failure rows |

| Failure reason | Cause |
|---|---|
| `object_busy` | Another action holds the reference or the image |
| `credential_unavailable`, `credential_mismatch`, `credential_choice_required` | The credential errors of [Pull](#pull) |
| `not_found_or_denied`, `pull_failed`, `push_failed` | The registry's answer |
| `reference_changed`, `replaced_changed`, `references_changed`, `in_use_changed` | Something changed since the review |
| `pinned`, `not_bound`, `anonymous_choice`, `binding_changed` | Rebind: the reference is pinned by digest, has no binding, or the binding changed since the review. `anonymous_choice`: a binding without a pull was asked to bind anonymously (a failure of `image_bound`). The reason of an unavailable set (`missing`, `not_granted_environment`, `not_granted_user`, `secret_unreadable`, `token_exchange_failed`, `credential_mismatch`) and the kind of a failed registry check (`dns`, `tcp`, `tls`, `certificate`, `authentication`, `not_found`, `rate_limited`, `no_digest`, `protocol`, `timeout`, `address_refused`) are recorded too |
| `image_in_use`, `image_has_dependents` | Docker or the confirmation refused the removal |
| `not_found` | The image is gone |
| `engine_error`, `conflict` | Docker refused |
| `unreachable`, `unsupported`, `credentials`, `engine`, `outcome_unknown`, `too_large` | Host failures, as in [Containers](Usage-Containers#host-failures) |

Rows about bindings are written with the operation that caused them. Target type `image_binding`, target id the canonical reference, scoped to the environment.

| Action | Written by | Detail |
|---|---|---|
| `image_bound` | A pull, push, create, recreate, Swarm service change or Swarm stack deployment that created the binding; `Adopt everything on this host` ([Bindings without a pull](#bindings-without-a-pull)); or [Bind unbound images](#bind-unbound-images) | `credential_set_id`, `anonymous` (`false`). From `Adopt everything on this host` and from `Bind unbound images` also `digest` (the registry digest that matched the local image), `verified` (`true`) and `pulled` (`false`). From `Bind unbound images` also `bulk` (`true`) |
| `image_rebound` | An administrator's choice that changed it, on a pull or push or on the [rebind page](#rebind-without-a-pull) | `credential_set_id`, `anonymous`, `previous_credential_set_id`, `previous_state`. From the rebind page also `registry_digest` and `verified` (`true` once the binding is changed), and `removed` (`true`) for an `Anonymous` choice, which deletes the binding and sends no request |
| `image_binding_credential_missing` | Deleting a set, or changing its address | `credential_set_id`, `credential_set_name`, `missing_reason` (`deleted` or `address_changed`) |
| `image_binding_removed` | A reap, or an administrator's `Anonymous` choice on a pull or push | `credential_set_id`, `state`. From the `Anonymous` choice also `reason` (`anonymous_choice`) |

A reap also writes `image_bindings_reaped`, with target type `environment` and the count in `count`.
