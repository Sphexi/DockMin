# Access control

## Roles

| Role | Reach |
|---|---|
| Administrator | Every environment and every object. Manages users, teams, groups, tags, and every page of the `Administration` group: Jobs, Activity, Registries, Template catalogues, Object storage, Backups, Certificate, Notifications, Encryption and Instance. Restores. Manages automation of every owner |
| Standard user | Only what grants give: environments they hold a grant on, and in them the objects their access allows. Manages their own API tokens and, in environments where they may create, their own automation rules |

- The first administrator is created on the setup page ([Start dockmin](Getting-Started-Quick-Start#start-dockmin)). The last enabled administrator cannot be demoted, disabled or deleted ([Deleting a user](Usage-Teams-and-Users#deleting-a-user)).
- A team leader, a standard user marked as leader of a team, manages the membership of that team. Administrators manage every team ([Teams](Usage-Teams-and-Users#teams)).
- A role change applies to the next request, including requests made with an existing API token.
- An account created at access platform sign-in is a standard user with no access until an administrator grants it ([Just-in-time accounts](Usage-Teams-and-Users#just-in-time-accounts)).

## Authorisation model

Four checks apply in this order. A hidden object answers `404`. An action the caller may not take on an object they can see, and an administrator-only page, answer `403`.

| Order | Check | Rule |
|---|---|---|
| 1 | Environment gate | A standard user needs a grant on the environment: a user or team grant on it, or on the group it is in ([Access](Usage-Environments#access)). Without one every route of the environment answers `404` |
| 2 | Resource control | The object's own control decides, otherwise its stack's control, otherwise the default for objects no control covers ([Visibility](Usage-Containers#visibility)) |
| 3 | Access level | `read` lets the caller see and inspect. `write` lets the caller change. A change needs `write` |
| 4 | Role | Some actions need the administrator role whatever the grants ([What a standard user cannot do](#what-a-standard-user-cannot-do)) |

| Concept | Fact |
|---|---|
| Controlled objects | Containers, Swarm services, configs, secrets, networks, volumes and stacks. A control holds a visibility, an access level, an owner and user and team grants ([Access panel](#access-panel)) |
| Visibility | `restricted` (specific users and teams), `public` (everyone who can sign in) or `administrators_only` |
| Access level | One level for the whole control, `read` or `write`. Every holder of access through it has that level |
| Inheritance | A container, Swarm service, config, secret, network or volume with no control of its own takes the control of the stack named by its `com.docker.compose.project` or `com.docker.stack.namespace` label |
| Objects no control covers | Visible to administrators only. No page or route changes this default |
| Teams | A grant to a team reaches every member |
| Create | A standard user needs a grant on the environment to create objects. In a local socket environment they cannot create ([Local socket read ceiling](#local-socket-read-ceiling)) |
| Where it is enforced | Where data is fetched. Lists omit what the caller may not see, counts cover only what the caller may see, and a template never receives a hidden object |
| Hidden and missing | A hidden object and a missing one answer identically |
| Effective access | Administrators read the reasons behind a decision on a container's effective-access page (`/environments/{id}/containers/effective-access`, [Containers](Usage-Containers)). The page asks for a username and lists the account's level and reasons for every container of the host |
| Recorded | Every change of a control, grant or role is an activity event ([Recorded actions](#recorded-actions)) |

| Reason | Meaning |
|---|---|
| `administrator` | The caller is an administrator |
| `owner` | The caller owns the control |
| `user_grant`, `team_grant` | A grant to the caller or to one of the caller's teams. The effective-access page names the teams |
| `public` | The control's visibility is public |
| `administrators_only` | The control hides the object from non-administrators. It overrides every grant and the owner |
| `not_granted` | A control exists and gives the caller nothing |
| `uncontrolled_default` | No control covers the object and the default gives access |
| `uncontrolled_denied` | No control covers the object and the default gives none |
| `no_environment_access` | The environment gate refused |
| `local_socket_read_only` | The access was capped at read ([Local socket read ceiling](#local-socket-read-ceiling)) |

## Access panel

The panel is the `Access` tab of a controlled object: container, Compose stack, Swarm stack, Swarm service, config and secret, network and volume. Administrators only. A standard user who can see the object receives `403`; one who cannot receives `404`.

### Set ownership and access on an object

1. Open the object's page and choose the `Access` tab. The heading is `Ownership and access`.
2. Read the facts: `Object`, `Control` (`Uncontrolled`, `Administrators only`, `Public` or `Restricted`), and for a controlled object `Access level`, `Owner`, `Users`, `Teams` and `Changed`. A stack control also shows `Covers N objects`, the members recorded at the last refresh of the stack, for reference only.
3. Open `Change ownership and access`.
4. Choose who can reach the object, set the access level, name the owner, and keep or add users and teams ([Fields](#panel-fields)).
5. Choose `Create control` for an uncontrolled object or `Save control` for a controlled one.
6. Success: the tab reloads with `Access control saved.` and the new facts. Failure: the form reloads at `422` with the message beside the field, or at `409` with the current control ([Stale form](#stale-form)).

<!-- screenshot: Usage-Access-Control-1: the Access tab of a container with the "Change ownership and access" disclosure open, showing the three visibility options, access level, owner, users and teams with access; a restricted control with one user and one team -->

### Panel fields

| Field | Posted as | Content |
|---|---|---|
| Who can reach this object | `visibility` | `restricted` (`Specific users and teams`), `public` (`Everyone who can sign in`) or `administrators_only` (`Administrators only`). Any other value is refused: `Choose who can reach this object.` |
| Access level | `access_level` | `read` or `write`. Every holder of access has this level. Any other value is refused: `Choose read or write.` |
| Owner | `owner_name` | A username. Empty means no owner. The page fills in the current owner's name |
| Owner account | `owner_user_id`, `owner_name_shown` | Hidden fields: the id of the owner the page showed and the name it filled in. Empty when the page showed no owner |
| Users with access | `keep_user_ids` | One checked box per user who holds a grant. Unticking a box removes the grant when the form is saved |
| Add users | `add_users` | Usernames separated by commas or new lines |
| Teams with access | `keep_team_ids` | One checked box per team that holds a grant |
| Add teams | `add_teams` | Team names, one per line. Commas are not separators, because a team name may contain one |
| Reviewed state | `expected_digest` | A hidden field ([Stale form](#stale-form)) |

| Part | On load |
|---|---|
| Control facts: visibility, access level, owner, who holds access | Expanded |
| `Change ownership and access` (the save form) | Collapsed. Open when the last save was rejected for a field error or a refused request, and with `?open=access-change` |
| `Remove the control` | Collapsed. Opens with `?open=access-remove` |

Both disclosures work without JavaScript. The form inside is unchanged, so `expected_digest` and every other field are posted as before ([Disclosures](Contributing-Frontend-Conventions#disclosures)).

| Rule | Value |
|---|---|
| Names per field | At most 50 in `add_users` and in `add_teams`: `Name at most 50 in one field.` |
| Grants per control | At most 500 users and 500 teams: `At most 500 users and 500 teams.` |
| Name length | At most 128 characters |
| Matching | Names are stripped, empty ones dropped, and repeats kept once. Matching ignores ASCII case |
| Unknown name | Refused at `422`, beside the field that held it: `No user is named “x”. Check the spelling.` for one, `No users are named “x”, “y”. Check the spelling.` for several, and the same for teams. Up to 5 names are quoted, then `and N more` |
| Unknown owner | Refused at `422` on `owner_name` |
| Owner name unchanged | When `owner_name` equals `owner_name_shown` and `owner_user_id` is set, the owner is that account, whatever it is called now. Renaming accounts after the page loaded does not move ownership |
| Owner name edited | The typed name is looked up as for any other name |
| Owner id not the reviewed owner | `409`, as for a changed control: `The access settings changed since this page was loaded. Review them and save again.` The refusal is recorded. Covers an id that is not a number and an id other than the reviewed owner's |
| Owner account deleted before the save | Refused at `422` on `owner_name`: `That account no longer exists.` |
| A user or team deleted before the save | Refused at `422`: `A user or team no longer exists.` |
| Shown names | Only the users and teams the control grants, and its owner, are looked up. The page never lists every account |
| A grant whose account is gone | Shown as `User N (no longer exists)` or `Team N (no longer exists)`, and keeps its box |
| Not restricted | With `public` or `administrators_only`, kept boxes and added names are dropped from the saved control |
| Stack names | A stack control is keyed by an ASCII name: `A stack name uses ASCII characters only.` |
| Typed text after a refusal | A `422` shows the form again with what was typed. A `409` shows the saved control and drops the typed names |

### Stale form

| Item | Value |
|---|---|
| Digest | `expected_digest` is the SHA-256 of the control as the page showed it: visibility flags, access level, owner, and the sorted user and team ids. It is empty when the object has no control |
| Posted by | The save form and the confirmation to remove the control |
| Mismatch | Nothing is changed and the answer is `409`: `The access settings changed since this page was loaded. Review them and save again.` The check runs before any typed name is looked up |
| Recorded | A failure row with reason `changed_since_review` |
| After the refusal | The page shows the current control and its current digest |

### Result

| Action | Result |
|---|---|
| Save | `303` to the object's `Access` tab with `Access control saved.` |
| Remove the control | `Remove control` in `Remove the control` opens the confirmation `Remove this control?`: `Every grant is dropped and the default for uncontrolled objects applies to the object this control covers.` It names the object and its type. `303` to the `Access` tab with `Access control removed.` The object returns to the default for uncontrolled objects |

## Bulk ownership in onboarding

`Adopt everything on this host` gives the ticked stacks and objects one owner and one team grant in a single confirmation ([Adopt everything on this host](Usage-Environments#adopt-everything-on-this-host)). The per-object `Access` tab remains the way to give each object its own.

| Field | Posted as | Rule |
|---|---|---|
| Owner | `owner` | A username, as on the [Access panel](#access-panel): names are stripped and matching ignores ASCII case. Empty means no owner. An unknown name is refused beside the field: `No user is named “x”. Check the spelling.` |
| Team | `team` | A team name. Empty means no team. An unknown name is refused beside the field |
| Team access | `level` | `read` or `write`. Default `write`. Any other value is refused: `Choose read or write.` |
| Length | | A name of at most 128 characters. A longer name matches nobody |

| Typed | Control written |
|---|---|
| Owner only | A control owned by the owner, access level `write` (the level the plan carries is the level stored), granting nothing to anyone else |
| Team only | A control granting the team the chosen level, with no owner, except a volume with a candidate owner |
| Owner and team | A control owned by the owner that grants the team the chosen level. The control's one access level is the chosen level, so the owner holds it too |
| Neither | No control, except a volume with a candidate owner. The object stays visible to administrators only |

| Rule | Value |
|---|---|
| Public | Never |
| Applies to | Every ticked object and every ticked stack. A stack's control covers its members |
| Stack | The control is claimed in the same operation that adopts the stack. A control the stack's name already has is kept |
| Volume candidate | A volume with a candidate owner ([Assign owners](Usage-Volumes#assign-owners)) is owned by the owner typed. With none typed, it is owned by the candidate, and a typed team is granted as well |
| Left alone | A ticked object with no owner, no team and no candidate gets no control. The confirmation and the result page count it as `visible to administrators only` |
| Existing control | An object that gained a control after the review is skipped and not changed |
| Recorded | One `resource_control_created` row for each object, with `onboarding` `true` ([Adopt everything rows](Administration-Activity#adopt-everything-rows)). A stack's row is `stack_adopted` |

## What a standard user cannot do

| Area | Restricted | Detail |
|---|---|---|
| Environments | Add, edit, delete, test, refresh; see endpoints, failed stages, counts and certificate details | [What a standard user sees](Usage-Environments#what-a-standard-user-sees) |
| Containers | Open the environment and access tabs; see commands, entrypoints, environment variables and bind mount sources; set options that give host access | [Withheld from standard users](Usage-Containers#withheld-from-standard-users), [Options restricted to administrators](Usage-Containers#options-restricted-to-administrators) |
| Stacks | Forget, adopt, read a project's files from its host or reconstruct a namespace from the cluster, open the access tab, reveal a secret variable, dismiss or restore a project or namespace that is not managed; change a stack unless every container of the project is writable to them; use Compose options that give host access. `DOCKER_HOST`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`, `DOCKER_CONFIG`, `HOME` and `TMPDIR` cannot be interpolated, or passed through a valueless `environment` entry, into a stack file or a companion file, by any user. No user can make a stack read a file of the server: `env_file`, `label_file`, `extends.file`, `include` paths and the `file` of a config or secret take only the name of a file added to the stack, as `name` or `./name`, and a value holding `$`, an absolute path, `..`, a subdirectory, `~`, a URL, `oci://` or a Git reference is refused. `develop`, `provider` and `models` are refused for everyone. `build` is refused for everyone but an administrator on a stored Compose stack: a standard user cannot save, deploy or redeploy a stack that builds images, and cannot add a build context | [Restrictions for standard users](Usage-Stacks#restrictions-for-standard-users), [Containers of the project](Usage-Stacks#containers-of-the-project), [Refused constructs](Usage-Stacks#refused-constructs), [The reference rule](Usage-Stacks#the-reference-rule), [Who may build](Usage-Stacks#who-may-build), [Reveal a secret variable](Usage-Stacks#reveal-a-secret-variable) |
| Swarm | List nodes; set service options that give host access; open access tabs | [Standard user restrictions](Usage-Swarm#standard-user-restrictions), [Access](Usage-Swarm#access) |
| Images | Tag, untag, remove (one or several), prune; change which credential set an image uses; see commands and environment variables | [Access](Usage-Images#access) |
| Networks | Prune; use drivers and options beyond the allowed set | [Access](Usage-Networks#access) |
| Volumes | Prune; use drivers and driver options; see mount points | [Access](Usage-Volumes#access) |
| Registries | Every page and action | [Registries](Administration-Registries#access) |
| Object storage, backups, restore | Every page and action | [Object storage](Administration-Object-Storage#access), [Backups](Administration-Backups#access) |
| Encryption | Every page and action | [Encryption](Administration-Encryption#access) |
| Certificate, Instance | Every page and action (`403`) | [Certificate](Administration-Certificate#access), [Instance](Administration-Instance#access) |
| Notifications | Every page and action (`404`) | [Notifications](Administration-Notifications) |
| Jobs, activity log | Every page and route | [Jobs](Administration-Jobs#access), [Activity](Administration-Activity#access) |
| Automation | Rules of other users; rules without an environment; rules in a local socket environment | [Automation](Operations-Automation#access) |
| Users, teams, groups, tags | Manage users, groups and tags. Manage teams, except the membership of a team they lead | [Teams and users](Usage-Teams-and-Users), [Groups](Usage-Environments#groups), [Tags](Usage-Environments#tags) |
| Local socket environments | Any change | [Local socket read ceiling](#local-socket-read-ceiling) |
| Template catalogues | Every catalogue page and action. Templates marked administrator-only are also hidden | [Template catalogues](Administration-Template-Catalogues), [Templates](Operations-Templates) |

## Local socket read ceiling

A local socket environment is the Docker host dockmin runs on. Write access to it is write access to that host, including this instance's data volume and key.

| Role | Reach in a local socket environment |
|---|---|
| Administrators | Read and write |
| Standard users with a grant | Read at most. A control, an inherited stack control or the default that would give write is capped at read; the reason is `local_socket_read_only` |

| Operation | Standard user |
|---|---|
| List, inspect, stats, logs | Allowed |
| Every change, create, duplicate, recreate, terminal, attach, prune | Refused (`403`) |

- The cap applies to every object type in the environment.
- The cap applies to environments reached over the local Unix socket only. When the same host is also added as an environment over TCP, standard users are not capped there: grant them no access to the volumes `dockmin-data` and `dockmin-key` in that environment.
- The Access section of a local socket environment repeats the notice ([Local socket environment](Usage-Environments#local-socket-environment)).
- Per-object tables: [Containers](Usage-Containers#local-socket-environments), [Stacks](Usage-Stacks#access), [Swarm](Usage-Swarm#access), [Networks](Usage-Networks#access), [Volumes](Usage-Volumes#access), [Images](Usage-Images#access).

## Recorded actions

| Action | Target | Written by | Detail keys |
|---|---|---|---|
| `resource_control_created` | The object's type and id (a stack's name), in its environment | `Create control`; adoption with ownership | `access_level`, `public`, `administrators_only`, `users`, `teams` (the counts); `onboarding` `true` from adoption |
| `resource_control_updated` | The same | `Save control` | The same keys |
| `resource_control_deleted` | The same | `Remove control` | None |
| `environment_access_granted`, `environment_access_revoked`, `environment_group_access_granted`, `environment_group_access_revoked` | The environment or group | Grants on environments and groups | [Recorded actions](Usage-Environments#recorded-actions) |
| `user_updated` with `old_role` and `new_role` | The user | A role change | [Recorded actions](Usage-Teams-and-Users#recorded-actions) |

- A save or removal refused because the control changed since the page loaded records a failure with the reason `changed_since_review`.
- A request answered `403` or `404` writes no row. The log line `access_refused` carries it, with the branch that refused (`no_environment_access`, `malformed_id`, `no_access`, `below_required`, `local_socket_read_only`) ([Not recorded](Administration-Activity#not-recorded)).

## Related settings

None. The default for objects no control covers is fixed: administrators only.
