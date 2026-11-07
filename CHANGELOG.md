# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project uses [Semantic Versioning](https://semver.org/).

## 0.2.0 - 2026-11-07

First public release.

### Added

#### Environments

- Environments for Docker Standalone and Docker Swarm hosts, reached over the local Unix socket or over TCP with mutual TLS. Plain TCP is refused.
- Connection test with staged results, and a stored status, Engine API version and Compose version per environment.
- Groups, tags, a cached status and object count per environment, and a refresh interval.
- Address policy that refuses loopback, link-local and metadata addresses unless permitted.
- First-run panel that adds the mounted Docker socket, and names the missing mount or group when the socket cannot be used.

#### Containers

- List and detail pages, create, duplicate, start, stop, restart, kill, pause, unpause, remove and recreate, singly or in batches.
- Logs, live statistics and an interactive terminal.
- NDJSON log downloads for containers, Compose stacks, services and Swarm stacks, at `schema_version` 2.
- Create options restricted to administrators.

#### Images

- List and detail pages, pull and push with progress, tag, untag, remove and prune.
- Update detection against the registry, with a stored result per reference.
- Binding of each image to the credential set that pulled it, reused by updates. Rebind to another credential set or to an anonymous pull, verified with a manifest request before the binding changes.

#### Networks and volumes

- Networks: list, create with driver options, attach, detach, remove and prune.
- Volumes: list, create, remove, force remove and prune.

#### Compose stacks

- Stacks created from pasted content or an uploaded file, with variables and secret variables.
- Deploy, redeploy, start, stop, restart and remove, run through the Compose CLI with machine-readable output. Docker Compose v2.32.0 or later is required.
- Adoption of projects that already exist on a host.
- Companion files for Compose and Swarm stacks, stored in the database: environment files, label files, config and secret sources, and files reached through `include` and `extends.file`. At most 20 files, 64 KiB each and 128 KiB in total. Secret files are encrypted at rest.
- `Changed since the last deploy` decided by digests of the file, variables and companion files. A redeploy confirmation is bound to the variables and files it showed.
- Container name and published port check on create and redeploy.
- Code editor for stack files with line numbers and a link from a refusal to its line.
- Image builds from uploaded contexts, for administrators.
- JSON routes to list, read, create, redeploy, start, stop and restart stacks, and to put and delete a companion file.

#### Swarm

- Services, tasks, nodes, configs and secrets, with scale, force update, rollback and node availability.
- Swarm stacks deployed through the Engine API from a Compose file, as services, networks, configs and secrets.

#### Automation

- Shared scheduler with interval and cron schedules, overrun policies, failure thresholds, pause, resume, run now and dry run.
- Subsystems: `container-health-checker`, `stack-restarter`, `container-updater`, `webhook-receiver`, `activity-log-exporter`, `backup-syncer`, `container-log-exporter` and `environment-snapshot-refresher`.
- `container-updater` for containers, Compose stacks, Swarm services and Swarm stacks, with verification and rollback, and optional removal of replaced images.
- `webhook-receiver`: an automation that runs when its URL is called, with `update` and `redeploy` actions, a hashed token shown once, rotation, revocation, and call limits.
- Suppression after a user acts on an object.
- JSON routes to list and read automations and runs, to pause, resume and disable an automation, and to start a run.

#### Templates

- Template catalogues in linuxserver.io or Portainer format, fetched over HTTPS and refreshed on demand.
- Templates that fill the container create form or the Compose stack create form. Nothing is created until the form is submitted.

#### Registries

- Credential sets for Docker Hub, GitHub Container Registry, Amazon ECR, Azure Container Registry, GitLab and generic registries, anonymous or authenticated.
- Connection test, per-set TLS settings and CA bundles, and token refresh for Amazon ECR.

#### Backups and restore

- Backup archives with a manifest and checksums. The archive never holds the encryption key.
- Download, and scheduled upload to S3-compatible object storage with path-style and virtual-host addressing and remote retention. Multipart upload of archives over 64 MiB, up to 7 GiB.
- Restore from an upload or from object storage, with key verification, an offline gate, a swap that keeps the replaced data, and progress.
- Operator command `python -m app restore` to review an archive and replace the data of a stopped instance.
- A backup is refused when a stored secret does not open.
- Automation stays paused after a restore until an administrator resumes it.

#### Notifications

- Channels for webhook, ntfy, Gotify, Discord, Slack, Microsoft Teams and email.
- Event types with per-channel subscriptions and environment scope, delivery with retries, and a signed webhook body.

#### Activity log

- A row for every action, with the user or subsystem that took it.
- Filters, event pages, per-object panels, CSV and NDJSON download, a JSON route and age-based retention.
- Scheduled export to an S3-compatible bucket as CSV or NDJSON.
- `request_id` on rows written by a user or an anonymous caller.
- Reading the activity log, container and service logs and dockmin's own output into a log platform: [Activity](https://github.com/Sphexi/DockMin/wiki/Administration-Activity).

#### Accounts and access control

- First-run setup with a one-time value written to the log.
- Users, teams, administrator and standard roles, and server-side sessions with idle and absolute expiry.
- Argon2id password hashing, a password policy, and a sign-in lock by username and by address. Operator command to lift a lock.
- API tokens that act as their owner.
- Access control at environment level and per object, enforced where data is fetched.

#### Encryption

- Per-field AES-256-GCM encryption of stored secrets, with a key generated at the first start into its own volume or supplied from the environment.
- Key rotation with a previous key, a check value that detects a wrong key at start, and an Encryption page that checks stored secrets and re-encrypts them under the offline gate.

#### Instance

- Instance page with the process and container, restart, and update from a newer image through an update helper, with rollback.

#### Deployment and interface

- A single `docker run` of the published image starts dockmin over HTTPS with a generated key and a self-signed certificate. Image for `linux/amd64` and `linux/arm64`, running as a non-root user.
- `compose.yaml` and examples for direct HTTPS and for nginx as a reverse proxy.
- Startup self-check, request limits, security headers, `Secure` and `__Host-` cookies, CSRF protection and a single-instance lock on the data directory.
- Server-rendered pages with HTMX fragments, plain CSS and JavaScript, light and dark themes, and pages that work without JavaScript.
