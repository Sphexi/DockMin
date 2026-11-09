# Swarm

dockmin manages the services, tasks, nodes, configs and secrets of a Docker Swarm through an environment whose type is `swarm`: a Docker host that is an active Swarm manager. Swarm stacks are on [Stacks](Usage-Stacks#swarm-stacks).

| Item | Value |
|---|---|
| Environment type | `swarm`, recorded by the connection test and the status refresh ([Recorded values](Usage-Environments#recorded-values)) |
| Objects | Services, tasks, nodes, configs, secrets, and the stack namespaces that group them |
| Managed here | Create, edit, scale, force update, roll back and remove services; change node availability; create and remove configs and secrets |
| Managed on the stack pages | Create, deploy, redeploy, remove, forget and adopt Swarm stacks ([Stacks](Usage-Stacks#swarm-stacks)). Export is described here ([Export (Swarm stacks)](#export-swarm-stacks)) |
| Read only | Tasks, stack namespaces that are not managed, cluster facts |
| Not managed | Creating, joining, leaving or unlocking a swarm; promoting, demoting, labelling or removing nodes; secret drivers; config templating; label edits on configs and secrets; removal of volumes |
| JSON API | None. Swarm pages are HTML only, Swarm stacks included |
| Interface | Server-rendered pages. Every action works without JavaScript |

## Environments

| Environment | Sidebar | Swarm paths |
|---|---|---|
| Swarm manager (`swarm`) | `Swarm` section | Resolve |
| Standalone host | None | `404` |
| Swarm worker (`standalone`, Swarm state `active`, control unavailable) | None. The page reads `Swarm worker: Swarm objects are managed through a manager of this cluster.` | `404` |
| Never contacted (type unrecorded) | None | `404` |
| Environment the user cannot see | None | `404` |

- A worker is listed as any host is. Its containers, task containers included, are visible under [Visibility and inheritance](#visibility-and-inheritance).
- The gate runs before any call to the host and answers `404` for every role, administrators included. A path for an object that does not exist answers the same.
- The recorded type can differ from the host. A host that is no longer an active Swarm manager, or whose swarm is locked, answers every Swarm call with `503`: `This environment is not an active Swarm manager, or its swarm is locked.` The page is the unavailable page (title `The environment is not reachable`). A refresh of the environment's status is requested, and its result replaces the recorded type.
- On a manager, the sidebar `Swarm` section shows Services and Nodes counts to administrators ([Snapshot cache](Usage-Environments#snapshot-cache)). The [environment page](Usage-Environments#environment-page) adds a `swarm running` figure to its `Stacks` card and lists managed Swarm stacks in its stack grid.
- A manager that is the local socket host is a Swarm environment like any other. Standard users hold read access there at most ([Local socket environments](Usage-Containers#local-socket-environments)).

## Pages and routes

All paths are under `/environments/{id}/swarm`. The sidebar's `Swarm` section lists Overview, Services, Nodes (administrators), Configs, Secrets and Swarm stacks.

| Page | Path | Method |
|---|---|---|
| Overview | The base path | `GET` |
| Service list | `/services` | `GET` |
| Create form, create | `/services/new` | `GET`, `POST` |
| Credential picker for the image typed in the create form | `/services/new/choices` | `POST`, htmx only; other requests redirect (`303`) to the create form |
| Service | `/services/{service_id}?tab={tab}` | `GET` |
| Tasks panel | `/services/{service_id}/tasks` | `GET` |
| Edit form, review | `/services/{service_id}/edit` | `GET`, `POST` |
| Apply an edit | `/services/{service_id}/edit/confirm` | `POST` |
| Scale confirmation, scale | `/services/{service_id}/scale` | `GET`, `POST` |
| Force update confirmation, force update | `/services/{service_id}/force-update` | `GET`, `POST` |
| Roll back confirmation, roll back | `/services/{service_id}/rollback` | `GET`, `POST` |
| Remove confirmation, remove | `/services/{service_id}/remove` | `GET`, `POST` |
| Service access control | `/services/{service_id}/access`, `/services/{service_id}/access/delete` | `POST` |
| Log stream (server-sent events) | `/services/{service_id}/logs/stream` | `GET` |
| Log download | `/services/{service_id}/logs/download` | `GET` |
| Task | `/tasks/{task_id}` | `GET` |
| Node list | `/nodes` | `GET` |
| Node | `/nodes/{node_id}` | `GET` |
| Availability confirmation, change | `/nodes/{node_id}/availability` | `GET`, `POST` |
| Config or secret list | `/configs`, `/secrets` | `GET` |
| Create form, create | `/configs/new`, `/secrets/new` | `GET`, `POST` |
| Config or secret | `/configs/{object_id}`, `/secrets/{object_id}` | `GET` |
| Remove confirmation, remove | `/configs/{object_id}/remove`, `/secrets/{object_id}/remove` | `GET`, `POST` |
| Config or secret access control | `/configs/{object_id}/access`, `/secrets/{object_id}/access`, and `…/access/delete` | `POST` |
| Stack list | `/stacks` | `GET` |
| Stack | `/stacks/{namespace}?tab={tab}` | `GET` |
| Stack access control | `/stacks/{namespace}/access`, `/stacks/{namespace}/access/delete` | `POST` |
| Stack export confirmation | `/stacks/{namespace}/export` | `GET` |
| Stack export download | `/stacks/{namespace}/export` | `POST` |
| Stack create, check, edit, files, redeploy, remove, forget, adopt, import, progress, dismiss and restore, and reveal of a secret variable | `/stacks/-/…` and `/stacks/{namespace}/…` | [Swarm stack pages and routes](Usage-Stacks#swarm-stack-pages-and-routes) |

- `{service_id}`, `{task_id}`, `{node_id}` and `{object_id}` are 25 lower-case letters and digits. Any other form of at most 128 characters, including a name, answers `404` for every role, before any call to the host. A longer segment answers `422`.
- `{namespace}` is `[A-Za-z0-9][A-Za-z0-9_.-]{0,62}`. Any other form answers `404`.
- `{tab}` of a service is `overview` (default), `configuration`, `tasks`, `logs`, `automation` or `access`. `{tab}` of a config, a secret or a namespace that is not managed is `overview` or `access`; `{tab}` of a managed stack is `overview`, `file`, `variables`, `files`, `logs`, `automation` or `access`. Any other value answers `404`. `access` answers `403` to standard users.
- Every `POST` carries the CSRF token. A form larger than 256 KiB in total answers `413`.
- Every Swarm page and redirect is sent with `Cache-Control: no-store`.
- A `POST` except the Swarm stack export download redirects (`303`) on success, with a notice. The download answers `200` with the archive. The redirects go: to the service after create, edit, scale, force update and roll back; to the list after a remove; to the node after an availability change; to the new object after a config or secret create. A refusal renders a page at the real status code, with a `Review again` link for `409`, `502` and `503`.
- Every confirmation lists its targets by name with a count: service remove, scale, roll back and force update; config and secret remove; node availability; Swarm stack forget, remove and redeploy ([Confirmations](Contributing-Frontend-Conventions#confirmations)).
- Below 40rem these list columns are hidden: services `Mode`, `Image`, `Stack`, `Ports`, `Updated`; configs and secrets `Stack`, `Created`; Swarm stacks `Last operation`, `Services`, `Configs`, `Secrets`, `Networks`.
- Swarm routes have no JSON form.

## Overview

The page is titled `Swarm`. Its tiles and cluster facts:

| Item | Administrators | Standard users |
|---|---|---|
| `Services` tile | Count on the cluster | Count of the services they may see (`Services you can see`) |
| `Tasks running` tile | `{running} of {desired}`, summed over the services counted | Same |
| `Configs`, `Secrets` tiles | Count; `Unknown` when the list cannot be read | Count they may see (`Configs you can see`, `Secrets you can see`); `Unknown` when the list cannot be read |
| `Nodes`, `Managers` tiles | Count | Not shown |
| `Cluster` facts: `Cluster ID`, `Created`, `This manager`, `Nodes ready`, `Availability` (`{N} active, {N} paused, {N} drained`) | Shown | Not shown. The page reads `Cluster and node details are shown to administrators only. The counts above cover the services, configs and secrets you can see.` |
| `Create service` button | Shown | When they may create in the environment |

## Access

Viewing needs sight of the environment ([Access](Usage-Environments#access)). A standard user without it receives `404` on every route. An object that does not exist and an object the user may not see answer identically (`404`). A user who may read an object and needs write answers `403`. A list omits what the user may not see and counts only what they may see.

| Operation | Administrators | Standard users, remote environment | Standard users, local socket |
|---|---|---|---|
| Overview | Cluster facts and counts | Counts of visible services, configs and secrets | Same |
| Service list, service page, tasks, task, logs | Every service | Read access to the service | Same |
| Service create | Yes | Create access in the environment; [restrictions](#standard-user-restrictions) apply | Refused (`403`) |
| Service edit, scale, force update, roll back, remove | Yes | Write access to the service. Edit and roll back also pass the [restrictions](#standard-user-restrictions) | Refused (`403`) |
| Node list, node, availability | Yes | Refused (`403`) | Refused (`403`) |
| Config list and page (content shown) | Every config | Read access to the config | Same |
| Secret list and page (value never shown) | Every secret | Read access to the secret | Same |
| Config or secret create | Yes | Create access in the environment | Refused (`403`) |
| Config or secret remove | Yes | Write access to it | Refused (`403`) |
| Attach a config to a service | Yes | Read access to the config | Refused (`403`) |
| Attach a secret to a service | Yes | Write access to the secret | Refused (`403`) |
| Stack list and stack page | Every namespace and managed stack | Read access to the stack's control, or the default policy when it has none; members by their own visibility | Same |
| Swarm stack create, check, save, redeploy, remove | Yes | [Swarm stack access](Usage-Stacks#swarm-stack-access) | Refused (`403`) |
| Swarm stack forget, adopt | Yes | Refused (`403`) | Refused (`403`) |
| Swarm stack reconstruct from the cluster, adopt as is | Yes | Refused (`404`) | Refused (`404`) |
| Access tab (service, config, secret, stack) | Yes | Refused (`403`) | Refused (`403`) |

- A standard user sees the host name of the node each visible task runs on, and nothing else about nodes.
- Attaching a secret needs write access to it. An attached secret is a file in the service's containers.
- A user with write access to a service can read secrets already attached to it, for example by changing the service's command to print the file. References already attached to a service are not checked again on edit or roll back, so removing a user's access to a secret does not remove it from services that hold it.

### Ownership

| Event | Access control |
|---|---|
| Create (service, config, secret) | The object is put under a resource control owned by its creator, administrators included, in the same operation. The control's access level is `write`, and the owner holds the control's level ([Ownership](Usage-Stacks#ownership)). No other user or team is granted anything |
| Saving fails | The object is kept. The page shows `Created, but its ownership could not be recorded. An administrator can assign access to it.` The row records `control_created` as `false`. The default access policy for uncontrolled objects applies |
| Remove | The control is deleted. The row records `control_released` as `false` when it could not be deleted |
| Object already gone at remove | The control is released and the row records `control_released` |
| Access tab | Administrators create, change and delete the control of a service, a config, a secret or a stack |

### Visibility and inheritance

Services, configs, secrets and networks are decided in this order, as containers are ([Visibility](Usage-Containers#visibility)):

| Order | Source | Result |
|---|---|---|
| 1 | The object's own resource control | Its grants decide |
| 2 | The control of its stack, named by the label `com.docker.stack.namespace` (a service: its `Spec.Labels`). The label `com.docker.compose.project` is read first when an object has both | Inherited |
| 3 | Neither | The default access policy for uncontrolled objects |

- Stack names are compared case-insensitively. A Compose project and a Swarm namespace created outside dockmin whose names fold to the same key share one control: changing the control on either changes it for both ([Swarm stack names](Usage-Stacks#swarm-stack-names)).
- A task container follows container visibility: its own control, then the stack control named by its labels, then the default policy. It does not inherit its service's control.
- A task is visible exactly when its service is.
- A standard user sees the container link of a task only when the task ran on the manager dockmin is connected to and the user may see that container.
- A container managed by a service cannot be recreated ([Blockers](Usage-Containers#blockers)).

## Services

### List

| Item | Value |
|---|---|
| Title | `Services` |
| Columns | Name (with short id), Mode, Replicas, Image, Stack, Ports, Updated, Actions |
| Mode | `Replicated`, `Global`, `Job` |
| Replicas | A badge (`Running`, `Partly running`, `Not running`, `Stopped`, `Unknown`) and `{running} of {desired} running` |
| Image | The image with a pinned digest shortened to 12 digits |
| Ports | One outlined chip per port: `{published}→{target}/{protocol}`, with `(host)` for host mode |
| Updated | Age, and the update state while it is not `completed` |
| Actions | `Details` for everyone; `Remove` for users with write access |
| Order | Name, ignoring case, then id. The Name, Mode, Replicas, Image, Stack and Updated headers sort the list ([Sorting](#sorting)) |
| Page size | 25 by default; `limit` 1 to 500. Outside the range answers `422` |
| Paging | `cursor`; an invalid cursor answers `400` |
| Count | `N services`, or `N services match these filters, of M you can see` |
| Buttons | `Create service` for users who may create |

| Filter | Parameter | Match |
|---|---|---|
| Name contains | `q` | At most 128 characters. Case-insensitive substring of the name |
| Mode | `mode` | `replicated`, `global` or `job` |
| Stack | `stack` | At most 128 characters. Case-insensitive exact match on the stack name |

An invalid filter replaces the table with the message and answers `400`. Filters survive paging. A host that cannot be read replaces the table with an error and answers `503`.

#### Sorting

The rules of [Sorting](Usage-Containers#sorting) apply: `sort` and `dir`, the header links, the cursor, and the `400` answers (`Sort by one of the listed columns.`, `Choose ascending or descending.`, `The page link is not valid.`).

| Column | `sort` | Orders by | Empty value |
|---|---|---|---|
| Name | `name` | Name, ignoring case | None |
| Mode | `mode` | Mode, ignoring case | None |
| Replicas | `replicas` | Desired replicas: the count in the service's status, else the configured count | A global or job service with no status |
| Image | `image` | Image as shown, ignoring case | A service with no image |
| Stack | `stack` | Stack name, ignoring case | A service with no stack |
| Updated | `updated` | Time of the last update | A time that cannot be read |

Default order: `name` ascending, then id. Empty values come first ascending and last descending. Filters apply before the sort.

### Service page

| Tab | Content | Shown to |
|---|---|---|
| Overview | Name, id, mode, tasks (`{running} of {desired} running`), image with `Pinned to a digest` when it names a digest, command, stack, created, updated, published ports and endpoint mode, rollback target image, the last update state with its message | Everyone who can read |
| Configuration | Environment variables, labels, placement, resources, restart policy, update configuration, rollback configuration, networks, mounts, configs, secrets | Everyone who can read |
| Tasks | [Tasks](#tasks) | Everyone who can read |
| Logs | [Service logs](#service-logs) | Everyone who can read |
| Automation | The rules that act on the service ([Automation](#automation)) | Everyone who can read |
| Access | Resource control | Administrators |

| Summary buttons | Shown when |
|---|---|
| `Edit`, `Force update`, `Remove` | The user has write access |
| `Roll back` | The user has write access and the service has a previous configuration |
| Scale form (Overview tab) | The user has write access and the service is replicated |

| Withheld from standard users | Shown to administrators |
|---|---|
| Environment variable values (`Hidden`) | Yes, except names that look like secrets (`Redacted`) |
| Values of label keys that look like secrets (`Redacted`) | Yes |
| Source of a bind mount (`Hidden`) | Yes |
| Name of a network they may not see: its id is shown with `Not available to you` | Yes |

Secret-named keys follow the rule in [Withheld from standard users](Usage-Containers#withheld-from-standard-users). Update and task messages are shown after the cleaning described in [Tasks](#tasks).

The Configuration tab lists mounts with the columns `Type`, `Source`, `Target`, `Access` (`Read-only` or `Read and write`), and configs and secrets with `Name`, `File`, `Owner` (`uid:gid`) and `Mode`.

#### Automation

The `Automation` tab lists the rules that act on the service by name or through its Swarm stack, with `Pause`, `Resume` and `Add automation` where allowed ([Per-object panel](Operations-Automation#per-object-panel)). No other tab shows it. `Pause` and `Resume` return to the tab.

#### Activity

Administrators see an activity panel on three tabs: `Recent activity` on Overview, `Automated actions` (events whose actor is a subsystem) on Automation, and `Access changes` on Access ([Panels](Administration-Activity#panels)). A tab swap carries the panel of the tab it opens.

### Create

The form is one page with no review step. Standard users are not offered the `bind` mount type, `Added capabilities`, `Kernel parameters` or log drivers outside the allowed set, and a submission that contains administrator-only options is refused ([Standard user restrictions](#standard-user-restrictions)).

1. Open `Create service` on the service list or the Overview page.
2. Fill `Name`, `Image` and `Mode`. Fill the other sections as needed; each section opens when it holds a value.
3. For an image that fits more than one credential set, choose the set in the credential picker ([Images and registry credentials](#images-and-registry-credentials)).
4. Choose `Create service`. The service page opens with a notice, and the Tasks tab shows the tasks starting. When a field is refused, the form returns with `Check the form` and every submitted value kept.

| Field | Rule |
|---|---|
| Name | 1 to 63 letters, digits, `-` and `_`, starting and ending with a letter or digit. Not 25 lower-case letters and digits. Cannot be changed later |
| Image | An image reference, normalised with the `latest` tag when none is given, or an image id (`sha256:` and 64 hexadecimal digits). An image id is never pulled or pinned |
| Mode | `replicated` (default) or `global`. Cannot be changed later |
| Replicas | Replicated mode only. 0 to 1000 for administrators, 0 to 100 for standard users. `Replicas apply only to replicated mode.` in global mode |
| Command | Replaces the image's command. Split as a POSIX shell would. At most 4096 characters |
| Environment variables | At most 100 rows of name and value. A name is at most 255 characters without `=` or NUL; names are unique; a value has no NUL. Names and values together at most 64 KiB |
| Labels | At most 64 rows. A key is at most 255 characters without control characters; keys are unique. The labels `com.docker.compose.project` and `com.docker.stack.namespace` are refused, whatever their case and surrounding whitespace: `This label is set by the product and cannot be supplied.` Service, container and mount-volume labels together at most 64 KiB |
| Published ports | At most 64 rows. Container port 1 to 65535. Published port 1 to 65535, blank for Swarm to choose. Protocol `tcp`, `udp` or `sctp`. Mode `ingress` (default) or `host`. A published port and protocol pair is used once. Mode `ingress` needs the `vip` endpoint mode |
| Endpoint mode | `vip` (default) or `dnsrr` |
| Networks | At most 32 rows. A network name or id, unique, with up to 16 aliases (DNS labels of letters, digits and `-`) |
| Mounts | At most 64 rows. Type `volume` (default), `tmpfs`, or `bind` for administrators. Volume source: a volume name, blank for an anonymous volume. Bind source: an absolute, normalised host path on the node. A tmpfs mount takes no source and an optional size. Target: absolute, normalised, not `/`, unique. `Read-only` checkbox |
| Configs | At most 64 rows: config name or id, file path (default `/{name}`), UID and GID (default `0`), mode in octal (default `0444`). File paths are unique and have no `..` |
| Secrets | At most 64 rows: the same fields. The file path defaults to the secret's name |
| Placement constraints | One per line, at most 32, each at most 256 characters: an attribute, `==` or `!=`, and a value, such as `node.role==worker` |
| Placement preferences | One per line, at most 8: `spread=<descriptor>`, such as `spread=node.labels.zone` |
| Most replicas per node | 0 to 2,147,483,647. Blank or 0 for no limit |
| Run on this manager node | Adds the constraint `node.id==<manager-node-id>`, the id of the connected manager, unless one is present. `The manager node of this environment could not be determined.` when the id cannot be read |
| CPU limit, CPU reservation | A number of CPUs from 0.001 to 100,000, such as `0.5` |
| Memory limit, memory reservation | A size of at least 4 MiB ([value syntax](Usage-Containers#value-syntax)) |
| PIDs limit | 1 to 2,147,483,647 |
| Reservations | A reservation cannot exceed its limit |
| Restart policy | Condition `any`, `on-failure` or `none`; delay and window as durations; maximum attempts 0 to 2,147,483,647 |
| Update configuration | Parallelism 0 to 2,147,483,647 (0 replaces all tasks at once); delay and monitor as durations; failure action `continue`, `pause` or `rollback`; failure ratio 0 to 1; order `stop-first` or `start-first` |
| Rollback configuration | The same, with failure action `continue` or `pause` |
| Health check, Process, Host and DNS, Security, Resource limits, Logging | [Container settings](#container-settings) |

- A duration is `0`, `0s`, or a duration in the [value syntax](Usage-Containers#value-syntax) (`500ms` to `24h`).
- Rows that are blank are ignored. Without JavaScript, `Add rows` posts the form back with more blank rows; with JavaScript, `Add row` appends a row.
- The network, config, secret and volume fields suggest names as the user types: at most 200 of each, from the objects the user may attach ([Networks, configs and secrets](#networks-configs-and-secrets)). Suggestions never replace the checks.
- Every problem is reported at once in a `Check the form` summary, each beside its field, and every submitted value is kept. The answer is `400`; a name already used is `409` (`A service with this name already exists.`).
- Container settings the form has no field for, such as `Privileges.Seccomp`, `Secrets` runtime fields and `GenericResources`, are not on the form. They are kept as they are when an existing service is edited.
- Creation sends the service to the manager and returns when the manager has accepted it. Tasks start afterwards; the Tasks tab shows their progress.
- A create that cannot pin the image to a digest still creates the service. The page reads `Created. The image could not be pinned to a digest, so nodes may run different images of this tag.`

### Container settings

Each section is a disclosure on the create and edit forms, open when it holds a value. Every field is optional; a blank field leaves the setting out. Specification paths are under `TaskTemplate.ContainerSpec` unless stated.

| Section | Field | Specification path | Rule |
|---|---|---|---|
| Health check | Test | `Healthcheck.Test` | `Inherit from the image` (default; no test stored), `Disabled` (`NONE`), `CMD (exec form)`, `CMD-SHELL (shell form)`. An edit also offers `Keep the stored test` when the stored test has none of these shapes |
| Health check | Command | `Healthcheck.Test` | `CMD`: split like a POSIX shell, stored as an argument list. `CMD-SHELL`: one command line. At most 4096 characters, no NUL. Required in those two modes: `A command is required.` Any other mode refuses one: `Choose CMD or CMD-SHELL to give a command.` |
| Health check | Interval, Timeout, Start interval | `Healthcheck.Interval`, `Timeout`, `StartInterval` | [Duration](Usage-Containers#value-syntax), `1ms` to `24h` |
| Health check | Start period | `Healthcheck.StartPeriod` | `0`, `0s`, or a duration |
| Health check | Retries | `Healthcheck.Retries` | Whole number from 0 to 100 |
| Process | User | `User` | `name` or `uid`, optionally `:group` or `:gid`. At most 128 characters of letters, digits, `_`, `.`, `-` |
| Process | Groups | `Groups` | At most 32, separated by spaces or commas. Each is 1 to 64 characters of letters, digits, `_`, `.`, `-` |
| Process | Working directory | `Dir` | [Absolute path](Usage-Containers#value-syntax) |
| Process | Init | `Init` | `Default` (not set), `On`, `Off` |
| Process | Stop signal | `StopSignal` | A signal name of letters and digits, such as `SIGTERM`, or a number from 1 to 64 |
| Process | Stop grace period | `StopGracePeriod` | `0`, `0s`, or a duration |
| Process | Allocate a terminal, Keep standard input open | `TTY`, `OpenStdin` | Checkboxes |
| Host and DNS | Host name | `Hostname` | At most 253 characters; a [host name](Usage-Containers#value-syntax) |
| Host and DNS | Host entries | `Hosts` | One per line, at most 32, as `host:address` with an IPv4 or IPv6 literal. Stored as `address host` |
| Host and DNS | Name servers | `DNSConfig.Nameservers` | At most 8 IP address literals, separated by spaces or commas |
| Host and DNS | Search domains | `DNSConfig.Search` | At most 8. Each is at most 253 characters of letters, digits, `.`, `_`, `-` |
| Host and DNS | Resolver options | `DNSConfig.Options` | At most 8. Each is at most 128 characters of letters, digits, `_`, `:`, `.`, `-`, such as `ndots:2` |
| Security | Read-only root file system | `ReadOnly` | Checkbox |
| Security | No new privileges | `Privileges.NoNewPrivileges` | Checkbox |
| Security | Dropped capabilities | `CapabilityDrop` | At most 64 Linux capability names, or `ALL`, separated by spaces or commas. Case-insensitive; `CAP_` is optional. Stored as `CAP_NAME` or `ALL` |
| Security | Added capabilities | `CapabilityAdd` | Administrators only. The same rule. A capability both added and dropped is refused: `A capability cannot be both added and dropped.` |
| Security | Kernel parameters | `Sysctls` | Administrators only. One per line as `name=value`, at most 64. The name is `net.` or `kernel.` followed by 1 to 250 characters of letters, digits, `_`, `.`, `-`. The value is at most 1024 characters without control characters. A name is set once |
| Resource limits | Limit, Soft limit, Hard limit | `Ulimits` | At most 32 rows. Limit: `core`, `cpu`, `data`, `fsize`, `locks`, `memlock`, `msgqueue`, `nice`, `nofile`, `nproc`, `rss`, `rtprio`, `rttime`, `sigpending` or `stack`; each once. Soft: required, a whole number from -1 (unlimited) to 9223372036854775807. Hard: the same range; blank equals the soft limit. The soft limit cannot exceed a hard limit other than -1 |
| Logging | Log driver | `TaskTemplate.LogDriver.Name` | Administrators: 1 to 64 characters of lower-case letters, digits, `_`, `-`; blank for the node's default. Standard users: `json-file`, `local` or `none`, or the default |
| Logging | Log options | `TaskTemplate.LogDriver.Options` | At most 16 rows of option and value. Option: 1 to 64 characters of lower-case letters, digits, `.`, `_`, `-`; each once. Value: at most 1024 characters without control characters. A driver is required: `A log driver is required when log options are set.` |

- A list field removes repeated entries.
- A section writes only the fields it lists. Other keys of the specification, including others under `Privileges`, are kept as stored.
- Clearing every field of a section removes its keys.
- A disabled test with timings is accepted and stored as given.
- Docker omits health check values of zero when it returns a specification, so a zero equals an absent value in comparisons.

### Edit and review

The edit form is filled from the stored configuration and offers the same fields as create, except the name and the mode.

1. Open `Edit` on the service page.
2. Change the fields and choose `Review changes`. The review page opens; nothing is sent to Docker.
3. Read `Changes`, `Running tasks` and, when the image changed, `Image now` and `Image after`. When the image changed, choose the credential set for the pull.
4. Choose `Update service`. The service page opens with the notice `Service updated. Its tasks are replaced as the update rolls out.` When the review shows a conflict, choose `Back to edit`.

| Rule | Value |
|---|---|
| Needs | Write access to the service |
| Kept | Every setting the form does not show is sent back as stored: unknown fields, mounts of a type the form does not model, reserved labels, extra settings of a kept network or port |
| Job mode | A service in job mode is shown and edited with its mode kept |
| Mode switch | `A service cannot change between replicated and global mode. Create a new one.` |
| Environment values | Administrators see every value except names that look like secrets. Standard users see names only |
| Label values | Administrators see every value. Standard users see values except secret-named keys and values that look like secrets |
| Value not shown | The replacement field is empty and the stored value is kept unless a new value is typed or the row is removed |
| Number, duration, list and name fields | A field left exactly as the form showed it keeps the stored value, even when the value is outside the rules of the create form: a `fs.*` kernel parameter, a duration with sub-millisecond precision, a limit name not in the list |
| Comparison | A review compares the stored and the new value as Docker holds them. Durations are compared exactly in nanoseconds. An empty list equals an absent one. A health check with no test and no timings equals `Inherit from the image`. Capability names are compared without `CAP_`, order and repeats. A value that means the same as the stored one is sent in the stored spelling |

| Review shows | Content |
|---|---|
| Changes | The settings that differ, as names only and never values: Image, Mode and replicas, Command, Environment variables, Labels, Placement, Resources, Restart policy, Update configuration, Rollback configuration, Networks, Published ports, Mounts, Configs, Secrets, Health check, Process, Host and DNS, Security, Resource limits, Logging, Other settings the form does not show |
| Image now, Image after | Shown when the image changed |
| Running tasks | `All N running tasks are replaced as the update rolls out, according to the service's update configuration.` (`task is` for one) when the change touches the image, command, environment, placement, resources, restart policy, networks, mounts, configs, secrets, health check, process, host and DNS, security, resource limits, logging or other settings. `Running tasks are not replaced by this change.` for mode and replicas, labels, published ports, update configuration and rollback configuration |
| Administrator options in use | For administrators: the [restricted options](#options-refused) the configuration uses |
| Credential set for the pull | When the image changed: the sets that fit the reference and `Anonymous`, with the bound or only fitting set preselected ([Images and registry credentials](#images-and-registry-credentials)) |
| No difference | `No setting differs from the stored configuration. Confirming changes nothing.` |

| Buttons | Effect |
|---|---|
| `Update service` | Applies the edit to exactly the version that was reviewed |
| `Back to edit` | Returns to the form with every value kept |
| `Cancel` | Returns to the service |

### Version binding

| Step | Binding |
|---|---|
| Edit form | Carries the service's version (`form_version`) |
| Review | Refused when the service's version differs: `This service changed since you reviewed it; nothing was changed.` (`409`). The form is shown again with the entered values and the current version |
| Confirmation | Carries the version that was reviewed (`expected_version`) |
| Apply | The service is read again under its lock. A different version is refused: `This service changed since you reviewed it; nothing was changed.` (`409`, reason `changed_since_review`) |
| Docker refuses the update | The version is read again. A different version is `changed_since_review`; the same version is `engine_rejected` (`502`, `Docker refused this service configuration.`) |
| Nothing differs | `Nothing changed: the service already has this configuration.` No row is recorded |

### Scale

| Item | Value |
|---|---|
| Needs | Write access to the service. The service is replicated |
| Form | The Overview tab: `Replicas` and `Scale` |
| Range | 0 to 1000 for administrators, 0 to 100 for standard users: `Replicas must be between 0 and {maximum}.` (`400`) |
| Not replicated | `Only a service in replicated mode can be scaled.` (`409`) |
| Scale up | A `POST` with no confirmation page. Notice: `Replica count changed. Tasks start or stop as the manager schedules them.` |
| Scale down, including to 0 | A confirmation names the number of tasks that stop (the current count minus the requested one). `Swarm chooses which tasks to stop. Their containers are removed.` At 0, the configuration is kept and the service can be scaled up again. Button: `Scale down service` |
| Bound to | `expected_version` and `expected_replicas`. A difference is refused (`409`, reason `changed_since_review`) |
| Same count | The confirmation reads `The service already has this many replicas. Nothing changes.` A posted scale to the same count sends nothing to Docker and records no row |
| Registry credentials | Not sent. Swarm reuses the ones stored with the service |
| Recorded | `service_scaled` with `replicas_before` and `replicas_after` |

### Force update

| Item | Value |
|---|---|
| Needs | Write access to the service |
| Effect | Swarm replaces every running task, following the update configuration. The configuration is not changed |
| Confirmation | The number of running tasks that will be replaced, and the running tasks by node (at most 50 nodes, then `and N more nodes`; `node` for one). Button: `Force update service` |
| Bound to | `expected_version` |
| Registry credentials | Not sent. Swarm reuses the ones stored with the service |
| Recorded | `service_force_updated`, by the user or, for a webhook's `redeploy` action, by `webhook-receiver` with the rule ([Webhooks](Operations-Automation#webhooks)) |

### Roll back

| Item | Value |
|---|---|
| Needs | Write access to the service. The service has a previous configuration: `This service has no previous configuration to roll back to.` (`409`) |
| Effect | The service returns to its previous configuration. The configuration that is running now becomes the previous one |
| Confirmation | The settings that differ (names only), the image now and after, and the number of running tasks that are replaced following the rollback configuration. Button: `Roll back service` |
| Standard users | The restore is checked against the [restrictions](#standard-user-restrictions) before the confirmation is shown, and again when it is applied. A refusal is `400` with the messages |
| Bound to | `expected_version` |
| Registry credentials | The ones stored with the previous configuration |
| Recorded | `service_rolled_back` |

- A [credential renewal](#credential-renewal-by-container-updater) and a Swarm stack redeploy replace the previous configuration of the services they send with the configuration the service holds at that moment. After either, `Roll back` restores the same configuration, not the image before the last update.
- `container-updater` rolls a service back through the same operation when its update pauses or leaves fewer tasks running ([Rollback of a service](Operations-Automation#rollback-of-a-service)).

### Remove

| Item | Value |
|---|---|
| Needs | Write access to the service |
| Confirmation | The service, its environment, mode and replicas, stack, published ports, and the tasks whose desired state is `running`, by state and by node. Button: `Remove service` |
| Effect | Every task stops and its container is removed, on every node. Volumes the tasks used are kept. The access control is deleted. This cannot be undone |
| Bound to | `expected_name` and `expected_version`. A difference is refused (`409`, reason `changed_since_review`) |
| Result | Redirect to the list with `Service removed.` |
| Recorded | `service_removed` |

### Images and registry credentials

A create, or an edit that changes the image, resolves the credentials for the image and pins it before anything is sent to Docker.

| Step | Behaviour |
|---|---|
| 1. Credentials | The set is chosen by the rules of [Which set a pull uses](Usage-Images#which-set-a-pull-uses). The create form and the review of an edit that changes the image show the [credential picker](Administration-Registries#the-credential-picker), which sends an explicit choice; with JavaScript, changing the image field fetches it without a page load (`POST …/swarm/services/new/choices`). Without a choice and with several fitting sets the form is rendered again with the picker (`400`). A deleted bound set is refused (`409`). A set the user or environment cannot use is refused (`409`). A standard user's choice that differs from the existing binding is refused (`400`, reason `restricted`) beside the image field: `Only an administrator can change which credential set this image uses.` |
| 2. Digest | dockmin asks the registry, with the same credentials, for the digest of the reference. The service is stored with the reference followed by `@sha256:` and the digest, so every node runs one image. The service page shows `Pinned to a digest` |
| 2. No digest | When the lookup fails, the service is created or updated with the image as typed. The page warns `The image could not be pinned to a digest, so nodes may run different images of this tag.` The row records `digest_pinned` as `false` |
| 2. Digest given | A reference that already names a digest is not looked up |
| 2. Image id | An image id is neither resolved nor pinned, and binds nothing |
| 3. Binding | The reference is bound to the set used, in the transaction that settles the service's access control ([Credential bindings](Usage-Images#credential-bindings)). The row records `binding` |

| Operation | Registry credentials sent to Docker |
|---|---|
| Create | A non-anonymous set is sent as the registry credential of the service. An anonymous create sends none |
| Edit that changes the image | Always sent explicitly. An anonymous pull sends the empty credential, so credentials stored with the earlier image are not reused for the new one |
| Edit that keeps the image | None. Swarm reuses the credentials stored with the service |
| Scale, force update | None. Swarm reuses the stored credentials |
| Roll back | None. Swarm uses the credentials stored with the previous configuration |
| Image update by `container-updater` | Always sent explicitly: the bound set's credential, or the empty credential for an anonymous binding |
| Credential renewal by `container-updater` | Always sent explicitly: the bound set's credential |

- Swarm keeps the credential with the service and hands it to the nodes that pull the image. Docker does not return it, and dockmin never shows it, stores it in a row, or writes it to a log.
- dockmin refreshes a credential stored with a service only through `container-updater`, and only for a service whose image is bound to an Amazon ECR credential set ([Credential renewal by container-updater](#credential-renewal-by-container-updater)). An Amazon ECR token is valid for at most 12 hours ([Amazon ECR tokens](Administration-Registries#amazon-ecr-tokens)). Without a renewal, a task scheduled onto a node that does not hold the image fails to pull once the token expires, until an edit changes the image, which sends the credential again.
- A service bound to any other provider's set is never renewed.
- Deleting a credential set does not remove the credential Swarm holds for a service.
- A binding is not written by scale, force update or roll back.
- A Swarm stack chooses credentials for each image reference of its file and sends them by the same header rules ([Swarm images and credentials](Usage-Stacks#swarm-images-and-credentials)).

### Update by container-updater

`container-updater` moves a service to a newer digest of the image it runs ([Swarm services](Operations-Automation#swarm-services)). No page sends this operation.

| Item | Value |
|---|---|
| Changes | Only the image: `reference:tag@sha256:…`, with the reference as the service has it and the digest the registry reported. Every other setting and the force-update counter are sent as read |
| Needs | The rule owner's write access to the service. The owner's grant on the bound credential set, for a standard user |
| Standard-user owner | The whole specification is checked against the [restrictions](#standard-user-restrictions). A refusal is `restricted` and nothing is sent |
| Lock | The service, action `update_image`, held while the rollout is watched |
| Bound to | The service's version and image reference as planned. A difference is `changed_since_review` and nothing is sent |
| Already at the digest | Not sent. No row |
| Watched | For `verify_seconds` and while the rollout runs, to 300 s ([Verification of a service update](Operations-Automation#verification-of-a-service-update)). A failure raises the reason on the row; the service is rolled back only after `update_paused` and `tasks_not_running` |
| Binding | Kept. Written after the watch; a failed rollout writes none |
| Recorded | `service_updated`, category `automation`, with the keys under [Recorded actions](#recorded-actions) |

### Credential renewal by container-updater

`container-updater` sends a service's own specification again with a fresh registry credential, for services whose image is bound to an Amazon ECR set ([Credential renewal for Swarm services](Operations-Automation#credential-renewal-for-swarm-services)). No page sends this operation.

| Item | Value |
|---|---|
| Sends | The service's specification unchanged, with its version and the bound set's credential in an explicit header |
| Effect | Docker stores the credential with the service. No running task is replaced. The next task placed on a node without the image pulls with the new credential |
| Previous configuration | Replaced by the current one |
| Lock | The service, action `refresh_credentials` |
| Refused | A rollout running or paused (`update_in_progress`); the image reference changed since planned (`changed_since_review`); a legacy top-level `Networks` setting that cannot be sent unchanged (`legacy_networks`) |
| Binding | Not written |
| Recorded | `service_credentials_refreshed`, category `automation` |

## Standard user restrictions

Administrators are not restricted. The restrictions apply to the final service specification sent to Docker on create, edit and roll back, and on an update or rollback by `container-updater` whose rule owner is a standard user. They are enforced on the specification, whatever the form offers.

A level of the specification has a fixed set of known keys, spelled exactly. A key outside the set is refused as `unknown_option` whatever its value, including a key spelled in another case. A `null` counts as absent. A specification as Docker returns it passes unchanged.

A refusal names the form field, or the specification path (`TaskTemplate.ContainerSpec.CapabilityAdd`), and reads `Only administrators may use {restricted use}.` For `unknown_option`: `This Docker option is not recognised, so only administrators may use it.` The answer is `400` and the row records the reason `restricted` and the option ids in `refused`. The option ids are those of [Options restricted to administrators](Usage-Containers#options-restricted-to-administrators), plus `swarm_volume_placement`. A refusal writes a failure row; an administrator's use of these options is recorded in `restricted`.

### Options refused

| Specification path | Option id | Standard user |
|---|---|---|
| `TaskTemplate.ContainerSpec.Mounts[].Type` `bind` | `bind_mount` | Refused |
| `TaskTemplate.ContainerSpec.Mounts[].Type` other than `volume`, `tmpfs`, `bind` (`npipe`, `cluster`, `image`, any other) | `mount_type` | Refused |
| `…Mounts[].Source` of a `volume` mount that is not a volume name | `bind_mount` | Refused |
| `…Mounts[].BindOptions` | `bind_mount` | Refused |
| `…Mounts[].VolumeOptions.DriverConfig` | `volume_driver` | Refused |
| `…Mounts[].Source` naming a volume, when the service is not pinned to the connected manager | `swarm_volume_placement` | Refused ([Named volumes](#named-volumes)) |
| An existing volume whose driver is not `local` or that has driver options | `host_backed_volume` | Refused |
| `…ContainerSpec.CapabilityAdd` | `cap_add` | Refused when not empty |
| `…ContainerSpec.Privileges.SELinuxContext`, `Seccomp` other than mode `default`, `AppArmor` other than mode `default` | `security_opt` | Refused |
| `…ContainerSpec.Privileges.CredentialSpec` | `unknown_option` | Refused |
| `…ContainerSpec.Sysctls` | `sysctls` | Refused when not empty |
| `…ContainerSpec.OomScoreAdj` | `oom_score_adj` | Refused when negative |
| `…ContainerSpec.Isolation` | `isolation` | `default` or absent only |
| `…ContainerSpec.Configs[].Runtime`, `Secrets[].Runtime` | `unknown_option` | Refused |
| `TaskTemplate.Resources.Reservations.GenericResources` | `devices` | Refused when not empty |
| `TaskTemplate.LogDriver` | `log_driver` | Driver `json-file`, `local` or `none`; options `max-size`, `max-file`, `compress`, `mode`, `max-buffer-size` |
| `TaskTemplate.Runtime` other than absent or `container`; `PluginSpec`; `NetworkAttachmentSpec` | `runtime` | Refused |
| `TaskTemplate.Networks[].DriverOpts` | `endpoint_driver_opts` | Refused when not empty |
| `TaskTemplate.Networks[].Target` naming a network with the `host` driver, or named `host` | `network_host` | Refused |
| Top-level `Networks` (the deprecated form) | `unknown_option` | Refused |
| Any key outside the known set of its level | `unknown_option` | Refused |

| Allowed for standard users |
|---|
| Mount types `volume` and `tmpfs`, with `ReadOnly`, `Consistency`, `VolumeOptions.NoCopy`, `VolumeOptions.Labels`, `VolumeOptions.Subpath`, and `TmpfsOptions.SizeBytes`, `Mode`, `Options` |
| `NoNewPrivileges`; `CapabilityDrop`, `Ulimits`, `Init`, `ReadOnly`, `Hosts`, `DNSConfig`, `Groups`, `User`, `Hostname`, `Dir`, `Env`, `Command`, `Args`, `Image`, `StopSignal`, `StopGracePeriod`, `Healthcheck`, `TTY`, `OpenStdin` |
| Limits `NanoCPUs`, `MemoryBytes`, `Pids` and reservations `NanoCPUs`, `MemoryBytes` |
| `Placement` (managers included), `RestartPolicy`, `ForceUpdate`, `Mode` (replicated, global, and the job modes), `UpdateConfig`, `RollbackConfig` |
| `EndpointSpec.Mode` (`vip`, `dnsrr`) and `EndpointSpec.Ports[]` in any publish mode |
| Labels, other than the reserved labels, which are refused for everyone |

- Scale and force update change only the replica count or the force counter and run no restriction check.
- A Swarm stack's services are checked by these rules on the specification as it is sent, together with the Compose-level rules of [Swarm stack restrictions for standard users](Usage-Stacks#swarm-stack-restrictions-for-standard-users).
- A roll back checks the previous configuration, which may hold settings the user could not set.
- The Swarm specification has no field for PID, IPC or UTS namespaces or device mappings. A key that tries to set one is refused as `unknown_option`.

### Stored values a standard user cannot edit

| Field | Standard user's form | Stored value |
|---|---|---|
| Added capabilities, Kernel parameters | Not offered, on create or edit. A posted value is refused as `cap_add` or `sysctls` | Shown read-only beneath the Security section, with `Set by an administrator`, and sent back unchanged |
| Log driver and log options | A choice of `json-file`, `local` and `none`, and the option names of [Options refused](#options-refused). A value outside them is refused as `log_driver` | When the stored driver or an option is outside the allowed set, shown read-only in the Logging section with `Set by an administrator`, and sent back unchanged |

- A standard user cannot edit a service whose stored specification carries added capabilities, kernel parameters, or a log driver or option outside the allowed set. The edit is refused as `restricted` (`400`) with the option ids `cap_add`, `sysctls` or `log_driver` in `refused`, and nothing is sent to Docker. The read-only display does not exempt the stored value.
- An administrator edits such a service without refusal. The row records the option ids in `restricted`.
- Scale and force update run no restriction check and work on such a service.

### Named volumes

A named volume resolves on the node that runs the task. dockmin can inspect volumes only on the manager it is connected to.

| Rule | Value |
|---|---|
| Pin | A standard user's service may mount a named volume only when its placement constraints include `node.id==<manager-node-id>`, the id of the connected manager. The form's `Run on this manager node` adds it. A constraint is accepted with spaces around the operator and any case in the attribute |
| Without the pin | Each named mount is refused as `swarm_volume_placement`, beside its source field |
| With the pin | Each named volume gets the container rules of [Volumes and networks](Usage-Containers#volumes-and-networks): a name that does not exist is accepted and Docker creates a `local` volume; an existing volume needs write access (read access when the mount is read-only) and is refused as `host_backed_volume` when its driver is not `local` or it has options |
| Not needing the pin | Anonymous volumes (blank source) and tmpfs mounts |
| No access | `This volume is not available.` |
| Unchanged service | An edit that changes neither the named mounts nor the placement constraints needs no pin. Volumes already mounted are not checked for access again |
| Swarm stack | The same pin and rules apply to each service of a stack's file, for project volumes and external volumes ([Swarm stack restrictions for standard users](Usage-Stacks#swarm-stack-restrictions-for-standard-users)) |

### Networks, configs and secrets

For a standard user, on create, edit and roll back:

| Reference | Rule | Message when not available |
|---|---|---|
| Network | Found by id (25 or 64 characters) and then by name. Needs write access. A network with the `host` driver is refused as `network_host`. Listed once | `This network is not available.` `This network is listed more than once.` |
| Config | Found by id and then by exact name. Needs read access | `This config is not available.` |
| Secret | Found by id and then by exact name. Needs write access | `This secret is not available.` |

- A missing object and an object the user may not see give the same message.
- The specification carries the ids that the check resolved, and for a config or secret its name as well. A reference cannot be re-pointed after the check.
- References the service already holds are not checked for access again. They are checked for the `host` driver.
- The networks offered by the form are the Swarm-scoped networks except `ingress`; for standard users, those they may write to, without the `host` driver. Configs: those they may read. Secrets: those they may write. Volumes: those they may read that use the `local` driver without options.

## dockmin's own service

When dockmin runs as a Swarm service, the service that runs its container, the Swarm stack that service belongs to, and the node its task runs on are protected ([dockmin's own container](Usage-Containers#dockmins-own-container)). dockmin matches its container to a task of the manager it is connected to, so the protection holds on a manager that does not run the container itself.

| Operation | Result |
|---|---|
| Edit (apply), scale, force update, roll back, remove, image update and credential renewal of the service | Refused for every role at the action: `409`, the page `Protected`: `This service runs the container dockmin runs in. It cannot be changed or removed from dockmin; restart dockmin from Administration > Instance.` Recorded as a failure with reason `self_protected`. The confirmations open |
| Swarm stack remove, deploy, redeploy that would change the service | Refused at the action for every role. `The stack {name} contains the container dockmin runs in. It cannot be stopped, removed or redeployed from dockmin; restart dockmin from Administration > Instance.` A line that keeps the service as it is does not refuse |
| Swarm stack export | Refused for every role, on the confirmation and on the download, when a service of the namespace is the one that runs the container: `The stack {name} contains the container dockmin runs in. It cannot be exported from dockmin.` Recorded as a failure with reason `self_protected` |
| Availability change to `drain` on the node that runs the container | Refused: `This node runs the container dockmin runs in. It cannot be drained from dockmin, because the container would be stopped; restart dockmin from Administration > Instance after moving it.` `pause` and `active` are allowed |
| Automations | Skipped with the reason `self_protected` ([Self-protection](Operations-Automation#self-protection)) |

Restart dockmin from [Instance](Administration-Instance).

## Tasks

A task is one scheduled instance of a service. Tasks are not controlled objects: a task is visible exactly when its service is.

| Item | Value |
|---|---|
| Tab | `Tasks` of the service page. Replicated services group tasks by slot (`Slot N`, `No slot`); global services group them by node host name (`Unassigned` before placement) |
| Summary line | `{shown} of {total} tasks, newest first`, with `Some are still changing state; this list updates itself` while any task is moving, and a `Refresh` link. `This service has no tasks.` when there are none |
| Order | Newest first by Docker's version of the task, not by timestamp |
| Columns | Task (short id, `Container` link), State, Desired, Node, Updated |
| History | What Docker keeps, at most 500 tasks shown: `Only the newest N tasks are shown.` |
| Refresh | The tab refreshes itself every 5 seconds while any task's state differs from its desired state and is not final. The `Refresh` link does the same without JavaScript; the page also reloads every 10 seconds then |
| Task page | `/tasks/{task_id}`: id, service, slot (`None (global service)` for a global service), state and desired state, message, error, exit code, node, container, created, updated |
| Node | The host name. A link to the node for administrators. `Not placed` on the tab and `Not placed on a node yet` on the task page before scheduling |
| Container link | Only when the task ran on the manager dockmin is connected to and the user may see the container. Otherwise the task page shows the 12-character container id with `on another node or not visible to you` |
| Exit code | Set for a task in a final state. The Tasks tab shows a non-zero code (`Exit code N`); the task page shows the code |

| State | Badge |
|---|---|
| `running` | ok |
| `complete`, `shutdown`, `remove` | neutral |
| `failed`, `rejected` | danger |
| `orphaned` | warning |
| `new`, `pending`, `allocated`, `assigned`, `accepted`, `preparing`, `ready`, `starting` | info |

Final states are `complete`, `shutdown`, `failed`, `rejected`, `remove` and `orphaned`.

Task and update messages are the only Docker text shown. They explain a task that cannot be scheduled (`no suitable node`) or started (a failed pull).

| Handling | Value |
|---|---|
| Cleaning | Control characters removed; text that looks like a secret is scrubbed |
| Length | Cut at 512 characters with `…` |
| Display | Escaped. Never written to a row or a log line |
| Shown in | The Tasks tab (`message`, `error`), the task page, the service's last update message |

## Service logs

The Logs tab shows the output of every task of a service, merged. It needs read access to the service. Reading logs records no activity row. Selection, time values, line handling, view limits and the download follow [Logs](Usage-Containers#logs) of containers, with the differences below.

| Item | Service logs |
|---|---|
| Line prefix | The task that wrote the line: `{service}.{slot}.{task}` for a replicated service, `{service}.{node host name}.{task}` for a global one. `{task}` is the first 12 characters of the task id. Characters outside letters, digits, `_`, `.` and `-` in a name become `_`. A task that is not known by id shows its 12-character id; a line with no task shows `-` |
| Source of the task | Docker's own attributes on the line. Attributes set by the service's log options are ignored |
| Newest lines (`tail`) | 0 to 5000. Default 200. The download also accepts `all`. Without JavaScript the tab reads at most 1000 lines |
| Message when the service stops while following | `The service stopped.` |
| Follow | Off and disabled when the service has no desired tasks |
| Values over the maximum | The stream and the download answer `400`: `Choose a number of lines from 0 to 5000.` |

| Mode | `tail` applies | Order |
|---|---|---|
| Live stream | Docker applies it to each task, so the stream opens with up to `tail` lines from every task | As the lines arrive; not ordered across tasks |
| Recent lines without JavaScript | The newest `tail` lines across all tasks | By timestamp. At most 1 MiB; older lines are left out and the page says so. Reads for at most 30 seconds |
| Download with a tail | The newest `tail` lines across all tasks, at most 32 MiB | By timestamp |
| Download without a tail | Every line, one task after another | Per task |

- Docker returns the lines of tasks that have ended as well as running ones, so the number of lines read can be larger than `tail` before the newest are chosen.
- The download is capped at 100,000 lines, 32 MiB and 120 seconds. A file that ends with `[output stopped at the …]` is incomplete. The filename is `{name}-{12-character id}-{UTC time}.log`. Each line is `[stdout|stderr ][timestamp ]{task} text`.
- The download takes `format`: `text` (default) or `ndjson`. With `ndjson` the media type is `application/x-ndjson`, the filename ends `.ndjson`, and each line is a service record: the common keys and the service keys ([NDJSON download](Usage-Containers#ndjson-download)). The `Download NDJSON` link carries the current selection with `tail=all`. Any other value answers `400`: `Choose text or ndjson.`
- The services of a Swarm stack are read together on the stack's Logs tab ([Swarm stack logs](Usage-Stacks#swarm-stack-logs)).
- A stream lasts 300 seconds, then asks the browser to reconnect after 3 seconds. A quiet stream sends a comment line every 15 seconds.
- Views share the pools of [view limits](Usage-Containers#view-limits): 4 per user, 64 per process (`Too many live views are open`, `429`).
- A log driver Docker cannot read back answers `503`: `Logs are not available`.

### Resume

| Item | Behaviour |
|---|---|
| Event id | The highest timestamp delivered so far, in nanoseconds |
| Reconnect | The browser sends the id (`Last-Event-ID` or `after`). The stream continues after that timestamp and ignores `tail` |
| Duplicates | A line already delivered is not repeated |
| Late lines | A line stamped at or before the resume point that reaches Docker after the reconnect is not delivered by the live view. The recent lines and the download are read by time and are not affected |

## Nodes

Nodes are cluster infrastructure. The node list and the node page are for administrators; any other role receives `403`.

| Item | Value |
|---|---|
| List columns | Node (short id, `This manager` badge), Role, Availability, State, Engine, Address |
| Role | `Manager` or `Worker`, with `Leader` or the manager's reachability |
| Order | Host name, ignoring case, then id |
| Node page | Id, role, state, availability, address, engine version, platform, CPUs, memory, node labels, engine labels, the tasks placed on the node (service, slot, task, state, desired; newest first, at most 500, then `and N older tasks not listed.`) |
| Not managed | Promotion, demotion, labels, removal. Join tokens and the unlock key are never shown, stored or logged |

### Availability

| Availability | Meaning |
|---|---|
| `active` | The node receives new tasks |
| `pause` | The node keeps its running tasks and receives no new ones |
| `drain` | The node's tasks are moved to other nodes |

1. Open the node page. Under `Availability`, choose `Set active`, `Pause…` or `Drain…`.
2. For `Pause…` and `Drain…`, read the confirmation. `Set active` posts at once.
3. Choose `Pause node` or `Drain node`. The node page opens with the notice `Availability changed.` Swarm reschedules; the Tasks tab of each service and the node page show the result.

| Change | Confirmation |
|---|---|
| To `active` | None. A `POST` with the CSRF token. Running tasks are not moved back |
| To `pause` | The node, its role, the other active nodes, and the number of running tasks that keep running. Tasks that stop are replaced elsewhere |
| To `drain` | The same, with the running tasks by service (at most 50 services, then `and N more services`; `service` for one), and the number of tasks that are stopped and replaced elsewhere. When no other node is active: `No other node is active, so the replacements stay pending until one is.` |

| Rule | Value |
|---|---|
| Bound to | `expected_version` and `expected_availability`. A difference is refused: `This node changed since you reviewed it; nothing was changed.` (`409`) |
| Same availability | `The node already had this availability.` No row is recorded |
| Effect on tasks | Swarm reschedules; dockmin removes nothing. The Tasks tab and the node page show the result |
| Recorded | `node_availability_changed` |

## Configs

A config is a named piece of content that services read as a file. It is not secret and its content is shown.

| Item | Value |
|---|---|
| List columns | Name (with short id), Stack, Created, Actions (`Details`; `Remove` for users with write access) |
| List filters | `q` name contains (at most 128 characters, case-insensitive), `stack` (at most 128 characters, case-insensitive exact). Page size 25 by default; `limit` 1 to 500 |
| List sorting | The Name, Stack and Created headers sort the list ([Sorting configs and secrets](#sorting-configs-and-secrets)). Default `name` ascending, then id |
| Config page | Name, id, created, updated, stack, the services that use it (`Used by`), content, labels. Tab `Access` for administrators |
| Content shown | The first 64 KiB, when the content is UTF-8 without control characters other than tab, line feed and carriage return (`Only the start is shown.` beyond that). Otherwise `Binary content, {size}. It is not shown.` and nothing is shown |
| Immutable | A config cannot be changed after it is created; create a new one to replace it. Labels cannot be edited |
| Templating | Not offered. A config templated by Docker shows its stored text |

### Sorting configs and secrets

The rules of [Sorting](Usage-Containers#sorting) apply to both lists: `sort` and `dir`, the header links, the cursor, and the `400` answers (`Sort by one of the listed columns.`, `Choose ascending or descending.`, `The page link is not valid.`).

| Column | `sort` | Orders by | Empty value |
|---|---|---|---|
| Name | `name` | Name, ignoring case | None |
| Stack | `stack` | Stack name, ignoring case | An object with no stack |
| Created | `created` | Creation time | A time that cannot be read |

Default order: `name` ascending, then id. Empty values come first ascending and last descending. Filters apply before the sort.

### Create a config

1. Open `Create config` on the config list.
2. Fill `Name`. Type or paste the content, or choose a file under `Or choose a file`, not both. Add labels as needed.
3. Choose `Create config`. The config page opens.

| Field | Rule |
|---|---|
| Name | 1 to 64 letters, digits, `-`, `_` and `.`, starting and ending with a letter or digit. Not 25 lower-case letters and digits. Unique: `A config with this name already exists.` (`409`) |
| Content | Typed text or one uploaded file, not both: `Enter the content or choose a file, not both.` At least 1 byte and at most 128 KiB: `Content must be at most 128 KiB.` (`413`). Typed text has `CRLF` turned into `LF`; an uploaded file is kept byte for byte |
| Labels | At most 64 rows; keys at most 255 characters without control characters and unique; values without NUL; the stack labels are refused; together at most 64 KiB |

- Control: [Ownership](#ownership).
- Messages never quote the input. A refused create shows the form with the content kept.

### Remove a config

| Item | Value |
|---|---|
| Needs | Write access to the config |
| Confirmation | The config, and the services that use it: the ones the user may see (at most 10, then a count) and a count of the others. Button: `Remove config` |
| In use | Refused (`409`, reason `config_in_use`) with no call to Docker. The confirmation has no button. There is no force |
| Bound to | `expected_name` and `expected_in_use` (the number of services using it). A difference is refused (`409`, reason `changed_since_review`) |
| Lock | The config is held for the removal ([Locks and busy](#locks-and-busy)). The use is counted again under the lock |
| Access control | Deleted with the config |
| Recorded | `config_removed` |

## Secrets

A secret is a named value that services read as a file. Its value is write-only.

| Item | Value |
|---|---|
| Value | Never stored, logged, echoed or rendered by dockmin, administrators included. No route returns it. Docker does not return it either |
| Stored by | Swarm, in its encrypted cluster store |
| List and page | Name, id, created, updated, stack, the services that use it, labels. No value, size or hash |
| List sorting | As for configs ([Sorting configs and secrets](#sorting-configs-and-secrets)) |
| Immutable | A secret cannot be changed after it is created. Labels cannot be edited |
| Drivers, templating | Not offered |
| Used in a service | As a file in the service's containers. Attaching one needs write access to it. A user with write access to a service can read the secrets already attached to it ([Access](#access)) |

### Create a secret

1. Open `Create secret` on the secret list.
2. Fill `Name`. Type or paste the value in the masked text area, or choose a file, not both. Add labels as needed.
3. Choose `Create secret`. The secret page opens.
4. When the create is refused, the form returns with the value field empty and the line `Enter the value again.` Type the value again.

| Field | Rule |
|---|---|
| Name | The rule of [config names](#create-a-config). Unique: `A secret with this name already exists.` (`409`) |
| Value | Typed in a masked text area, or one uploaded file, not both. At least 1 byte and at most 64 KiB: `The value must be at most 64 KiB.` (`413`). Typed text has `CRLF` turned into `LF` |
| Labels | As for a config |

- A refused create shows the form with the value field empty and no hidden field carrying it. Enter the value again. Messages never quote the input.
- The value is held in memory from the form to the request to Docker and dropped. It is never in a log line, a database row, an exception message or a page.
- The row holds the name and the number of labels. It records no size, hash or prefix of the value.

### Remove a secret

As for [a config](#remove-a-config), with the reason `secret_in_use`, the action `secret_removed`, the button `Remove secret`, and the removal of the value from the cluster: `Its value is gone with it.`

## Reconstruct a stack from the cluster

`Reconstruct from the cluster` on the Adopt page of a namespace that is not managed writes the stack's file from the specifications of its services, networks, configs and secrets. Nothing is stored and nothing on the cluster changes. The adoption pages are in [Adopt a Swarm stack](Usage-Stacks#adopt-a-swarm-stack).

| Item | Value |
|---|---|
| Who | Administrators. A standard user receives `404` on the reconstruct and adopt-as-is routes, before any read |
| Environment | A Swarm manager ([Environments](#environments)) |
| Read | The specification of each service, config, secret and network carrying the namespace label (compared case-insensitively), the content of each config, and each network a service attaches to, whether or not it carries the label |
| Lock | The stack's lock, action `read` ([Swarm stack locks](Usage-Stacks#swarm-stack-locks)) |
| Recorded | One `stack_files_read` row for each reconstruction, with `kind` `swarm` and `file_source` `reconstructed` ([Swarm stack activity](Usage-Stacks#swarm-stack-activity)) |
| Result | The page and the held result of [The result page](Usage-Stacks#the-result-page), with `Reconstruct from the cluster` and `Adopt as is` in place of `Read from host` |

| Object | Written as |
|---|---|
| Service | `services.{key}`. `{key}` is the service name without the `{namespace}_` prefix of its own label's spelling. Every field of the specification is written as the Compose key that the [translation of the Compose file](Usage-Stacks#translation-of-the-compose-file) reads back |
| Network carrying the namespace label | `networks.{key}`, the name without the prefix. Driver options, labels and address pools are written as the cluster holds them |
| Any other network | `external: true`, with `name` where it differs from the key |
| Volume a service mounts with the namespace label in its options | `volumes.{key}`, the name without the prefix, with its labels and driver options |
| Any other volume | `external: true` |
| Config carrying the namespace label | `configs.{key}` with `file: ./config-{key}`. `{key}` drops the content digest suffix of the name. The content is stored as a plain companion file named `config-{key}` (`-{N}` on a collision) |
| Any other config | `external: true` |
| Secret | `secrets.{key}` with `external: true` and `name` set to the secret's full name. `{key}` drops a trailing 12-character token. A secret's value cannot be read and is not stored |
| Environment values | A value whose name matches the [secret names](Usage-Stacks#environment-file) becomes a secret stack variable, written `${NAME}`, under the naming rules of the [generated file](Usage-Stacks#generated-file) |
| Image references | Written without their digest pin |
| `$` | A literal `$` in any written value is written `$$` |

Left out because the Engine fills them in: default isolation, the container runtime, placement platforms and the force-update counter. Losses listed on the result page:

| Loss |
|---|
| Comments, anchors and the original layout |
| Image digests |
| Fields the Engine fills in |
| Secret values; secrets are references to the existing objects |
| Network address pools, written as the cluster holds them |

On the first redeploy after `Adopt as is`, each config is created as `{stack}_{key}_{digest}`, the digest being the first 12 hexadecimal digits of the SHA-256 of its content. A namespace deployed by dockmin already holds these names and keeps them. A namespace deployed by the Docker command line holds names with no suffix: the services are pointed at the new names and the old configs are removed once no service uses them. Secrets are kept by name and their values stay in the cluster.

### When a file is needed

The result page shows the reason and the path. The paste form stays available.

| Reason | Cause |
|---|---|
| `no_translator_row` | A field of a service, network or config has a value and no Compose equivalent. The path names the Engine field, such as `services.web.TaskTemplate.Placement`. A zero or empty value is not a field. Job modes and plugin runtimes are refused this way, as are a network driver other than `overlay`, an ingress network, a network's `ConfigFrom` and IPAM options, and a config that uses templating |
| `no_services` | No service carries the namespace label |
| `name_without_namespace` | A service name does not start with the stack name |
| `network_unresolved` | A network a service attaches to cannot be found |
| `config_unreadable` | A config's content cannot be read or is empty |
| `image_missing` | A service has no image |
| `duration_out_of_range` | A duration the translation would refuse |
| `secret_value_unusable` | An environment value with a secret name holds several lines or is over 8 KiB. The value is never shown |
| `inspect_mismatch` | The cluster answered for another object |
| `invalid`, `too_large`, `refused_construct`, `invalid_compose` | The text, a config file or a variable fails the [Swarm stack checks](Usage-Stacks#swarm-stack-checks), for example a config that is not UTF-8 text |

- An environment entry with no value, or a name set twice, is `no_translator_row`.

## Export (Swarm stacks)

Export downloads a managed Swarm stack as one gzip archive of its file, variables, companion files, build contexts and the data of the volumes chosen on the manager. Administrators only. Secret values are sealed under a passphrase or left out. Everything else in the archive is plain text.

1. Open `Export` on the stack page.
2. Read the confirmation.
3. Choose to seal the secret values or leave them out, enter the passphrase twice when sealing, and tick the volumes whose data goes into the archive.
4. Choose `Download archive`. The browser saves `stack-{namespace}-{YYYYMMDDTHHMMSSZ}.tar.gz`.

| Item | Value |
|---|---|
| Who | Administrators. A standard user receives `404` on both routes and nothing is recorded |
| Environment | A Swarm manager ([Environments](#environments)). Any other environment answers `404` |
| Stack | A managed Swarm stack. A name that folds to its name reaches it. A namespace that is not managed answers `404`; [adopt](Usage-Stacks#adopt-a-swarm-stack) it first. dockmin's own stack is refused |
| Archive | The layout, manifest, limits and format version of [Export](Usage-Stacks#export-archive), with `kind` `swarm_stack`, `stack.stack_kind` `swarm` and `description` `Swarm stack {name}` |
| Secret values | The secret variables and secret companion files that dockmin stores, sealed or left out ([Export secret values](Usage-Stacks#export-secret-values)). The cluster's configs and secrets are not read, and Swarm secret values that exist only in the cluster are not exported |
| Build contexts | Included when the stack's record holds any ([Export archive](Usage-Stacks#export-archive)) |
| Volumes | Chosen one by one. Only volumes visible on the manager are offered ([Volumes](#export-volumes)) |
| Running tasks | Nothing is stopped. The confirmation names the running containers visible on the manager and states that volume data can be inconsistent |
| Writes | Nothing on the cluster. One helper container on the manager for each chosen volume, created and removed. Nothing on dockmin's disk |
| Output | `application/gzip`, `Content-Disposition: attachment`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` |
| Lock | The stack by its folded name and each chosen volume, from the confirmed download until it ends ([Locks and busy](#locks-and-busy)) |
| Concurrency | One export at a time on an environment, two in all, shared with volume, Compose stack and container exports ([Export limits](Usage-Stacks#export-limits)) |
| Import | [Import (Swarm stacks)](#import-swarm-stacks) |
| Recorded | `stack_export_started` and `stack_exported`, with `stack_kind` `swarm` ([Activity](Administration-Activity#stack-export-rows)) |

### Export confirmation (Swarm stacks)

`GET /environments/{id}/swarm/stacks/{namespace}/export`. Nothing is created, locked or recorded. The elements are those of the [Compose confirmation](Usage-Stacks#export-confirmation), with these differences:

| Element | Text |
|---|---|
| Heading | `Export swarm stack {name} on {environment}?` |
| `Left out` | The Compose list, and `The values of Swarm secrets that exist only in the cluster: Docker does not return them, so only secret variables and secret files that dockmin stores can travel` |
| Alert `The stack is running` | The Compose text, ending `Nothing is stopped. Only the tasks on this manager node are visible here.` |
| Line when none runs | `No running container of this stack is visible. Tasks on other nodes cannot be seen from the manager.` |
| No volume | `No volume of this stack is visible on the host. A Swarm stack's volumes are created on the nodes that run its tasks; only the manager's are visible here.` |

#### Export volumes

| Item | Value |
|---|---|
| Offered | Volumes on the manager that carry the stack's namespace label, and named volumes mounted by the stack's task containers that are visible on the manager. At most 64; more are counted and cannot be chosen |
| Other nodes | A volume that exists only on another node is not offered and is not exported |
| Not selectable, helper, size, consistency | As [Export volumes](Usage-Stacks#export-volumes) |
| Running containers | The stack's containers visible on the manager, in state `running`, `paused` or `restarting` |

### Export refusals (Swarm stacks)

The refusals, statuses, titles and messages are those of [Export refusals](Usage-Stacks#export-refusals), with these differences:

| Condition | Status | Message | Reason |
|---|---|---|---|
| The environment is not a Swarm manager the user can see | 404 | Not found | No row |
| A service of the namespace is the one that runs the container dockmin runs in | 409, page `Protected` | `The stack {name} contains the container dockmin runs in. It cannot be exported from dockmin.` | `self_protected` |
| Another action holds the stack, by its folded name, or a chosen volume | 409, page `This stack is busy` | `{Who} started a {action} on it {N} seconds ago and it has not finished. Wait for it to complete, then try again.` The action is `export` | `object_busy` |
| The manager is no longer an active Swarm manager, or its swarm is locked | 503 | `This environment is not an active Swarm manager, or its swarm is locked.` | The host failure reason |

## Import (Swarm stacks)

Import creates one managed Swarm stack from a stack export archive, creates and fills its volumes on the manager, and starts its first deployment. Administrators only. The pages, checks, review, passphrase, host paths, credential choices, confirmation, failure behaviour and limits are those of [Import (Compose)](Usage-Stacks#import-compose), with the differences below.

1. Open `Import stack` on the Swarm stack list.
2. Choose the archive and `Upload and check`. Nothing is created.
3. Open the sealed values with the passphrase, or enter each secret value, when the archive holds secrets.
4. Read the review. Change the name if a name is in use, decide each host path, and choose a credential set for each image, then choose `Check the choices`.
5. Choose `Import stack`. The stack, its volumes and their data are created, and the progress page of the first deployment opens.

| Item | Value |
|---|---|
| Who | Administrators. A standard user receives `404` on every import route, before a request body is read, and nothing is recorded |
| Environment | A Swarm manager ([Environments](#environments)). Any other environment answers `404` |
| Accepts | An archive whose manifest `kind` is `swarm_stack`. A Compose stack archive is refused at upload: `The archive holds a Compose stack. Import it from the Compose stacks list.` A container archive is refused: `The archive holds a container. Import it from the Containers list.` A Swarm stack imports only into a Swarm environment |
| Pages | [Import pages (Swarm stacks)](#import-pages-swarm-stacks) |
| Objects | Services, networks, configs and secrets of the stack, and its volumes ([Import review (Swarm stacks)](#import-review-swarm-stacks)) |
| Secret values | The secret variables and secret companion files that were exported. Swarm secrets that existed only in the cluster are not in the archive |
| Volumes | Created and filled on the manager the environment connects to ([Import volumes (Swarm stacks)](#import-volumes-swarm-stacks)) |
| Deploy | Starts as a stack job on the Swarm routes, through the Engine ([Swarm stack deployment](Usage-Stacks#deploy-and-redeploy-a-swarm-stack)) |
| Owner | The importing administrator. No team or other grant is carried |
| Upload, validation, hold, limits | As [Import (Compose)](Usage-Stacks#import-compose): 7 GiB as uploaded, 3,600 seconds, held 30 minutes |
| Recorded | `stack_imported` with `stack_kind` `swarm`, then `stack_deployed` ([Activity](Administration-Activity#stack-import-rows)) |

### Import pages (Swarm stacks)

All paths are under `/environments/{id}/swarm/stacks`.

| Page | Path | Method |
|---|---|---|
| Upload form | `/-/import` | `GET` |
| Upload | `/-/import` | `POST` |
| Review | `/-/import/{upload_id}` | `GET` |
| Check the choices, import | `/-/import/{upload_id}` | `POST` |
| Open the sealed values | `/-/import/{upload_id}/unlock` | `POST` |
| Discard the upload | `/-/import/{upload_id}/cancel` | `POST` |

- `{upload_id}` is 32 lowercase hexadecimal characters. Any other form of at most 64 characters answers `404`; a longer one answers `422`.
- `Import stack` sits beside `Create stack` on the Swarm stack list, for administrators.
- The texts of [Import (Compose)](Usage-Stacks#import-pages) apply, with `Swarm stack` for `Compose stack`. The upload page reads `The archive must hold a Swarm stack written by a stack export.`
- The upload route's body cap and deadline are those of the Compose route ([Import limits](Usage-Stacks#import-limits)).
- The progress page after an import is `/environments/{id}/swarm/stacks/-/jobs/{job_id}`.

### Import review (Swarm stacks)

| Section | Difference |
|---|---|
| `The archive` | Kind `Swarm stack` |
| `Objects it creates` | `service`, `network`, `config`, `secret`. A config or secret reads `named when it is deployed` in `Note`. A network that is `external` is not listed |
| `Name` | Checked against the [Compose stack name](Usage-Stacks#stack-names) grammar, lower case only. A name that breaks it, including an archived name with upper case letters, is shown with the message beside `Name`, and `Import stack` is refused until the name follows it |
| Collisions | `stack`, `volume`, `network`, `service`, `config`, `secret`. Texts: `A service with this name already exists and does not belong to this stack.` and the same for `config` and `secret`. A Swarm namespace, a Compose project and a stack of either kind with the name in any case is a `stack` collision. |
| Images | One picker for each image the file names ([Swarm images and credentials](Usage-Stacks#swarm-images-and-credentials)) |
| Host paths | The same choices, with the text of the Compose review. The import creates no directory |
| Digest | `On this host` reports the digest the manager holds for the reference |

### Import volumes (Swarm stacks)

| Item | Value |
|---|---|
| Where | On the manager the environment connects to. A Swarm service whose task runs on another node does not see the volume |
| Created with | Driver `local` and the label `com.docker.stack.namespace` set to the stack name. Archived labels and driver options are not applied |
| Written | Before the stack record is stored, through a helper container on the manager ([Import volumes](Usage-Stacks#import-volumes)) |
| Matching, data rules, helper | As [Import volumes](Usage-Stacks#import-volumes) |
| First deployment | The [existing-object check of a Swarm stack](Usage-Stacks#swarm-objects-that-already-exist) accepts a volume that exists only when it is one this import created, with the creation time the manager reported then. The binding ends when the first deployment succeeds, when the stack is removed or forgotten, and when the import fails |
| After a restart before the first deployment | The binding is gone. A volume is accepted when its `com.docker.stack.namespace` label equals the stack name in any case and it has no access control of its own |

### Import refusals (Swarm stacks)

The refusals, statuses, pages, messages and reasons are those of [Import refusals](Usage-Stacks#import-refusals), with these differences:

| Condition | Status | Message |
|---|---|---|
| The environment is not a Swarm manager the user can see | 404 | Not found. No row |
| The manager is no longer an active Swarm manager, or its swarm is locked | 503 | `This environment is not an active Swarm manager, or its swarm is locked.` |

## Locks and busy

One action runs on an object at a time. A second is refused at once; it does not wait.

| Action | Held |
|---|---|
| Edit (apply), scale, force update, roll back, remove of a service. Also the image update and credential renewal of `container-updater` | The service |
| Remove of a config or secret | That config or secret |
| Availability change | The node |
| Create | Nothing. Docker answers a duplicate name with a conflict (`409`, reason `name_in_use`) |
| Create, save, deploy, redeploy, remove, forget, adopt and reconstruct of a Swarm stack | The stack, and for deploy, redeploy and remove each existing service, config and secret they touch ([Swarm stack locks](Usage-Stacks#swarm-stack-locks)) |
| Export of a Swarm stack | The stack, and each volume chosen ([Swarm stack locks](Usage-Stacks#swarm-stack-locks)) |
| Review, confirmations, reads | Nothing |

Message: `{Who} started a {action} on it {N} seconds ago and it has not finished. Wait for it to complete, then try again.` (`409`, title `This object is busy`, reason `object_busy`). `{Who}` is `You`, `Another user` or, for administrators, the holder's name. The action is `update`, `scale`, `force_update`, `rollback`, `remove`, `availability`, or, for `container-updater`, `update_image` or `refresh_credentials`. The holders follow the table in [Busy](Usage-Containers#busy).

A service, config or secret held by a stack operation is busy on its own page for the operation's length.

A config or secret removed while a service create references it is refused by Docker and reported as `config_gone` or `secret_gone`: `A config this service uses no longer exists.` or `A secret this service uses no longer exists.`

## Errors

| Status | Cause |
|---|---|
| 400 | A field broke its rule; a restricted option; a credential set must be chosen; a malformed confirmation (`The confirmation was incomplete. Review it again.`) |
| 403 | The user may read the object but not change it; a standard user on a node, an Access tab or a local socket write |
| 404 | The environment is not a Swarm manager the user can see; an object that does not exist, is hidden, or has a malformed id; an unknown tab |
| 409 | Busy; changed since the review; a name in use; not replicated; no previous configuration; a config or secret in use; a credential set that cannot be used |
| 413 | A config content or secret value over its limit; a form over 256 KiB |
| 422 | A `limit` outside 1 to 500; a path segment over 128 characters |
| 429 | No log view is free |
| 502 | Docker refused the request (`Docker refused this service configuration.`, `Docker refused this config.`, `Docker refused this secret.`, `Docker refused the change to this node.`) |
| 503 | The host could not be reached, answered in an unexpected way, did not answer in time, is not an active Swarm manager, or reported more data than dockmin reads |

| Title on the problem page | Status |
|---|---|
| `This object is busy` | 409 |
| `It changed since you reviewed it` | 409 |
| `The name is already used` | 409 |
| `This service cannot be scaled` | 409 |
| `There is nothing to roll back to` | 409 |
| `It is in use` | 409 |
| `The credential set cannot be used` | 409 |
| `Protected` | 409 |
| `Choose a credential set` | 400 |
| `Check the request` | 400 |
| `It no longer exists` | 404 |
| `Docker refused the action` | 502 |
| `The environment is not reachable` | 503. The message is followed by `Check that the Docker host is running and reachable, then try again.` |
| `Not available right now` | 503. Any other failure to read the host |

| Host message (`503`) | Cause |
|---|---|
| This environment is not an active Swarm manager, or its swarm is locked. | A Swarm call answered `503` |
| The Docker host could not be reached. and the other messages of [Host failures](Usage-Containers#host-failures) | As for any host failure |
| This host reports more Swarm data than can be listed here. | The task list exceeds 16 MiB or the node list exceeds 4 MiB |
| This host reports more service data than can be listed here (limit 16 MiB). | The service list exceeds 16 MiB |
| This host reports more config data than can be listed here (limit 32 MiB). | The config list exceeds 32 MiB |
| This host reports more secret data than can be listed here (limit 16 MiB). | The secret list exceeds 16 MiB |
| The Docker host did not answer in time. The action may still complete. | The result of a create, edit, scale or removal is not known. Check the object before submitting again |

- Docker's own error text is shown to administrators only, under the message, in a block headed `Docker said`, except task and update messages ([Tasks](#tasks)), which are shown to everyone who may read them. The text is scrubbed, reduced to one line and cut to 300 characters; it appears only when the Engine refused with status `400` or `404` and sent a message; it is not written to any log line or activity row. The block appears on the service create and edit forms and on the problem page of a `Docker refused the action` answer (service, config, secret and node actions).
- An object that cannot be read in full is never shown in part.

## Recorded actions

Every Swarm mutation records one activity row, for success and for failure, with the object as the target and the environment. Category `user_action`, except the rows `container-updater` writes, which are `automation`: the actor is the subsystem, and the row carries `rule_id`, `rule_name` and the run. A failure leaves a row with `reason` and `message`.

| Action | Target | Written by |
|---|---|---|
| `service_created` | The service id; none for a failure | Create |
| `service_updated` | The service id | Apply an edit. None when nothing changed. Also `container-updater`: an image update |
| `service_scaled` | The service id | Scale. None when the count did not change |
| `service_force_updated` | The service id | Force update |
| `service_rolled_back` | The service id | Roll back. Also `container-updater`, after a paused update |
| `service_credentials_refreshed` | The service id | `container-updater`: a renewal of the credential stored with the service |
| `service_removed` | The service id | Remove |
| `node_availability_changed` | The node id | Availability change. None when the availability did not change |
| `config_created`, `config_removed` | The config id; none for a failed create | Create, remove |
| `secret_created`, `secret_removed` | The secret id; none for a failed create | Create, remove |

Access tab changes and image bindings are recorded as for other objects ([Recorded actions](Usage-Images#recorded-actions), [Swarm stack activity](Usage-Stacks#swarm-stack-activity)). Swarm stack operations record the `stack_*` actions with `kind` set to `swarm`.

| Detail key | Present on |
|---|---|
| `name` | Every service action; `config_created`, `config_removed`, `secret_created`, `secret_removed` |
| `mode`, `replicas` | `service_created`, `service_removed` |
| `image`, `digest_pinned` | `service_created`. `digest_pinned` also on `service_updated` |
| `networks`, `ports`, `mounts`, `configs`, `secrets`, `constraints`, `labels` | `service_created`: counts |
| `pinned_to_manager` | `service_created`: whether the constraints pin every task to the connected manager |
| `credential_set_id`, `anonymous`, `credential_source` | `service_created`, `service_updated`: the set used (`null` for none), whether the pull was anonymous, and how it was chosen: `choice`, `binding`, `only_match` or `no_match`. Absent for an image id. `service_credentials_refreshed` carries `credential_set_id` |
| `binding` | `service_created`, `service_updated` that changed the image: `created`, `rebound`, `kept`, `skipped`, or `not_recorded`. `container-updater` records `kept` |
| `version_before` | `service_updated`, `service_rolled_back`, `service_credentials_refreshed` |
| `changed` | `service_updated`, `service_rolled_back`: the changed categories, comma-separated (`image`, `mode`, `command`, `environment`, `labels`, `placement`, `resources`, `restart_policy`, `update_config`, `rollback_config`, `networks`, `ports`, `mounts`, `configs`, `secrets`, `healthcheck`, `process`, `host`, `security`, `ulimits`, `logging`, `other`). Values are never recorded |
| `image_changed` | `service_updated` |
| `image_before`, `image_after` | `service_rolled_back`; `service_updated` by `container-updater`: the image text before and after, cut to 512 characters |
| `digest_before`, `digest_after` | `service_updated` by `container-updater`: the digest in the image before, and the registry's digest that was sent |
| `trigger` | By `container-updater`: `update` on `service_updated` and `service_rolled_back`; `refresh` on `service_credentials_refreshed` |
| `reference` | By `container-updater`: the image reference as the service has it, cut to 400 characters (255 on a renewal) |
| `failure_action`, `verify_seconds` | `service_updated` by `container-updater`: the service's failure action (`pause` when unset) and the rule's watch window |
| `tasks_running_before`, `tasks_running_after` | `service_updated` by `container-updater`: running tasks before the update and at the last count after it |
| `update_state`, `verified`, `verify_failure` | `service_updated` by `container-updater`: the last update state read (`updating`, `paused`, `completed`, `rollback_started`, `rollback_paused`, `rollback_completed`, or `other`), whether the watch passed, and why it did not. A successful row can carry `verify_failure` `still_updating` or `not_observed` with `verified` false |
| `rollback_reason`, `failed_digest` | `service_rolled_back` by `container-updater`: `update_paused` or `tasks_not_running`; the digest that failed |
| `provider`, `stack` | `service_credentials_refreshed`: `ecr`; the stack's namespace when the service has one |
| `rule_id`, `rule_name` | Every row `container-updater` writes |
| `replicas_before`, `replicas_after` | `service_scaled`: the replica counts before and after |
| `force_update_before`, `force_update_after`, `tasks_running` | `service_force_updated`: the counter before and after, and the running tasks at the time |
| `tasks` | `service_removed`: the running tasks at the time |
| `restricted` | `service_created`, `service_updated`, `service_rolled_back` by an administrator: option ids the configuration uses, comma-separated |
| `refused` | Failure rows with reason `restricted`: the option ids refused |
| `control_created` | `service_created`, `config_created`, `secret_created` |
| `control_released` | `service_removed`, `config_removed`, `secret_removed` |
| `hostname`, `role`, `availability_before`, `availability_after`, `tasks_running` | `node_availability_changed` |
| `size` | `config_created`: the content size in bytes. Never on `secret_created` |
| `fields` | Failure rows for an invalid request: the names of the refused fields, comma-separated |
| `interrupted` | The request ended before the Engine answered. The row is written when the Engine call settles, or as `outcome_unknown` if it does not settle |
| `settled_late` | A create that ended after its row was written as `outcome_unknown`; a second row follows |
| `holder_action` | Failure row for a busy object |
| `reason`, `message` | Failure rows |

| Failure reason | Cause |
|---|---|
| `invalid` | A field broke its rule |
| `restricted` | A standard user used an administrator-only option |
| `name_in_use` | An object with the name exists |
| `changed_since_review` | The version, name, replica count, availability or use differs from the confirmation |
| `object_busy` | Another action holds the object |
| `service_gone`, `node_gone`, `config_gone`, `secret_gone` | The object no longer exists |
| `config_in_use`, `secret_in_use` | Services reference the object |
| `not_replicated` | Scale of a service that is not replicated |
| `no_previous_spec` | Roll back of a service with no previous configuration |
| `credential_choice_required`, `credential_missing`, `credential_unavailable` | The credential errors of [Images and registry credentials](#images-and-registry-credentials) |
| `update_paused`, `tasks_not_running`, `rolled_back_by_swarm`, `rollback_paused`, `unverifiable`, `interrupted` | `service_updated` by `container-updater`: how the watched rollout ended ([Verification of a service update](Operations-Automation#verification-of-a-service-update)) |
| `update_in_progress`, `legacy_networks` | `service_credentials_refreshed`: a rollout is running or paused; the legacy `Networks` setting cannot be sent unchanged |
| `self_protected` | The service, Swarm stack or node runs the container dockmin runs in |
| `not_swarm_manager` | A Swarm call answered `503` |
| `engine_rejected` | Docker refused the request |
| `unreachable`, `unsupported`, `credentials`, `engine`, `outcome_unknown`, `too_large` | Host failures, as in [Host failures](Usage-Containers#host-failures) |

- A refusal on access leaves no row: an environment that is not a Swarm manager, an object the user may not see, a user who lacks the access level, a malformed id, and a request whose access could not be decided because the host could not be read.
- A row never holds a secret's value or size, config content, an environment variable value, a label value, a registry credential, or Docker's own text.
