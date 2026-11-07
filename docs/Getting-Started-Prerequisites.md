# Prerequisites

## Requirements

| Component | Requirement | Needed for |
|---|---|---|
| Server | Linux with Docker Engine. The image is published for `linux/amd64` and `linux/arm64` | Every install |
| Docker Engine on every managed host | API v1.44 or later | Every install |
| Access to the Docker socket | `/var/run/docker.sock` on the dockmin server, to manage the server itself. Remote hosts: [Environments](Usage-Environments) | Managing the server itself |
| GNU coreutils | `stat -c`, and `install` for the Compose path | The commands on [Quick start](Getting-Started-Quick-Start) and [Installation](Getting-Started-Installation) |
| Network access to `ghcr.io` | Pulls the image | Every install except a build from source |
| TCP port 8443 | Open from the browser to the server, including any cloud security group or host firewall | `docker run` install |
| Docker Compose plugin | v2.32.0 or later. Tested with v2.33.0 and v5.5.1 | Compose path: runs `compose.yaml` and the examples |
| `git`, GNU `sed`, `openssl` | `openssl` generates the encryption key and, for a trial, a certificate | Compose path |
| Root on the dockmin server | Changes the owner of the key and certificate files | Compose path |
| Docker with BuildKit, network access to Docker Hub and the Python package index | Builds the image | Build from source |
| Compose binary for stacks | v2.32.0 or later. The image carries one; versions: [Image contents](Reference-Deployment#the-image) | Stack deployments |

## Check the server

1. Check the Docker Engine version and its API version on the dockmin server.

```bash
sudo docker version --format '{{.Server.Version}} (API {{.Server.APIVersion}})'
```

Success: one line such as `<version> (API 1.<minor>)`. The API version is 1.44 or later. Failure: `Cannot connect to the Docker daemon` means Docker is not running or is not installed. Install it ([Install Docker](#install-docker)) or start it with `sudo systemctl start docker`.

2. Find the group that owns the Docker socket. The quick start passes it to the container.

```bash
stat -c %g /var/run/docker.sock
```

Success: a number, for example `999`. Failure: `No such file or directory` means the daemon is not running or listens elsewhere.

3. Check that nothing listens on port 8443.

```bash
sudo ss -ltn 'sport = :8443'
```

Success: only the header line. A listener row means another program holds the port, and the container cannot publish it.

4. Compose path only. Check the Compose plugin version.

```bash
docker compose version
```

Success: `Docker Compose version v2.32.0` or later. Failure: `docker: 'compose' is not a docker command` means the plugin is missing ([Install Docker](#install-docker) installs it).

5. Open port 8443 on any cloud security group between the browser and the server.

## Install Docker

Skip this section when the server already runs a supported Docker Engine ([Requirements](#requirements)). The commands install Docker Engine, the Docker CLI, containerd, Buildx and the Docker Compose plugin from Docker's own repository. They run as an administrator with `sudo`.

| Item | Detail |
|---|---|
| Confirmation prompts | `apt install` and `dnf install` ask for confirmation. Add `-y` to run them without a terminal |
| `dnf install -y` | Imports Docker's GPG key without the fingerprint prompt described in step 3 of the Fedora and Red Hat Enterprise Linux sections |
| Published ports | Ports Docker publishes bypass `ufw` and `firewalld` rules. Port 8443 of the quick start is reachable on every interface whatever those rules say |
| Firewall backend | Docker supports `iptables-nft` and `iptables-legacy` only |

| Distribution | Releases Docker lists | Architectures Docker lists |
|---|---|---|
| Ubuntu | 26.10, 26.04, 24.04, 22.04 (64-bit) | amd64, armhf, arm64, s390x, ppc64el |
| Debian | 13 (Trixie), 12 (Bookworm) | amd64, armhf, arm64, ppc64el |
| Fedora | 45, 44, 43 | Not listed |
| Red Hat Enterprise Linux | 8, 9, 10 (maintained releases) | x86_64, aarch64, s390x |

The dockmin image is published for `linux/amd64` and `linux/arm64` only. Docker does not officially support Ubuntu derivatives such as Linux Mint. Current lists: [Official pages](#official-pages).

### Ubuntu

1. Remove conflicting packages. The command removes only those that are installed, and prints `dpkg: no packages found matching <package>` for each one that is not. Removing an installed Docker package stops its daemon and the containers it runs. Images, containers, volumes and networks in `/var/lib/docker` are kept.

```bash
sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc | cut -f1)
```

2. Add Docker's signing key. This also installs the dependencies `ca-certificates` and `curl`.

```bash
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

3. Add Docker's repository.

```bash
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
```

Success: `apt update` lists `download.docker.com` without an error. Failure: `The repository ... does not have a Release file` means the release is not in Docker's list above.

4. Install the packages.

```bash
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

5. Start the service when it is not running.

```bash
sudo systemctl start docker
```

6. Check the versions.

```bash
sudo docker version
sudo docker compose version
```

Success: `docker version` prints a `Client` section and a `Server` section, and `docker compose version` prints `Docker Compose version v<version>`. Failure: a `Server` section missing with `Cannot connect to the Docker daemon` means the service is not running. Run `sudo systemctl status docker`.

Source: [docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/).

### Debian

1. Remove conflicting packages. The command removes only those that are installed, and prints `dpkg: no packages found matching <package>` for each one that is not. Removing an installed Docker package stops its daemon and the containers it runs. Images, containers, volumes and networks in `/var/lib/docker` are kept.

```bash
sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-doc docker-buildx podman-docker containerd runc | cut -f1)
```

2. Add Docker's signing key. This also installs the dependencies `ca-certificates` and `curl`.

```bash
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

3. Add Docker's repository.

```bash
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
```

Success: `apt update` lists `download.docker.com` without an error.

4. Install the packages.

```bash
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

5. Start the service when it is not running.

```bash
sudo systemctl start docker
```

6. Check the versions.

```bash
sudo docker version
sudo docker compose version
```

Success: `docker version` prints a `Client` section and a `Server` section, and `docker compose version` prints `Docker Compose version v<version>`. Failure: `Cannot connect to the Docker daemon` means the service is not running. Run `sudo systemctl status docker`.

Source: [docs.docker.com/engine/install/debian](https://docs.docker.com/engine/install/debian/).

### Fedora

1. Remove conflicting packages. `dnf` prints `No packages to remove for argument: <package>` or `No match for argument: <package>` for each one that is not installed. Removing an installed Docker package stops its daemon and the containers it runs. Images, containers, volumes and networks in `/var/lib/docker` are kept.

```bash
sudo dnf remove docker \
                docker-client \
                docker-client-latest \
                docker-common \
                docker-latest \
                docker-latest-logrotate \
                docker-logrotate \
                docker-selinux \
                docker-engine-selinux \
                docker-engine \
                moby-engine \
                docker-cli \
                docker-compose
```

2. Add Docker's repository. Fedora's `dnf` provides `config-manager`, so no extra package is installed.

```bash
sudo dnf config-manager addrepo --from-repofile https://download.docker.com/linux/fedora/docker-ce.repo
```

3. Install the packages. When `dnf` prompts to accept the GPG key, accept it only if the fingerprint is `060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35`. `dnf` on Fedora prints it without spaces, and `dnf` on Red Hat Enterprise Linux in groups of four.

```bash
sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

4. Start the service and start it at boot. The install does not start Docker.

```bash
sudo systemctl enable --now docker
```

5. Check the versions.

```bash
sudo docker version
sudo docker compose version
```

Success: `docker version` prints a `Client` section and a `Server` section, and `docker compose version` prints `Docker Compose version v<version>`. Failure: `Cannot connect to the Docker daemon` means the service is not running. Run `sudo systemctl status docker`.

Source: [docs.docker.com/engine/install/fedora](https://docs.docker.com/engine/install/fedora/).

### Red Hat Enterprise Linux

The steps are for Red Hat Enterprise Linux. Docker's page does not cover CentOS.

1. Remove conflicting packages. `dnf` prints `No packages to remove for argument: <package>` or `No match for argument: <package>` for each one that is not installed. Removing an installed Docker package stops its daemon and the containers it runs. Images, containers, volumes and networks in `/var/lib/docker` are kept. The command also removes `podman`.

```bash
sudo dnf remove docker \
                docker-client \
                docker-client-latest \
                docker-common \
                docker-latest \
                docker-latest-logrotate \
                docker-logrotate \
                docker-engine \
                podman \
                runc
```

2. Install the dependency `dnf-plugins-core` and add Docker's repository.

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
```

3. Install the packages. When `dnf` prompts to accept the GPG key, accept it only if the fingerprint is `060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35`. `dnf` on Fedora prints it without spaces, and `dnf` on Red Hat Enterprise Linux in groups of four.

```bash
sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

4. Start the service and start it at boot.

```bash
sudo systemctl enable --now docker
```

5. Check the versions.

```bash
sudo docker version
sudo docker compose version
```

Success: `docker version` prints a `Client` section and a `Server` section, and `docker compose version` prints `Docker Compose version v<version>`. Failure: `Cannot connect to the Docker daemon` means the service is not running. Run `sudo systemctl status docker`.

Source: [docs.docker.com/engine/install/rhel](https://docs.docker.com/engine/install/rhel/).

### After installing

| Item | Detail |
|---|---|
| Versions dockmin needs | [Requirements](#requirements). The packages above install the current Engine and the Docker Compose plugin |
| `sudo` | Every `docker` command in these pages also runs as `sudo docker`, including the [Quick start](Getting-Started-Quick-Start) command. A file that `sudo docker cp` writes belongs to root. Before a command reads it with `<` or `$(cat …)`, give it to the current user ([Copy the key](Getting-Started-Quick-Start#copy-the-key)) |
| Running `docker` without `sudo` | Optional: add the user to the `docker` group, below |
| `docker` group | Membership grants root-level privileges on the host. Docker's warning is on the [post-installation page](https://docs.docker.com/engine/install/linux-postinstall/) |
| Start at boot | Ubuntu and Debian start Docker at boot. The Fedora and Red Hat Enterprise Linux steps above enable it |

Optional. Add the current user to the `docker` group. The change applies to new sessions, and `newgrp` applies it to the current shell.

```bash
getent group docker >/dev/null || sudo groupadd docker
sudo usermod -aG docker $USER
newgrp docker
```

Success: `docker ps` runs without `sudo` and prints a table header. Failure: `permission denied while trying to connect to the Docker daemon socket` means the shell has not picked up the group. Open a new session.

### Official pages

| Page | Covers |
|---|---|
| [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) | Supported releases, apt repository, other install methods |
| [Debian](https://docs.docker.com/engine/install/debian/) | Supported releases, apt repository, other install methods |
| [Fedora](https://docs.docker.com/engine/install/fedora/) | Supported releases, rpm repository, other install methods |
| [Red Hat Enterprise Linux](https://docs.docker.com/engine/install/rhel/) | Supported releases, rpm repository, other install methods |
| [Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/) | Non-root use, start at boot |

## Next

[Quick start](Getting-Started-Quick-Start) installs dockmin with one command. [Installation](Getting-Started-Installation) covers the other paths.
