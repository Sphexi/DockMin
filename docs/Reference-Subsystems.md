# Subsystems

A subsystem name is the actor recorded against an automated action. Subsystems run as scheduled jobs ([Jobs](Administration-Jobs)), except `activity-log-pruner`, which runs inside the scheduler's housekeeping pass. Rules for every subsystem that takes rules are managed on the Automation pages ([Automation](Operations-Automation)).

| Name | Responsibility |
|---|---|
| `container-updater` | Detects newer images, pulls with the bound credential set, recreates containers, redeploys stacks, updates Swarm services and renews their stored registry credentials |
| `container-health-checker` | Watches Docker health status and restarts containers reporting unhealthy |
| `stack-restarter` | Scheduled periodic restarts of containers and stacks |
| `backup-syncer` | Makes a backup archive on a schedule, uploads it to the rule's S3-compatible bucket and applies the target's remote retention |
| `activity-log-exporter` | Copies the activity log to the configured bucket as gzip-compressed CSV or NDJSON |
| `container-log-exporter` | Copies the new log lines of matched containers, or of the tasks of matched Swarm services, to the configured bucket as gzip-compressed NDJSON |
| `notification-sender` | Delivers queued notifications to the configured channels. One job of its own; takes no rules |
| `webhook-receiver` | Runs a webhook automation when its URL is called: updates or redeploys the named containers, stacks or services ([Automation](Operations-Automation#webhooks)) |
| `activity-log-pruner` | Removes activity events past the retention age. Not scheduled: no job, rule or switch |
| `environment-snapshot-refresher` | Refreshes the cached per-environment counts and status that the multi-host list view reads |

## Name format

- Lowercase kebab-case, ASCII only.
- `<object>-<agent-noun>`, with the object spelled out.
- No version, environment or deployment detail.
- One name per subsystem, however many jobs it registers.
- A name is a stable identifier: activity rows keep the name they were written with.

## Attribution

- Every action a subsystem takes is recorded with the subsystem as actor, the rule where the subsystem takes rules, and the run.
- The scheduler is never recorded as an actor.
- Events carry the rule's id and name in their detail, which stay after the rule is deleted.
- Events and the job tables: [Jobs](Administration-Jobs).

## Properties

Each subsystem declares how its jobs behave. Administrators see them on the job page and in the JSON `catch_up`, `overrun_policy` and `failure_threshold` fields ([JSON API](Reference-JSON-API)).

| Property | Default | Snapshot refresher | `container-health-checker` | `stack-restarter` | `container-updater` | `activity-log-exporter` | `backup-syncer` | `container-log-exporter` | `notification-sender` | `webhook-receiver` |
|---|---|---|---|---|---|---|---|---|---|---|
| Rules | Takes rules | None; one job of its own | Takes rules | Takes rules | Takes rules | Takes rules; administrators only | Takes rules; administrators only | Takes rules; administrators only | None; one job of its own | Takes rules |
| Schedule kinds | Interval, cron | Interval | Interval | Interval, cron | Interval, cron | Interval, cron | Interval, cron | Interval, cron | Interval | Webhook: no schedule, runs when called |
| Minimum interval | 60 s | 15 s | 60 s | 3600 s (interval only; cron is not checked) | 900 s (interval only; cron is not checked) | 3600 s (interval only; cron is not checked) | 3600 s (interval only; cron is not checked) | 900 s (interval only; cron is not checked) | 15 s | Not used |
| Overrun policy | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` |
| Overrun policies allowed | `skip`, `queue` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` | `skip` |
| `cancel_previous` allowed | No; only where runs are safe to cancel | No | No | No | No | No | No | No | No | No |
| Missed runs | Skipped | One catch-up run | Skipped | Skipped | Skipped | Skipped | One catch-up run | Skipped | One catch-up run | Not used |
| Time limit of a run | 30 minutes | 10 minutes | 30 minutes | 30 minutes | 60 minutes | 30 minutes | 180 minutes | 30 minutes | 15 minutes | 60 minutes |
| Object lock wait | 10 s | Not used | 10 s | 60 s | 60 s | Not used | Not used | Not used | Not used | 60 s |
| Suppression window | 5 minutes | Not used | 5 minutes | 5 minutes | 5 minutes | Not used | Not used | 5 minutes | Not used | 0; `suppression_seconds` sets one |
| Failures before a job is disabled | 5 | Never | 5 | 5 | 5 | 5 | 5 | 5 | Never | 5 |
| Event policy | `per_action` | `none` | `per_action` | `per_action` | `per_action` | `failures_only` | `per_action` | `failures_only` | `none` | `per_action` |
| Dry run | Not supported | Not supported | Supported | Supported | Supported | Supported | Supported | Supported | Not supported | Supported |
| Needs an environment | No | No | Yes | Yes | Yes | No | No | Yes | No | Yes |
| Target kinds | None | None | Container, Compose stack, labels, environment | Container, Compose stack | Container, Compose stack, Swarm stack, Swarm service, labels | None | None | Container, Compose stack, Swarm stack, Swarm service, labels, Swarm service labels, environment | None | Container, Compose stack, Swarm stack, Swarm service |

- A subsystem with no implementation in this build is listed `Not available`. Its jobs are kept and do not run.
- A subsystem is switched on or off for all its jobs from the Jobs page ([Jobs](Administration-Jobs)). `activity-log-pruner` has no switch.
- Event policies: [Jobs](Administration-Jobs).

## Rules

- A rule is created disabled and runs only after it is enabled.
- A rule's runs act with its owner's authority. The owner is checked at every run, and again for each object before it is acted on. A run whose owner lost access fails with `owner_access_lost` and writes nothing to the host.
- A manual run and a dry run act with the owner's authority too, whoever starts them. The rule's owner or an administrator may start them ([Automation](Operations-Automation#run-now)).
- Limits: 50 rules per owner, 2 concurrent runs per owner ([Automation](Operations-Automation#limits)).
- Other rule fields: [Jobs](Administration-Jobs). Pages, targets and settings: [Automation](Operations-Automation).

## `environment-snapshot-refresher`

| Item | Value |
|---|---|
| Job | One, always present. Created at first start; no rule |
| Work | Refreshes the status and object counts of every environment |
| Schedule | Interval of `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS`, 15 to 3600 s, default 60 s ([Configuration](Reference-Configuration)) |
| Overrun | A refresh pass still running when the next is due makes a `skipped` run, reason `overrun` |
| Missed runs | One catch-up run at start after a stop that left the next run more than 60 s past |
| Activity rows | `environment_unreachable` when an environment's status moves from `reachable` to `unreachable`, and `environment_reachable` when it moves back. Result `success`, category `automation`, target `environment`, no automation, no run. Detail: `previous_status`, and `failed_stage` when there is one. No row for a first observation or any other change. Each status change is also a log line named `environment_status_changed` |
| Failures | Recorded as `failure` runs. The job is never disabled and never backed off |
| Where to see it | `Jobs`, under the subsystem name ([Environments](Usage-Environments)) |

- A refresh requested on demand (`Refresh counts`, after an environment is created, after a connection change) is not a run.
- While the job is paused, or the subsystem is off, snapshots age and are marked `Stale` ([Snapshot cache](Usage-Environments#snapshot-cache)). Refreshes requested on demand still work.

## `container-updater`

| Item | Value |
|---|---|
| Job | One per rule |
| Work | Checks the image references of its target against their registries, with the bound set or, for an unbound reference, anonymously. A check that reaches no verdict fails the object. In update mode, pulls newer images with the credential set each reference is bound to (anonymously when unbound), recreates containers with verification and rollback, and redeploys a Compose stack ([Automation](Operations-Automation#container-updater)). For a Swarm service, sends the new digest with the bound set's credential, watches the rollout and rolls the service back when the update pauses or leaves fewer tasks running ([Automation](Operations-Automation#swarm-services)). For a Swarm stack, redeploys it when an image changed and nothing would be removed, created or overwritten, with no verification and no rollback ([Automation](Operations-Automation#swarm-stacks)). Renews the stored registry credential of services bound to an Amazon ECR set every 6 hours ([Automation](Operations-Automation#credential-renewal-for-swarm-services)) |
| Targets | A container, a Compose stack, a Swarm stack, a Swarm service, containers with labels |
| Settings | `action` (`report` or `update`, default `report`), `verify_seconds` 0 to 300 (default 30), `require_healthy` (default on; not used for Swarm services), `prune_replaced` (default off; administrator-owned rules in `update` mode, not for a Swarm service target) and `prune_after_hours` 1 to 720 (default 24) |
| Schedule | Interval of 900 s or more, or cron |
| Per run | At most 100 references checked, 200 containers or Swarm services examined, 25 recreates, redeploys and service updates, 25 pulls, 25 credential renewals; none begins after 30 minutes of the run. With `prune_replaced`, 20 replaced-image records examined and 10 images removed |
| Replaced images | With `prune_replaced`, records the image each container recreate or Compose stack redeploy replaced, and removes it after the grace period unless it carries a tag, a container uses it, an image depends on it or a Swarm service names it. A Swarm stack redeploy records nothing. Turning the setting off deletes the waiting records ([Automation](Operations-Automation#removal-of-replaced-images)) |
| Swarm | The nodes pull; dockmin pulls nothing and writes no `image_pulled` row. Credentials: the reference's bound set, never another; none for a reference with no binding or a `credential_missing` binding |
| Activity rows | `image_pulled`, `container_recreated`, `container_update_rolled_back`, `image_removed`, `stack_redeployed`, `service_updated`, `service_rolled_back`, `service_credentials_refreshed` and `image_update_refused`, with the rule and the run. Report mode writes none. A run cancelled during verification that ends rolled back writes `container_update_rolled_back` with `interrupted` ([Automation](Operations-Automation#verification)) |
| Access checks | The owner's access to the matched containers is decided in one pass; at most 25 containers whose listed labels were cut get a check of their own per run, and later runs reach the rest ([Automation](Operations-Automation#owner-access-checks)) |
| Also writes | The update status of each container and Compose reference it checks ([Images](Usage-Images)). Nothing for Swarm services and stacks |
| Where to see it | `Automation`, and `Jobs` under the subsystem name |

## `webhook-receiver`

| Item | Value |
|---|---|
| Job | One per rule, with the schedule kind `webhook`: no fire times, armed by a call ([Automation](Operations-Automation#call-limits)) |
| Work | Applies the rule's `action` to its named targets in one pass of the `container-updater` code: `update` pulls newer images with the bound credential set and recreates, redeploys or updates; `redeploy` recreates, redeploys or force-updates without a registry request ([Automation](Operations-Automation#webhook-actions)) |
| Targets | Named containers, Compose stacks, Swarm stacks and Swarm services, 1 to 20 names. No labels, no whole environment |
| Settings | `action` (`update` or `redeploy`, default `update`), `verify_seconds` 0 to 300 (default 30), `require_healthy` (default on; not used for Swarm services) |
| Schedule | None. A call to the rule's URL arms one run at once, or 60 s after the start of the job's previous run |
| Runs at once | 3 across all webhook rules |
| Per run | The caps of `container-updater` for recreates, redeploys, service updates and pulls. No credential renewal, no removal of replaced images |
| Run detail | `calls` and `caller_address` for a run a call started ([Automation](Operations-Automation#webhook-run-history)) |
| Activity rows | `container_recreated`, `container_update_rolled_back`, `stack_redeployed`, `service_updated`, `service_rolled_back`, `service_force_updated`, `image_pulled` and `image_update_refused`, with the rule and the run, `trigger` `webhook` and `webhook_action` |
| Where to see it | `Automation`, and `Jobs` under the subsystem name |

## `container-health-checker`

| Item | Value |
|---|---|
| Job | One per rule |
| Work | Restarts a running container that reports `unhealthy`, at most `max_restarts` times in `window_seconds`. A container that has used its budget is held and reported ([Automation](Operations-Automation#budget-and-hold)). A user can release a hold, and with `auto_release` a held container that stays healthy long enough is released ([Automation](Operations-Automation#release)). A held container the host no longer has is released by the run, with `auto_release` on or off ([Automation](Operations-Automation#a-container-that-is-gone)) |
| Targets | A container, a Compose stack, containers with labels, every container in the environment |
| Settings | `max_restarts` 1 to 20 (default 3), `window_seconds` 300 to 86400 (default 3600), `auto_release` (default off) |
| Schedule | Interval of 60 s or more. A health event on the host brings the next run forward ([Automation](Operations-Automation#early-runs-and-the-polled-fallback)) |
| Per run | At most 25 restarts, 200 containers examined |
| Activity rows | `container_restarted`, `container_restart_budget_exhausted` and, for an automatic release or a held container that is gone, `container_restart_budget_released`, with the automation and the run |
| Access checks | As for `container-updater`: one pass over the matched containers, at most 25 own checks per run for containers whose listed labels were cut ([Automation](Operations-Automation#owner-access-checks)) |
| Where to see it | `Automation`, and `Jobs` under the subsystem name |

## `stack-restarter`

| Item | Value |
|---|---|
| Job | One per rule |
| Work | Restarts a running container, or stops and starts the running services of a Compose stack in dependency order ([Automation](Operations-Automation#stack-restarter)) |
| Targets | A container, a Compose stack |
| Settings | None of its own |
| Schedule | Interval of 3600 s or more, or cron |
| Activity rows | `container_restarted` or `stack_restarted`, with the rule and the run |
| Where to see it | `Automation`, and `Jobs` under the subsystem name |

## `activity-log-exporter`

| Item | Value |
|---|---|
| Job | One per rule. Rules are administrators only and take no environment or target selector |
| Work | Copies activity events past the rule's cursor to the object storage target as gzip-compressed CSV or NDJSON, then moves the cursor ([Activity](Administration-Activity)). Deletes nothing |
| Targets | None |
| Settings | `object_storage_target_id`; `format`, `csv` (default) or `ndjson` |
| Schedule | Interval of 3600 s or more, or cron. Overrun policy `skip` only |
| Per run | At most 50 objects of up to 20,000 events or 32 MiB of CSV or NDJSON; none begins after 20 minutes of the run |
| Activity rows | `activity_log_exported`, on failure only, with the rule and the run |
| Retention | The lowest cursor of all its rules holds events back from retention ([Activity](Administration-Activity)) |
| Configuration pages | `Automation`, `New automation` for the automations ([Automation](Operations-Automation#create)); `Object storage` for the targets ([Object storage](Administration-Object-Storage)) |
| Where to see it | `Jobs` under the subsystem name, and `Automation` for an existing rule |

## `container-log-exporter`

| Item | Value |
|---|---|
| Job | One per rule. Rules are administrators only, and each has an environment and a target |
| Work | Copies the log lines each matched container has written since the rule's cursor to the object storage target, as one gzip-compressed NDJSON object per container per run, or per task of a Swarm service. Reads the Engine's log; changes nothing on the host ([Automation](Operations-Automation#container-log-exporter)) |
| Targets | A container, a Compose stack, containers with labels, every container in the environment, a Swarm stack, a Swarm service, or Swarm services with labels ([Log exporter Swarm services](#log-exporter-swarm-services)). Stopped containers and Swarm task containers on the host are matched like any other by the first four |
| Label convention | `dockmin.export-logs=true`. A labels target naming it selects the containers that carry it. dockmin reads no label by itself |
| Settings | `object_storage_target_id`. The format is always NDJSON ([Containers](Usage-Containers)) |
| Schedule | Interval of 900 s or more, or cron. Overrun policy `skip` only. A missed run is not made up |
| Per run | At most 50 containers examined. For each, at most 100,000 lines or 32 MiB of NDJSON before compression, read for at most 120 s. No container begins after 20 minutes of the run. A service target has its own limits ([Log exporter Swarm services](#log-exporter-swarm-services)) |
| Order | Containers with no cursor first, then the container examined least recently, then by name |
| Left for the next run | Containers past 50 or past the start budget (`deferred`), and lines past a container's limits. The run reports `more_pending` `true` |
| Time limit of a run | 30 minutes. The run is safe to cancel: the cursor moves only after an object is stored |
| Activity rows | `container_log_exported`, on failure only, with the rule and the run |
| Who | The owner must be an administrator at every run, else `owner_access_lost`. Standard users cannot create the rule |
| Configuration pages | `Automation`, `New automation`; `Object storage` ([Object storage](Administration-Object-Storage)) |
| Where to see it | `Jobs` under the subsystem name, and `Automation` for an existing rule |

### Log exporter cursor

| Item | Behaviour |
|---|---|
| Kept per | Rule, environment and container id |
| Holds | The timestamp of the newest exported line in nanoseconds, how many exported lines carry exactly that timestamp, and the key of the last object |
| Read | From the cursor's timestamp, inclusive. The first lines stamped exactly then, as many as the count, are skipped. A line is neither repeated nor dropped when several lines share a timestamp |
| Moves | After the object is stored (any `2xx` answer), in its own transaction. Forward only |
| Failed `PUT` | The cursor stays. The next run reads the same lines and writes the same key |
| New container | Starts at the container's creation time |
| Recreated container | A new container id, so a new cursor at its creation time |
| Line without a timestamp | Exported with `time` `null`. It does not move the cursor. Lines with no timestamp among a run's new lines are stored only with a timestamped line; a run whose new lines have none stores nothing (`no_timestamps`) |
| Removed with | The rule. A rule that reuses a deleted rule's id starts with no cursors |
| Pruned | When the container is no longer on the host and was last examined before the oldest run still kept for the rule's job (500 runs) |

A service target keeps its cursors per rule, environment, service and task:

| Item | Behaviour |
|---|---|
| Holds | As for a container, and whether the task is finished. A row without a stored object only marks the task as seen |
| Task id | The 25-character task id. An empty id holds the lines that named no task |
| Moves | After the task's object is stored, in its own transaction. Forward only |
| New task | Starts at the task's creation time |
| Removed | When the task is no longer listed, when the service is gone from the Swarm, and with the rule. A rule that reuses a deleted rule's id starts with no cursors |

### Log exporter Swarm services

| Item | Behaviour |
|---|---|
| Target | A Swarm service target: 1 to 20 exact service names. A stack target naming a stored Swarm stack: each service whose `com.docker.stack.namespace` label equals the stack name ignoring case. A `Services with labels` target: each service whose own labels carry every term ([Automation](Operations-Automation#targets)). A rule naming a Compose stack and a Swarm stack exports both in one run. Saving a service target, a `Services with labels` target or a stack target naming a stored Swarm stack is refused outside a Swarm manager: `This environment is not a Swarm manager.` A run whose stored environment type is not Swarm fails with reason `target_missing` and writes a failure row; it counts toward the failure threshold. A run on an environment that is stored as Swarm but whose host answers as a node that is not a usable manager is `skipped`, reason `environment_unreachable`, `kind` `not_swarm_manager` ([Automation](Operations-Automation#unreachable-environments)) |
| Services with labels | The same term grammar and caps as the labels target: 1 to 8 terms, a key of 1 to 128 printable characters without `=`, a value of at most 256. A term without a value matches the key alone. Matched by dockmin against `Spec.Labels` of the services of one list; the terms are never sent to the Engine as a filter. A service whose labels were cut when read is not matched. Offered by this subsystem only: a stored rule of another subsystem carrying the kind is refused: `This automation cannot target that.` `com.docker.stack.namespace=<name>` selects the services of a stack dockmin does not manage |
| Objects | One per task with new lines, per run. Tasks of the service are listed, the service's log is read once, and the lines are split by the task id Docker names on each line |
| Task start | A task with no cursor starts at its creation time. A task with a cursor starts at the cursor |
| Lines naming no task | Stored under the folder `no-task` |
| Lines of a task that is not listed | Dropped and counted `unlisted_task` |
| Ended tasks | A task in the state `complete`, `failed`, `shutdown`, `rejected`, `orphaned` or `remove` is read from its own log, not from the service's. Once it is read to its end it is marked finished, no longer read, and no longer holds back the start of the service's read |
| Per run | At most 20 services examined, services with no cursor first, then the one examined least recently. The cap counts the services of every service, Swarm stack and `Services with labels` target of the rule together. The rest are `deferred`, and the run reports `more_pending` `true`. At most 100 objects stored across the services; tasks past that keep their cursors |
| Caps | Each read stops at 100,000 lines, 32 MiB of NDJSON before compression or 120 s. The service's live tasks share one read; each ended task is read on its own. A capped read sets `more_pending` |
| Records | The service keys ([Containers](Usage-Containers)) |
| Activity rows | As for containers: `container_log_exported`, on failure only, with target type `service` and the service's id |

- Lines of one task that reach the Engine out of timestamp order across its two streams after a cap are not exported.
- A task far behind the others makes each run read the other tasks' lines again from its position; the 120 s read cap bounds it.

### Log exporter object key

```text
<prefix>/container-logs/rule-<rule-id>-<rule-created-utc>/<environment-id>/<container-name>-<container-id-12>/<yyyy>/<mm>/<dd>/<first-line-ns-20-digits>.ndjson.gz
```

A service target writes under `service-logs/`, one folder per task:

```text
<prefix>/service-logs/rule-<rule-id>-<rule-created-utc>/<environment-id>/<service-name>-<service-id-12>/<task-id-or-no-task>/<yyyy>/<mm>/<dd>/<first-line-ns-20-digits>.ndjson.gz
```

| Part | Value |
|---|---|
| Prefix | The target's activity log folder, without a trailing `/` ([Object storage](Administration-Object-Storage)) |
| Rule folder | `rule-<id>-<created>`: the rule's id and creation time, as `YYYYMMDDTHHMMSS` in UTC. A later rule that reuses the id writes elsewhere |
| Container folder | The container's name, with characters outside letters, digits, `.`, `_` and `-` replaced by `_`, a hyphen, and the first 12 characters of its id. For a service: the same with the service's name and id |
| Task folder | Service keys only. The 25-character task id, or `no-task` for lines that named none |
| Date folders | The UTC date of the first line in the object |
| File name | The first line's timestamp in nanoseconds since the Unix epoch, zero-padded to 20 digits, and `.ndjson.gz`. An object that continues from lines already exported at that same nanosecond has `-<count>` before the suffix, such as `...-2.ndjson.gz` |

- The key depends on the rule, the container and the first exported line, which the cursor fixes. A retried `PUT` overwrites the same object.
- Content type `application/gzip`; gzip header time 0.

### Log exporter failures

| Case | Result |
|---|---|
| Storage `PUT` fails with `unreachable` or a status of 500 or above | Repeated once after 2 s |
| Storage failure that remains | The run stops. `failure`, or `partial` when an object was stored earlier. One failure row. `more_pending` `true` |
| Target missing, secret unreadable, prefix not a valid key | The run stops before reading. `target_missing`, `credential_unreadable`, `prefix_invalid` ([Activity](Administration-Activity)) |
| A container's log cannot be read | That container fails with `logs_unavailable`. One failure row. The run goes on |
| A service's log or a task's log cannot be read | That service fails with `logs_unavailable`. One failure row. The run goes on |
| The log driver is `none`, or the Engine answers `501` | Not a failure. Counted `logs_not_readable` |
| Read reaches 120 s with nothing to store | Counted `timed_out`. `more_pending` `true` |
| The owner may not read a named container | That container fails with `permission_denied`. A container found by a labels or environment target is counted `not_permitted` instead |
| Object path not a valid key | That container fails with `key_invalid` |
| The run ends before any container or service is examined: no named target found, a stored environment that is not Swarm for a service, `Services with labels` or Swarm stack target, a service list over the read limit, or an environment failure that needs a person | `failure` with the reason of the failure. One failure row for the target. Not written by a dry run |

| Failure row | Value |
|---|---|
| Actor | `container-log-exporter`, with the rule and the run |
| Action, result | `container_log_exported`, `failure`. Category `automation` |
| Target | `container` and its full id for a container's failure; `service` and its id for a service's failure; `object_storage_target` and the target's id for a stop on the target and for a run that ended before examining anything |
| Environment | The rule's environment |
| Detail, every row | `reason`, `message`, `rule_id`, `rule_name` |
| Detail, a container's own failure | `container_name` |
| Detail, a service's own failure | `service_name`, and `task_id` when one task failed |
| Detail, a failed `PUT` | `object_path`, `lines`, and `storage_status` and `storage_code` when there are any |
| Detail, a stop on the target | `target_name` when it was read, and `storage_status` and `storage_code` when there are any |

After 5 failed runs in a row the job is disabled ([Jobs](Administration-Jobs)).

### Log exporter run detail

- `target_id`: the target.
- `exported`: containers whose lines were stored in the run. In a service run, the objects stored: one for each task.
- `lines`: lines stored.
- `more_pending`: `true` when containers or lines remain for the next run.
- `failed`: containers that failed.
- `not_permitted`: containers the owner may not read.
- `suppressed`: containers a user acted on inside the leave-alone window. They are read by the next run.
- `deferred`: containers, services or tasks left for the next run.
- `up_to_date`: containers with no new lines.
- `gone`: containers removed before they were read.
- `unaddressable`: containers whose id could not be used.
- `no_timestamps`: containers whose new lines carried no timestamp.
- `timed_out`: containers whose read reached its time limit before a timestamped line.
- `logs_not_readable`: containers whose log cannot be read back.
- `targets`, `target_outcomes`, `not_found`, `not_found_names`: how each name the owner typed fared ([Automation](Operations-Automation#names)).
- `services`, `tasks` and `unlisted_task`: in a service run only. Services read, tasks listed for them, and lines dropped because their task was not listed. They are present even at zero.
- `matched`: a `Services with labels` run only. The services the terms matched, before the 20-service cap. Present even at zero.

Keys appear when above zero, except `target_id`, `lines`, `more_pending` and the three keys above.

### Log exporter dry run

A dry run reads no log, opens no connection and writes nothing. It lists the containers a run would examine, in order.

- `target_id`, `more_pending`.
- `would_export_count` and `would_export`: the containers, by name.
- `with_cursor`: containers that would continue from their cursor.
- `without_cursor`: containers that would start at their creation.
- `next_object_path`: the folder, ending in `/`, that holds the first container's objects. Absent when no container would be examined.

A dry run of a service target reports the tasks that would be read:

- `would_export_count` and `would_export`: the tasks, as `<service name>/<first 12 characters of the task id>` or `<service name>/no-task`.
- `services` and `tasks`: the services and the tasks that would be read.
- `matched`: a `Services with labels` dry run only. The services the terms matched, before the cap.
- `with_cursor` and `without_cursor`: the tasks that would continue from a cursor, and those that would start at their creation.
- `target_id`, `more_pending` and `next_object_path`, which names the first task's folder.

## `notification-sender`

| Item | Value |
|---|---|
| Job | One, always present. Created at first start; no rule |
| Work | Sends the queued notifications of each channel as one message and retries failures ([Notifications](Administration-Notifications)) |
| Schedule | Interval of 60 s, on the grid: it fires when Unix time is a whole multiple of 60 s. Overrun policy `skip` only |
| Early runs | A queued notification brings the next run forward to the time it was queued. A job that is disabled, paused or backing off, and a subsystem that is switched off, are not moved: the job has no next run then, and the queued notifications wait. Requests made meanwhile are folded into one more pass |
| Missed runs | One catch-up run at start |
| Per run, retries | [Notifications](Administration-Notifications#delivery) |
| Time limit of a run | 15 minutes |
| Activity rows | `notification_delivery_failed`, for each message that failed its third attempt. Actor `notification-sender`; the run when the run is recorded; no automation |
| Run detail | `delivered`, `retried` and `failed`: messages in the run |
| Run record | A successful scheduled run leaves no run row ([Jobs](Administration-Jobs)). A manual or catch-up run is recorded |
| Switch | A paused job or a subsystem switched off sends nothing. Queued events stay queued |
| Configuration pages | `Notifications` ([Notifications](Administration-Notifications)). Administrators only |
| Where to see it | `Notifications` for each channel's last success and last error, and `Jobs` under the subsystem name |

## `backup-syncer`

| Item | Value |
|---|---|
| Job | One per rule. Rules are administrators only and take no environment or target selector |
| Work | Produces a backup archive, uploads it to the object storage target, and deletes older archives beyond the target's retention count ([Backups](Administration-Backups)) |
| Targets | None |
| Settings | `object_storage_target_id` |
| Schedule | Interval of 3600 s or more, or cron. Overrun policy `skip` only |
| Missed runs | One catch-up run at start |
| Time limit of a run | 180 minutes |
| Activity rows | `backup_uploaded` for each stored archive and each failure; `backup_remote_pruned` for a retention pass that deleted or failed, with the rule and the run ([Backups](Administration-Backups)) |
| Also writes | A backup record for each stored or failed archive ([Backups](Administration-Backups)) |
| Run detail | `target_id`, `object_path`, `size_bytes`, `retention_deleted`, `retention_kept`, `retention_skipped` |
| Where to see it | `Backups`, `Automation`, and `Jobs` under the subsystem name |

## `activity-log-pruner`

| Item | Value |
|---|---|
| Job | None. No rule, no schedule, no switch, no handler |
| Work | Removes activity events older than `DOCKMIN_ACTIVITY_RETENTION_DAYS`, except those automation reads back and those an exporter has not copied ([Activity](Administration-Activity)) |
| Runs | Inside the scheduler's hourly housekeeping pass, only when the setting is present ([Jobs](Administration-Jobs)) |
| Activity rows | `activity_log_pruned`, one per batch that removes events. Category `automation`, no rule, no run, no environment |
| Not shown | The Jobs page and the subsystem switches do not list it. It is listed in the activity filter |
| Pruned | Its own rows are never removed by retention |

## Adding a subsystem

| Change | Location |
|---|---|
| Registry entry, with whether its actions carry a rule | `SUBSYSTEMS` in [app/services/activity.py](https://github.com/Sphexi/DockMin/blob/main/app/services/activity.py) |
| A name that owns no job | `UNSCHEDULED_SUBSYSTEMS` in [app/services/activity.py](https://github.com/Sphexi/DockMin/blob/main/app/services/activity.py) |
| Row in the table at the top of this page | This page |
| Migration widening the subsystem name checks of `automation_rule` (a subsystem that takes rules), `scheduled_job` (one that owns a job) and `activity_event` | [app/db/migrations/](https://github.com/Sphexi/DockMin/blob/main/app/db/migrations) |
| Implementation declaring the properties above; one that takes rules also implements its target authorisation | [app/subsystems/](https://github.com/Sphexi/DockMin/blob/main/app/subsystems) |
