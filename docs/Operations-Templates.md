# Templates

A template is an application a catalogue offers. Opening one fills the container create form or the Compose stack create form. Nothing is created until the form is submitted. Catalogues are managed on [Template catalogues](Administration-Template-Catalogues).

| Surface | Path | Who |
|---|---|---|
| Template list | `/templates` | Every signed-in user |
| Template page | `/templates/{catalogue_id}/{key}` | Every signed-in user |
| Container create form from a template | `/environments/{id}/containers/new?template={catalogue_id}:{key}` | Users who may create containers in the environment |
| Stack create form from a template | `/environments/{id}/stacks/-/new?template={catalogue_id}:{key}` | Users who may create stacks in the environment |

## Access

| Surface | Administrators | Standard users | No session |
|---|---|---|---|
| Template list and template page | Every template of every enabled catalogue | Every template except `administrator_only` ones | Redirect to the sign-in page |

- Every template page answers `Cache-Control: no-store`.
- The sidebar shows `Templates`, under Operations, to every user, and `Template catalogues`, under Administration, to administrators.
- An `administrator_only` template is not listed, not counted, and answers `404` on its page to a standard user. A create form opened with its reference is blank.
- A disabled catalogue, and one that has never been fetched, contributes nothing to any user.
- Creating from a template applies the rules of the create form unchanged: the user's right to create in the environment, the [restricted options](Usage-Containers#options-restricted-to-administrators), the credential choice for the image, the image binding ([Images](Usage-Images)) and the create job ([Containers](Usage-Containers#create-and-recreate-jobs)).

## Template list

`GET /templates` lists the templates of the enabled catalogues, ordered by title, then catalogue name, then key.

| Parameter | Rule |
|---|---|
| `q` | At most 200 characters, otherwise `422`. Every word must appear in the title, the description or the categories, ignoring case. The description is searched as stored, markup included |
| `category` | At most 64 characters, otherwise `422`. A category of the template, ignoring case |
| `catalogue` | A catalogue id, a whole number from 1. Empty, as with `All catalogues`, means every catalogue. A value that is not a whole number answers `422`. An id that names no enabled catalogue lists nothing |
| `limit` | 1 to 200, otherwise `422`. Default 24 |
| `cursor` | The value the pager's next link carries. An invalid cursor answers `400` |

| Item | Value |
|---|---|
| Filter form | `Search`, `Category` (`All categories`), `Catalogue` (`All catalogues`), `Apply filter`, `Reset` |
| Columns | `Application`, `Catalogue`, `Categories`, `Image`. `Categories` and `Image` are hidden on narrow screens |
| Application cell | The title as a link to the template page, and under it the description as plain text. A description longer than 145 characters is shortened at a word boundary to at most 140 characters, ending in `...` |
| Category menu | The categories of the templates the viewer can see, sorted, ignoring case |
| Catalogue menu | The enabled catalogues that hold templates the viewer may see, by name |
| Administrators | A `Template catalogues` button |
| Empty list | `No templates` |
| htmx request | The table fragment |

