# Introduction

| Item | Value |
|---|---|
| Licence | MIT |
| Runs as | One container, one process, one SQLite database |

## Scope

### Managed

| Area | What dockmin does | Page |
|---|---|---|
| Containers | List, inspect, create, start, stop, restart, remove, recreate, logs, terminal, stats | [Containers](Usage-Containers) |
| Images | List, pull, tag, push, remove, update detection | [Images](Usage-Images) |
| Networks | List, create, remove, attach, detach, prune | [Networks](Usage-Networks) |
| Volumes | List, create, remove, prune | [Volumes](Usage-Volumes) |
| Compose stacks | Deploy from pasted content or an uploaded file, with variables and companion files. Redeploy, start, stop, restart, remove, adopt. Build images from uploaded contexts (administrators) | [Stacks](Usage-Stacks) |
| Swarm | Services, configs, secrets, nodes, tasks | [Swarm](Usage-Swarm) |
| Swarm stacks | Deploy from pasted content or an uploaded file, with variables and companion files, as services, networks, configs and secrets created through the Engine API | [Stacks](Usage-Stacks) |
| Registries | Docker Hub, GitHub Container Registry, AWS ECR, Azure Container Registry, GitLab, and generic registries, anonymous or authenticated | [Registries](Administration-Registries) |
| Docker hosts | Many hosts, with groups and tags | [Environments](Usage-Environments) |
| Users and access | Users, teams, and per-object access control | [Teams and users](Usage-Teams-and-Users), [Access control](Usage-Access-Control) |
| Automation | Health-based restarts, image updates, periodic restarts | [Automation](Operations-Automation) |
| Activity | Activity and authentication logging, with CSV and NDJSON download and scheduled S3 export | [Activity](Administration-Activity) |
| Backup | Backup and restore, with scheduled S3 upload | [Backups](Administration-Backups) |

### Not managed

- Kubernetes, Helm, and cluster provisioning
- Azure Container Instances
- Hosts that cannot be reached inbound
- Creating, joining or leaving a swarm
- Deployment from a Git repository, and builds from Git or remote contexts

## Supported environments

| Environment type | Connection |
|---|---|
| Docker Standalone | Local Unix socket, or TCP with mutual TLS |
| Docker Swarm (manager node) | Local Unix socket, or TCP with mutual TLS |

Plain TCP to a daemon is refused. Connection setup: [Environments](Usage-Environments).

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+, FastAPI |
| Frontend | HTMX, Jinja2 templates, plain CSS and JavaScript |
| Datastore | SQLite |
| Compose | Docker Compose CLI |

## Next

| Step | Page |
|---|---|
| Check the server and install Docker | [Prerequisites](Getting-Started-Prerequisites) |
| Run dockmin with one command and create the first administrator | [Quick start](Getting-Started-Quick-Start) |
| Own certificate, reverse proxy, hardened container, Docker Compose, build from source, upgrade, remove | [Installation](Getting-Started-Installation) |
| Add the Docker host | [Environments](Usage-Environments) |
