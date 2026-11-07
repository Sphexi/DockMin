# dockmin

> [!NOTE]
> dockmin is provided as-is, without warranty, under the [MIT licence](LICENSE). It was built with LLM assistance.

## Summary

dockmin is a free and open-source web interface for managing Docker Standalone and Docker Swarm hosts: containers, images, networks, volumes, Compose stacks and Swarm stacks, across many environments. Pages are rendered on the server. It runs as one container with one process and one SQLite database.

## Quick start

Starts dockmin on the local Docker host with HTTPS. The encryption key and a self-signed certificate are generated at the first start.

```bash
docker run -d --name dockmin --restart unless-stopped \
  -p 8443:8443 \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockmin-data:/data \
  -v dockmin-key:/key \
  ghcr.io/sphexi/dockmin:latest
```

The written-out procedure, with the first-run setup and the encryption key copy: [Quick start](https://github.com/Sphexi/DockMin/wiki/Getting-Started-Quick-Start).

## Documentation

| Document | Contents |
|---|---|
| [Home](https://github.com/Sphexi/DockMin/wiki/Home) | Overview |
| [Changelog](https://github.com/Sphexi/DockMin/wiki/Changelog) | Release notes |
| [Introduction](https://github.com/Sphexi/DockMin/wiki/Getting-Started-Introduction) | Getting started: scope, supported environments, stack |
| [Prerequisites](https://github.com/Sphexi/DockMin/wiki/Getting-Started-Prerequisites) | Getting started: requirements, installing Docker |
| [Quick start](https://github.com/Sphexi/DockMin/wiki/Getting-Started-Quick-Start) | Getting started: the `docker run` procedure |
| [Installation](https://github.com/Sphexi/DockMin/wiki/Getting-Started-Installation) | Getting started: key copy, own certificate, hardened variant, Docker Compose, upgrading, removing, build from source |
| [Environments](https://github.com/Sphexi/DockMin/wiki/Usage-Environments) | Usage: managed Docker hosts, connections, groups, tags, status |
| [Containers](https://github.com/Sphexi/DockMin/wiki/Usage-Containers) | Usage: list, detail, lifecycle, create, logs, terminal |
| [Images](https://github.com/Sphexi/DockMin/wiki/Usage-Images) | Usage: pulls, pushes, credential bindings, update checks |
| [Stacks](https://github.com/Sphexi/DockMin/wiki/Usage-Stacks) | Usage: Compose stacks and Swarm stacks |
| [Swarm](https://github.com/Sphexi/DockMin/wiki/Usage-Swarm) | Usage: services, tasks, nodes, configs, secrets |
| [Networks](https://github.com/Sphexi/DockMin/wiki/Usage-Networks) | Usage: networks, attach, detach, prune |
| [Volumes](https://github.com/Sphexi/DockMin/wiki/Usage-Volumes) | Usage: volumes, removal, prune |
| [Automation](https://github.com/Sphexi/DockMin/wiki/Operations-Automation) | Usage, operations: automation rules and webhooks |
| [Templates](https://github.com/Sphexi/DockMin/wiki/Operations-Templates) | Usage, operations: application templates |
| [Jobs](https://github.com/Sphexi/DockMin/wiki/Administration-Jobs) | Usage, administration: scheduler jobs and runs |
| [Activity](https://github.com/Sphexi/DockMin/wiki/Administration-Activity) | Usage, administration: activity log, export, log ingestion |
| [Registries](https://github.com/Sphexi/DockMin/wiki/Administration-Registries) | Usage, administration: registry credential sets |
| [Template catalogues](https://github.com/Sphexi/DockMin/wiki/Administration-Template-Catalogues) | Usage, administration: template catalogues |
| [Object storage](https://github.com/Sphexi/DockMin/wiki/Administration-Object-Storage) | Usage, administration: S3-compatible targets |
| [Backups](https://github.com/Sphexi/DockMin/wiki/Administration-Backups) | Usage, administration: backup archives and restore |
| [Certificate](https://github.com/Sphexi/DockMin/wiki/Administration-Certificate) | Usage, administration: the served certificate |
| [Notifications](https://github.com/Sphexi/DockMin/wiki/Administration-Notifications) | Usage, administration: channels and event types |
| [Encryption](https://github.com/Sphexi/DockMin/wiki/Administration-Encryption) | Usage, administration: encryption at rest, key loss, re-key |
| [Instance](https://github.com/Sphexi/DockMin/wiki/Administration-Instance) | Usage, administration: restart and self-update |
| [Teams and users](https://github.com/Sphexi/DockMin/wiki/Usage-Teams-and-Users) | Usage: accounts, teams, sessions, API tokens |
| [Access control](https://github.com/Sphexi/DockMin/wiki/Usage-Access-Control) | Usage: roles and per-object access |
| [Configuration](https://github.com/Sphexi/DockMin/wiki/Reference-Configuration) | Reference: every environment variable |
| [JSON API](https://github.com/Sphexi/DockMin/wiki/Reference-JSON-API) | Reference: JSON routes, tokens, pagination, errors |
| [Deployment](https://github.com/Sphexi/DockMin/wiki/Reference-Deployment) | Reference: listeners, TLS, proxies, image, `compose.yaml` |
| [Security](https://github.com/Sphexi/DockMin/wiki/Reference-Security) | Reference: authorisation model, encryption, address policy, reporting a vulnerability |
| [Subsystems](https://github.com/Sphexi/DockMin/wiki/Reference-Subsystems) | Reference: names recorded for automated actions |
| [Troubleshooting](https://github.com/Sphexi/DockMin/wiki/Troubleshooting) | Troubleshooting: symptom, cause and action |
| [FAQ](https://github.com/Sphexi/DockMin/wiki/Troubleshooting-FAQ) | Troubleshooting: frequently asked questions |
| [Contributing](https://github.com/Sphexi/DockMin/wiki/Contributing) | Contributing: development setup and submitting changes |
| [Code standards](https://github.com/Sphexi/DockMin/wiki/Contributing-Code-Standards) | Contributing: comment discipline |
| [Frontend conventions](https://github.com/Sphexi/DockMin/wiki/Contributing-Frontend-Conventions) | Contributing: templates, partials, HTMX and CSS |

## Licence

MIT. See [LICENSE](LICENSE).

- The browser libraries under `app/static/vendor/` keep their own licences, in their directories: htmx (Zero-Clause BSD), Lucide icons (ISC, with MIT for the icons derived from Feather) and xterm.js with its fit addon (MIT).
- The image bundles the Docker Compose binary, licensed Apache-2.0. Its licence and notice are in the image at `/usr/share/licenses/docker-compose/`.
- The image bundles the Docker Buildx binary, licensed Apache-2.0. Its licence is in the image at `/usr/share/licenses/buildx/`.
