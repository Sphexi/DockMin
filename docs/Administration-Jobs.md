# Jobs

A job is a schedule owned by one subsystem. A run is one execution of a job. One scheduler runs every job.

| Term | Meaning |
|---|---|
| Subsystem | A named automation actor ([Subsystems](Reference-Subsystems)) |
| Automation | A user-defined target and parameter set for a subsystem. An automation has exactly one job |
| Job | A schedule, an overrun policy, a failure counter and the next time it fires. One per automation. A subsystem without automations has one job of its own |
| Run | One execution of a job, with a trigger, an outcome and a reason |

- The Jobs page, the run pages and the JSON routes are for administrators ([Access](#access)).
- Automations are created and managed on the Automation pages, by their owners and by administrators ([Automation](Operations-Automation)).
- Job and run state is stored in the database and survives restarts.
- A subsystem without automations, such as the snapshot refresher and `notification-sender`, has one job that exists from first start.
- `container-health-checker`, `stack-restarter` and `container-updater` take automations ([Automation](Operations-Automation)). Each automation has one job, listed on the Jobs page.
- `activity-log-exporter` and `backup-syncer` take automations too, for administrators only and without an environment. Administrators create them on the Automation pages ([Automation](Operations-Automation#automations-without-an-environment)); their jobs are listed on the Jobs page.
- `container-log-exporter` takes automations too, for administrators only, with an environment and a target ([Automation](Operations-Automation#container-log-exporter)).
- `webhook-receiver` takes automations whose job has no schedule: a call to the automation's URL starts a run ([Automation](Operations-Automation#webhooks)).
- `activity-log-pruner` owns no job. It is not listed on the Jobs page or among the subsystem switches ([Housekeeping](#housekeeping)).

## Access

| Item | Administrators | Standard users |
|---|---|---|
| Jobs page, job and run pages | Yes | `403` |
| Actions on the pages | Yes | `403` |
| JSON routes | Yes | `403` |
| `Jobs` link, first entry of the `Administration` group in the sidebar | Shown | Not shown |

The Automation pages are open to every signed-in user: a standard user sees and manages their own automations, their jobs' runs and their dry runs ([Automation](Operations-Automation#access)).

- A job or run id that does not exist, or is not a plain number, answers `404`.
- Every page answers `Cache-Control: no-store`.
- Every post carries the CSRF token ([Frontend conventions](Contributing-Frontend-Conventions)) and answers with a redirect, except a refused run, which renders a page at the refusal's status.
- Every action is a form post and works with JavaScript disabled.

## Jobs page

`/jobs`. Heading `Jobs`. Introduction: `The administrator's view of every scheduled job: every user's automations, and the system tasks that have no automation.`

<!-- screenshot: Administration-Jobs-1: the Jobs list with both filters and several rows; an instance with at least one automation, one paused job and one job disabled after failures -->

| Column | Content |
|---|---|
| Name | The automation name, or the subsystem name for a job without an automation. The subsystem name is shown beneath. A job of an automation also has an `Open automation` link to the automation's page |
| Kind | `Automation` for the job of an automation, `System task` for a job without one |
| Environment | The environment of the automation, or `All` |
| Schedule | `Every 5 minutes`, or the cron expression with its time zone. `When called` for a webhook job |
| State | See [States](#states) |
| Next run | UTC. `—` when nothing is scheduled. For a webhook job, `Pending` while a call is waiting and `When called` otherwise |
| Last run | Outcome of the latest run with its start time, linked to the run page when the run has a history row. `Never run` when there is none ([Latest run](#latest-run)) |

| Control | Behaviour |
|---|---|
| `Subsystem` filter | `All subsystems` or one name. Accepts every subsystem name, `activity-log-pruner` included, and shows `Nothing matches these filters` for a subsystem that owns no job. An unknown name answers `422` with `Choose a known subsystem.` |
| `Kind` filter | `All kinds`, `System task` or `Automation`. An unknown value answers `422` with `Choose a known kind.` The two filters combine |
| `Apply filter`, `Reset` | A filter applies as soon as a value is chosen, and with `Apply filter`. `Reset` clears both |
| `Subsystems` | Opens `/jobs/-/subsystems` ([Subsystem switch](#subsystem-switch)) |

- Below 40rem, `Kind`, `Environment`, `Schedule` and `Next run` are hidden.
- 25 jobs per page; `limit` 1 to 500. An invalid `cursor` answers `400`.
- Empty list: `No jobs`, with an `Open subsystems` link.

### Job page

`/jobs/{id}`. Heading: the automation name, or the subsystem name. Sections `Job`, `Actions` and `Run history`.

| Item | Content |
|---|---|
| Facts | State, kind, subsystem, automation, environment, schedule, next run, last run ([Latest run](#latest-run)), pause end, `If a run is still going when the next is due` (the overrun policy), `Missed runs`, `Consecutive failures` with the threshold, `Subsystem switch`, `Automation status` |
| `Missed runs` | `Run once as soon as the application is running again` for a subsystem with catch-up, `Skipped` otherwise |
| Overrun policy labels | `Skip the new run`, `Queue one new run`, `Cancel the running one` |
| Actions | `Run now`, `Pause job` with `Pause for`, or `Resume`, `Enable job` after an automatic disable, `Dry run` where the subsystem supports it |
| Run history | Newest first. Started, trigger, outcome, result (reason and message), duration. 25 per page; `limit` 1 to 500 |

- `Pause for` offers `1 hour`, `4 hours`, `24 hours`, `7 days`. On a job that belongs to an automation, `Pause job` and `Resume` act on the automation.
- A job in a state where it cannot run shows `Cannot run now.` with the reason in place of `Run now`.
- `Dry run` is offered when the subsystem supports it and the job is not `Not available`, `Subsystem disabled` or already running. A disabled automation and a job disabled after failures still offer it.
- A job without an automation shows a note above the facts where its behaviour is set:

  | Job of | Note |
  |---|---|
  | `notification-sender` | Built in; delivers queued notifications to the channels, which are set on the Notifications page (linked) |
  | `environment-snapshot-refresher` | Built in; refreshes each environment's status and object counts. Its interval is `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS`, which takes effect at the next start |

  Another job without an automation shows no note. A job of an automation shows none.
- Below 40rem the run history hides `Trigger` and `Duration`; both are on the run page.
- `Run now`, `Enable job` and the subsystem switch open a confirmation page that changes nothing. `Pause job` and `Resume` act at once.
- Empty run history: `No runs recorded`.

Trigger labels in the history:

| Label | Trigger |
|---|---|
| `On schedule` | `schedule` |
| `Catch-up` | `catch_up` |
| `Queued` | `queued` |
| `Run now` | `manual` |
| `Dry run` | `dry_run` |
| `Webhook call` | `webhook` |

Outcome labels: `Running` (no outcome yet), `Success`, `Partial`, `Failed` (`failure`), `Skipped`, `Interrupted`.

### Run page

`/jobs/-/runs/{id}`. Heading `Run <id>`. Sections `Outcome`, `Detail` and `Job`.

| Item | Content |
|---|---|
| Outcome, `Reason`, `Message` | See [Outcomes and reasons](#outcomes-and-reasons) |
| `Trigger`, `Scheduled for`, `Started`, `Finished`, `Took` | `Scheduled for` is absent for a run no schedule fired. `Finished` reads `Still running` while the run is in progress |
| `Detail` | The recorded key and value pairs. `Yes` and `No` for booleans, `—` for null. `This run recorded no detail.` when empty |
| `Job` | Link to the job, the subsystem, the automation, and `State now` |

| Control | Behaviour |
|---|---|
| `Back to the job` | Opens the job page |
| `Events of this run` | Opens the activity log filtered to the run: `/activity?job_run_id=<id>` ([Activity](Administration-Activity#filters)) |
| `Refresh` | Shown without JavaScript while the run is in progress |

- A dry run shows the note `Dry run: no container, stack or automation was changed.`
- A run in progress reloads its outcome and detail every 2 seconds with JavaScript. Without it the page shows a `Refresh` link and reloads itself every 5 seconds until the run ends. The automation run page behaves the same ([Automation](Operations-Automation#run-page)).

## States

| State | Label | Meaning |
|---|---|---|
| `running` | `Running` | A run is in progress in this process |
| `scheduled` | `Scheduled` | The job has a next run |
| `paused` | `Paused until <time>` | The job, or the automation that owns it, is paused |
| `rule_disabled` | `Automation disabled` | The automation that owns the job is switched off |
| `auto_disabled` | `Disabled after N failures` | The job was switched off automatically ([Failures](#failures)) |
| `subsystem_disabled` | `Subsystem disabled` | The subsystem switch is off ([Subsystem switch](#subsystem-switch)) |
| `unavailable` | `Not available` | This build has no implementation of the subsystem. Its jobs are kept and do not run |
| `unscheduled` | `Nothing scheduled` | The job is enabled and available, and has no next run |
| `on_call` | `Waits for calls` | A webhook job that is enabled and available, with no call waiting. A job with a call waiting is `Scheduled` and its next run reads `Pending` |

- When several apply, the first of these is shown: `Not available`, `Subsystem disabled`, `Disabled after N failures`, `Automation disabled`, `Running`, `Paused`, `Nothing scheduled` or `Waits for calls`, `Scheduled`.
- A job has a next run only when every gate is open: the job is enabled, the automation is enabled, no pause is in force, the subsystem is on and available.
- `Nothing scheduled` is reached when:

  | Cause | Logged |
  |---|---|
  | The scheduler failed to serve the job three times in a row | `scheduler_job_unscheduled` |
  | The stored schedule has no further fire time or cannot be read | `scheduler_schedule_exhausted`, `scheduler_schedule_invalid`, `job_schedule_invalid` |

- A restart recomputes the next run of every enabled job that has none.
- The JSON `state` values are the first column.

## Schedules

A schedule is an interval or a five-field cron expression, with a time zone. Both are validated when saved.

### Interval

| Item | Rule |
|---|---|
| Value | Whole seconds, decimal digits only, up to 10 digits |
| Minimum | 60 s. A subsystem may set another: 900 s for `container-updater` and `container-log-exporter`, 3600 s for `stack-restarter`, `backup-syncer` and `activity-log-exporter`, 15 s for the snapshot refresher and `notification-sender` |
| Maximum | 2,592,000 s (30 days) |
| Phase, job of an automation | Fires keep their phase: the next fire is the previous fire plus whole intervals, the first one after now |
| Phase, job without an automation | Fires when Unix time is a whole multiple of the interval ([System tasks](#system-tasks)) |
| First fire, job of an automation | One interval after the job is enabled or enabled again |
| First fire, job without an automation | The first multiple of the interval after the job is created. A subsystem with catch-up creates its job due at once |
| Time zone | Validated and stored; not used |
| Description | The largest unit among days, hours, minutes and seconds that divides the value: `Every 5 minutes`, `Every hour`, `Every 2 days`, `Every 90 seconds` |

### System tasks

| Task | Interval | Fires |
|---|---|---|
| Job of `environment-snapshot-refresher` | `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS`, default 60 s | When Unix time is a whole multiple of the interval |
| Job of `notification-sender` | 60 s | When Unix time is a whole multiple of 60 s, and as soon as a notification is queued ([Subsystems](Reference-Subsystems#notification-sender)) |
| Scheduler housekeeping | 3600 s | At the top of each hour, UTC. One pass runs at start first ([Housekeeping](#housekeeping)) |

| Interval | Fire times |
|---|---|
| 60 s | Every minute, at second 0 |
| 90 s | `hh:00:00`, `hh:01:30`, `hh:03:00`, `hh:04:30`, and so on. 90 divides 3600, so each hour repeats |
| 70 s | Every 70 s counted from the Unix epoch. 70 divides neither 60 nor 3600, so the offset into the hour changes from hour to hour |

- An interval that divides neither 60 nor 3600 still fires on whole multiples of itself in Unix time.
- After a fire, the next fire is the first multiple of the interval after the current time.
- At start, a stored next run in the future that is not a multiple of the interval moves to the first multiple after start. A stored next run that has passed is served first ([Next run and missed runs](#next-run-and-missed-runs)).
- A changed interval setting takes effect at the next start. The next fire is recomputed on the new multiples; a fire already due is kept.
- After a pause, the next fire is the first multiple at or after the pause end.
- Jobs of automations do not use this grid. Each keeps its own phase.

### Cron

Five fields separated by spaces, at most 128 characters. Runs of whitespace count as one space.

| Field | Values |
|---|---|
| Minute | 0-59 |
| Hour | 0-23 |
| Day of month | 1-31 |
| Month | 1-12, or `JAN` to `DEC` |
| Day of week | 0-7 (0 and 7 are Sunday), or `SUN` to `SAT` |

| Element | Meaning |
|---|---|
| `*` | Every value of the field |
| `N` | One value |
| `A-B` | A range, inclusive. `A` must not exceed `B` |
| `*/S` | Every `S`-th value of the field |
| `A-B/S` | Every `S`-th value of the range |
| `N/S` | Every `S`-th value from `N` to the end of the field |
| `a,b,c` | A list of any of the elements above |

- Names are case-insensitive and are valid in lists and ranges, for example `MON-FRI`, `JAN,JUL`.
- A step is a whole number from 1 to the field's highest value.
- Day of month and day of week combine as in Vixie cron:

  | Fields | A day fires when |
  |---|---|
  | Neither field begins with `*` | Either field matches |
  | At least one begins with `*` | Both fields match |

- Macros, case-insensitive, stored in lower case:

  | Macro | Equals |
  |---|---|
  | `@hourly` | `0 * * * *` |
  | `@daily`, `@midnight` | `0 0 * * *` |
  | `@weekly` | `0 0 * * 0` |
  | `@monthly` | `0 0 1 * *` |
  | `@yearly`, `@annually` | `0 0 1 1 *` |

- Refused:

  | Input | Reason |
  |---|---|
  | Six fields, or any count other than five | Seconds are not supported |
  | `?`, `L`, `W`, `#`, such as `L`, `15W`, `MON#2` | Not supported |
  | `@reboot` and any other macro | Not supported |
  | Empty list items, a missing value, a step of 0 | Malformed |
  | A value outside its field's range, an unknown name | Out of range |
  | A range that runs backwards, such as `FRI-MON` | Malformed |
  | An expression with no matching date within five years, such as `0 0 31 2 *` | Never fires. A leap-day expression such as `0 0 29 2 *` is accepted |
  | More than 128 characters | Too long |

- A cron schedule is described as `<expression> (<time zone>)`.

### Time zones

| Item | Rule |
|---|---|
| Value | An IANA zone name such as `Europe/London` or `America/New_York`. Case-sensitive. At most 64 characters |
| Default | `UTC` |
| Source | The zone database installed with dockmin, not the host's |
| Refused | A name not in the database. The pseudo-zones `localtime`, `Factory`, `posixrules`, `posix`, `right`, and any name beginning `posix/`, `right/` or `SystemV/` |
| Jobs without an automation | Always `UTC` |

### Daylight saving time

A cron expression is evaluated on local wall-clock minutes.

| Local time | Behaviour |
|---|---|
| Does not exist (clock moves forward) | Fires at the instant the gap ends. Several times inside one gap fire once |
| Occurs twice (clock moves back) | Fires once, at the first occurrence |

- An hourly cron job in a zone whose clock moves back therefore skips the repeated hour.
- Use `UTC`, or an interval, for a job that must fire at a fixed spacing.

## Next run and missed runs

| Item | Behaviour |
|---|---|
| Misfire grace | 60 s. A fire later than this is a missed fire |
| Missed fire, subsystem with catch-up | One run, trigger `catch_up`, `scheduled for` the first missed fire. Later missed fires are not run |
| Missed fire, no catch-up | One `skipped` run, reason `missed`, then the schedule moves forward. Detail: `first_missed`, `missed_count` (counted up to 10,000) |
| No free run slot when due | The fire waits and is served as soon as a slot frees. If it is by then later than the grace: a catch-up run for a job with catch-up, otherwise a `skipped` run, reason `capacity` |
| Causes of a late fire | The application was stopped, the host was suspended, or the process was starved |
| Pause | Fires that fall inside a pause are not missed and not recorded. The first fire at or after the pause end is the next run |
| Subsystem switched off, or automation disabled | Fires in the meantime are not missed, not replayed and not recorded |
| Failure backoff | See [Failures](#failures) |
| Webhook job | No fire times and no misfire grace. A call arms the job: `next run` is the call time, or 60 s after the start of the job's previous run if that is later. The run has trigger `webhook`. A waiting call is kept across a restart and served at the first tick. A pause, a disabled automation or a switched-off subsystem clears it. At most one call waits ([Automation](Operations-Automation#call-limits)) |
| Early run | A health event can bring the next run of a `container-health-checker` automation forward to now. Every gate is respected, and a job with failed runs in a row is not moved ([Automation](Operations-Automation#early-runs-and-the-polled-fallback)) |

- The scheduler re-reads the stored next run at least once a minute, so a clock change is absorbed within that time.
- The snapshot refresher, `notification-sender` and `backup-syncer` have catch-up. At start:

  | Stored next run | Run at start |
  |---|---|
  | None: the first start ever, the job is created due at once | One run, trigger `schedule` |
  | Passed by 60 s or less | One run, trigger `schedule` |
  | Passed by more than 60 s | One run, trigger `catch_up`, `scheduled for` the first missed fire |
  | In the future | None. The job fires when due |

  The next fire is then the first multiple of the interval after start ([System tasks](#system-tasks)).
- `catch_up` is a property of the subsystem. It is shown on the job page as `Missed runs` and in JSON as `catch_up`.

## Overrun policies

A fire arrives while the job's previous run is still in progress.

| Policy | Behaviour | Recorded |
|---|---|---|
| `skip` (default) | The new fire does not run | `skipped` run, reason `overrun`, detail `running_run_id` |
| `queue` | One new run starts when the running run ends, trigger `queued`. A second fire while one is queued does not run | `skipped` run, reason `overrun_collapsed` |
| `cancel_previous` | The running run is cancelled and recorded `interrupted`, reason `superseded`. The new run starts when it has stopped, within 30 s | If the old run has not stopped in 30 s: `skipped` run, reason `overrun`, message `The previous run did not stop in time.` |

- Every subsystem allows `skip` only.
- `cancel_previous` takes effect only for a subsystem whose runs are safe to cancel. A job holding `cancel_previous` under a subsystem that is not safe behaves as `skip`.
- The queued fire is held in memory. A restart discards it.
- A queued fire is dropped, recorded `skipped` with reason `queue_dropped`, when the job's gates or schedule change, or when the job cannot run at the moment the queued run would start.

## Runs

### Triggers

| Trigger | Started by |
|---|---|
| `schedule` | The schedule |
| `catch_up` | A missed fire of a job with catch-up |
| `queued` | The `queue` policy |
| `manual` | An administrator, with `Run now`; or the automation's owner, with `Run now` on the automation's page |
| `dry_run` | An administrator, with `Dry run`; or the automation's owner, with `Dry run` on the automation's page |
| `webhook` | A call to a webhook automation's URL ([Automation](Operations-Automation#calls)). `scheduled for` is the time the call armed the job |

### Outcomes and reasons

| Outcome | Meaning |
|---|---|
| In progress | No outcome yet |
| `success` | The run finished |
| `partial` | The run finished and some of its objects failed |
| `failure` | The run failed |
| `skipped` | The run did not act, and says why |
| `interrupted` | The run was cut short |

Every outcome other than `success` and `partial` has a reason, a lowercase code of up to 64 characters.

| Outcome | Reason | Meaning |
|---|---|---|
| `skipped` | `missed` | The fire was later than the misfire grace |
| `skipped` | `capacity` | No run slot was free |
| `skipped` | `overrun`, `overrun_collapsed` | [Overrun policies](#overrun-policies) |
| `skipped` | `queue_dropped` | A queued fire was dropped |
| `skipped` | `object_busy` | Another action held the object's lock ([Object locks](#object-locks)) |
| `skipped` | `suppressed` | A user acted on the object recently ([Suppression after a user acts](#suppression-after-a-user-acts)) |
| `skipped` | `environment_unreachable` | The host could not be used when the run began, and no person needs to act. Detail `kind`: `unreachable`, `outcome_unknown` or `not_swarm_manager`. Not counted toward the failure threshold ([Automation](Operations-Automation#unreachable-environments)) |
| `skipped` | `rule_disabled`, `rule_paused` | The automation was switched off or paused as the run began |
| `skipped` | `scheduler_stopped` | The application was stopping, or the scheduler was suspended for a restore, as the run was claimed |
| `skipped` | `dry_run_unsupported` | A dry run reached a subsystem that does not support one |
| `failure` | `owner_access_lost` | The automation's owner can no longer run it ([Owner access at every run](#owner-access-at-every-run)) |
| `failure` | `rule_missing`, `environment_missing`, `parameters_invalid` | The automation is gone, has no environment, or its parameters no longer validate |
| `failure` | `deadline` | The run exceeded its time limit and was cancelled safely |
| `failure` | `outcome_unknown` | The run exceeded its time limit and was cancelled; what it changed is not known. Detail: `time_limit_seconds` |
| `failure` | `no_activity_event` | The run acted and recorded no activity event ([Required events](#required-events)) |
| `failure` | `dry_run_took_lock`, `dry_run_wrote_event` | A dry run took an object lock or wrote an activity event ([Dry run](#dry-run)) |
| `failure` | `target_missing`, `target_invalid`, `selector_invalid`, `permission_denied`, `restart_budget_exhausted`, `all_objects_failed`, `compose_unavailable`, `engine_error`, `environment_credentials`, `environment_unsupported` | Reasons written by automations ([Automation](Operations-Automation#reasons)) |
| `failure` | `subsystem_unavailable` | No implementation of the subsystem is registered |
| `failure` | `detail_invalid`, `run_unsettled` | The run's result could not be stored |
| `failure` | The exception class name in lower case with underscores, such as `connection_error` | The run raised an error. `unspecified` when no code can be derived |
| `interrupted` | `process_stopped` | The application stopped while the run was in progress. Found at the next start |
| `interrupted` | `shutdown` | The application shut down in an orderly way during the run |
| `interrupted` | `superseded` | Replaced by `cancel_previous` |
| `interrupted` | `suspended` | A restore took the instance offline while the run was in progress ([Restore and the scheduler](#restore-and-the-scheduler)). Message: `The scheduler was paused while this run was in progress.` |

- `message` is up to 512 characters of text the product wrote. Text from an unexpected error is never stored; only the reason code is.
- `detail` is a flat map of at most 24 keys with string, number, boolean or null values. Strings are cut to 3,072 characters.
- Message and detail are scrubbed of secrets before storage and escaped when displayed.

### Time limit

| Subsystem | Limit |
|---|---|
| `environment-snapshot-refresher` | 10 minutes |
| `notification-sender` | 15 minutes |
| `container-updater`, `webhook-receiver` | 60 minutes |
| `backup-syncer` | 180 minutes |
| Default for any other subsystem | 30 minutes |

### Heartbeat, shutdown and restart

| Event | Behaviour |
|---|---|
| While running | The scheduler stamps every running run every 30 s |
| Orderly shutdown | The scheduler stops first. Every running run is cancelled and recorded `interrupted`, reason `shutdown`. Waits up to 10 s in total |
| A run not recorded within that time, or a crash | Recorded `interrupted`, reason `process_stopped`, at the next start. The message names the last heartbeat |

At every start, in this order:

1. Every run left in progress becomes `interrupted`, reason `process_stopped`. Each gets an activity event, except runs of subsystems that write none.
2. Each enabled job's gates are re-read. A job that cannot run has its next run cleared. A job that can run and has none gets one. A stored next run that has already passed is kept.
3. The scheduler serves every job whose next run has passed. Within the misfire grace the run is a normal `schedule` run. Later, it is one `catch_up` run or one `skipped` run with reason `missed` ([Next run and missed runs](#next-run-and-missed-runs)).
4. The loop starts.

- The start-up sweep logs `scheduler_recovered`.
- A job whose subsystem has no implementation in this build is logged once at start as `scheduler_subsystem_unavailable`.

### Restore and the scheduler

A restore suspends the scheduler ([Backups](Administration-Backups#the-offline-gate)).

| Item | Behaviour |
|---|---|
| Runs in progress | Cancelled and recorded `interrupted`, reason `suspended`. They are recorded like other interrupted runs, and leave the failure count unchanged |
| Due jobs | Not claimed while the scheduler is suspended |
| `Run now`, `Dry run` | Refused: `The scheduler is not accepting runs right now.` |
| A fire in flight | A fire that was being started when the suspension began is recorded `skipped`, reason `scheduler_stopped` |
| End of a cancelled or failed restore | The suspension ends and jobs are claimed again |
| Re-encryption of stored secrets | Suspended from the start of the re-key until it ends |
| After a restore that replaced the data | The suspension stays until an administrator resumes automation, across restarts ([Backups](Administration-Backups#after-a-restore)) |

## Failures

| Item | Behaviour |
|---|---|
| A run fails when | It reports `failure`, raises an error, exceeds its time limit, finds its owner without access, or records no required activity event |
| Counter | `consecutive failures` rises by 1 on `failure`. `success` and `partial` reset it to 0. `skipped`, `interrupted` and dry runs leave it |
| Threshold | The count at which the job is disabled. 5 unless the subsystem declares another. An automation sets its own with the parameter `max_consecutive_failures` (1 to 100). A subsystem with no threshold never disables its jobs |
| Backoff | After a failure of a scheduled run: `min(1 hour, 60 s × 2^(n−1))`, where `n` is the failure count, multiplied by a random factor from 0.5 up to but excluding 1.0. The retry time is now plus that delay. Interval schedule: the next run is the first slot of the schedule's own phase at or after the retry time, and not earlier than the next scheduled fire. Cron schedule: the later of the next scheduled fire and the retry time |
| No backoff | Jobs with no threshold, failed `manual` runs, and webhook jobs |
| Auto-disable | At the threshold the job is disabled: no next run, state `Disabled after N failures`. One activity event is recorded. The automation is not deleted or changed |
| Re-enable | An administrator, with `Enable job` (a confirmation page); or the automation's owner or an administrator, by enabling the automation on its page ([Automation](Operations-Automation#enable)). Resets the counter to 0 and schedules the next run. The job fires again after one interval for an interval schedule, or at the next matching minute for cron |

- A failure is never retried sooner than the schedule says.
- The snapshot refresher and `notification-sender` have no threshold. They are never disabled by failures and are not backed off.
- Re-enabling a job whose automation is disabled leaves it without a next run until the automation is enabled.

### Enable a job disabled after failures

1. Open `Jobs` and find the job. Its state reads `Disabled after N failures`.
2. Open the job and read `Last run` and the run history for the reason of the last failures. Fix the cause named there ([Troubleshooting](Troubleshooting)).
3. Select `Enable job`. The confirmation page lists what changes: the failure count is reset to 0, the job runs on its schedule again, and it is disabled again after the threshold.
4. Select `Enable job`. The job page reads `The job is enabled.`; the state is `Scheduled` and `Next run` shows a time.
5. When the state is not `Scheduled`, check the other gates in [States](#states): the automation, the subsystem switch and any pause.

An enable that finds the job already enabled records nothing. The confirmation page says `The job is already enabled. Confirming changes nothing.`

## Pause, resume and switches

| Action | Applies to | Effect |
|---|---|---|
| Pause | A job without an automation, or the automation of a job | No new runs for 1 hour, 4 hours, 24 hours or 7 days. A run in progress finishes |
| Resume | The same | Clears the pause at once |
| Subsystem switch | All jobs of a subsystem | See [Subsystem switch](#subsystem-switch) |
| `Run now` | One job | See [Run now](#run-now) |

- A pause ends by itself at its end time. Nothing is recorded when it does.
- On a job that belongs to an automation, `Pause` and `Resume` act on the automation.
- None of these actions deletes an automation, a job or a run.
- There is no indefinite pause.
- An invalid `duration` answers `422` with `Choose 1h, 4h, 24h or 7d.`
- A resume that finds no pause records nothing.

### Subsystem switch

`/jobs/-/subsystems`. Heading `Subsystems`. Introduction: `Switching a subsystem off stops every job it owns. Nothing is deleted.`

| Column | Content |
|---|---|
| Subsystem | The name |
| Status | `On` or `Off`, and `Not available in this build` |
| Actions | `View jobs` (the Jobs page filtered by the subsystem), `Switch off` or `Switch on` |

| Action | Effect |
|---|---|
| `Switch off` | No job of the subsystem fires, and `Run now` is refused. A run in progress finishes. The next run of each job is cleared |
| `Switch on` | Every job is scheduled again and runs when next due. Fires missed while it was off are not replayed |

1. Open `Jobs`, then `Subsystems`.
2. Select `Switch off` or `Switch on` for the subsystem.
3. Read the confirmation page. `What changes` lists the effect, and `N jobs affected` lists the first 25 jobs with the total. The page changes nothing.
4. Select `Switch off <name>` or `Switch on <name>`. The list answers `The subsystem is disabled.` or `The subsystem is enabled.`

- Nothing is deleted. The state is the stored setting `subsystem.<name>.enabled`; no setting means on.
- Switching to the state a subsystem is already in records nothing. The confirmation page says `The subsystem is already on. Confirming changes nothing.` or `The subsystem is already off. Confirming changes nothing.`
- `activity-log-pruner` has no switch; its confirmation routes answer `404`.

### Run now

An administrator starts one run outside the schedule from the job page. A confirmation page precedes it. The automation's owner does the same from the automation's page ([Automation](Operations-Automation#run-now)).

<!-- screenshot: Administration-Jobs-2: the Run job now confirmation page for an automation's job, with the target list and the What changes list; an automation with an environment -->

1. Open the job page and select `Run now`.
2. Read the confirmation page `Run job now`. It lists the job, subsystem, automation, environment, schedule and state, then `What changes`: the job runs once, now, on its environment, and the schedule is not changed.
3. Select `Run job now`. The run page opens with `The run has started.`
4. A refusal renders `The job was not started` at `409` or `422`. Use the table below for the cause.

| Item | Behaviour |
|---|---|
| Trigger | `manual`. `scheduled for` is empty. The run records the user who asked |
| Schedule | `Next run` does not move |
| While paused | Allowed. The pause stays |
| Refused with `409 The job was not started` | The text names the cause, see below |
| Binding | The confirmation form carries a digest of the job and, for a job with an automation, of the automation. When the run is claimed both are read again; if the digest differs, nothing runs: `The job changed since you reviewed it; nothing was run.` (`422 The job was not started`, recorded on `job_run_requested` with reason `changed_since_review`). A form posted without the digest is refused the same way. A dry run is not bound |
| Digest | SHA-256 over the job (id, creation time, subsystem, automation, environment, schedule type, schedule, time zone) and the automation (id, creation time, last-changed time, target, settings, environment). The job's pause, failure count and next run are not part of it. Any write to the automation is, a pause of the automation included |
| Counts toward | The run slots and the failure counter |
| Attribution | The run's actions are attributed to the subsystem, the automation and the run. The user's request is recorded separately, as `job_run_requested` |
| Authority | The automation's owner's, whoever asks |

| Refusal text | Cause |
|---|---|
| `This automation is not available.` | The subsystem has no implementation in this build |
| `The subsystem is disabled.` | The subsystem is off |
| `The job was disabled after repeated failures. Enable it first.` | The job is disabled after failures |
| `The automation is disabled.` | The automation is switched off |
| `The job is already running.`, `This job is already running.` | A run of the job is in progress |
| `No run slot is free right now. Try again shortly.` | All run slots are taken |
| `The automation's owner already has the most runs allowed at once.` | The owner already has 2 runs |
| `This subsystem already has the most runs allowed at once.` | The subsystem's cap is reached; `webhook-receiver` allows 3 |
| `The scheduler is not accepting runs right now.` | The scheduler is stopping or suspended |

### Dry run

- A subsystem declares whether it supports a dry run. `container-health-checker`, `stack-restarter`, `container-updater`, `webhook-receiver`, `container-log-exporter`, `activity-log-exporter` and `backup-syncer` support one ([Automation](Operations-Automation#dry-run)). The snapshot refresher and `notification-sender` do not.
- Where supported, a dry run has trigger `dry_run`. It changes no container, stack or automation, takes no object lock and writes no activity event. A dry run that takes a lock or writes an event fails with reason `dry_run_took_lock` or `dry_run_wrote_event`. A `container-updater` dry run stores the update status it checked for host images; Swarm results are not stored.
- A dry run is allowed on a disabled automation and on a job disabled after failures. It is refused when the subsystem is off or not available, when a run is in progress, when no run slot is free, when the automation's owner already has 2 runs, and when the scheduler is stopping.
- A dry run does not change the failure counter, the backoff or the next run. A subsystem that does not support it is refused: `This job does not support a dry run.`
- An administrator starts one with `Dry run` on the job page, which acts at once and opens the run page with `The dry run has started.`; the automation's owner starts one from the automation's page.

## Limits

| Limit | Value |
|---|---|
| Concurrent runs, per process | 8, counting scheduled and manual runs |
| Slots usable by jobs that belong to an automation | 7. One slot is reserved for jobs without an automation |
| Concurrent runs per automation owner | 2 |
| Concurrent runs of `webhook-receiver` | 3, across all its automations. A due call waits for a slot |
| Start of a webhook run | At least 60 s after the start of the job's previous run |
| Automations per owner | 50 |
| Jobs per automation | 1 |
| Jobs per subsystem without automations | 1 |
| Finished runs kept per job | 500 ([Run history](#run-history)) |
| Interval | 60 s to 30 days, unless the subsystem sets another minimum |
| Cron expression | 128 characters |
| Run `message` | 512 characters |
| Run `detail` | 24 keys; strings cut to 3,072 characters |
| Wait for an object lock | 10 s unless the subsystem sets another, 0 to 60 s |
| Shutdown wait for running runs | 10 s |

- A fire that finds no free slot waits ([Next run and missed runs](#next-run-and-missed-runs)).
- A manual run that finds no free slot is refused.

## Run history

| Item | Behaviour |
|---|---|
| Not recorded | A successful scheduled run (trigger `schedule`) of a job without an automation whose subsystem has event policy `none`. The snapshot refresher and `notification-sender` are such subsystems |
| Recorded | Every other run of those jobs: `manual`, `catch_up`, and any run ending `partial`, `failure`, `skipped` or `interrupted`. Every run of a job of an automation |
| Kept | The newest 500 finished runs of each job |
| Deleted | Finished runs beyond those 500, by the hourly pass |
| Never deleted | A run in progress, and any run an activity event refers to. Those runs stay as long as the event does |
| Deleted with | The job. A job is deleted with its environment |
| Not deleted by this pass | Activity events. Retention removes them when it is set ([Housekeeping](#housekeeping)) |

- The pass runs at start and then at the top of each hour ([Housekeeping](#housekeeping)). A failure of the run step is logged as `run_pruning_failed`; the step runs again the next hour.
- A run that leaves no row has no run page. An orderly shutdown or a suspension that ends one inserts its row as `interrupted`. A run in progress when the process stops without an orderly shutdown leaves no row.
- A deleted run's id is never reused.
- Deleting an environment deletes its automations, their jobs and the jobs' runs ([Environments](Usage-Environments#deleting)). The activity events written for them stay and keep the automation's id and name.
- Deleting an automation deletes its job and the job's runs ([Automation](Operations-Automation#delete-an-automation)). The activity events stay.

### Latest run

| Place | Shows |
|---|---|
| Jobs list, column `Last run` | The latest run, recorded or not: outcome and start time. Linked to the run page when the run has a history row |
| Job page, fact `Last run` | The same |
| JSON `last_run_at`, `last_run_outcome` | The same. `null` when the job has never run |
| JSON `last_run` | The latest run that has a history row, as a run object. `null` when none. Older than `last_run_at` when a later run left no row |

- The start time and outcome of a run that leaves no row are held in memory since start and stored on the job. The stored copy is written when the job's previous run did not succeed or its failure counter is above 0, and otherwise at most once an hour. After a restart the stored copy is shown until the job runs again.
- A recorded run later than the held one replaces it.

## Environment status and check times

| Item | Behaviour |
|---|---|
| Stored by a refresh | The environment's status, check time and counts, when the status, the failed stage, the status message, a host version field, the Compose CLI version, a count or the snapshot error differs from the stored value |
| Stored on the first refresh | The first refresh of each environment after start always writes |
| Stored at least hourly | A refresh writes when the stored check time, or the stored snapshot time, is an hour or more behind |
| Not stored | A refresh that finds nothing changed and is none of the above |
| Shown | Pages and the JSON API show the time of the latest check, stored or not: `last_checked_at`, `last_contacted_at`, `snapshot.captured_at` ([Environments](Usage-Environments#snapshot-cache)) |

## Housekeeping

One pass runs at start and then at the top of each hour (UTC, a whole multiple of 3600 s in Unix time), inside the scheduler's loop. It has two steps, in this order.

| Step | Work | Failure |
|---|---|---|
| 1 | Prunes finished runs ([Run history](#run-history)) | Logged as `run_pruning_failed`. Step 2 still runs |
| 2 | Applies activity retention, only when `DOCKMIN_ACTIVITY_RETENTION_DAYS` is set ([Activity](Administration-Activity#retention)) | Logged as `retention_failed`. The next pass retries |

- No job is claimed while a pass runs. Retention is bounded to 20 batches a pass.
- The pass logs `scheduler_housekeeping` when it removed runs or events ([Log lines](#log-lines)).
- Retention removes events as the actor `activity-log-pruner` ([Subsystems](Reference-Subsystems#activity-log-pruner)). Each batch that removes events writes one `activity_log_pruned` event.

## Activity events

Actions a run takes are attributed to the subsystem that owns the job. The scheduler is never recorded as an actor.

| Item | Value |
|---|---|
| Actor of a run's actions | The owning subsystem, with the automation (for a subsystem that takes automations) and the run |
| Actor of an administrator's action | The administrator |
| Category | `automation` for a subsystem, `user_action` for an administrator |
| Automation detail | Events written under an automation carry `rule_id` and `rule_name` in their detail. They stay readable after the automation is deleted |
| Parameters | Never recorded. An automation update records `parameters_changed: true` |

### Recorded actions

| Action | Actor | Target | When |
|---|---|---|---|
| `automation_rule_created`, `automation_rule_updated` | User | `automation_rule` | An automation is created or changed. Detail on create: `subsystem`, `name`, `schedule_kind`, `schedule_expression`, `time_zone`, `overrun_policy`, and for `webhook-receiver` `prefix`, the token prefix. Detail on update: `name_changed`, `target_changed`, `parameters_changed`, `schedule_changed`, `overrun_changed`, and `rule_disabled` when the save disabled an enabled automation |
| `automation_rule_enabled`, `automation_rule_disabled` | User | `automation_rule` | An automation is switched. Detail on enable: `job_reenabled` when a job disabled after failures was enabled again. Failure reason `changed_since_review` when the automation was written to after the confirmation page opened |
| `webhook_token_rotated`, `webhook_token_revoked` | User, category `authentication` | `automation_rule` | A webhook URL is rotated or revoked ([Automation](Operations-Automation#webhook-recorded-actions)) |
| `automation_rule_deleted` | User | `automation_rule` | An automation is deleted. Detail: `subsystem`, `name`, `job_id`, `runs_deleted`. Failure reasons: `changed_since_review`, `rule_running` |
| `automation_rule_paused`, `automation_rule_resumed` | User | `automation_rule` | An automation is paused or resumed. Detail: `duration`, `paused_until` |
| `scheduled_job_paused`, `scheduled_job_resumed` | User | `scheduled_job` | A job without an automation is paused or resumed. Detail on pause: `duration`, `paused_until` |
| `scheduled_job_enabled` | User | `scheduled_job` | A job disabled after failures is enabled |
| `job_run_requested` | User: an administrator, or the automation's owner | `scheduled_job` | `Run now` or `Dry run`. Detail: `job_run_id`, `dry_run`. A run refused because the job or automation changed after the review is a failure row with reason `changed_since_review` |
| `container_restarted`, `container_restart_budget_exhausted`, `stack_restarted` | The owning subsystem, its automation and the run | `container`, `stack` | Restarts by automation ([Automation](Operations-Automation#activity-events)) |
| `activity_log_exported` | `activity-log-exporter`, its automation and the run | `object_storage_target` | A failed export, result `failure` only ([Activity](Administration-Activity#failure-row)) |
| `container_log_exported` | `container-log-exporter`, its automation and the run | `container`, `service`, `object_storage_target` | A failed export, result `failure` only ([Subsystems](Reference-Subsystems#log-exporter-failures)) |
| `automation_run_failed` | The owning subsystem, its automation and the run | `scheduled_job` | A run of an automation ended `failure`. Not written for a dry run. Detail: `reason` |
| `environment_unreachable`, `environment_reachable` | `environment-snapshot-refresher`; no automation, no run | `environment` | An environment's status moved between `reachable` and `unreachable` ([Subsystems](Reference-Subsystems#environment-snapshot-refresher)) |
| `notification_delivery_failed` | `notification-sender` | `notification_channel` | A message was not delivered after three attempts ([Notifications](Administration-Notifications#delivery)) |
| `backup_uploaded`, `backup_remote_pruned` | `backup-syncer`, its automation and the run | `object_storage_target` | A stored or failed archive; a retention pass that deleted or failed ([Backups](Administration-Backups#activity-rows)) |
| `activity_log_pruned` | `activity-log-pruner`; no automation, no run | `activity_log` | A retention batch removed events ([Activity](Administration-Activity#pruning-row)) |
| `subsystem_enabled`, `subsystem_disabled` | User | `subsystem`, the name | The subsystem switch. Detail: `jobs`, the number of jobs |
| `scheduled_job_disabled` | The owning subsystem, its automation and the run | `scheduled_job` | Automatic disable. Detail: `consecutive_failures`, `threshold`, `last_reason` |
| `job_run_interrupted` | The owning subsystem, its automation and the run | `scheduled_job` | A run is interrupted. Result `failure`. Detail: `reason`, `trigger` |

- An automation or job event carries the automation's environment.
- Skipped runs and successful runs write no scheduler-level event. Their run record is the record.
- The snapshot refresher writes an activity event only when an environment's status moves between `reachable` and `unreachable`. `notification-sender` writes one only for a message it could not deliver.
- `job_run_interrupted` is written for subsystems that record events, and for a job whose subsystem is not available.
- A change that alters nothing, such as pausing a paused automation's job again or enabling an enabled automation, records nothing.

### Required events

Each subsystem declares when a run must have written at least one activity event for its own actions.

| Event policy | An event is required when |
|---|---|
| `per_action` (default) | The run attempted at least one action |
| `failures_only` | The outcome is `failure` or `partial` |
| `none` | Never. The snapshot refresher and `notification-sender` |

- A run that falls short fails with reason `no_activity_event`. The check applies in every configuration.
- Under `per_action`, a run that attempted nothing needs no event.

## Object locks

| Item | Behaviour |
|---|---|
| Shared with | Actions taken by users. A user and a subsystem contend for the same object lock |
| A subsystem finds the object locked | Waits up to its lock wait (10 s unless the subsystem sets another) |
| Still locked | `skipped` run, reason `object_busy`. Detail: `holder_type` (`user` or `subsystem`), `holder_label` (the user name or the subsystem name), `holder_action`, `holder_age_seconds` |
| A user finds the object locked by a subsystem | The user's action fails at once |
| Administrator kill during a held `stop` or `restart` | Allowed, whoever holds the lock. The held action finishes and records its own outcome |

## Suppression after a user acts

Automation stands back from an object a user has just acted on.

| Item | Behaviour |
|---|---|
| Starts when | A user's action on a container, stack, network, volume, image or node ends. An administrator's kill that overrides a held `stop` or `restart` counts |
| Does not start for | An action a subsystem takes, an action refused because the object was locked, and `Run now` |
| Window | The automation's `suppression_seconds` (0 to 3600; 0 turns suppression off for the automation), otherwise the subsystem's default, 5 minutes unless it sets another |
| Checked | By the subsystem, for each object it is about to act on |
| Effect | The object is skipped. A run that acts on that one object only is `skipped`, reason `suppressed` |
| Memory | Entries older than 60 minutes are dropped. At most 10,000 objects are held. After an entry has been dropped for space, a lookup that finds nothing reads the activity log for the last 60 minutes |
| After a restart | Reloaded from the user actions in the activity log of the last 60 minutes, up to 10,000 |

## Automations

An automation names a subsystem, an environment, a target selector, parameters and a schedule.

| Item | Rule |
|---|---|
| Created | Disabled, with its job. It runs only after it is enabled |
| Name | 1 to 64 printable characters. Unique among the owner's automations of one subsystem, ignoring case |
| Target selector | At most 1 KiB, in the form the subsystem defines |
| Parameters | At most 16 KiB of JSON, validated by the subsystem. Unknown keys are refused. They are stored unencrypted and never hold a secret |
| Common parameters | `suppression_seconds` (0 to 3600), `max_consecutive_failures` (1 to 100) |
| Schedule | A kind the subsystem allows, with its minimum interval |
| Overrun policy | One the subsystem allows. The subsystem's default when none is given |
| Per owner | 50 automations |

- Automations are created, edited, enabled, disabled, paused, run and deleted on the Automation pages ([Automation](Operations-Automation)). Automations are also read and controlled through the rule routes of the JSON API ([JSON API](Reference-JSON-API#automation-rules)).
- Administrators manage every automation. A standard user manages their own automations, and only for subsystems that act on one environment. An automation with no environment is for administrators only.
- Creating, changing, enabling or resuming an automation needs create access to the automation's environment and the subsystem's own target check. A standard user is refused on a local-socket environment ([Environments](Usage-Environments#local-socket-environment)). Another user's automation answers not found.
- The automation's owner is the user who created it.

### Owner access at every run

An automation's runs act with the authority of its owner, whoever started them. The owner is checked at every run: scheduled, `Run now` and dry run.

| Owner | Result |
|---|---|
| Deleted, or disabled | `failure`, reason `owner_access_lost` |
| Administrator | Run proceeds |
| Standard user | Needs create access to the automation's environment (not a local socket) and the subsystem's own target check. Otherwise `failure`, reason `owner_access_lost` |

- Before acting on each object, the run checks the owner's current write access to that object. An object the owner may not write to is not acted on. For an administrator the check confirms the account is still enabled and still an administrator.
- `owner_access_lost` counts toward the threshold, so an automation whose owner lost access ends disabled, with an activity event.
- Nothing is written to the host by a run that fails this check.

## Routes

| Route | Purpose |
|---|---|
| `GET /jobs` | Job list. Query: `subsystem`, `kind` (`system` or `automation`), `limit`, `cursor` |
| `GET /jobs/-/subsystems` | Subsystem switches |
| `GET /jobs/-/subsystems/{name}/enable`, `GET /jobs/-/subsystems/{name}/disable` | Confirmation pages; change nothing |
| `POST /jobs/-/subsystems/{name}/enable`, `POST /jobs/-/subsystems/{name}/disable` | Switch a subsystem |
| `GET /jobs/-/runs/{run_id}` | One run |
| `GET /jobs/{job_id}` | Job and run history. Query: `limit`, `cursor` |
| `POST /jobs/{job_id}/pause` | Form field `duration`: `1h`, `4h`, `24h`, `7d`. Any other value answers `422` |
| `POST /jobs/{job_id}/resume` | Clear the pause |
| `GET /jobs/{job_id}/enable`, `POST /jobs/{job_id}/enable` | Confirmation, then enable after an automatic disable |
| `GET /jobs/{job_id}/run`, `POST /jobs/{job_id}/run` | Confirmation, then run now. Form fields `expected_digest` and, for a dry run, `dry_run=yes`. A refusal answers `409`; a changed job answers `422` |

## JSON API

Read-only. Every route requires a bearer token (`Authorization: Bearer <token>`) of an administrator. Create tokens at `/account/tokens`. A session cookie alone answers `401`.

| Route | Returns |
|---|---|
| `GET /api/v1/jobs` | A page of jobs by ascending id |
| `GET /api/v1/jobs/{id}` | One job |
| `GET /api/v1/jobs/{id}/runs` | A page of the job's runs, newest first |
| `GET /api/v1/jobs/-/runs/{run_id}` | One run with its job |

### List parameters

| Parameter | Rule |
|---|---|
| `limit` | 1 to 500; default 25 |
| `cursor` | The `next_cursor` of the previous page; at most 512 characters |
| `subsystem` | A subsystem name; `GET /api/v1/jobs` only; at most 64 characters |
| `kind` | `system` or `automation`; `GET /api/v1/jobs` only; at most 16 characters |

### Responses

| Status | Cause |
|---|---|
| 200 | A page is `{"items": [...], "next_cursor": string or null}`; `next_cursor` is `null` on the last page |
| 400 | Invalid cursor |
| 401 | Missing or invalid token |
| 403 | The token's user is not an administrator |
| 404 | No such job or run |
| 422 | Invalid parameter: an unknown `subsystem`, an unknown `kind`, or an id that is not a number from 1 to 9223372036854775807 |

### Job fields

| Field | Type | Notes |
|---|---|---|
| `id` | integer | |
| `subsystem` | string | |
| `kind` | string | `automation` when the job belongs to an automation, `system` otherwise |
| `rule` | object or null | `{id, name}`; `null` for a job without an automation |
| `environment_id` | integer or null | |
| `schedule` | object | `{kind, expression, time_zone, description}`; `kind` is `interval`, `cron` or `webhook`. A webhook job has an empty `expression` and the description `When called` |
| `next_run_at` | string or null | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `state` | string | See [States](#states) |
| `running` | boolean | A run is in progress in this process |
| `enabled` | boolean | `false` after an automatic disable |
| `rule_enabled` | boolean or null | `null` for a job without an automation |
| `subsystem_enabled` | boolean | |
| `available` | boolean | |
| `supports_dry_run` | boolean | |
| `overrun_policy` | string | `skip`, `queue`, `cancel_previous` |
| `catch_up` | boolean | |
| `consecutive_failures` | integer | |
| `failure_threshold` | integer or null | `null` when the job is never disabled by failures |
| `paused_until` | string or null | The pause in force, from the job or its automation |
| `last_run` | object or null | The latest run that has a history row, as a run object below ([Latest run](#latest-run)) |
| `last_run_at` | string or null | Start of the latest run, recorded or not. UTC, `YYYY-MM-DDTHH:MM:SSZ`. `null` when the job has never run |
| `last_run_outcome` | string or null | Outcome of that run. `null` when the job has never run, or the run is in progress |

### Run fields

| Field | Type | Notes |
|---|---|---|
| `id` | integer | |
| `job_id` | integer | |
| `trigger` | string | `schedule`, `catch_up`, `queued`, `manual`, `dry_run`, `webhook` |
| `requested_by_user_id` | integer or null | The administrator who asked for a manual or dry run |
| `scheduled_for` | string or null | |
| `started_at` | string | |
| `finished_at` | string or null | `null` while in progress |
| `outcome` | string or null | `success`, `partial`, `failure`, `skipped`, `interrupted`; `null` while in progress |
| `reason` | string or null | |
| `message` | string or null | |
| `detail` | object | Flat; values are strings, numbers, booleans or `null` |

The single-run route adds `job`, the job object above.

## Related settings

| Setting | Effect here |
|---|---|
| `DOCKMIN_SNAPSHOT_INTERVAL_SECONDS` | Interval of the snapshot refresher's job, 15 to 3600 s. A changed value takes effect at the next start ([System tasks](#system-tasks)) |
| `DOCKMIN_ACTIVITY_RETENTION_DAYS` | Step 2 of [Housekeeping](#housekeeping) |
| `DOCKMIN_DEVELOPMENT_CHECKS` | A failed run logs `job_run_failed` with the error's traceback instead of the class name only; `development_checks_enabled` is logged at warning level at start |

Defaults and ranges: [Configuration](Reference-Configuration#storage), [Configuration](Reference-Configuration#logging-and-diagnostics).

## Failure states

| Symptom | Cause | Next step |
|---|---|---|
| State `Disabled after N failures` | The failure threshold was reached | [Enable a job disabled after failures](#enable-a-job-disabled-after-failures) |
| State `Not available` | The build has no implementation of the subsystem | Jobs are kept and do not run; `scheduler_subsystem_unavailable` is logged at start |
| State `Nothing scheduled` | Three failed serves, or a schedule with no further fire time | Logged as `scheduler_job_unscheduled`, `scheduler_schedule_exhausted` or `scheduler_schedule_invalid`; edit the automation's schedule, or restart to recompute |
| State `Subsystem disabled` | The subsystem switch is off | [Subsystem switch](#subsystem-switch) |
| `Run now` answers `409` | A gate is closed or no slot is free | [Run now](#run-now) |
| Run `interrupted`, reason `process_stopped` | The application stopped during the run | [Heartbeat, shutdown and restart](#heartbeat-shutdown-and-restart) |
| Run `failure`, reason `owner_access_lost` | The owner is disabled, deleted or lost access | [Owner access at every run](#owner-access-at-every-run) |
| Run `skipped`, reason `object_busy` or `suppressed` | A user holds or recently acted on the object | [Object locks](#object-locks), [Suppression after a user acts](#suppression-after-a-user-acts) |

Further causes: [Troubleshooting](Troubleshooting).

## Log lines

| Line | Level | Meaning |
|---|---|---|
| `scheduler_recovered` | info | The start-up sweep finished. Fields: `interrupted`, `gated`, `revived` |
| `scheduler_subsystem_unavailable` | warning | A job's subsystem has no implementation in this build. Fields: `subsystem` |
| `scheduled_job_disabled` | warning | A job was disabled after repeated failures |
| `job_run_failed` | error | A run raised an error. Fields: `run_id`, `job_id`, `error` (class name) |
| `subsystem_event_missing` | error | A run acted and recorded no required event |
| `scheduler_tick_failed` | error | A scheduling pass failed. The loop pauses 5 s and continues |
| `scheduler_job_failed`, `scheduler_job_unscheduled` | error | A job could not be served; after three failures in a row it is left without a next run |
| `scheduler_housekeeping` | info | The hourly pass removed runs or activity events. Fields: `runs_pruned`, `events_removed` |
| `run_pruning_failed`, `retention_failed` | error | A step of the hourly pass failed |
| Retention and export lines | | Listed in [Activity](Administration-Activity#log-lines) |