The description in the list is plain text: tags are removed, character references are decoded, whitespace is collapsed, and the content of the elements named in [Description and note markup](#description-and-note-markup) as dropped with their content is removed. Text on both sides of a block element such as `p`, `li`, `br`, `div` or a heading is separated by a space.

## Template page

`GET /templates/{catalogue_id}/{key}` shows what a template sets. An unknown key, a disabled catalogue and an administrator-only template for a standard user answer `404`.

| Section | Content |
|---|---|
| Header | Title, `Template from {catalogue name}`, `Back to templates`. A note `This template is offered to administrators only.` when the template is administrator-only |
| Description, Note | The description under the header, and `Note`, when the template has one ([Description and note markup](#description-and-note-markup)) |
| What it sets | Image, container name, categories, `Published for` (the architectures the catalogue lists), restart policy, command, host name, shared memory, `Interactive` (`Keeps standard input open and allocates a terminal.`), setup guide link |
| Ports, Volumes, Environment variables | One row each. Ports: container port and protocol, suggested host port (`Docker assigns one` when none), description. Volumes: path in the container (`(read-only)` when so), host path (`A named volume` unless the template binds one), description. Variables: name, value with `(one of …)` for its choices, description. Each row ends with `Filled in` or `Optional` |
| Labels | Name and value |
| Settings only an administrator can set | Added capabilities, devices, kernel parameters, security options, host networking, privileged mode, host bind mounts. Administrators read `These are filled in on the create form, except privileged mode and host bind mounts, which you set yourself.` Standard users read `The create form leaves these out for your role.` |
| Create | One row for each environment where the viewer may create, with `Create container` and `Create stack`. At most 50 environments are listed, then the notice `Only the first environments are listed. Open the create form from an environment's own page for the others.` A viewer who may create nowhere reads `You cannot create containers or stacks in any environment, so there is nowhere to create this template. Ask an administrator for access.` |

- `{catalogue_id}` is a whole number from 1; any other form answers `422`. `{key}` is at most 64 characters, otherwise `422`. A key that is not one of the catalogue's templates answers `404`.
- Every text of the template except the description and the note is escaped and shown as text.
- No logo or other resource of the catalogue is loaded.
- The environment's architecture is not compared with `Published for`; the template is never hidden for it.
- The setup guide is a link to the address the catalogue gave. It carries `rel="noopener noreferrer nofollow"`.

<!-- screenshot: Operations-Templates-1: a template page showing the header, a description with a link, What it sets, the ports and environment variables tables, and the Create table with two environments; an administrator session and an enabled catalogue holding a Portainer-format template with a note -->

### Description and note markup

The template page renders the description and the note through an allow-list. Everything not listed is removed.

| Element | Result |
|---|---|
| `b`, `strong`, `i`, `em`, `p`, `ul`, `ol`, `li`, `code`, `pre`, `br` | Kept, without attributes |
| `h1` to `h6` | Shown as bold text followed by a line break |
| `a` | Kept only when `href` is an `http` or `https` address with a host, at most 2,048 characters once whitespace and control characters are removed. The link opens in a new tab (`target="_blank"`) with `rel="noopener noreferrer nofollow"`. Any other `href`, an `a` with no `href`, and an `a` inside another link: the element is dropped and its text is kept |
| `script`, `style`, `iframe`, `object`, `embed`, `template`, `noscript`, `svg`, `math`, `form`, `select`, `textarea` | Dropped with everything inside them |
| Every other element, including `img`, `div`, `span` and `table` | The tags are dropped; text inside is kept. An image is not shown and is not requested |
| Attributes | All dropped, except `href` on a kept link. No style, class, event handler, `src` or form attribute survives |
| Text | Character references are decoded and the text is escaped before it is shown |

| Limit | Value |
|---|---|
| Input | Cut to 10,000 characters. A tag cut by the limit is removed |
| Output | At most 40,000 characters; the rest is dropped |
| Nesting | 16 open elements. Deeper elements are dropped and their text is kept |
| Unclosed elements | Closed at the end of the text |

- Text with no tag keeps its line breaks, shown as line breaks. Text with any tag treats a newline as a space; `br` and `p` break lines.
- Stored text keeps the markup the catalogue sent ([Template catalogues](Administration-Template-Catalogues#what-is-read)). Only the description and the note are shown as formatted text. The same text is plain text in the template list.

## Create from a template

1. Open `Templates` under Operations.
2. Enter words in `Search`, or choose a `Category` or a `Catalogue`, and choose `Apply filter`.
3. Open a template by its title and read what it sets.
4. In `Create`, choose `Create container` or `Create stack` on the row of an environment.
   Result: the create form opens filled in, with a panel `From the template {title}`. Nothing is created yet.
5. Review every value, change what is needed and submit the form as any other create form.

When the form is blank and the panel reads `That template is not available to you, so the form is blank.`, the template is unknown, its catalogue is disabled or it is administrator-only and the viewer is a standard user. Failures of the submission are those of the create form ([Containers](Usage-Containers#create), [Stacks](Usage-Stacks#create)).

`?template={catalogue_id}:{key}` on the container or stack create form fills it on the server.

| Rule | Behaviour |
|---|---|
| Parameter | Exactly one `template` value, at most 128 characters. Several values are ignored |
| What travels | The reference only. Every value comes from the stored template |
| Unknown, disabled or administrator-only for a standard user | The blank form with the notice `That template is not available to you, so the form is blank.` |
| With `from` on the container form | The duplicate wins and the template is ignored |
| Panel on the form | The template's title with a link to its page, `Catalogue {name}.`, `Review every value before you create the container.` (container) or `The file below is generated from the template. Review it before you create the stack.` (stack), notices about settings left out, and the choices an environment variable offers (`Values the template offers for an environment variable:` and one `NAME: value, value` line each) |
| Submit | The form posts as it does without a template, through the same validation, restrictions, credential choice, image binding and create job |
| Hidden field | `template_ref` carries the reference. On submit it is kept when it still names a template the user may use, and dropped otherwise |
| Recorded | `template` in the detail of `container_created` and `stack_created` ([Recorded actions](#recorded-actions)) |

<!-- screenshot: Operations-Templates-2: the container create form opened from a template, showing the From the template panel with its notices and the filled Image, Name, Ports and Environment variables; a standard user opening a template that asks for added capabilities -->

### Container form

| Template | Container form |
|---|---|
| Image | `Image` |
| Name | `Name`: the template's container name, else its title in lower case. Runs of characters outside `A-Za-z0-9_.-` become `-`, and `-`, `_` and `.` at either end are removed. Left blank when no valid name of at least 2 characters results |
| Restart policy | `Restart policy` |
| Command | `Command` |
| Shared memory | `Shared memory size` |
| Labels | `Labels` rows |
| Environment variables, not optional | `Environment variables` rows with the template's value, preset values included. A variable with choices is listed in the panel as `NAME: value, value` |
| Ports, not optional | `Ports` rows (`Published ports`). The suggested host port is filled in when the template has one |
| Volumes, not optional, no host path | `Volumes and mounts` rows of type volume, named `{container name}-{last path segment}`. The prefix is the template key when there is no container name. A repeated name gets `-2`, `-3`. A path with no segment uses `data` |
| Volumes with a host path (Portainer) | Administrators and standard users: no mount row. Named in the notice |
| Privileged | Administrators and standard users: `Privileged mode` is left unticked. Named in the notice |
| Capabilities, devices, host networking | Administrators: `Added capabilities`, `Devices`, `Network mode` `Host`. Standard users: named in the notice |
| Host networking, for an administrator | `Ports` rows are left out; the form refuses ports in host network mode |
| Host name, interactive mode | No field exists. Named in the notice as not carried over, for every user |
| Kernel parameters, security options | No field exists. Administrators: named in the notice as not carried over. Standard users: named in the notice of settings only an administrator can set |
| More rows than the form holds | The first rows that fit are filled. The notice reads `{N} {rows} were not carried over: the form holds {M}.` with `environment variables`, `labels`, `ports`, `mounts` or `devices`. The limits are those of [Create](Usage-Containers#create) |
| Optional ports, volumes and variables | Not filled |

The notice for a standard user reads `This template asks for {list}, which only an administrator can set.` The list names the added capabilities, device mappings, kernel parameters, security options, host networking, privileged mode and host bind mounts, with their values.

The notice for an administrator names, with their values, the elevated settings the template sets: `This template sets elevated options: {list}. Review them before you create it.` The list holds host networking, added capabilities, device mappings and security options. A template that asks for privileged mode or host bind mounts adds `It also asks for {list}, which are not filled in. Set them yourself if you want them.` (`This template asks for {list}, which are not filled in. Set them yourself if you want them.` when nothing else is elevated).

The notice for settings with no field reads `The create form has no field for {list}, so they were not carried over.`, with the host name, interactive mode, kernel parameters and security options.

### Stack form

| Template | Stack form |
|---|---|
| Name | `Name`: the template's container name, else its title, else its key. Lower case, runs of characters outside `a-z0-9_-` become `-`, `-` and `_` at either end are removed, at most 63 characters. Left blank when none fits the [stack name](Usage-Stacks#stack-names) grammar |
| Compose file | A one-service file, written with a safe YAML writer. The service is named after the stack, or `app` when the name is blank |

| Service key | From |
|---|---|
| `image` | Image |
| `restart` | Restart policy |
| `hostname` | Host name |
| `tty`, `stdin_open` | Interactive mode |
| `command` | Command |
| `shm_size` | Shared memory |
| `network_mode: host` | Host networking, administrators only |
| `ports` | Ports, not optional, as `{host}:{container}/{protocol}`; `{container}/{protocol}` without a suggested host port. Left out with host networking, which only administrators get |
| `volumes` | Volumes, not optional. Named volumes are also declared under the top-level `volumes`. Host binds are never written |
| `environment` | Variables, not optional |
| `labels` | Labels |
| `cap_add`, `devices`, `sysctls`, `security_opt` | Administrators only |

- A `$` in a command, variable value, label value, mount, kernel parameter or security option is written as `$$`. Catalogue text is never interpolated from the stack's variables.
- The file is an ordinary Compose file in the form. Editing it, the checks, the restrictions for standard users and deploy are those of [Create](Usage-Stacks#create).
- No file carries `privileged` or a host bind, whoever generates it. A standard user's file also omits the administrator-only keys. The notice names them. Adding them back is refused by the [restrictions for standard users](Usage-Stacks#restrictions-for-standard-users).
- The panel on the stack form carries the notice for the settings left out and the choices an environment variable offers; it has no notice for host name or interactive mode, which the file carries.

## Recorded actions

| Action | Detail |
|---|---|
| `container_created` | `template`: `{catalogue id}:{template key}`, when the form was filled from a template the user could still use ([Containers](Usage-Containers#recorded-actions)) |
| `stack_created` | The same `template` key ([Stacks](Usage-Stacks)) |

Opening, listing and searching templates writes no row. Rows for catalogue changes and fetches: [Template catalogues](Administration-Template-Catalogues#recorded-actions).
