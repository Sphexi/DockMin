# Frontend conventions

Server-rendered Jinja2 templates, HTMX for in-place updates, plain CSS and JavaScript. No build step, no bundler, no framework.

## Full page or fragment

A route returns one of two things, chosen by `is_htmx_request()` in [`app/web/templating.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/templating.py):

| Caller | Request headers | Helper | Returns |
|---|---|---|---|
| HTMX element request | `HX-Request: true`, neither `HX-Boosted: true` nor `HX-History-Restore-Request: true` | `render_partial()` | A bare fragment |
| Browser navigation | Neither `HX-Request` nor the two headers above | `render_page()` | A full page extending `base.html` |
| HTMX boosted navigation | `HX-Request: true` **and** `HX-Boosted: true` | `render_page()` | A full page |
| HTMX history restore | `HX-Request: true` **and** `HX-History-Restore-Request: true` | `render_page()` | A full page |

A boosted navigation is a whole-page load whose body HTMX extracts, so it takes the page branch. Both forms share one URL, so both carry `Vary: HX-Request`.

```python
if is_htmx_request(request):
    return render_partial(request, "progress_panel", panel_id="pull", title=title, ...)
return render_page(request, "stacks.html", {"stacks": stacks})
```

| Helper | Context is passed as | Template resolved |
|---|---|---|
| `render_page(request, name, context)` | A dict | `templates/<name>` |
| `render_partial(request, name, **arguments)` | Keyword arguments to the macro | The macro `name` in `templates/partials/<name>.html` |
| `render_fragment(request, template_name, macro_name, **arguments)` | Keyword arguments to the macro | The macro `macro_name` in `templates/<template_name>`; `template_name` is a constant of the calling route |

`render_partial` requires the macro name and the file stem to match, and the name to match `[a-z][a-z0-9_]*`. A file defining several macros can only be rendered through `render_partial` by the one that shares its file name; the others are imported from a template.

With `DOCKMIN_DEVELOPMENT_CHECKS` on, templates are recompiled and static file versions are re-read as files change. Otherwise both are read once at startup.

## Partials

Partials live in [`app/templates/partials/`](https://github.com/Sphexi/DockMin/tree/main/app/templates/partials) and are named for what they render, never for the route that returns them. Each file defines one macro of the same name, plus any helper macros listed below.

| Partial | Renders | Required arguments | Optional arguments |
|---|---|---|---|
| `page_header` | `<h1>`, subtitle, and an action area | `title` | `subtitle`; `{% call %}` body becomes the actions |
| `data_table` | Scrollable table (a focusable region labelled `<caption> table`), sortable headers, loading line, pagination, empty state | `table_id`, `caption`, `columns`, `pager`; `{% call %}` body supplies the `<tr>` rows | `empty_title`, `empty_body`, `empty_action_label`, `empty_action_href`, `filtered`, `clear_href` ([Empty, loading and error states](#empty-loading-and-error-states)) |
| `pagination` | First-page and next-page links with a count summary | `pager`, `table_id` | `label` (default `Pagination`; `data_table` passes `<caption> pagination`) |
| `back_actions` | The page's back button, then `Back to <kind> <name>` when the page was reached from another object ([Origin](#origin)) | `back_href`, `back_label` | `origin` |
| `empty_state` | "Nothing here" panel with an optional call to action | `title` | `body`, `action_label`, `action_href` |
| `error_state` | Error panel with `role="alert"`, the `Docker said` block, reference id, retry link ([Retry region](#retry-region)) | `title`, `message` | `request_id`, `retry_href`, `region_id`, `swap` (`outerHTML` or `innerHTML`; default `outerHTML`), `region_root` (default true), `engine_detail` |
| `engine_detail` | The `Docker said` block: Engine text in a labelled, focusable `<pre>`; renders nothing for an empty value. Defines `engine_detail_block(detail)` | `detail` | — |
| `confirm_targets` | The exact targets of a confirmation: a count line, the names in `.mono` with an optional detail, and `and N more not shown` ([Confirmations](#confirmations)) | `targets`, `noun` | `more`, `plural` |
| `confirmation_dialog` | `<dialog>` holding a POST form, plus `confirm_button(dialog_id, label, opener_id)`, the link that opens it ([Confirmations](#confirmations)) | `dialog_id`, `title`, `message`, `action`, `confirm_label` | `subject`, `hx_target`, `targets`, `noun`, `hidden`, `opener_id` |
| `check_status` | A hidden live status line for an asynchronous check, replaced out of band: `<p id=… class="visually-hidden" role="status" hx-swap-oob="innerHTML">` | `status_id`, `text` | — |
| `toast` | One dismissible notification. `role="alert"` for `warn` and `danger`, `role="status"` for `info` and `ok` | `level`, `message` | — |
| `badge` | Inline status pill. An empty `label` renders the tone word | `label` | `tone` |
| `container_state_badge` | A container's state as a `badge`, followed by a health badge: `Unhealthy` (`danger`), `Health starting` (`info`), `Healthy` (`ok`). Tones: `running` `ok`, `paused` `info`, `restarting` and `removing` `warn`, `dead` `danger`, any other `neutral` | `status` | `health` |
| `container_state_label` | The label of a container state: `Created`, `Running`, `Paused`, `Restarting`, `Removing`, `Exited`, `Dead`, `Stopped`; any other value as given | `status` | — |
| `form_field` | Label, input, help text, error text, wired `aria-describedby` | `name`, `label` | `value`, `error`, `type`, `help`, `required`, `autocomplete` |
| `select_field` | Label and `<select>` over a list of option strings, each shown capitalised | `name`, `label`, `options` | `value`, `error`, `help`, `required` |
| `pem_field` | A `<textarea>` for PEM text and a file input `<name>_file` (`.pem`, `.crt`, `.cer`, `.key`), with help and error text | `name`, `label`, `help` | `error` |
| `name_field` | A text input whose `<datalist>` fills with names of existing objects while the user types ([Name suggestions](#name-suggestions)). Also defines `name_cell(name, label, subject, suggest_href, …)`: the same input as one cell of a repeated row, with `subject` as visually hidden text before the label and `id_suffix` replacing `name` in its ids | `name`, `label`, `suggest_href` | `value`, `error`, `help`, `required`, `placeholder`, `type` (`text` or `search`), `include`, `choices_href`. `name_cell` takes `value`, `error`, `placeholder`, `include`, `id_suffix` |
| `name_options` | The `<option>` elements of a suggestion answer. Defines `name_options(options)` | `options`: one name, or a list of names and objects with `value` and an optional `label` | — |
| `list_bar` | The row above a list: the results line on the left, the page's actions on the right, wrapping on a narrow viewport. The `{% call %}` body holds the actions. Also defines `list_count(total, singular, plural, filtered, oob)` ([List bar](#list-bar)) | `summary`: the results line, rendered by the caller | — |
| `progress_panel` | Long-operation panel that polls itself until a terminal state. A visually hidden `role="status"` line states the title, state and percentage; the `detail` line is `role="status"`, or `role="alert"` when `state` is `failed` | `panel_id`, `title`, `state`, `href` | `percent`, `detail`, `steps`, `poll_seconds` (default 2) |
| `checkbox_field` | One checkbox with label and help; the value is part of the id, so boxes may share a name | `name`, `label` | `value` (default `on`), `checked`, `help`, `error`, `group_error` |
| `radio_field` | One option of a radio group; wrap the group in a `<fieldset>` with a `<legend>` | `name`, `label`, `value` | `checked`, `help`, `group_error` |
| `key_value_rows` | Repeatable key and value rows with a blank row in a `<template>`; `Add rows` posts the form back without script. Also defines the helpers `cell` and `row`. Import with context | `prefix` (`option` or `label`), `legend`, `subject`, `help`, `count`, `cap`, `values`, `errors`, `submit_href` | `key_list` (id of a `<datalist>` for the key inputs) |
| `error_summary` | Alert panel listing form errors | `errors`: a mapping of field to message, or a single message | — |
| `users_table` | The user list table (`#users-table`) with `You` on the signed-in user's row | `rows`, `columns`, `pager`, `current_user_id` | — |
| `object_users` | Containers that use a network or volume, linked, with a note for the remainder. Also defines the helper `users_overflow(more, hidden, noun)` | `rows` (items with `id` and `name`), `more`, `hidden`, `total`, `link_base` | `empty` (default `Not used`), `suffix` |
| `network_badge` | Badge for a predefined or system network | `label` | `kind` (`predefined` or `system`) |
| `job_state_badge` | Badge for the state of a scheduled job. The label names the state; the tone never carries it alone. Labels: `Running`, `Scheduled`, `Waits for calls` (`on_call`), `Paused`, `Automation disabled` (`rule_disabled`), `Disabled after` (`Disabled after failures` without `detail`), `Subsystem disabled`, `Not available`. An unknown `status` renders as its own text with the `neutral` tone | `status`: `running`, `scheduled`, `on_call`, `paused`, `rule_disabled`, `auto_disabled`, `subsystem_disabled`, `unavailable` | `detail`: text appended to the label, such as `until 2026-01-30 14:05 UTC` or `5 failures` |
| `automation_panel` | The automation acting on one object: each rule's schedule, state, next run and last outcome, with `Pause` or `Resume` and `Add automation` where allowed. A rule of another user renders without name, target or link. It draws no heading: the Automation tab supplies it. Import with context | `panel` (reads `rules`, `more`, `can_add`, `pause_options`), `add_href`, `return_to` (`container:<id>`, `stack:<name>`, `service:<id>` or `swarm_stack:<name>`) | — |
| `activity_panel` | The newest events of one object, each with time (linked to the event), action, actor and result, and an `All activity` link. Heading id `activity-heading`; the heading text is `title`. Import with context | `title`, `panel` (reads `events`, a list of `ActivityEventView`, and `all_href`) | — |
| `stack_logs` | The Logs tab of a Compose stack, a managed Swarm stack and a Swarm namespace ([Stack logs tab](#stack-logs-tab)). Also defines `log_error(message, field_id)` and `member_count(count, noun)` | `logs`: the context `logs_tab_context` builds | — |
| `icon` | One Lucide icon from the vendored sprite, hidden from assistive technology | `name` | `extra` (more classes, such as `icon--sm`) |
| `disclosure` | A collapsed section: `<details class="panel disclosure" id="{id}">` whose `<summary>` holds the heading (`id="{id}-heading"`, class `disclosure__title`). The `{% call %}` body is the content. Closed unless `open` is true or the request's `open` query parameter equals `id`. Import with context | `id`, `title` | `open` (default `false`; the route passes `true` when the content holds an error or a refusal), `level` (`h2` by default, `h3` or `h4`) |
| `port_list` | Ports as outlined chips: `<ul class="port-list">` of `<li class="port-chip mono">`, or `empty` as a `.cell-note` when there are none. Accepts a string, or a sequence of strings, objects with `text`, or Swarm ports (`published`, `target`, `protocol`, `mode`). Also defines `port_text(port)` | `items` | `empty` (default `None`, the text) |
| `resource_tiles` | A grid of count tiles, `<ul class="resource-tiles">`; the `{% call %}` body holds `resource_tile` items. Also defines `resource_tile(label, count, href=none)`, one link holding the label and the count (plain text without `href`; `—` for a count of `none`), and `resource_links(label, links)`, which renders `(text, href)` pairs as separate links | `label` | — |
| `stack_variable_row` | One masked secret variable row of a stack's Variables tab: the name, the chip `Secret`, the mask and, for an administrator, a `Reveal` form. A key that cannot open the value reads `Cannot be opened with the current key.` with a link to the Encryption page. A refusal is a `.field__error` with `role="alert"` ([Revealed values](#revealed-values)) | `variable`, `environment_id`, `stack_kind` (`compose` or `swarm`), `stack_name`, `row_id`, `reveal_href`, `row_href`, `back_href` | `error`, `revealed`, `window_expires_at`, `reveals_left` |
| `stack_variable_reveal_form` | The same row as a password form: `Your password`, the help text, `Reveal` and `Cancel` (a link to `row_href`) | As `stack_variable_row` | As `stack_variable_row` |
| `stack_variable_revealed` | The same row with the value in `code[data-revealed]`, the note `Reveals stay open until {time}. {N} reveals left in this window.`, `Hide` (a link to `row_href`) and an element that requests `row_href` after 60 seconds | As `stack_variable_row` | As `stack_variable_row` |
| `navigation_counts` | The sidebar list of one environment's pages with their counts. For an administrator it re-fetches itself ([Sidebar counts](#sidebar-counts)). Also defines `nav_link(key, icon_name, label, href, active, count)` | `environment`, `active` | `pending` |
| `restore_banner` | Notice above the page while the restore gate is not open. `live` gives `role="alert"` for a banner inserted after load; otherwise the banner is a region labelled "Instance status". `tone` is one of the `badge` tones | `message` | `tone` (default `warn`), `href`, `link_label`, `resume_action`, `csrf_token`, `live` |
| `offline_notice` | The offline answer to an HTMX request. The title is `Offline for a restore`, or `Offline while stored secrets are re-encrypted` when `purpose` (or the gate's purpose) is `rekey` | `message` | `detail`, `purpose` |

`ui_kit_table` is the worked example behind `/ui-kit`, not shared vocabulary. `/ui-kit` and its sample forms are served only when `DOCKMIN_DEVELOPMENT_CHECKS` is `true`; otherwise they answer `404` and the `UI kit` link is absent.

### Macros that take view objects

The partials directory is exercised with plain strings, so a macro that needs a `ControlView`, a `TeamView` or a `TokenView` cannot live there. It sits beside its page instead, in a file whose name starts with an underscore (`auth/_forms.html`, `access/_panel.html`, `teams/_tables.html`, `tokens/_table.html`, `swarm/_parts.html`, `swarm/services/_detail.html`, `jobs/_parts.html`, `jobs/_table.html`, `jobs/_runs.html`, `jobs/_subsystems.html`, `automation/_parts.html`, `automation/_table.html`, `automation/_runs.html`, `automation/_fields.html`, `containers/_confirm.html`, `containers/_batch.html`, `stacks/_confirm.html`, `networks/_attach.html`, `restore/_banner.html`, `app_templates/_table.html`, `app_templates/_panel.html`). A route answers an htmx request for one of these macros with `render_fragment(request, template_name, macro_name, **arguments)`, which is `render_partial` with the template named explicitly; `render_partial` is `render_fragment` for `partials/<name>.html`.

### Stack pages

Compose and Swarm stack pages share their form, validation and job parts. A Swarm template imports the Compose macro; it does not copy it.

| Shared | Where | Used by |
|---|---|---|
| Compose file box, variable rows and file rows | `stacks/_form.html` (`compose_block`, `variables_block`, `files_block`, `file_row`) | `swarm/stacks/_form.html` (`swarm_stack_form`) |
| Code editor around a stack file textarea | `data-code-editor` on the `<textarea>` in `compose_block`, `file_row` and `stacks/file_form.html`; script in `app/static/js/app.js`, styles `.code-editor` in `components.css` ([Code editor](#code-editor)) | Both kinds |
| Files tab, use list | `stacks/_files.html` (`files_tab`, `file_uses`) | `stacks/_detail.html` and `swarm/stacks/_managed.html` |
| Build contexts section | `stacks/_contexts.html` (`contexts_section`); `stacks/context_remove_confirm.html` | `stacks/_detail.html` |
| Add, edit and remove one file | `stacks/file_form.html`, `stacks/file_remove_confirm.html`, rendered with `swarm` true or false | Both kinds |
| Restart confirmation | `stacks/restart_confirm.html` with `restart_form` in `stacks/_confirm.html` | Compose stacks |
| File form reading, refusal state | `app/web/stack_file_pages.py` | The Compose and Swarm stack routers |
| Service table, validation result | `stacks/_validation.html` (`service_table`, `validation_result`, `goto_line`) | `swarm/stacks/_validation.html` (`swarm_validation_result`) |
| Status chips, banners, credential selects, Compose output block | `stacks/_parts.html` (`changed_chip`, `last_operation`, `origin_text`, `compose_banner`, `busy_note`, `credential_choices`, `stderr_block`) | Both kinds |
| Leftovers and dismissal: summary line, linked object lists, `Dismissed` state, `Dismiss` and `Restore` forms, the line that counts hidden entries | `stacks/_parts.html` (`leftover_summary`, `leftover_links`, `dismissal_state`, `dismiss_form`, `restore_form`, `dismissed_line`) | `stacks/_tables.html` (`unmanaged_section`), `swarm/stacks/list.html`, `swarm/stacks/_detail.html` |
| Secret variable rows | `partials/stack_variable_row.html`, `partials/stack_variable_reveal_form.html`, `partials/stack_variable_revealed.html`; the no-script page `stacks/variable_reveal.html` | `stacks/_detail.html` and `swarm/stacks/_managed.html` (the Variables tab), `app/web/stack_variable_routes.py` |
| Reveal and dismissal routes | `app/web/stack_variable_routes.py`, `app/web/stack_dismissal_routes.py` | Both kinds |
| Job page | `stacks/job.html` with `stack_job_progress` from `stacks/_parts.html`; `swarm/stacks/job.html` with `swarm_job_progress` in `swarm/stacks/_job.html` | One per kind; each reads only the job and route names, because it is also rendered as each stream event |
| Form reading, variable rows, problem mapping, credential offers, job stream frames | `app/web/stack_pages.py` | The Compose and Swarm stack routers |

- A variable row of the editor never carries the value of a secret variable. A stored secret renders an empty field with the placeholder `Set. Leave empty to keep it.` The Variables tab shows a secret as its mask and carries a value only in the answer to a reveal ([Revealed values](#revealed-values)).
- No stack template context carries the value of a secret variable.
- A job stream belongs to one kind of stack: a job page and its stream answer `404` on the other kind's path.
- A file row never carries the content of a secret file. A stored secret file renders an empty field with the placeholder `Set. Leave empty to keep it.`
- A file name in a path is constrained to the file name grammar; a name outside it answers `404` for every role before anything is read. `-` cannot begin a name, so `files/-/new` never matches a file.
- The Files tab (`?tab=files`) lists each file with where the documents name it. A reader sees the content of a plain file and `Set` for a secret file. Add, Edit and Remove are shown to writers only.
- The Restart action (a link to `…/restart`) is shown to writers while the stack's recorded status is Running or Partly running, and not on the stack dockmin runs in.

### Revealed values

A secret variable's value reaches the page only in the answer to one reveal request ([Stacks](Usage-Stacks#reveal-a-secret-variable)).

| Rule | Value |
|---|---|
| One row per answer | Every answer to a reveal or a masked-row request is one `<tr id="variable-row-{id}">`: the masked row, the password form or the revealed row. Each replaces the row it came from (`hx-target="#variable-row-{id}"`, `hx-swap="outerHTML"`, `hx-push-url="false"`), so the table holds one row for each variable |
| Refusals are rows | A refusal of an htmx request is the masked row with its message in a `.field__error` with `role="alert"`, at the real status: `404`, `409`, `422` or `503`. `app.js` swaps an HTML answer of status 400 or more. A request without htmx gets the shared problem page, or the reveal page with the form |
| Fields | The form posts `csrf_token`, `expected_name` and, on the password form, `password`. `hx-sync="this:drop"` and `hx-disabled-elt` stop a second submit while one is running |
| No cache | Every answer is sent with `Cache-Control: no-store` (`fragment_response`, `page_response`) |
| Timed hide, server | The revealed row holds an element with `hx-trigger="load delay:60s"` that requests the masked row and replaces the row |
| Timed hide, client | The value is the text of an element carrying `data-revealed`. `app.js` replaces that text with 16 `•` characters 60 seconds after the element appears, and on `pagehide`. Nothing else holds the value |
| History | `base.html` sets `historyCacheSize` to `0` in its `htmx-config`, so no snapshot of a page that shows a value is kept for back navigation. `historyRestoreAsHxRequest` is `false` |
| Focus | The password field and `Hide` carry `data-autofocus`; `Reveal` carries `data-focus-return`. When a swap replaces the row that held focus, the first element in the replacement with either attribute takes focus |
| Value in the template context | Only the object the reveal route passes: a `RevealedValue`, read with `reveal()` in the revealed row. It prints as `<redacted>` anywhere else |
| Without script | The form posts to the same route and the answer is the page `stacks/variable_reveal.html`. `Cancel` and the masked-row route redirect (`303`) to the Variables tab |

### Code editor

| Element | Behaviour |
|---|---|
| `textarea[data-code-editor]` | Stays the form field with its id, name, label, description and content. Script wraps it in `.code-editor`, sets `wrap="off"`, and adds an `aria-hidden` gutter (`.code-editor__gutter`) holding one number per line and a status line (`.code-editor__status`, `data-code-editor-status`, not live) reading `Line N, column M` |
| Gutter | One text node, rewritten at most once per animation frame after input and only when the line count changes. Its scroll follows the textarea's. It takes no pointer events |
| Keyboard | Tab is not captured. Nothing is coloured |
| `a[data-goto-line]` | The `goto_line` macro of `stacks/_validation.html`. A field error links to a line, `href` being `#` and the textarea's id and the attribute the 1-based line. Script puts the caret at the start of that line, scrolls it into view and focuses the field. Without script the link goes to the field |
| Without script | The textarea is rendered and posted as before. No gutter, no status line |

A refusal found by the pre-check, a YAML syntax error, a repeated key or a refused construct carries a line. A restricted option of the resolved file carries one when its path is spelled out in the text.

`access/_panel.html` is the ownership and access panel every controlled object page embeds. It takes a control and a resource key, never a container-shaped object, and is imported `with context`.

```jinja
{{ access_panel(panel.control, panel.key, panel.names, panel.digest, save_action, remove_action, values=values, fields=fields) }}
```

| Argument | Content |
|---|---|
| `control` | A `ControlView`, or none when the object has no control |
| `resource` (`panel.key`) | The `ResourceKey`; for a stack, `resource_id` is its name |
| `names` | `GranteeNames`: the names of the users and teams the control grants, by id |
| `digest` | `control_digest` of the control as shown. A refused form re-renders under the digest it submitted |
| `save_action`, `remove_action` | The URLs the forms post to |
| `values` | The refused submission to show again, or none to show the saved control. Keys: `visibility`, `access_level`, `owner_name`, `owner_user_id`, `owner_name_shown`, `keep_user_ids`, `keep_team_ids`, `add_users`, `add_teams`. A refused remove passes none |
| `fields` | Field name to message from a `ValidationFailed`. The key `resource` carries a refusal that belongs to no field and renders as `That change was refused` |

| Posted field | Content |
|---|---|
| `csrf_token`, `expected_digest` | On the save form and on the remove dialog |
| `visibility` | `restricted`, `public` or `administrators_only` |
| `access_level` | `read` or `write` |
| `owner_name` | A username; empty for no owner |
| `owner_user_id` | Hidden. The id of the owner the page showed; empty when it showed none |
| `owner_name_shown` | Hidden. The name the page put in `owner_name` |
| `keep_user_ids`, `keep_team_ids` | Repeated; one checked box per existing grant |
| `add_users` | Usernames separated by commas or new lines; at most 50 |
| `add_teams` | Team names separated by new lines; at most 50 |

Behaviour of the fields: [Access control](Usage-Access-Control#access-panel).

Every access-panel save and remove route fetches its panel through its own query service, then calls `apply_access` or `remove_access` in [`app/web/object_pages.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/object_pages.py). Both return an `AccessRefusal` for a `ValidationFailed` and none on success; the route renders the refusal or redirects with `ACCESS_SAVED` or `ACCESS_REMOVED`.

Values that end up in a class name or a `data-` attribute pass through the `choice()` global, which fails the render on anything outside its allowlist:

| Argument | Allowed values |
|---|---|
| `badge(tone=…)` | `neutral`, `ok`, `warn`, `danger`, `info` |
| `restore_banner(tone=…)` | `neutral`, `ok`, `warn`, `danger`, `info` |
| `toast(level=…)` | `info`, `ok`, `warn`, `danger` |
| `form_field(type=…)` | `text`, `email`, `url`, `number`, `search`, `tel`, `password` |
| `name_field(type=…)` | `text`, `search` |
| `error_state(swap=…)` | `outerHTML`, `innerHTML` |
| `progress_panel(state=…)` | `queued`, `running`, `succeeded`, `failed` |

`confirmation_dialog` is imported `with context`, because the form inside it reads `csrf_token` from the template context:

```jinja
{% from "partials/confirmation_dialog.html" import confirm_button, confirmation_dialog with context %}
```

`progress_panel` carries its polling attributes only while `state` is `queued` or `running`. A terminal state renders no `hx-get`, which is what stops the poll.

### Stack logs tab

`partials/stack_logs.html` renders the Logs tab of a Compose stack, a managed Swarm stack and a Swarm namespace. Its context comes from `logs_tab_context` in `app/web/stack_log_routes.py`; the three stack routes pass it as `logs` when `tab=logs`.

| Element | Behaviour |
|---|---|
| Root | `[data-log-view]` with `data-log-labelled`, `data-log-subject="stack"`, `data-log-stream` (the stack's stream route) and `data-log-download` (its download route). Script binds the same log view as the container Logs tab ([Logs tab](#logs-tab)) |
| Form | A `GET` form (`[data-log-form]`) to the stack page with hidden `tab=logs` and `recent=1`. Without script the browser loads the page; with script it reads again |
| Services group | `<fieldset class="choice-group">` legend `Services`, one `checkbox_field("services", name, name, checked)` per service. Rendered only when there is more than one service. The boxes share the name `services` |
| Script and `services` | The stream and download URLs carry one `services` parameter per ticked box, and none when every box is ticked |
| Member lines | `.log-members` holds `Reading N containers.`, `N containers not shown.` (hidden), `N more containers are not shown; filter by service.` (over the cap) and `N containers could not be read.` Swarm stacks say services |
| Notices | `[data-log-notice]`: the text of each `notice` event, joined, hidden until one arrives. A notice is also written to `[data-log-announce]` |
| Recent lines | `[data-log-static]`: `Show recent lines`, or the tail read for `recent=1`, in `.log-output--static`. Hidden by script |
| Downloads | `Download text` (`[data-log-download-link]`) and `Download NDJSON` (`[data-log-download-ndjson]`), each with the form's values and `format` |
| States | No members: `This stack has no container to read logs from.` None readable: `You can read the logs of none of this stack's containers.` A host failure: `error_state` with the failure's title and a retry link |

- The page status follows `logs_status`: `400` for a form error, the failure's own status for a refused read, else `200`.
- Behaviour of the tab: [Stacks](Usage-Stacks#logs).

### Scheduled job pages

The Jobs pages are in `templates/jobs/`. Their macros take job and run view objects, so they sit beside the pages ([Macros that take view objects](#macros-that-take-view-objects)).

| Template | Renders | Macros |
|---|---|---|
| `list.html` | Job list with the subsystem filter | Imports `jobs_table` |
| `_table.html` | Job table, swapped in place by the filter and pagination (`#jobs-table`) | `jobs_table(rows, columns, pager, filtered)` |
| `detail.html` | Job facts, actions and run history | Imports `runs_table` |
| `_runs.html` | Run history table, swapped in place by pagination (`#runs-table`) | `runs_table(rows, columns, pager)` |
| `run.html`, `_run_region.html` | One run. The region (`#run-region`) polls itself every 2 seconds while the run is unfinished, with `aria-busy` and a visually hidden `role="status"` line | `run_region(view, row, detail, finished, region_href)` |
| `run_confirm.html`, `enable_confirm.html`, `subsystem_confirm.html` | Confirmation pages for run now, enable and the subsystem switch. They change nothing | — |
| `subsystems.html`, `_subsystems.html` | Subsystem switches | `subsystems_table(subsystems)` |
| `_parts.html` | Job vocabulary, imported `with context` | `outcome_badge`, `trigger_label`, `job_state`, `job_name`, `job_target`, `held_badges` |

- State is shown with `job_state_badge`. `job_state` in `_parts.html` adds the `until <time>` and `N failures` details, and shows the `unscheduled` state as a `warn` badge, `Nothing scheduled`. The `rule_disabled` state reads `Automation disabled`.
- `trigger_label` maps `schedule` to `On schedule`, `catch_up` to `Catch-up`, `queued` to `Queued`, `manual` to `Run now`, `dry_run` to `Dry run` and `webhook` to `Webhook call`.
- `JOB_COLUMNS` in `app/web/job_routes.py` sets the widths of the job list: `Kind` `short`; `State` `badge`; `Next run` and `Last run` `time`. `Name`, `Environment` and `Schedule` have none and share the remaining width. `Kind`, `Environment`, `Schedule` and `Next run` are `optional`. The first column is `Name`. The run history uses `RUN_COLUMNS` ([Automation pages](#automation-pages)).
- `outcome_badge` tones: `success` `ok`, `partial` `warn`, `failure` `danger`, `skipped` `neutral`, `interrupted` `warn`, in progress `info`.
- A run's `message` and `detail` are stored text. They render escaped, like any other value.
- Every action is a form post with `csrf_token` and works with JavaScript disabled. Forms carry `data-submit-once`; no other hook is used.
- `.job-actions` in `components.css` lays out the action blocks of the job page.

### Automation pages

The Automation pages are in `templates/automation/`. Their macros take automation and run view objects, so they sit beside the pages ([Macros that take view objects](#macros-that-take-view-objects)).

| Template | Renders | Macros |
|---|---|---|
| `overview.html` | Automation list with the type and environment filters. The results line and `New automation` sit in a `list_bar`. The `Type` select posts `subsystem` | Imports `rules_table`, `list_bar`, `list_count` |
| `_table.html` | Automation table, swapped in place by the filters and pagination (`#rules-table`). A request from the filters also carries the results line as an out-of-band `list_count` | `rules_table(rows, columns, pager, filtered, total, oob)` |
| `new_choose.html` | New automation, step 1: type and environment. The radio of a type that takes no environment carries `data-environment="none"`; `.automation-choice:has(…)` in `components.css` hides the `Environment` field while it is checked | — |
| `form.html` | New automation, step 2, and edit. Sections in order: `Name`; `Schedule` (omitted for a type whose only schedule kind is `webhook`, which posts hidden `schedule_kind=webhook`); `Target` (only for a type that takes an environment); `Settings`. The `Target` section: the `Acts on` select (`target_kind`), the name rows, the `Labels` text area (`target_labels`) and a note for the environment kind. The stack label (`stack_label`) reads `Stack name` when the type acts on Compose and Swarm stacks and `Compose stack name` otherwise | Includes the settings partials; imports `choice_select`, `number_param`, `storage_target_param`, `target_rows` |
| `_target_rows.html` | The name rows of the target field, inside `fieldset#target-names`: one `name_cell` per name (input `target_name`), `Remove` on each row when there is more than one, and `Add another target` below the last, up to 20. Without an `environment_id` the rows are plain inputs with no suggestions. The route answers the htmx buttons with this fragment | `target_rows(names, target_kind, subsystem, environment_id, error, max_names)` |
| `_params_<subsystem>.html`, `_params_default.html`, `_params_common.html` | The type's own settings, the fallback when it has no partial, and the settings every automation has. `form.html` includes the first that exists, then the common one. Partials exist for `container-health-checker`, `container-log-exporter`, `container-updater`, `stack-restarter` and `webhook-receiver` | — |
| `_params_container-log-exporter.html` | The log exporter's settings: the `param_object_storage_target_id` select through `storage_target_param`, with help naming `container-logs/` and `service-logs/` in the target's activity log folder. The type takes an environment, so `form.html` includes this partial and the common settings, and keeps the `Target` section | Imports `storage_target_param` |
| `_params_container-updater.html` | The updater's settings: radio buttons `param_action` (`report` first), number input `param_verify_seconds` (0 to 300), checkbox `param_require_healthy`. For an administrator, also checkbox `param_prune_replaced` and number input `param_prune_after_hours` (1 to 720) | Imports `number_param`, `checkbox_field`, `radio_field` |
| `_params_webhook-receiver.html` | The webhook type's settings: radio buttons `param_action` (`update` or `redeploy`), `param_verify_seconds`, `param_require_healthy` | Imports `number_param`, `checkbox_field`, `radio_field` |
| `_params_container-health-checker.html` | `param_max_restarts` (1 to 20), `param_window_seconds` (300 to 86400), checkbox `param_auto_release` | Imports `number_param`, `checkbox_field` |
| `webhook_token.html`, `webhook_rotate_confirm.html`, `webhook_revoke_confirm.html` | The one-time webhook URL page (`Copy the webhook URL now`, the URL in a read-only textarea and the `Authorization: Bearer` form), and the confirmations for rotating and revoking the URL. The token is read only in `webhook_token.html` and is never placed in a link. The revoke form posts `expected_prefix` | — |
| `_target_preview.html` | What an automation's target matches now, for the viewer. Imported by `enable_confirm.html` and `run_confirm.html`. Renders nothing when `target` is none | `target_preview(target)`; `target` is a `TargetPreview` with `names`, `others` and `note`, or none |
| `_fields.html` | Form fields shared by the settings partials | `number_param(key, label, state, help, minimum, maximum, placeholder)`, `choice_select(name, label, options, selected, help, error)`, `storage_target_param(state, errors, storage_targets, help)`: the required `param_object_storage_target_id` select of target names, or a notice with an `Add a target` link when there are none |
| `detail.html` | Automation facts, the `Held containers` section of a `container-health-checker` automation, actions and run history | Imports `rule_runs_table` |
| `_runs.html` | Run history table, swapped in place by pagination (`#runs-table`) | `rule_runs_table(rule_id, rows, columns, pager)` |
| `run.html`, `_run_region.html` | One run. The region (`#run-region`) polls itself every 2 seconds while the run is unfinished; a key in the run's detail is shown with its plain-language label from `detail_label` | `run_region(view, row, detail, finished, region_href)` |
| `enable_confirm.html`, `run_confirm.html`, `delete_confirm.html` | Confirmation pages for enable, run now and delete. They change nothing | — |
| `_parts.html` | Automation vocabulary, imported `with context` | `rule_state`, `run_marker`, `environment_link`, `rule_facts`, `updates_summary`, `what_runs_do`, `what_is`, `detail_label(subsystem, key)` |

- For a type that takes no environment (`requires_environment` false), `form.html` omits the `Target` section and renders `param_object_storage_target_id` as a `<select>` of the targets passed as `storage_targets` (pairs of id and name), or a notice with an `Add a target` link when there are none. `activity-log-exporter` adds the `File format` select `param_format` (`csv` or `ndjson`, CSV selected unless the stored value is `ndjson`) through `choice_select`. Both add the number input `param_max_consecutive_failures` (1 to 100; empty takes the type's default). `detail.html` shows the target by name from `storage_target`.
- `storage_targets` is filled for every type in `TARGETED_SUBSYSTEMS`, including `container-log-exporter`, which takes an environment. The create page offers a type with `administrators_only` to administrators only.
- A type whose only schedule kind is `webhook` shows the note `The webhook URL is shown once, when you create the automation.` above the form on create.
- `detail_label(subsystem, key)` returns the plain-language name of a key in a run's detail, or an empty string. `_run_region.html` shows it with the key as a `.cell-note.mono` beside it, and shows the bare key when the label is empty. `container-log-exporter` has labels; the other types do not.
- The `Target` section is a `[data-target-form]` wrapper. Each kind's fields sit in an element with `data-target-section="<kinds>"`, the kinds that use it separated by spaces. `app.js` shows the sections of the chosen `target_kind` and hides and disables the others, so a hidden field is not posted. Without script every section is shown and the route refuses a value in a field the chosen kind does not use.
- Target names post as repeated `target_name` fields, one per row. Blank rows are dropped; the stored target holds at most 20 names. The `Add another target` and `Remove` buttons are submit buttons named `target_rows` with the value `add` or `remove-<row index>`, with `formnovalidate` and `hx-post` to `automation_rule_targets`, `hx-target="#target-names"`, `hx-swap="outerHTML"` and `hx-include="closest form"`. Without script they post the whole form to the create or save route, which shows the form again with the rows changed and saves nothing.
- Each name row is a `name_cell` that includes `#field-target_kind` and `#field-subsystem` in its suggestion request, so the suggestions follow the chosen kind and type ([Name suggestions](#name-suggestions)).
- The `Held containers` section of `detail.html` renders for a `container-health-checker` automation when the route supplies `holds`. Each row holds a `Release` form posting `csrf_token` to `automation_rule_hold_release`; the button carries the container name as visually hidden text. A held count shows as the `warn` badge `Held N`, and a release count as the `ok` badge `Released N`, from `held_badges(detail)` in `jobs/_parts.html`. The list table shows `Held N` from the rule's held count.
- Settings fields post as `param_<name>`. The route keeps only names in the type's parameter model and drops empty values, so an empty field takes the default.
- `what_runs_do` and `what_is` hold one branch per type. A new type adds its branch in both, and a `_params_<subsystem>.html` partial.
- `what_runs_do(view, parameters, target_kind, stack_kind)` takes the kind of target the automation names (`container`, `stack`, `service` or `labels`) and, for a stack target, the kind of stack (`compose` or `swarm`; none when the stack is not known). The `container-updater` branch reads `action` from `parameters`:
  - `report` lists the compare-only line and drops the activity-events line.
  - `update` with `target_kind` `service` lists the rolling update, rollback, credential renewal and no-removal lines.
  - `update` with a Swarm stack lists the redeploy, refusal and no-removal lines.
  - Any other `update` lists the recreate, rollback and no-removal lines, and adds the Compose redeploy line when `target_kind` is `stack`. When `stack_kind` is none it also adds the line for a Swarm stack of that name.
- The route reads the stack's kind from the stored stack behind the viewer's read access, only for a type that acts on Swarm stacks; it passes `target`, `target_kind` and `stack_kind` to the enable and Run now confirmations.
- `updates_summary(view)` renders the `Updates` column from the last real run's detail (`mode`, `updated`, `rolled_back`, `refreshed`, `unknown`, `update_available`). It renders `—` for any other type. The column key is `updates` in `RULE_COLUMNS`.
- The enable confirmation also posts `expected_digest` (hidden), the digest of the automation as shown. A missing or different digest refuses the enable.
- The enable and Run now confirmations render `target_preview(target)` before the `What changes` list. The route supplies `target`, `target_kind` and `stack_kind`.
- `run_confirm.html` adds a `Dry run instead` form for a `container-updater` automation whose `action` is `update`. Its help text depends on `target_kind` and `stack_kind`: a service, a Swarm stack, a stack of unknown kind, a Compose stack, or containers.
- A boolean setting posts a hidden `param_<name>` with the value `false`, then the checkbox with `true`. The route keeps the last value posted under a name, so an unticked box posts `false`.
- `.target-preview` in `components.css` lays out the names listed by `target_preview`.
- The delete confirmation posts the automation's name twice: `expected_name` (hidden, the name shown) and `typed_name` (typed).
- The enable confirmation posts `expected_updated_at`, the automation's last-changed time as shown. The Run now confirmation posts `expected_updated_at` and `expected_digest`.
- Panel forms post `return_to`. The route maps `container:<id>`, `stack:<name>`, `service:<id>` and `swarm_stack:<name>` to the object's page and ignores any other value. `service:<id>` takes a canonical 25-character service id; `swarm_stack:<name>` a Swarm stack name.
- The panel kind passed to `for_object` is `container`, `stack`, `swarm_stack` or `service`. `swarm_stack` matches automations by the stack's name only, and only for types that act on Swarm stacks; `service` matches an automation by the service's exact name or by the Swarm stack the service belongs to. Environment-wide automations are listed on the container and Compose stack panels only.
- The service and managed Swarm stack pages render the panel after the tab wrapper, so a tab swap does not carry it.
- `.automation-panel`, `.automation-list`, `.automation-list__item` and `.automation-list__facts` in `components.css` lay out the panel.
- `outcome_badge` and `job_state_badge` are shared with the Jobs pages. A dry run is shown with the `Dry run` badge, `neutral`, in place of an outcome.
- `RULE_COLUMNS` in `app/web/automation_routes.py` sets the widths of the list: `State` `badge`; `Next run` and `Last run` `time`; `Updates` and `Recent runs` `count`. `Name`, `Environment`, `Target` and `Schedule` have none and share the remaining width, as does `Owner`, which administrators get after `Name`. `Environment`, `Schedule`, `Next run`, `Updates`, `Recent runs` and `Owner` are `optional`; `Updates` and `Recent runs` are also `wide_only` ([Column widths](#column-widths)).
- `RUN_COLUMNS` in `app/web/job_routes.py`, shared by the run history of an automation and of a job, sets `Started` `time`, `Trigger` `short`, `Outcome` `badge` and `Duration` `count`; `Result` has no width. `Trigger` and `Duration` are `optional`.

### Activity pages

The Activity pages are in `templates/activity/`. Their macros take event view objects, so they sit beside the pages ([Macros that take view objects](#macros-that-take-view-objects)).

| Template | Renders | Macros |
|---|---|---|
| `list.html` | Retention line, filter form and results | Imports `filter_form`, `activity_results` |
| `_filters.html` | The filter form | `filter_form(f, oob=false)`, `moment_field(name, label, value, error)`; imports `choice_select`, `form_field` |
| `_table.html` | The results area and its table | `activity_table(rows, columns, pager)`, `activity_results(rows, columns, pager, export_href, errors)`, `activity_failed(errors, filters)` |
| `detail.html` | One event | Imports from `_parts.html` |
| `_parts.html` | Event vocabulary, imported `with context` | `result_badge`, `when`, `actor_cell`, `target_cell`, `environment_cell`, `detail_value` |

| Element | Id |
|---|---|
| Results area: download link and table, or the error summary | `#activity-results` |
| The table, swapped in place by the pager | `#activity-table` |
| The filter form | `#activity-filters` |
| Its submit button | `#activity-apply` |

The route picks the answer to an HTMX request from the `HX-Target` header:

| `HX-Target` | No field error (`200`) | A field error (`422`) |
|---|---|---|
| `activity-table` (the pager) | `activity_table` | `error_summary` |
| Anything else (the filter form) | `activity_results` | `activity_failed` |

- `activity_failed` renders `#activity-results` holding the error summary, then the filter form with `oob` true. The form carries `hx-swap-oob="true"`, so HTMX replaces the form on the page by its id, and the field errors and the values typed appear beside the fields. The results area is swapped by the form's own `hx-target` and `hx-swap="outerHTML"`.
- `oob` is true only in this answer. The form on a full page, and in `activity_results`, renders without it.
- The filter form is a `GET` form with `action` and `hx-get` set to the list. Without script the browser loads the page. `hx-sync="this:replace"` drops a request still running when the form is submitted again.
- The `filters` context entry holds `values`, `errors`, `size_options`, `size_text`, `more_open`, `categories`, `actor_types`, `results`, `subsystems`, `environment_options` and `list_href`.
- Every cell that shows stored text goes through `visible_text`.
- `.cell-wrap`, `.visible-char`, `.filters__group`, `.activity-list`, `.activity-list__item` and `.activity-list__actor` in `components.css` lay out the pages and the panel.

#### Activity panel

- A page route builds the panel for administrators and passes it as `activity_log`: an `ActivityPanel` with `events` and `all_href`, or none. The template includes `activity_panel("Recent activity", activity_log)` when `activity_log` is defined and not none.
- The panel is in the full page of the container, Compose stack, managed Swarm stack and environment pages. A tab swap does not carry it.

### Backup, restore and object storage pages

The pages are in `templates/backups/`, `templates/object_storage/` and `templates/restore/`. Their macros take view objects, so they sit beside the pages ([Macros that take view objects](#macros-that-take-view-objects)).

| Template | Renders | Macros |
|---|---|---|
| `backups/list.html` | Download form, link to the scheduled-backup rules, records | Imports `backups_table` |
| `backups/_table.html` | Backup records (`#backups-table`) | `backups_table(rows, columns, pager, targets)` |
| `object_storage/list.html`, `_table.html` | Target list (`#targets-table`) | `targets_table(rows, columns, pager)` |
| `object_storage/form.html` | Create and edit. Multipart, for the CA file | Imports `form_field`, `checkbox_field`, `pem_field` |
| `object_storage/detail.html` | Settings, warnings, connection test, rules | Imports `target_test_result` |
| `object_storage/_checks.html` | Connection test result, swapped into `#object-storage-test` | `target_test_result(checks, error, note)` |
| `object_storage/delete_confirm.html` | Delete confirmation. Changes nothing | — |
| `restore/index.html` | Begin form, or the state of the restore in progress with upload and cancel forms | — |
| `restore/remote.html` | `Archives in object storage`: the archives of one target, newest first, with `Refresh list` and `Back to the restore`; the error state `The archives could not be listed` when the listing fails | — |
| `restore/fetching.html`, `restore/_fetch.html` | `Fetching the archive`: a progress panel (`#restore-fetch`) that polls `restore_review` every 2 seconds, with `Refresh status` and a `Cancel the restore` form | `fetch_progress(status)` |
| `restore/review.html` | Manifest, what it replaces, key form | — |
| `restore/progress.html`, `_progress.html` | Steps and outcome (`#restore-progress`) | `restore_progress(progress, labels, paused, can_resume, page=false)` |
| `restore/_banner.html` | The banner for the gate's current state. Import with context | `gate_banner(live=false)` |
| `restore/offline.html` | The page the offline gate answers with | — |
| `partials/restore_banner.html` | Notice above the page while the gate is not open ([Partials](#partials)) | `restore_banner(message, tone, href, link_label, resume_action, csrf_token, live=false)` |
| `partials/offline_notice.html` | The offline answer to an HTMX request ([Partials](#partials)) | `offline_notice(message, detail, purpose)` |

- `base.html` wraps `gate_banner()` in `<div id="restore-banner">` on every page. The banner comes from `request.app.state.restore_gate`: `engaged` links administrators to `restore`, `swapping` to the progress page, `failed` shows no link and the `danger` tone, and an open gate with automation paused shows a `Resume automation` form (`POST` with `csrf_token`).
- `restore_progress` polls itself with `hx-trigger="every 2s"` while the outcome is `running`. A finished restore renders without `hx-get`, which stops the poll. `Refresh status` is the link without script.
- The polled fragment of a finished restore also carries `<div id="restore-banner" hx-swap-oob="true">` holding `gate_banner(true)`, so the banner on the page changes to the gate's current state without navigation. `restore/progress.html` passes `page=true`, which omits the out-of-band copy from the full page.
- The upload form on `restore/index.html` is `multipart/form-data`. The hidden `csrf_token` input comes before the file input; the route reads the token from the first part ([Backups](Administration-Backups#upload)).
- The key field on `restore/review.html` is `type="password"`, `autocomplete="off"`, and is never rendered back on an error.
- The target form never renders the secret or the certificate text. A `tls-warning` panel is shown by CSS (`.target-form:has(…)`) while `verify_tls` is unchecked.
- Forms carry `data-submit-once`. Every action works with JavaScript disabled.
- `.restore-banner`, `.target-form` and `.automation-choice` in `components.css` lay out the banner and the forms.

### Notification pages

The pages are in `templates/notifications/`. They are for administrators; a standard user receives `404` on every route.

| Template | Renders | Macros |
|---|---|---|
| `list.html` | Channel table (name, kind, state, event types, last success, last error) and the `Link base URL` form | Imports `badge`, `empty_state`, `form_field` |
| `new.html` | The kinds, as a list of links to `?kind=` | — |
| `form.html` | Create and edit. Hidden `kind`, and on edit `expected_updated_at`. Carries `data-submit-once` | Imports `channel_field` |
| `_fields.html` | One input of the form from a `FieldSpec` | `channel_field(item, state, errors, stored)`, `choice_input`, `text_area` |
| `detail.html` | Settings, delivery state, the test | Imports `channel_test_result` |
| `_test.html` | The test result | `channel_test_result(result)` |
| `delete_confirm.html` | Delete confirmation naming the channel | Imports `confirm_targets` |
| `subscriptions.html` | Event type checkboxes (`event_type`), scope radios (`environment_scope`), environment checkboxes (`environment_id`) | Imports `checkbox_field`, `radio_field` |

- A secret field is `type="password"` with `autocomplete="new-password"`. It never carries a value. The help text reads `Set. Leave blank to keep it.` or `Not set. It is stored encrypted and never shown again.` An optional stored secret adds the checkbox `clear_<field>` (`Remove the stored <field>`).
- The channel page reports a set secret as its mask with the note `Set. Shown masked only.`, or `Not set.` When the key cannot open the secrets, the page shows no mask and reads `Set. It is never shown.` ([Notifications](Administration-Notifications#secrets)).
- The test form carries `hx-post`, `hx-target="#notification-test"`, `hx-swap="outerHTML"`, `hx-sync="this:drop"` and `hx-disabled-elt`. `channel_test_result` always renders `#notification-test` and a `check_status` line `notification-test-status`, so a swap and the first load share one target. Without script the form posts and the page renders again with the result.
- Warnings about a channel (`http`, internal address) render as `.panel--notice` with `role="note"`.
- The sidebar link is under `Administration`, with the `bell` icon.

### Template pages

The pages are in `templates/app_templates/`. Every signed-in user can open them; a template marked administrator-only is offered to administrators only.

| Template | Renders | Macros |
|---|---|---|
| `list.html` | Search (`q`, at most 200 characters), `Category` and `Catalogue` filters, `Apply filter` and `Reset`, and a `Template catalogues` link for administrators | Imports `templates_table`, `list_bar`, `list_count` |
| `_table.html` | Template table (`#templates-table`): `Application`, `Catalogue`, `Categories` (optional), `Image` (optional, wide-only) | `templates_table(rows, columns, pager, filtered)` |
| `detail.html` | One template: description, `Note`, `What it sets`, ports, volumes, environment variables, labels, the settings only an administrator can set, and the environments where the viewer can create from it, each with `Create container` and `Create stack` links carrying `?template=` | — |
| `_panel.html` | The `From the template` notice on the container and stack create forms: title link, catalogue, notices, and the values the template offers for an environment variable | `template_panel(panel, intro)` |

- Catalogue text reaches a page only through two filters: `catalogue_markup` renders the description and note, and `catalogue_text` renders the one-line summary in the table.
- `catalogue_markup` keeps `b`, `strong`, `i`, `em`, `p`, `ul`, `ol`, `li`, `code` and `pre`; renders `h1` to `h6` as bold text followed by a line break; keeps an `a` whose `href` is an absolute `http` or `https` URL of at most 2048 characters, with `target="_blank" rel="noopener noreferrer nofollow"`, and drops every other attribute and tag. `script`, `style`, `iframe`, `object`, `embed`, `template`, `noscript`, `svg`, `math`, `form`, `select` and `textarea` are dropped with their content.
- Both filters remove control, format, surrogate and line-separator characters other than newline, carriage return and tab, and cut the input at 10,000 characters. `catalogue_markup` also caps the output at 40,000 characters and the nesting at 16 levels, and keeps the line breaks of input that holds no tag.
- `catalogue_text` returns the text with tags removed, entities decoded and whitespace collapsed.

### Environment pages

| Macro | Arguments |
|---|---|
| `connection_test_result(report, level)` | `level` is the heading element, `h2` or `h3` (default `h3`). `environments/form.html` passes `h2` |
| `endpoint_preview(preview, errors, level)` | `level` is `h2` or `h3` (default `h2`) |
| `environment_overview(overview, environment)` | `environments/_overview.html`, imported with context. The cards, then the stack grid and the container table when the host was read; otherwise a `Stacks and containers` panel with a `role="status"` line. `overview` is an `OverviewData` from [`app/services/environment_overview.py`](https://github.com/Sphexi/DockMin/blob/main/app/services/environment_overview.py) |
| `stat_cards(cards)` | `<ul class="stat-cards" aria-label="Environment summary">`. Each card is `<li class="stat-card" data-card="{key}">` with the label and an icon; `stat-card--single` when it has one figure |
| `stat_figure(card, figure, headline)` | One `<a class="stat-figure">` per figure: a visually hidden `{card label}: `, the value (`.stat-figure__value`) and the label (`.stat-figure__label`). The first figure of a card is the headline. A count reads `N`, `N / M` or `—`; a time figure reads the time or `Never` |
| `stack_grid(overview, environment_id)`, `container_table(overview, environment_id)` | The two sections under the cards |
| `stack_state(state)`, `card_href(link)` | A stack's state badge (`running` `ok`, `partly running` `warn`, `stopped` `neutral`, `Unknown` `warn`); the path and query of a `CardLink` built from its route name |

A macro that draws a heading on pages of different depth takes `level`, so a page never skips a heading level.

- The environment page lays out `Host`, `Status` and `Engine` in `.facts-row`, then `environment_overview`, then the sections that follow.
- The cards have the keys `stacks`, `containers`, `health`, `updates` and `automation`. Each figure links to a page by route name and query, so no template builds a URL.
- `container_table` is a `table table--fixed` with `data-min="40"` and `data-optional="2 3"`: `Name`, `Stack` and `Image` have no width, `State` is `col--badge`, `Status` has none. It has no sort links and no pager. `Stack` and `Image` are hidden below `40rem` ([Column widths](#column-widths)).
- `.stack-grid` has one column, two from `40rem` and three from `64rem`. `.stat-cards` fills the row with cards at least `9.5rem` wide.
- The environment list (`environments/dashboard.html`) shows the `Add your first environment` panel instead of the filter form and table while no environment exists. Its state is `present` (a form `Test and add` for the local Docker socket, posting `kind=unix_socket`, `socket_path` and `reviewed_endpoint`), `no_access` (the socket is mounted and cannot be opened; the panel shows the `--group-add` argument), `absent` (no socket is mounted; the panel shows the `-v` argument) or a path that is not a socket. A link `Add another kind of environment` follows in every state.

### Image pages

The image list is in `templates/images/`; its macros are in `_parts.html`.

| Macro | Renders |
|---|---|
| `reference_list(references, hidden, environment_id, empty)` | One item per reference: the reference text, the binding state, then `stored_update` |
| `stored_update(reference)` | The stored update status of one reference: `update_badge(reference.update_state)` and `checked <time>`; `Not checked` when `update_state` is empty or `not_bound` |
| `update_badge(state)` | The badge for an update state |
| `transfer_progress(progress, running, label)` | The bar, the `role="status"` summary line and the layer table of one pull or push. Used by the image transfer page and, per image, by the Compose and Swarm stack job pages |

- `update_state` and `update_checked_at` come from the stored status ([Images](Usage-Images#stored-status)).

## Forms

### Form rows

A field is `.field` holding, in this order, `.field__head` (the label, then the help text), the control, and `.field__error`. The `form_field`, `select_field`, `pem_field` and `key_value_rows` macros emit this structure; a hand-written field follows it.

| Container | Layout |
|---|---|
| `.create-section__body`, `.filters`, `.filters__group`, `.filters__labels`, `.form-pair`, `.address-grid` | A grid with several fields to a row. Each `.field` spans three rows of the container's grid (`grid-row: span 3`, `grid-template-rows: subgrid`): head, control, error |
| Result | The controls of one row share a top edge, whatever help or error text sits above them |

| Rule | Value |
|---|---|
| First child | In a multi-column container, the first child of a `.field` is `.field__head` |
| Row gap | `--field-row-gap` (`--space-4`) is the padding below each field. Other children of `.create-section__body` and `.filters` take it as a bottom margin |
| Actions | `.filters > .form__actions` ends the grid, on the control row of the last column |
| Error | `.field__error` below the control; `data-invalid` on the `.field` |

### Form pages

| Rule | Value |
|---|---|
| Columns | A `.create-form` page is one column. Sections are never laid side by side |
| Section width | At most `--size-form-column` (`60rem`) |
| Wide sections | A section holding `.field--wide`, `.field__input--code`, a repeat group for mounts, ports, stack variables or service files, or a `.data-table__scroll` takes the width of the page |
| Section order | New automation: Name, Schedule, Target, Settings, then the buttons |
| Other forms | `.form` is at most `30rem` wide; `.form--wide` at most `--size-form` (`48rem`) |

### Name suggestions

A name field fills its `<datalist>` with names of existing objects while the user types. The input stays a plain text input: without script, or when no suggestion arrives, the user types the whole name.

| Element | Value |
|---|---|
| Wrapper | `.suggest` on `name_field` and `name_cell`, holding the input (`list="suggest-<name>"`) and an empty `<datalist id="suggest-<name>">` |
| Request | `hx-get` to the suggestion route with `field=<input name>` appended, `hx-trigger="keyup changed delay:250ms from:find input"`, `hx-include="find input"` plus the selectors in `include`, `hx-target="find datalist"`, `hx-swap="innerHTML"`, `hx-sync="this:replace"`, `hx-push-url="false"` |
| Answer | `<option>` elements from `name_options`, `Cache-Control: no-store` |

| Route | Method | Kinds |
|---|---|---|
| `/environments/{environment_id}/suggestions/{kind}` | `GET` | `containers`, `images`, `volumes`, `networks`, `stacks`, `services`, `configs`, `secrets`, `rule-targets` |
| `/suggestions/{kind}` | `GET` | `users`, `grantees` |

| Query parameter | Behaviour |
|---|---|
| `field` | The name of the query parameter that holds the typed text, matching `[A-Za-z0-9_-]{1,64}`; any other value answers `404`. The typed parameter must appear exactly once; otherwise the answer is empty |
| `stack_kind` | `stacks` only: `compose`, `swarm` or `any` (default `any`). Any other value suggests no stack |
| `subsystem`, `target_kind` | `rule-targets` only: the type of the automation and the target kind (`container`, `stack` or `service`). The type must offer the kind; otherwise the answer is empty. Stacks are limited to the kinds the type acts on |

| Item | Behaviour |
|---|---|
| Who | Any signed-in user. `users` and `grantees` answer empty to a standard user |
| Matching | The typed text, trimmed, is a case-insensitive prefix. Empty text, or text over 512 characters, answers empty |
| Size | At most 20 options, sorted ignoring case, each name once. A Compose and a Swarm stack whose names differ only in case appear once |
| Source | The names the caller's own list page shows. An object the caller cannot see, a missing object, an unreachable host and a refused read all answer empty, so no answer tells them apart |
| Labels | `volumes`: `read-only` for a volume the caller cannot write. `grantees`: `user` or `team`, users first |
| In flight | One read per user and kind. A later request cancels the earlier one, which answers empty |
| Unknown `kind` | `404` |

### Disclosures

A `<details>` section opened and closed by its heading. It works without script.

| Rule | Value |
|---|---|
| Macro | `disclosure(id, title, open, level)` ([Partials](#partials)) |
| Closed on load | Unless the content holds an error or a refusal, or the request carries `?open={id}` |
| After an action | A redirect that must leave one open appends `?open={id}#{id}` |
| Content | The form is unchanged inside it. A hidden field, such as `expected_digest`, is still submitted |
| Used for | The Access section of an environment and of a group; the save form and the remove control of every Access tab; `Effective access` on the container list and the container Access tab |

## Targets and swaps

`hx-get`, `hx-post`, `hx-target`, `hx-swap`, `hx-trigger` and `hx-push-url` are written in the template. JavaScript sets no HTMX attribute and issues no HTMX request; [`app/static/js/app.js`](https://github.com/Sphexi/DockMin/blob/main/app/static/js/app.js) only reads the `hx-trigger` of an element to tell a poll (`every …`) from other requests.

Script opens the stats, log, job and image transfer event streams (`EventSource`) and the terminal WebSocket, and makes one `fetch` to read why a log stream was refused. Every other request is made by HTMX or by the browser.

A response may carry an element with `hx-swap-oob="true"`. HTMX replaces the element with the same id on the page, in addition to the swap into the request's target. The Activity filter form uses this to show field errors ([Activity pages](#activity-pages)).

Filter forms have an id and trigger on a submit and on a change of a select or checkbox, never on a change of a text field, so Enter issues one request:

```jinja
<form id="container-filters" method="get" action="{{ list_href }}" hx-get="{{ list_href }}" hx-target="#containers-results" hx-swap="innerHTML"
      hx-push-url="true" hx-trigger="submit, change from:(#container-filters select, #container-filters input[type=checkbox])" hx-sync="this:replace">
```

- The Activity filter form triggers on `submit` only.
- `hx-sync="this:replace"` drops a request still running when the form is submitted again.

Every element carrying an HTMX attribute also carries the plain equivalent — `href` on a link, `method`/`action` on a form — so the same control works with JavaScript disabled.

```jinja
<a class="sort-link" href="{{ pager.sort_href(column.key) }}"
   hx-get="{{ pager.sort_href(column.key) }}" hx-target="#{{ table_id }}"
   hx-swap="outerHTML" hx-push-url="true">{{ column.label }}</a>
```

URLs come from the `route_path()` global for routes and the `static_path()` global for assets. `static_path()` appends the file's modification time as a `?v=` query, so a changed asset is not served from cache.

## CSRF

The token reaches a request by two routes; which one applies depends on how the request is made.

| Request | Carrier | Declared in |
|---|---|---|
| Any HTMX request | `X-CSRF-Token` header | `hx-headers` on `<body>` in [`base.html`](https://github.com/Sphexi/DockMin/blob/main/app/templates/base.html), inherited by every descendant |
| A plain form POST | Hidden input named `csrf_token` | The form itself |

A form that can be submitted without JavaScript needs the hidden input as well as the inherited header:

```jinja
<form method="post" action="{{ route_path('ui_kit') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
```

The token is a 256-bit URL-safe string in the CSRF cookie ([Cookies](Reference-Security#cookies)). Script never reads it; it reaches the page through the template context. The cookie is set only on a request that minted a token.

Names and the header constant live in [`app/web/csrf.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/csrf.py). `enforce_csrf` is an application-wide dependency. A mutating request without the cookie's token, or from another origin, is refused with `403`.

| Exemption | Reason |
|---|---|
| A bearer request to the JSON surface | It carries no cookie |
| `POST /restore/archive` | The route reads the body as a stream and verifies the token from the first multipart part, with the same cookie and Origin checks, before it stores the first file byte. The route is also exempt from the 256 KiB request body cap. The exemption is keyed on the exact method and path (`STREAMED_UPLOAD_ROUTES`) |
| `POST /environments/{id}/stacks/{name}/contexts/-/upload` | The route reads the body as a stream and verifies the token from the first multipart part, as the restore upload does. It has its own body cap of 16 MiB and 64 KiB and a 300 s total deadline in place of the 256 KiB cap and 30 s. The exemption is keyed on the path pattern |

## Errors

An error response is a rendered fragment or a rendered page, never a bare status code. The status code itself is preserved. `_error_response()` in [`app/main.py`](https://github.com/Sphexi/DockMin/blob/main/app/main.py) picks the form:

| Request | Response body |
|---|---|
| Path under `/api` | JSON `{"error": {"status", "message", "fields"?}}`. `message` is the `HTTPStatus` phrase |
| HTMX request | The `error_state` partial with `title`, `message` and `request_id`, at the error status |
| Anything else | `error.html` with `status_code`, `title`, `message`, `request_id`, `back_href` (the start page) and `back_label` (`Return to the start page`), at the error status |

HTMX discards the body of a 4xx or 5xx response by default. `app.js` reverses that for responses that are `text/html` with a non-empty body, so the fragment swaps into the target the user is looking at. A failure that produces no fragment — a transport error, a timeout, a non-HTML error body — raises a toast instead.

A failed action answered through the shared problem page (`problem_page`) follows the same rule: a full page for a navigation, and for an HTMX request the `error_state` partial alone, at the error status, with a retry link for `409`, `502` and `503` when the page has somewhere to retry. `problem_page` fills an empty title or message from `error_copy`.

### Retry region

`Try again` in a fragment error reloads the region that failed, not the page.

| Rule | Value |
|---|---|
| Declaring a region | `problem_page` and `unavailable_page` take the required keyword `region`: a `Region(id, swap)` from [`app/web/object_pages.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/object_pages.py), or `None`. `id` matches `[A-Za-z][A-Za-z0-9_-]{0,63}`; `swap` is `outerHTML` or `innerHTML` |
| `None` | The failure is never a fragment: a POST action answered with the full problem page, a confirmation, a list or overview page with no results element |
| When it applies | The request is an htmx fragment request and its `HX-Target` header equals `region.id`. Any other request gets the plain link, which reloads the page |
| Link | `href` stays the page URL. With a region the link also carries `hx-get` (the same URL), `hx-target="#<region id>"` and `hx-swap`. Without script the link reloads the page |
| Element ids | With `outerHTML` the error root carries `id="<region id>"`, so the next request has the same target. With `innerHTML` the region element stays in the page and the root has no id |
| In a template | `error_state(..., retry_href=<URL>, region_id=<id of an element in the page>, swap=...)`. `region_root=false` when the error renders inside the region element, so the id is not repeated. The URL must answer a request that targets that element with the region's content |
| Route modules | Each detail route declares `TABS_REGION = Region("<kind>-tabs", "outerHTML")` and passes it as `region=` to every call in its `_detail_response`. Routes answering a request that does not target a page element pass `region=None` |
| Check | A test parses `app/web` and fails on a `problem_page` or `unavailable_page` call without `region` |

Regions in use: the tabs element of each detail page (`container-tabs`, `network-tabs`, `volume-tabs`, `image-tabs`, `stack-tabs`, `service-tabs`, `config-tabs`, `secret-tabs`; `outerHTML`), the list results element (`<kind>s-results`; `innerHTML`), `service-tasks` (`outerHTML`) and `environment-overview` (`innerHTML`). The static log tail on a plain page load keeps the plain link.

Validation errors return the field location and message only. Submitted input is never echoed back.

### Error copy

`error_copy(status)` in [`app/web/problems.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/problems.py) holds the title and message of each status. Pages, fragments and the fallback of `problem_page` use it. JSON keeps the `HTTPStatus` phrases.

| Status | Title | Message |
|---|---|---|
| 400 | The request could not be read | Reload the page and try again. |
| 403 | Your role does not allow this | Ask an administrator if you need it. |
| 404 | Not found | It does not exist, or you do not have access to it. Check the address, or go back to the list. |
| 405 | Not available here | Use the page's own controls. |
| 408 | The request took too long | Try again. |
| 409 | Something changed | Reload the page to see the current state, then try again. |
| 413 | Too large | Send a smaller file or form. |
| 422 | Check the form | Correct the fields shown, then submit again. |
| 429 | Too many requests | Wait a few seconds, then try again. |
| 500 | Something went wrong | Try again. If it keeps failing, give an administrator the reference below. |
| 502 | Docker answered with an error | Try again. If it keeps failing, check the Docker host. |
| 503 | Not available right now | Try again shortly. |

- A status not in the table uses its `HTTPStatus` phrase as the title and the 500 message.
- `unreachable_copy(error)` is the copy for a host that cannot be reached or read. For an `EnvironmentUnavailable` the title is `The environment is not reachable` and the message is the error's text followed by `Check that the Docker host is running and reachable, then try again.` Any other error uses the 503 title and its own message.
- An object the caller cannot see answers the 404 copy, identical to the answer for a missing object apart from the reference id. `403` is a role refusal on a page whose existence is not secret.
- `engine_detail` on a failure is the Engine's text for administrators ([Engine text](#engine-text)).

### Engine text

| Item | Value |
|---|---|
| Carried by | `Failure.detail`, the form banners and `engine_detail` in the page context, set through `administrator_detail(error, actor)` |
| Shown | In the `Docker said` block (`engine_detail_block`), under the message, to administrators only |
| Text | Scrubbed of secrets, control characters removed, reduced to one line, cut to 300 characters |
| Source | The Engine's reply to a request it answered with `400` or `404` |
| Not stored | Not part of `str(error)`, any log line, or the activity row |

## Flash messages

A form post that succeeds answers with a redirect and a notice. The page the redirect lands on shows the notice as a toast.

```python
return redirect(request, "/teams/3", flashes=[Flash("ok", "Team created.")])
```

| Item | Value |
|---|---|
| Helper | `redirect(request, location, flashes=())` in [`app/web/navigation.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/navigation.py). `Flash(level, message)` is in [`app/web/flash.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/flash.py); `level` is `info`, `ok`, `warn` or `danger` |
| Response | `303` to `location`. For an HTMX request: `204` with `HX-Redirect: location` |
| Cookie | The notice cookie ([Cookies](Reference-Security#cookies)) |
| Value | Base64url JSON, a dot, and an HMAC-SHA-256 signature. The JSON holds the issue time, the session binding and the notices |
| Key | 32 random bytes generated at process start (`app.state.flash_key`) |
| Session binding | The first 16 hex digits of the SHA-256 of the session cookie value; empty before sign-in |
| Limits | 3 notices of at most 500 characters. The value is at most 3500 characters: the oldest notices are dropped, then the last message is halved, until it fits |
| Accepted when | The signature, the shape and the limits are valid, the age is at most 120 seconds, and the binding equals the current session's. Anything else is ignored |
| Pending notice | A new write replaces it |
| Read | `template_context` puts the verified notices in `flashes`; `base.html` renders each as a toast |
| Cleared | By `render_page` on the response that displayed them, and by login and logout. A fragment, a JSON response or a redirect neither reads nor clears them |

- A post that fails (`422`, `409`) renders its form with the errors and writes no notice.
- Environment create and save with TLS warnings write one folded `warn` notice ([Environments](Usage-Environments#conditions-that-do-not-block-saving)).
- The page that creates an API token renders the token once and does not redirect.

## Result pages

A post whose outcome is a list of per-target results stores the page and answers with a redirect to it. Reloading the page repeats nothing.

| Item | Value |
|---|---|
| Helper | `show_result(request, template, context)` in [`app/web/results.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/results.py). The context holds view objects ready for the template and never a secret |
| Response | `303` to `GET /results/{token}` (`action_result`), `Cache-Control: no-store` |
| Token | 32 characters of `A-Za-z0-9_-`, random, bound to the user id |
| Life | 10 minutes. At most 20 results per user and 200 in the process; the oldest is dropped first |
| Read | The page is rendered again on each `GET`; the entry is not consumed |
| Not found | A token that is malformed, unknown, expired or another user's answers the `404` copy, identical for all four |
| Dropped | A user's results are dropped on sign-out, on the user's own password change, and on an administrator's password reset or user update (role change) |
| Storage | Process memory; lost on restart |

| Template | Posted by |
|---|---|
| `containers/action_result.html` | A container action sent without htmx |
| `containers/batch_result.html` | A container batch |
| `containers/create/recreate_result.html` | A recreate that failed |
| `networks/prune_result.html`, `volumes/prune_result.html`, `images/prune_result.html` | A prune |
| `volumes/force_result.html` | A forced volume removal |
| `environments/delete_result.html`, `registries/delete_result.html` | A delete of environments, a delete of a registry credential set |

## Confirmations

Every action that removes, overwrites or changes something for others is confirmed first. The confirmation changes nothing.

| Rule | Value |
|---|---|
| Targets | Rendered through `confirm_targets(targets, noun, more=0, plural=none)`: a count line (`3 containers`), then each name in `.mono` with an optional detail, then `and N more not shown` when `more` is above zero |
| `noun` | Singular. The plural adds `s`, or replaces a final `y` after a consonant with `ies`; `plural` overrides it |
| `targets` | A string is one target. Otherwise a list of strings, or of objects with `name` and an optional `detail` |
| Button | Repeats the verb and the count or object: `Remove container`, `Delete 2 environments`, `Revoke token` |
| Force | A separate page or dialog headed `Force remove …`, never a checkbox on the ordinary confirmation |
| Force marker | A force page posts a hidden `confirm_force=yes`. A post that carries `force` or `volumes` (containers) or `remove_orphans` (Compose stacks) without it answers `400` and changes nothing |
| Batch names | A batch form posts a hidden `display_name` beside `expected_name` for each container. The result names a container by the name its action read, else by `display_name`, else by short id. `display_name` is never used to find an object |
| Reviewed state | A confirmation that binds to what was reviewed posts it back (`expected_name`, `expected_digest`, `expected_version` and the like) |

### Dialogs

| Kind | With script | Without script |
|---|---|---|
| Modal (`confirm_button`, `confirmation_dialog`) | The opener click is cancelled and the dialog opens with `showModal()`. The opener is remembered. On `close`, Escape and Cancel included, focus returns to the opener; if the opener is gone, to `#main`. A click on the backdrop closes it | `nojs.css` hides `.dialog` and shows `.dialog:target`: following the opener link shows that dialog alone, and Cancel returns to the opener |
| Inline (an htmx-fetched `<dialog open data-confirm-dialog>`, such as `containers/_confirm.html` and `networks/_attach.html`) | On `htmx:afterSettle` the element that issued the request is recorded. Escape and Cancel remove the dialog, empty its region and focus the recorded element | The link's `href` is a confirmation page |

- The opener is `<a class="btn btn--danger" href="#<dialog_id>" id="<dialog_id>-opener" data-dialog-open="<dialog_id>" aria-haspopup="dialog">`. Cancel is `<a class="btn" href="#<opener id>" data-dialog-close>`.
- `opener_id` names an opener whose id is not `<dialog_id>-opener`, such as a button written by hand in a table row. Pass it to `confirm_button` and `confirmation_dialog` both.
- `data-dialog-return="<id>"` on an inline dialog sets the element that takes focus when it closes, in place of the recorded one. Container action links have stable ids, `act-<container id>-<action>`.
- `hidden` on `confirmation_dialog` maps names to values of extra hidden inputs, such as `expected_name`.
- A dialog keeps `aria-labelledby` (its title) and `aria-describedby` (its body).

## Empty, loading and error states

| State | Rule |
|---|---|
| Empty list | `data_table` shows `empty_title` and `empty_body`. An administrator is told the action and given its link (`empty_action_label`, `empty_action_href`). A standard user is told `Ask an administrator for access to more …` where nothing is visible to them |
| Filtered and empty | `filtered=true` shows `Nothing matches these filters` and a `Clear filters` link to `clear_href`, or to `pager.base_path` |
| Empty page after the first | The table shows `Back to the first page` |
| Loading | A region that is the target of an htmx request carries `<p class="loading" role="status">Loading…</p>`. `app.js` sets `aria-busy="true"` on `detail.target` at `htmx:beforeRequest` and restores the previous value at `htmx:afterRequest`. Polls (`hx-trigger` with `every`) and requests that target `<body>` are left alone. The stylesheet shows `.loading` inside a busy region, or as its next sibling, and sets `cursor: progress`. A busy region is not dimmed |
| Error | [Errors](#errors) |
| Host not reachable | `unreachable_copy` ([Error copy](#error-copy)) |

## Live regions

| Element | Rule |
|---|---|
| `#toasts` | `role="region"`, `aria-label="Notifications"`, no `aria-live`. Each toast carries its own role: `alert` for `warn` and `danger`, `status` for `info` and `ok` |
| Server-rendered toast | Stays until dismissed. `app.js` removes it and appends it again 100 ms after load, so its role is announced |
| Toast raised by script (`showToast`) | `ok` and `info` dismiss after 20 seconds. The timer stops while the pointer or focus is on the toast. `warn` and `danger` stay until dismissed |
| Dismissal | The close button, or `Escape` outside a dialog, which dismisses every toast |
| Without script | Toasts are static, in the page flow, and the close buttons are hidden |
| Asynchronous check | The connection test, endpoint preview, file validation, update check and the registry and object storage tests each keep a hidden `role="status"` line, replaced out of band by `check_status`. The visible result has no live role |
| Log view | `data-log-output` is `role="log"` with `aria-live="off"`. A hidden `role="status"` line (`data-log-announce`) is written with the line count when a filter is applied and when the stream ends |
| Tables, panels | Never `aria-live` |
| Static notices | A confirmation, certificate or review panel is `role="note"`, not `role="alert"` |

## Focus and keyboard

| Rule | Value |
|---|---|
| Focus style | One rule in `base.css`: `:focus-visible`, and `:focus-within` on `date`, `datetime-local`, `month`, `time` and `week` inputs, take `outline: var(--focus-width) solid var(--color-focus)` and `outline-offset: var(--focus-width)` |
| Removed outline | Only `main:focus`; `#main` takes focus from script and the skip link. No other rule removes an outline |
| Tab order | Document order. No positive `tabindex`. `tabindex="-1"` only on elements script focuses (`#main`) |
| Scrollable regions | `.data-table__scroll`, the log output and `<pre>` content views have `tabindex="0"` and a label, so they scroll by keyboard |
| Skip link | `base.html` has a link to `#main` |
| Terminal | Tab goes to the terminal. `Ctrl+Shift+F6` moves focus to `Back to the container`; the terminal page states this above the screen ([Containers](Usage-Containers#leaving-the-terminal)) |

## Narrow viewports

| Rule | Value |
|---|---|
| Breakpoint | `40rem`; the sidebar becomes a drawer below `48rem`, and stacks above the page without script |
| Optional columns | `Column(key, label, optional=True)`. `data_table` lists the 1-based positions of optional columns in `data-optional="2 4"` on the `<table>` and puts `.col-optional` on their `<th>`. Below the breakpoint those positions are hidden in the head and in every row, so cells need no class |
| Wide-only columns | `Column(key, label, wide_only=True)`. `data_table` lists their positions in `data-wide`. At a viewport of `90rem` or narrower those positions are hidden in the head, the `<colgroup>` and every row |
| Hand-written table | Sets `data-optional` and `.col-optional` itself |
| Never optional | The name, the status and the actions column. The value of an optional column is on the linked page |
| Overflow | Every table sits in `.data-table__scroll` (`position: relative`), which scrolls inside the page. The document never scrolls sideways |
| Names | A name cell (`<th scope="row">`) wraps with `overflow-wrap: anywhere` |
| Forms | `.form` and `.field` grids use `minmax(0, 1fr)`; `.form-pair` is one column; the page's side padding is `--space-4` |
| Select column | The header text of the `select` column is visually hidden |

## Copy

| Term | Use | Not |
|---|---|---|
| Environment | The managed entity | Endpoint, host (for the entity) |
| Docker host | The machine an environment reaches | — |
| Stack | Either kind; `Compose stack` or `Swarm stack` where the kind matters | Project, except `Compose project` for an unmanaged one |
| Service | A Swarm service only. A Compose service is a `service in the stack file` | — |
| Automation | One configured automation. Its kind is its `Type`, such as `container-health-checker` | Rule, job. A job is the scheduler's |

| Verb | Use |
|---|---|
| `Create` | A new object |
| `Save` | An edit |
| `Remove` | Docker objects, access controls and grants |
| `Delete` | dockmin's own records: users, teams, automations, environments, registries, targets |
| `Revoke` | An API token, a webhook URL |
| `Cancel` | Leaves without changing anything |
| `Back to <page>` | Returns to a named page |

- Times: `<time datetime="{{ value|timestamp_iso }}">{{ value|timestamp }}</time>` everywhere. Durations go through `duration`. Templates contain no `strftime` and render no raw datetime.
- A refusal or validation message names the next step.
- Product strings in services are copy where they reach a page. Activity reason codes are not copy.

## Page shell

`base.html` renders the top bar, the sidebar ([`_sidebar.html`](https://github.com/Sphexi/DockMin/blob/main/app/templates/_sidebar.html)) and the page. Signed out, there is no sidebar and no user menu.

| Part | Contents |
|---|---|
| Top bar | Menu button (narrow viewports), brand, breadcrumbs, theme toggle, user menu (`Account`, `API tokens`, `Sign out`) |
| Sidebar | `Environments` group (`All environments`, and `Groups` and `Tags` for administrators); the environment switcher and the current environment's pages; `Operations` (`Automation`, `Templates`); `Teams and users`; `Administration`; `UI kit` when development checks are on |
| Footer | Product version and Compose CLI version, below the page |

| Sidebar item | Shown to |
|---|---|
| `All environments`, the environment pages, `Automation`, `Templates`, `Teams` | Every signed-in user |
| `Groups`, `Tags`, `Users`, Swarm `Nodes`, and the `Administration` group: `Jobs`, `Activity`, `Registries`, `Template catalogues`, `Object storage`, `Backups`, `Certificate`, `Notifications`, `Encryption`, `Instance` (in that order) | Administrators |
| `Swarm` section | An environment of type `swarm` |

Sidebar data:

| Name | Value |
|---|---|
| `nav_environments` | The environments the caller may see, by name, at most 100: `id`, `name`, `status`, `connection` (`Local socket` or `TCP+TLS`) |
| `nav_environment` | The environment named by the route's `environment_id`, when the caller may see it. On a page with no `environment_id`, the [remembered environment](#remembered-environment) when there is one. Otherwise `None`. Adds `engine_version`, `is_swarm` and `counts` |
| `nav_environment_remembered` | `True` when `nav_environment` is the remembered environment, not the page's own. No sidebar item of the environment is marked current then |
| `nav_counts_pending` | `True` when a snapshot refresh is pending for the environment; the counts list then also reloads on `load` ([Sidebar counts](#sidebar-counts)) |
| `counts` | `containers`, `images`, `volumes`, `networks`, `stacks`, `services`, `nodes` from the stored snapshot; any may be `None`. `None` for a standard user |
| `nav_key` | The current sidebar item, from the route name by `navigation_key()` in [`app/web/nav_keys.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/nav_keys.py); the first matching prefix wins. Empty for a page outside the sidebar |

- `resolve_navigation()` in [`app/web/sidebar.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/sidebar.py) loads the sidebar data for a full page from stored records only. An htmx fragment, an API request, a bearer request and a WebSocket load none. No host is contacted.
- Groups are `<details>` elements; the server opens the group that holds the current page. `nav.js` remembers the groups a user opens or closes in `localStorage` (`dockmin.nav.open`), opens the sidebar as a drawer below `48rem`, shows the switcher's filter box, and closes open menus on Escape or an outside click.

### Remembered environment

The sidebar keeps showing the last environment on pages that belong to none.

| Item | Value |
|---|---|
| Stored | On the user's session, as `nav_environment_id`. A second session of the same user has its own |
| Written | By `resolve_navigation()` after a full page whose route has an `environment_id` the caller may see, when it differs from the stored one. An htmx fragment, an API request, a bearer request and a WebSocket write nothing. A write that fails is dropped |
| Shown | On a full page whose route has no `environment_id`: the switcher names the remembered environment, with its status and the links to its pages. Administrators see its counts from the stored snapshot. The environment's links carry no current marker, and the counts are not marked refresh-pending |
| Visibility | The caller must still be able to see it, by the rule of the environment gate ([Environments](Usage-Environments#access)) |
| Forgotten | On a page with no `environment_id`, when the caller can no longer see the remembered environment: the stored value is cleared. When the environment is deleted: the database clears it. When the session ends: it goes with the session |
| Replaced | By the environment of any later page that names one the caller may see |
| Not remembered | An environment the caller cannot see, or that does not exist |

### Breadcrumbs

`base.html` builds the crumbs from the sidebar key (`nav_key`) and three variables a page template sets at its top level. The resource name and the link to its list come from the maps beside `nav_labels` in `base.html`.

| Variable | Value |
|---|---|
| `crumb_object` | The object's name. Rendered as text, escaped |
| `crumb_object_href` | The object's page. Without it the object crumb is plain text |
| `crumb_page` | The page title, for a page under an object or a create page |

| Page | Crumbs |
|---|---|
| List | `Environments / {environment} / {resource}`, the resource as the current crumb. Outside an environment: `{resource}` alone |
| Object page | `… / {resource}` (link to the list) `/ {object}` (current) |
| Page under an object | `… / {resource}` (link) `/ {object}` (link to `crumb_object_href`) `/ {page}` (current) |
| Create page | `… / {resource}` (link) `/ {page}` (current) |
| Environment page | `Environments / {environment}` (current). With `crumb_page`: `{environment}` (link) `/ {page}` (current) |

### Origin

An object page reached from another object's page shows a second button, `Back to <kind> <name>`, beside its own back button.

| Rule | Value |
|---|---|
| Parameter | `from=<kind>:<environment id>:<key>`, at most 320 characters. The kinds are `stack`, `container`, `service`, `network`, `volume`, `image`, `automation` and `activity`. `automation` and `activity` use `-` for the environment and a row id for the key. Anything else is no origin |
| Resolution | Per request, under the viewer's access, by [`app/services/origins.py`](https://github.com/Sphexi/DockMin/blob/main/app/services/origins.py). An object the viewer cannot read, a missing one, an unparseable value, a host read that fails and a read that takes more than 2 seconds give no button |
| Rendering | The label and `href` come from the object that was read. The parameter is never rendered or echoed, and no value of it can produce an off-site or arbitrary path |
| Route | Adds `dependencies=[RequestOrigin]` from [`app/web/origins.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/origins.py) to a detail route. The templates receive `origin` and `origin_ref` ([Template context and filters](#template-context-and-filters)) |
| Button | The page header calls `back_actions(back_href, back_label, origin)` |
| Carrying it on a link | `with_origin(href, ref)` appends `from` to an internal path. `ref` is the canonical reference of the page the link sits on, `'<kind>:<environment id>:<key>'`, or `origin_ref` for a tab link of the target page. `ref` of `none`, an unparseable `ref`, and an `href` that is not an internal path return `href` unchanged |
| Tabs | Tab links, and a refresh trigger that re-requests a tab, carry `origin_ref`. They never copy the received text |
| Forms | A form does not post `from`, and a redirect after a post does not carry it |
| Chain | Only the immediate origin is kept. The target page's own links carry the target as their origin, not its origin |
| Links that carry it | Only links to a page that shows `back_actions` |

### Sidebar counts

| Item | Value |
|---|---|
| Route | `GET /environments/{id}/navigation-counts?active={nav key}` renders the `navigation_counts` partial. `active` outside the known keys is treated as empty |
| Shown to | Administrators. Anyone else receives an empty `204`; a standard user's sidebar carries no counts |
| Unknown environment | `404` |
| Wait | Up to 10 s for a refresh already pending for the environment ([Environments](Usage-Environments#snapshot-cache)). The route never starts a refresh |
| Cache | `Cache-Control: no-store` |
| Reload triggers | The browser event `counts-changed` on `body`; the `load` event when the page is rendered while a refresh is pending for the environment |
| Swap | The list replaces itself (`outerHTML`); a newer request replaces one in flight (`hx-sync="this:replace"`) |

| Source of `counts-changed` | When |
|---|---|
| Response header `HX-Trigger` | A non-`GET` htmx request answered `2xx` after an audited mutation of the environment committed during the request. Merged with any trigger the response already carries. Not added to an `HX-Redirect` answer |
| Script | An image transfer or stack operation page, when the job ends |

## Icons

| Item | Value |
|---|---|
| Set | Lucide, as one SVG sprite: `static/vendor/lucide/sprite.svg`, symbol ids `i-<name>` |
| Macro | `icon(name, extra="")` in `partials/icon.html`; import it with context. The icon is `aria-hidden`; the control carries the name |
| Adding an icon | Copy the `<symbol>` of `icons/<name>.svg` from the `lucide-static` package of the same version into the sprite, with the attributes of the existing symbols |

## Favicon

| Item | Value |
|---|---|
| File | `static/icons/favicon.svg`: the Lucide `container` glyph, stroked in the accent colour. A `prefers-color-scheme: dark` rule inside the file switches to the dark accent. The file holds its own colour values, outside `tokens.css` |
| Link | `<link rel="icon">` in `base.html` |
| `GET /favicon.ico` | `308` to `{root_path}/static/icons/favicon.svg`. No sign-in is needed, and the setup gate does not redirect it |

## Design tokens

[`app/static/css/tokens.css`](https://github.com/Sphexi/DockMin/blob/main/app/static/css/tokens.css) is the only stylesheet containing a colour value. Component rules in `base.css` and `components.css` reference custom properties and nothing else: no hex literal, no `rgb()`, no `hsl()`, and no `prefers-color-scheme` branch.

Tokens cover colour, spacing (`--space-1` … `--space-8`), radius, font stacks, the type scale, weights, line heights, sizes, border and focus widths, motion duration, and two elevation shadows.

Three blocks define the palette, and adding a colour means adding it to all three:

| Block | Applies when |
|---|---|
| `:root` | Always — the light values |
| `[data-theme="dark"]` | The user chose dark |
| `@media (prefers-color-scheme: dark) { :root:not([data-theme]) }` | No choice stored, OS prefers dark |

The second and third blocks hold identical values. A token present in only one of them gives a theme that is correct in one path and broken in the other.

Non-colour tokens are theme-independent and are declared once, in the second `:root` block.

- The terminal's ANSI colours are tokens in all three blocks (`--color-terminal-*`). `.terminal-frame` maps them to `--terminal-*`, which `terminal.js` reads through `getComputedStyle`.
- Chart colours are `--color-chart-1` to `--color-chart-4` and the plot grid `--color-chart-grid`, in all three blocks. `.stats-slot--1` to `.stats-slot--4` set `color` to a series colour; the SVG strokes and fills use `currentcolor`. `.stats-slot--0`, the memory limit, uses `--color-border-strong`.

| Pair | Minimum contrast |
|---|---|
| Body, muted, link, field and table text on the surface it sits on | 4.5:1 |
| Button and badge text on its fill, every tone | 4.5:1 |
| Terminal text colours on the terminal background, both themes | 4.5:1 |
| Input borders, control outlines and the focus ring against the adjacent surface | 3:1 |
| A status fill that carries no text of its own | 3:1 |
| A chart series colour against the surface | 3:1 |

## Theme

The server decides the theme; script only changes it afterwards.

1. `requested_theme()` in [`app/web/theme.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/theme.py) reads the `dockmin_theme` cookie and returns `light`, `dark`, or `None`. Any other value is treated as `None`; the value is written into the opening `<html>` tag.
2. `base.html` emits `data-theme="…"` on `<html>` when a choice is stored, and no attribute when it is not.
3. With no attribute, the `prefers-color-scheme` block supplies the dark palette. The OS default is resolved in CSS, not on the server, and needs no script.
4. The toggle button is `hidden` in the markup and unhidden by `app.js`. Its text is constant (`Dark theme`) and `aria-pressed` carries the state. Clicking it rewrites `data-theme` on `<html>`, updates `aria-pressed` and stores the choice in the cookie for a year.

First paint is correct: the attribute is in the HTML the server sent. Nothing about the theme runs before paint.

## Pagination

`data_table` and `pagination` both pass their `pager` argument through the `require_pager()` global, which raises `TypeError` unless the value is a `PageView`; the render fails rather than producing an unpaginated table.

`PageView` is defined in [`app/web/pagination.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/pagination.py) and built from a repository `Page`:

```python
pager = PageView.of(
    page, base_path="/containers", limit=limit, cursor=cursor, sort=sort, direction=direction
)
```

| Field | Meaning |
|---|---|
| `base_path` | Same-origin absolute path every link is built from |
| `limit` | Page size, 1 to `MAX_PAGE_SIZE` (500) |
| `item_count` | Rows on this page |
| `cursor` / `next_cursor` | Opaque keyset tokens; `next_cursor` is `None` on the last page |
| `sort`, `direction` | Active column key and `asc`/`desc` |
| `params` | Filter pairs reattached to every link, so paging never drops a filter |

Construction validates the limit range, that `base_path` is absolute and not protocol-relative, that `item_count` is not negative, that `direction` is one of the two values, and that both cursors decode. An invalid pager fails at construction, not in the template.

Templates read links and ARIA state off the object — `first_href`, `next_href`, `sort_href(key)`, `aria_sort(key)`, `is_empty`, `has_next`, `is_first_page` — and build no URL themselves. `sort_href` drops the cursor; a cursor encodes a position in the previous ordering.

Columns are `Column(key, label, sortable=False, numeric=False, optional=False, width=None, wide_only=False)`. Mark a column sortable only when the repository can order by that key. An `optional` column is hidden below `40rem` and a `wide_only` column at `90rem` and narrower ([Narrow viewports](#narrow-viewports)). `width` is one of the names in [Column widths](#column-widths).

### Column widths

A column with a width keeps it in every render of the table; a column without one takes the remaining space.

| `width` | Class on `<col>` | Token | Value |
|---|---|---|---|
| `select` | `col--select` | `--col-select` | `2.75rem` |
| `badge` | `col--badge` | `--col-badge` | `8rem` |
| `time` | `col--time` | `--col-time` | `9.75rem` |
| `count` | `col--count` | `--col-count` | `6rem` |
| `short` | `col--short` | `--col-short` | `7rem` |
| `medium` | `col--medium` | `--col-medium` | `12rem` |
| `names` | `col--names` | `--col-names` | `16rem` |
| `actions` | `col--actions` | `--col-actions` | `8.5rem` |
| `actions-wide` | `col--actions-wide` | `--col-actions-wide` | `15rem` |
| `actions-row` | `col--actions-row` | `--col-actions-row` | `26rem`; `28rem` with a coarse pointer |

| Item | Behaviour |
|---|---|
| Fixed table | `data_table` renders `table--fixed` when any column has a width. A column keyed `select` takes `select` without declaring it |
| `<colgroup>` | One `<col>` per column, with the class of its width, or none |
| `data-min` | The widths added up, a column without one counting 7.5rem and a `wide_only` column counting nothing, rounded up to a multiple of 4. The stylesheet has a rule for each multiple of 4 from 28 to 100, setting `min-inline-size` to that many `rem` between `40rem` and `90rem`; the table scrolls inside `.data-table__scroll` below it |
| Wider than `40rem` | `table-layout: fixed`, the `.col--*` widths apply and a cell breaks anywhere in a long word (`overflow-wrap: anywhere`) |
| `40rem` and narrower | The table lays out automatically and its optional columns are hidden ([Narrow viewports](#narrow-viewports)) |
| Headers | Never wrap: `.table thead th` is `white-space: nowrap` at every width |
| Row actions | `actions-row` holds the container list's full set of row buttons on one line. In other fixed tables `.row-actions` wraps inside its column |
| Listed names | A cell or field holding several object names or values (containers, stacks, services, references, subnets) is `<ul class="chip-list">`, adding `mono` for references; each `<li>` is an outlined chip like `port-chip`. A chip breaks only when its name is longer than the whole cell |
| Medium and names | `medium` caps a single-name column such as `Stack` or `Subnets`; `names` sizes a `Used by` column of chips |
| Invalid width | `Column` raises `ValueError` |
| Tokens | `--col-*` are in `tokens.css`; the `rem` values in `data_table.html` mirror them and only size `data-min` |

A hand-written table uses the same classes and `data-min` ([Narrow viewports](#narrow-viewports)).

### List bar

`list_bar` lays out the row above a list: the results line, then the page's actions.

| Item | Behaviour |
|---|---|
| Markup | `<div class="list-bar">` holding the summary and `<div class="list-bar__actions">` with the `{% call %}` body. The actions are right-aligned and wrap with the summary |
| `list_count` | The results line for a list whose page keeps none of its own: `<p id="results-summary" class="results-summary" role="status">`. It reads `N <singular>` for one and `N <plural>` otherwise, the singular with `s` added when `plural` is not given. With `filtered`, `match these filters` follows (`matches these filters` for one). It ends with a full stop and is empty when `total` is none |
| Out-of-band update | `oob=true` adds `hx-swap-oob="innerHTML"`, so a table fragment also replaces the text of the line on the page |
| Lists with their own line | Containers, images, volumes, networks, stacks and the Swarm service and object lists render `results_summary(…)` with the same id and class |

A route that accepts a cursor catches `InvalidCursorError` and `TypeError` and returns 400; a cursor minted under a different ordering compares a string against a number.

## No inline code

| Prohibited | Where behaviour goes instead |
|---|---|
| `<script>` with a body | `app/static/js/app.js`, loaded with `defer`; page-specific script in its own file, loaded from [`head_extra`](#page-specific-assets) |
| `onclick=`, `onsubmit=`, any `on*` attribute | A delegated listener in `app.js`, keyed on a `data-` attribute |
| `style=` | A class in `components.css` |

Elements are addressed from script by `data-` attribute, never by a selector that encodes layout. The hooks are listed in [Data hooks](#data-hooks).

The HTMX configuration in `base.html` sets `allowEval: false`, `allowScriptTags: false`, `selfRequestsOnly: true`, and `includeIndicatorStyles: false`, so HTMX itself neither evaluates strings, nor runs script arriving in swapped content, nor generates inline style.

Every response carries a `Content-Security-Policy` header ([Deployment](Reference-Deployment#content-security-policy)). Its `script-src` and `style-src` are `'self'`, with no `unsafe-inline`, `unsafe-eval`, nonce or hash. A template or script that adds an inline handler, an inline script, a `style` attribute or a `<style>` element is blocked by the browser and logs a console violation.

The terminal page is sent with a second policy, the default plus one allowance:

| Directive | Allowance on the terminal page |
|---|---|
| `style-src-elem` | `'self' 'unsafe-inline'`, for the `<style>` elements xterm.js creates at run time |

`connect-src 'self'` in the default policy admits the same-origin WebSocket, `/environments/{id}/containers/{container_id}/terminal/ws`. A route selects the terminal policy with `use_policy(request, "terminal")`. No other page loads xterm.js.

## Page-specific assets

`base.html` defines an empty `head_extra` block at the end of `<head>`, after `app.js` and `nav.js`. A page loads assets that no other page needs by overriding it:

```jinja
{% block head_extra %}
<script src="{{ static_path('js/terminal.js') }}" defer></script>
{% endblock %}
```

| Page | Loads from `head_extra` |
|---|---|
| `containers/terminal.html` | `vendor/xterm/xterm.css`, `vendor/xterm/xterm.js`, `vendor/xterm/addon-fit.js`, `js/terminal.js` |
| `containers/terminal_form.html` | `js/terminal.js` |

Scripts load with `defer`, so they run in document order after parsing and after `app.js`.

## Vendored libraries

Third-party files are served from `app/static/vendor/`. Each script and stylesheet is loaded with a Subresource Integrity hash (`integrity="sha384-…"`) of the vendored file itself. Update the hash in the template whenever the file changes.

| Library | Version | Directory | Loaded on |
|---|---|---|---|
| htmx | 2.0.11 | `vendor/htmx/` | Every page, from `base.html` |
| xterm.js | 5.5.0 | `vendor/xterm/` | The terminal page only |
| xterm.js fit addon (`addon-fit`) | 0.10.0 | `vendor/xterm/` | The terminal page only |
| Lucide icons | 1.66.0 | `vendor/lucide/` | Every page, referenced by `<use>` from `partials/icon.html` |

- The licence files of xterm.js and its fit addon are in `vendor/xterm/`.
- The Lucide sprite is an SVG image, not a script, so it has no integrity hash. Its licence is `vendor/lucide/LICENSE`.
- The comments above the tags in `base.html` and `containers/terminal.html` record the npm tarball integrity the files were taken from.

## Data hooks

| Hook | Element | Script behaviour |
|---|---|---|
| `data-theme-toggle` | Theme button | Unhidden by script; switches the theme and sets `aria-pressed` |
| `data-dialog-open`, `data-dialog-close`, `data-confirm-dialog` | Dialog opener (a link to the dialog), closer (a link to the opener, or a button in an inline dialog) and dialog | Opens and closes the native `<dialog>`; returns focus to the opener ([Dialogs](#dialogs)) |
| `data-dialog-return` | Inline dialog; value is the id of an element | That element takes focus when the dialog closes |
| `data-toast`, `data-toast-close`, `data-toast-message` | Notification, its dismiss button and its text | The button dismisses the notification. A toast raised by script with level `info` or `ok` dismisses itself after 20 seconds; a server-rendered one does not ([Live regions](#live-regions)) |
| `data-autofocus` | Any focusable element | The first one inside the swapped element receives focus after an HTMX swap settles |
| `data-focus-return` | The control that opens a swapped-in state, such as `Reveal` | When a swap replaces the element that held focus, the first element in the replacement with `data-autofocus` or `data-focus-return` takes focus; without one the replacement itself does |
| `data-revealed` | The element that holds a revealed secret value | Its text becomes 16 `•` characters after 60 seconds and on `pagehide` ([Revealed values](#revealed-values)) |
| `data-optional` | `<table>`; value is the 1-based positions of the optional columns | Read by CSS, not script: those columns are hidden below `40rem` |
| `data-wide` | `<table>`; value is the 1-based positions of the wide-only columns | Read by CSS, not script: those columns are hidden at `90rem` and narrower |
| `data-min` | `<table class="table--fixed">`; value is the minimum width in `rem` ([Column widths](#column-widths)) | Read by CSS, not script |
| `data-target-form`, `data-target-section` | The `Target` section of the automation form, and each group of fields in it; the value of a section is the kinds that use it, separated by spaces | Shows the sections of the chosen `target_kind` and hides and disables the others ([Automation pages](#automation-pages)) |
| `data-code-editor` | `<textarea>` of a stack file | Wraps the field with a line-number gutter and a line and column status ([Code editor](#code-editor)) |
| `data-goto-line` | `<a>` whose `href` is `#` and a textarea's id; value is a 1-based line | Puts the caret at the start of that line, scrolls it into view and focuses the field |
| `data-submit-once` | `<form>` | Disables the form's submit buttons after submission; restored when the page returns from the back-forward cache |
| `data-command-fill` | Button on the terminal form; value is a command | Copies the value into the command field |
| `data-quick-commands` | Container of those buttons | Hidden until script runs |
| `data-nojs-only` | Wrapper of a control that only works without script, such as a `Refresh` link or a `Continue` button. Put it on the wrapper, not on a `.btn` | Hidden once `html[data-enhanced]` is set ([Progressive enhancement](#progressive-enhancement)) |

### Job pages

The image transfer page and the Compose and Swarm stack operation pages use these hooks.

| Hook | Element | Script behaviour |
|---|---|---|
| `data-job-stream` | Panel of a running pull, push or stack operation; value is the URL of the event stream | Script opens the stream while the page is visible, and removes the attribute when the job ends |
| `data-job-body` | Element holding the rendered state | Its content is replaced by each event |
| `data-job-status` | Status line | Text is set from the stream state; hidden when the job ends |
| `data-job-refresh` | `Refresh status` link | Hidden while the stream is open; shown when the stream stops for good; hidden when the job ends |
| `data-job-cancel` | Cancel form. Image transfer pages only; a stack operation has no cancel | Hidden when the job ends |

The page carries `data-job-stream`, and the controls, only while the job runs. Without script the controls stay and the state is the one rendered at load.

### Logs tab

| Hook | Element | Script behaviour |
|---|---|---|
| `data-log-view` | Root of the Logs tab | Binds the log view |
| `data-log-stream`, `data-log-download` | Root | URLs of the event stream and of the download |
| `data-log-running` | Root | `true` or `false`: whether the container was running, or whether the service has desired tasks |
| `data-log-labelled` | Root | Present when each line's text begins with a label: the task that wrote a service line, or the service of a stack line. Script shows the first word of every line as a separate label and the rest as the text |
| `data-log-subject` | Root | `container` (default), `service` or `stack`: the noun in the status line when a followed stream ends (`The container stopped.`, `The service stopped.`) |
| `data-log-invalid` | Root | Present when the form failed validation; no stream is opened |
| `data-log-form` | The selection form | Submitting it reads again instead of navigating |
| `data-log-controls` | Follow, line budget and `Jump to latest` | Hidden until script runs |
| `data-log-follow`, `data-log-budget`, `data-log-jump` | Follow checkbox, line budget select, jump button | |
| `data-log-status`, `data-log-count` | Status line, line count | Hidden until script writes to them |
| `data-log-notice` | `<p>` below the status line | Hidden until a `notice` event arrives on the stream. Holds the text of every notice received since the last read, joined by a space. Cleared when the view reads again |
| `data-log-announce` | Visually hidden `role="status"` line | Written with the line count when a filter is applied and when the stream ends |
| `data-log-output` | Live output region | Hidden until script runs; receives the lines |
| `data-log-static` | The server-rendered recent lines | Hidden by script |
| `data-log-download-link` | `Download text` link | Its `href` follows the form's values, with `format=text` |
| `data-log-download-ndjson` | `Download NDJSON` link | Its `href` follows the form's values, with `format=ndjson` |
| `data-timestamps` | Output region; `on` or `off` | Styling hides the time of each line when `off` |

### Terminal

| Hook | Element | Script behaviour |
|---|---|---|
| `data-terminal` | Session section | Hidden until script starts the session |
| `data-terminal-ws` | Session section | Path of the WebSocket |
| `data-terminal-ticket` | Session section | The one-time ticket; script reads it and removes the attribute |
| `data-terminal-mode` | Session section | `exec` or `attach` |
| `data-terminal-screen` | Element xterm.js renders into | |
| `data-terminal-status` | Status line | Text and `data-state` are set from the session |
| `data-terminal-actions` | `Open a new terminal` and `Back to the container` | Hidden until the session ends |
| `data-terminal-back` | `Back to the container` link in the page header | Receives focus on `Ctrl+Shift+F6` |
| `data-terminal-form` | Terminal form | Script fills the size fields and shows the quick choices |
| `data-terminal-rows`, `data-terminal-cols` | Hidden inputs of the form | Receive the estimated size |
| `data-terminal-probe` | Hidden frame the size is measured from | |

### Stats tab

| Hook | Element | Script behaviour |
|---|---|---|
| `data-stats-stream` | Root of the tab; value is the URL of the event stream | Opened while the tab is on screen and the browser tab is visible |
| `data-stats-body` | Element holding the latest reading | Its content is replaced by each event |
| `data-stats-sample` | The reading's `<dl>`. Also carries `data-cpu`, `data-mem-used`, `data-mem-limit`, `data-net-rx`, `data-net-tx`, `data-blk-read`, `data-blk-write` (raw values) and `data-read-microsecond` | Read to draw the charts. The text of the `<dl>` is the only rendering of the values |
| `data-stats-charts` | Container of the four charts | Carries `hidden`; script removes it when it opens the stream |
| `data-chart` | One chart's `<figure>`: `cpu`, `mem`, `net` or `blk` | Script draws an SVG into `[data-chart-plot]`, the ceiling into `[data-chart-scale]` and each series' latest value into `[data-chart-value]` |
| `data-stats-status` | Status line | `Reconnecting.` or `Stats are no longer available.` |
| `data-stats-refresh` | `Take another reading` link | Hidden while the stream is open |

### Restart page

| Hook | Element | Script behaviour |
|---|---|---|
| `data-restart-poll` | Status panel. Carries `data-boot-id` (the run identifier from before the restart) and, on the restart page, `data-give-up` (seconds) | Polls `/server/instance/boot` every 2 s through htmx. Sets `data-restart-state` to `done` when the identifier differs, or to `failed` when `data-give-up` passes. On the updating page there is no `data-give-up`, and the update hook below sets the state `settled` |
| `data-restart-message` | Progress line | Hidden when the state is set |
| `data-restart-boot` | Hidden target of the poll | Receives the current run identifier |
| `data-restart-done`, `data-restart-failed` | Outcome lines | Shown for their state |

### Update page

The updating page (`server/instance_restarting.html` rendered for an update) carries the restart hooks and these.

| Hook | Element | Script behaviour |
|---|---|---|
| `data-update-poll` | Progress panel. Carries `data-give-up` (seconds) and `data-give-up-phase` | Starts the give-up clock when `[data-phase]` first equals `data-give-up-phase`. Sets `data-update-state` on the panel |
| `data-give-up`, `data-give-up-phase` | Progress panel | Seconds to wait, and the phase (`handed_over`) from which they count |
| `data-phase` | `#update-status`, the polled fragment (`GET /server/instance/update/{update_id}`) | The phase: `pulling_target`, `pulling_helper`, `preparing`, `starting_helper`, `handed_over`, `abandoned` or `finished`. The fragment polls itself every 2 s while the phase is not terminal |
| `data-terminal` | `#update-status` | Present for `abandoned` and `finished`: the poll stops (the fragment carries no `hx-get`) |
| `data-update-failed` | Alert paragraph in the panel | Shown when the give-up time passes before a terminal phase. Hidden again if a terminal phase arrives |
| `data-update-state` | Progress panel | `terminal` when the status is terminal, `failed` when the give-up time passed first. Once set, the give-up timer does not set `failed` |

- On a terminal status the script sets `data-restart-state="settled"` on the `[data-restart-poll]` panel and hides its `[data-restart-message]`. `settled` is a third state beside `done` and `failed`: the boot poll stops without claiming that dockmin restarted.
- Without script the page opens the Instance page after the refresh time.

## Progressive enhancement

HTMX enhances pages that already work. Every page renders and navigates with JavaScript disabled. No action on a Docker host is reachable only through script.

The terminal session is the one exception. The form that opens it renders and submits without script, and the session page, which needs a WebSocket and xterm.js, says `JavaScript is required` and links back to the form. The Logs tab without script shows a bounded tail of recent lines on request ([Containers](Usage-Containers#without-javascript)).

Checks before a page is done, with JavaScript turned off in the browser:

- The page renders, and its content is in the HTML the server sent.
- Every link navigates, including sort links and pagination links.
- Every form submits and the POST handler redirects somewhere sensible, status 303, with a notice ([Flash messages](#flash-messages)).
- Every destructive action is reachable. `nojs.css` is loaded inside `<noscript>`: it hides the controls that need script, and shows a `<dialog>` in the page when the link to it is followed (`:target`), so a confirmation form can still be submitted ([Dialogs](#dialogs)).
- Nothing that only script can reach is the sole route to an action.

Controls that exist only to drive script — the theme toggle, dialog open and close buttons, toast dismiss buttons — are hidden by `nojs.css` or by a `hidden` attribute that script removes. Dialog openers and Cancel are links, so they work without it.

Controls that exist only to replace script — a `Refresh` link, a `Continue` button, the no-script refresh note — carry `data-nojs-only` on their wrapper.

| State | Result |
|---|---|
| Script off, or htmx did not load | `<html>` has no `data-enhanced`. The wrapper is displayed |
| Script on and htmx loaded | `app.js` sets `data-enhanced` on `<html>` after its setup. `html[data-enhanced] [data-nojs-only]` is `display: none` |

A page that replaces such a control with an htmx trigger keeps the control's own `href` or `action` working. The image pull `Continue` is the exception to hiding: it stays visible with script and fetches the credential choices in place (`hx-post` to the choices route).

## Autoescaping

Autoescaping is on and `|safe` appears nowhere in the templates. Undefined names are an error: the environment uses `StrictUndefined`, so a typo in a variable name fails the render rather than rendering an empty string.

**Do not apply `|safe` to any of these.** All are attacker-influenced and contain whatever the Docker host, a remote daemon or a template catalogue returns:

- Container, image, network, volume, stack, and service names
- Image tags, digests, and registry references
- Environment names, labels, and Compose project names
- Daemon error text, Compose CLI output, and container logs
- Template catalogue text
- Anything from a user-submitted form

Where HTML must be built into an attribute, it is serialised and escaped rather than trusted. `base.html` writes its `hx-headers` value as `{{ {csrf_header: csrf_token}|tojson|forceescape }}`.

A value passed to `choice()` is additionally restricted to an allowlist, which keeps free text out of class names and `data-` attributes even if escaping were ever bypassed.

## Template context and filters

Every template receives these, derived from the request alone, without any route passing them:

| Name | Value |
|---|---|
| `request` | The Starlette request |
| `principal` | The authenticated user, or `None` |
| `csrf_token` | This request's token |
| `theme` | `light`, `dark`, or `None` |
| `flashes` | The verified `Flash` objects (`level`, `message`) this request carries; at most 3 ([Flash messages](#flash-messages)) |
| `product_version` | Installed package version, or `dev` |
| `compose_version` | Detected Compose CLI version, or `None` |
| `nav_environments`, `nav_environment`, `nav_environment_remembered`, `nav_counts_pending`, `nav_key` | The sidebar data ([Page shell](#page-shell)) |
| `origin`, `origin_ref` | The resolved origin of the page, or `None`; its canonical reference, or `None` ([Origin](#origin)) |

Globals: `static_path(path)`, `route_path(name, **params)`, `with_origin(href, ref)`, `require_pager(value)`, `choice(value, allowed)`, `csrf_header`.

Filters, defined in [`app/web/filters.py`](https://github.com/Sphexi/DockMin/blob/main/app/web/filters.py) and [`app/services/template_markup.py`](https://github.com/Sphexi/DockMin/blob/main/app/services/template_markup.py):

| Filter | Input | Output |
|---|---|---|
| `timestamp` | `datetime`, epoch seconds, ISO 8601 string, `None`; `seconds=true` adds seconds | `2026-01-30 14:05 UTC`; `—` for `None` or an unparseable value |
| `timestamp_iso` | The same | ISO 8601 for a `<time datetime>` attribute; empty string on failure |
| `filesize` | Byte count or `None` | Binary units, e.g. `4.7 MiB`; `—` for `None` or a negative value |
| `duration` | `timedelta`, seconds, or `None` | Largest two non-zero units, e.g. `2h 5m`; `—` for `None` |
| `relative_age` | The same as `timestamp` | How long ago, e.g. `2 min ago`; `just now` under a minute; `—` for `None` or an unparseable value |
| `visible_text` | Any value; `None` is empty text | Escaped markup in which each character of category `Cc`, `Cf`, `Cs`, `Zl` or `Zp` is a `<span class="visible-char">U+XXXX</span>`. A bidirectional override, a line break or a control character in stored text cannot reorder or hide what follows it |
| `catalogue_markup` | Template catalogue text, or `None` | The allowed formatting as safe markup ([Template pages](#template-pages)) |
| `catalogue_text` | Template catalogue text, or `None` | Plain text with tags removed and whitespace collapsed |

`visible_text` and `catalogue_markup` are the only filters that return markup. `visible_text` builds it from escaped text and a fixed span; `catalogue_markup` builds it from escaped text and a fixed set of tags. A filter in `filters.py` never raises on a malformed value. One bad timestamp renders a dash rather than failing the page.

## Conventions checklist

- [ ] The route returns a partial for an HTMX request and a page otherwise.
- [ ] New partials are in `templates/partials/`, named for what they render, one macro per file matching the file name.
- [ ] Targets and swaps are in the template; no HTMX attribute is built in JavaScript.
- [ ] Every HTMX control has a working `href` or `action` fallback.
- [ ] Mutating forms carry the hidden `csrf_token` input.
- [ ] Errors return a fragment or a page, at the real status code.
- [ ] No colour literal outside `tokens.css`; a new colour is added to all three palette blocks.
- [ ] Lists are rendered from a `PageView`.
- [ ] No inline `<script>`, no `on*` attribute, no `style=`.
- [ ] Page-specific assets load from `head_extra`; a vendored file carries a Subresource Integrity hash.
- [ ] The page works with JavaScript disabled.
- [ ] No `|safe`.
- [ ] The page has one `<h1>` (from `page_header`) and no skipped heading level.
- [ ] Every form control has a `<label>`, `aria-label` or `aria-labelledby`. An accessible name starts with the visible text. A control that shows only an icon or symbol has a name.
- [ ] Every table has a `<caption>`, `<th scope="col">` headers and the name cell as `<th scope="row">`.
- [ ] Every state shown in colour also shows text; every `.badge` has text.
- [ ] Nothing removes the focus outline; every focusable element shows it; no positive `tabindex`.
- [ ] Every confirmation renders its targets through `confirm_targets`, and a force variant is a separate confirmation that posts `confirm_force=yes`.
- [ ] A dialog opener is a link to the dialog and Cancel is a link to the opener, so both work without script, and focus returns to the opener.
- [ ] Every list passes an empty state by role; every htmx target carries a `.loading` line; an asynchronous check keeps its `check_status` line.
- [ ] No `aria-live` on a table, a panel or a log; toasts carry their own role.
- [ ] A successful non-htmx post redirects (`303`) with a `Flash`, or to a result page; a failed post renders the form at its real status.
- [ ] Times are `<time datetime>` through `timestamp`; durations go through `duration`.
- [ ] Text meets 4.5:1 and borders and the focus ring meet 3:1, in both themes.
- [ ] Nothing widens the document at 375 px; columns that can go are `optional`.
- [ ] Copy uses the glossary and verbs of [Copy](#copy).
