# FAQ

Symptoms with causes and actions: [Troubleshooting](Troubleshooting).

## Installation and first start

### Why does the browser warn about the certificate on the first visit?

dockmin serves a self-signed certificate that no browser trusts. Compare the certificate's SHA-256 fingerprint with the one in `docker logs dockmin | grep tls_certificate_self_signed`, then accept it. To remove the warning, supply a certificate or terminate TLS at a reverse proxy. [Certificate warning](Getting-Started-Quick-Start#certificate-warning), [Own certificate](Getting-Started-Installation#own-certificate), [Troubleshooting](Troubleshooting#certificate-warning).

### Where is the setup token?

It is in the container log: `docker logs dockmin | grep "First-run setup"`. It is held in memory, and a restart before setup completes issues a new one. [Start dockmin](Getting-Started-Quick-Start#start-dockmin).

### Why does `/setup` answer `404`, or the log hold no setup line?

An administrator already exists in the database, or the container did not finish starting. `/setup` answers `404` for good once an administrator exists. [Start dockmin](Getting-Started-Quick-Start#start-dockmin).

### Why does the container exit or restart repeatedly?

A startup check failed. The log line `startup_check_failed` names the check, the problem and the remedy. [Startup self-check failures](Troubleshooting#startup-self-check-failures).

### Can a reverse proxy sit in front of dockmin?

Yes. Start dockmin with `DOCKMIN_TLS_SELF_SIGNED=false` and point the proxy at the plain port `8000`. With the default `true`, the plain port answers `308` redirects to port 8443. [Behind a reverse proxy](Getting-Started-Installation#behind-a-reverse-proxy), [A proxy receives a redirect](Troubleshooting#a-proxy-receives-a-redirect).

### Can two dockmin containers share one data volume?

No. One process serves one data directory; a second process on the same directory refuses to start. [Single instance](Reference-Deployment#single-instance).

## Encryption key

### Where is the encryption key, and does it need a copy?

With no key variable set, dockmin generates the key in `/key/secret.key` on the key volume at the first start. Copy it off the host and keep it apart from every backup archive. [Copy the key](Getting-Started-Quick-Start#copy-the-key).

### What happens when the encryption key is lost?

The stored credentials and every archive made with that key cannot be read by any means. With a copy, supply it or put it back into the key volume. Without one, start a new instance and enter every credential again. [Key loss](Administration-Encryption#key-loss), [Restoring the key volume](Administration-Backups#restoring-the-key-volume).

### Why does the container refuse to start after a key change?

The configured key does not open the database. Start with the key the database was created with; after a rotation, supply the old key as the previous key. [encryption_key](Troubleshooting#encryption_key), [Re-key](Administration-Encryption#re-key).

## Environments and objects

### Why does the connection test of the local socket fail at `Socket` with `Permission denied`?

The container user (uid 10001) is not in the socket's group. Add `--group-add "$(stat -c %g /var/run/docker.sock)"` to `docker run`, or set `DOCKER_GID` with Compose, and recreate the container. [Local socket](Troubleshooting#local-socket).

### How is a remote host connected over TLS?

Configure the daemon for TLS on port `2376`, generate or supply the CA, client certificate and client key, then add a remote TLS environment. Plain TCP to a daemon is refused. [Add a remote TLS environment](Usage-Environments#add-a-remote-tls-environment), [Configure the daemon](Usage-Environments#configure-the-daemon), [Remote host over TLS](Troubleshooting#remote-host-over-tls).

### Which Docker versions are supported?

Daemons with Engine API 1.44 or later. [Supported Engine API versions](Usage-Environments#supported-engine-api-versions).

### What is the difference between a Standalone and a Swarm environment?

The connection test records the type. A host that is an active Swarm manager is `swarm` and gets the `Swarm` sidebar section; a Standalone host and a Swarm worker have no Swarm pages. [Swarm environments](Usage-Swarm#environments), [Recorded values](Usage-Environments#recorded-values).

### Why is a container not shown?

Administrators see every container on every environment. A standard user sees only containers of environments they hold a grant on, and in them only containers their resource control allows; objects no control covers are visible to administrators only. [Visibility](Usage-Containers#visibility), [Authorisation model](Usage-Access-Control#authorisation-model).

### Why is a container shown read-only, or why can dockmin's own container not be stopped?

A standard user has read access at most in a local socket environment. dockmin's own container, volumes, Compose project and Swarm service are protected from stop, remove and recreate for every role. [Local socket read ceiling](Usage-Access-Control#local-socket-read-ceiling), [dockmin's own container](Usage-Containers#dockmins-own-container).

## Operation

### How is dockmin updated?

An administrator updates a container started with `docker run` from `Administration > Instance`, with `Check for updates`. A Compose deployment is updated with `docker compose pull` and `docker compose up -d`. dockmin never checks or updates by itself. [Self-update](Administration-Instance#self-update), [Upgrading](Getting-Started-Installation#upgrading).

### How is dockmin backed up?

An administrator chooses `Download a backup` on `Administration > Backups`, or adds a scheduled backup to an object storage bucket. The archive never contains the encryption key; store the key separately. [Download](Administration-Backups#download), [Send backups to a bucket](Administration-Backups#send-backups-to-a-bucket), [The key is not in the archive](Administration-Backups#the-key-is-not-in-the-archive).

### What if a password is forgotten?

Another administrator resets it on the user's page. No command or setting resets a password or creates an administrator. When no administrator can sign in, restore an archive in which an administrator's password is known. [Reset a password](Usage-Teams-and-Users#reset-a-password), [Restore from the command line](Administration-Backups#restore-from-the-command-line).

### Why is an account locked?

`DOCKMIN_LOGIN_MAX_FAILURES` failed sign-ins (default 5) within `DOCKMIN_LOGIN_LOCKOUT_WINDOW` (default 15 minutes) lock a username; sign-in answers `429`. An administrator lifts the lock on the user's page, or `python -m app clear-lockout <username-or-address>` lifts it from inside the container. [Sign-in lock](Usage-Teams-and-Users#sign-in-lock), [Unlock an account](Usage-Teams-and-Users#unlock-an-account).

## Scope

### Can dockmin manage Kubernetes?

No. Docker Standalone and Docker Swarm are the only supported environment types. Kubernetes, Helm and Azure Container Instances are not managed. [Scope](Getting-Started-Introduction#scope).

### Does dockmin phone home?

No. There is no telemetry, update check or catalogue fetch at start or on a schedule. Outbound connections go to addresses an administrator saves and to a registry for a connection test, a review an administrator starts, an update check a user starts or a `container-updater` run. An update check reaches the registry the image names, anonymously unless the image is bound to a credential set, and needs no registry entry unless the registry is on a private network. Nothing contacts a registry at start, at setup or when a page loads. [Outbound contacts](Reference-Security#outbound-contacts).
