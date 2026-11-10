# Registries

A registry credential set holds the address, the login and the TLS settings dockmin uses to reach one container registry.

| Item | Value |
|---|---|
| Sidebar | Administration, `Registries` |
| Who can open it | Administrators. A standard user is answered `403` on every route under `/registries`. A request with no session is redirected to the sign-in page |
| Caching | Every registry page answers `Cache-Control: no-store` |
| Related settings | `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` ([Configuration](Reference-Configuration#listeners-and-network)) |
| Stored secrets | Sealed with the encryption key ([Encryption](Administration-Encryption)) |

| Page | Path |
|---|---|
| Registry list | `/registries` |
| Add registry | `/registries/new` |
| Registry | `/registries/{id}` |
| Edit registry | `/registries/{id}/edit` |
| Delete registry | `/registries/{id}/delete` |

| Route | Method | Effect |
|---|---|---|
| `/registries` | `POST` | Create a set; redirects to its page |
| `/registries/form-fields` | `POST` | Render the provider-dependent fields; saves nothing |
| `/registries/{id}/edit` | `POST` | Save changes; redirects to the set's page with `Changes saved.` |
| `/registries/{id}/test` | `POST` | Run the connection test |
| `/registries/{id}/environments` | `POST` | Grant the set to an environment |
| `/registries/{id}/environments/remove?environment_id=` | `POST` | Withdraw the set from an environment |
| `/registries/{id}/access` | `POST` | Grant the set to a user or team |
| `/registries/{id}/access/remove?user_id=` or `?team_id=` | `POST` | Withdraw a user or team grant |
| `/registries/{id}/default` | `POST` | Mark the set as the default for its address, or clear the mark ([Default for an address](#default-for-an-address)) |
| `/registries/{id}/delete` | `POST` | Delete the set |

- `{id}` is a whole number from 1; any other form answers `422`.
- Every `POST` carries the CSRF token ([Frontend conventions](Contributing-Frontend-Conventions)).
- A refused form is rendered again with status `422`, with the problem beside the field it belongs to.

## List

`GET /registries` lists every set.

| Item | Value |
|---|---|
| Columns | Name, Provider, Registry address, Authentication, Environments, Users and teams |
| Order | Name, then id |
| Page size | 25 by default; `limit` 1 to 500, otherwise `422` |
| Paging | `cursor`; an invalid cursor answers `400` |
| Authentication column | `Authenticated` or `Anonymous` |
| `Offered nowhere` | Shown under the Environments count when the set has no environment grant |
| `TLS not verified` | Badge beside the name when certificate verification is off |
| `Default` | Badge beside the name when the set is the default for its address |
| `Added with this instance` | Badge beside the name when the set is one of the [entries a new instance starts with](#entries-a-new-instance-starts-with) and still carries that mark |
| Users and teams | The number of user grants plus team grants |
| Empty list | `No registries`, with the `Add registry` action |

The list shows no user name and no secret. An htmx request receives the table fragment.

## Add a registry

1. Sign in as an administrator and open `Registries` under Administration.
2. Choose `Add registry`. The form opens with `Docker Hub` selected.
3. Choose the `Provider`. With JavaScript the provider fields below change at once. Without JavaScript, choose `Use this provider` to reload the form. The name and user name entered so far stay; the address and the provider settings reset to the new provider's defaults.
4. Enter the `Name`: 1 to 64 characters, unique ignoring case.
5. Enter the address and the provider settings ([Providers](#providers)).
6. Under `Authentication`, check `Authenticate to this registry` and enter the user name and the secret. Leave it unchecked for a registry that allows anonymous pulls. Amazon ECR always needs credentials.
7. For a GitLab or custom registry, set `Server certificate`: `Verify the registry's TLS certificate` is checked by default. For a private CA, paste PEM certificates into `Additional CA certificates` or choose a file ([TLS](#tls)).
8. Choose `Add registry`.

Result: the registry's page opens. A set with no environment grant is offered nowhere, so the page reads `This set is offered nowhere. Grant it to an environment before it can be used for a pull.` Grant it ([Access](#access)) and run `Test connection` ([Connection test](#connection-test)).

If the form returns with errors, each one is beside its field. The secret and the certificates are never filled back in; enter them again. Refusal texts: [Fields](#fields), [Address rules](#address-rules). For a failure that is not a refusal, see [Troubleshooting](Troubleshooting).

![Add registry form with GitLab Container Registry chosen](images/Administration-Registries-1.png)

## Fields

| Field | Rule |
|---|---|
| Name | 1 to 64 characters after trimming; control and format characters refused; unique, ignoring case |
| Provider | One of the six below. The add form opens with Docker Hub; a submission with no provider is refused |
| Registry address | [Address rules](#address-rules) |
| Authenticate to this registry | On or off. Off is refused for Amazon ECR |
| User name | Required when authentication is on. At most 256 characters; no colon; control and format characters refused |
| Secret | Required when authentication is on, unless a stored one is kept ([Editing](#editing)). At most 16,384 characters; control characters other than tab, carriage return and line feed refused; blank or whitespace-only counts as not entered |
| Verify the registry's TLS certificate | GitLab and custom only ([TLS](#tls)) |
| Additional CA certificates | GitLab and custom only ([TLS](#tls)). A text area and `Or choose a file` |
| Remove the stored CA certificates | Edit form, when a bundle is stored |
| Provider settings | The settings of the chosen provider |

The label of the user name and secret fields follows the provider ([Providers](#providers)).

All problems found are reported together, beside the field they belong to, with status `422`. A CA bundle that is too large, not text, or not valid PEM is reported with the other problems, not alone.

| Field | Refusal text |
|---|---|
| Name | `Enter a name.` / `The name is at most 64 characters.` / `The name may not contain control characters.` / `A registry credential set with this name already exists.` |
| Provider | `Choose a registry provider.` |
| Authentication | `This provider always needs credentials.` |
| User name | `Enter the user name.` / `The user name is at most 256 characters.` / `The user name may not contain a colon.` / `The user name may not contain control characters.` |
| Secret | `Enter the secret.` / `The secret is at most 16384 characters.` / `The secret may not contain control characters.` / the re-entry text in [Editing](#editing) |
| Provider settings | `<label> <reason>.`, for example `The region is not an AWS region such as eu-west-2.` ([Providers](#providers)). `These settings are not valid for this provider.` when a setting does not belong to the provider |

## Providers

### Docker Hub

| Item | Value |
|---|---|
| Address | Fixed: `docker.io`. Contacted at `registry-1.docker.io` |
| Settings | None |
| Authentication | Optional |
| User name label | `Docker Hub user name` |
| Secret label | `Access token`: a personal access token, not the account password |
| TLS options | None. Certificates are always verified |
| Secret kind | Static |

### GitHub Container Registry

| Item | Value |
|---|---|
| Address | Fixed: `ghcr.io` |
| Settings | `GitHub organisation` (optional), `Pull images under the organisation` (checkbox, default off) |
| Authentication | Optional |
| User name label | `GitHub user name` |
| Secret label | `Personal access token`: a classic personal access token with the `read:packages` scope |
| TLS options | None. Certificates are always verified |
| Secret kind | Static |

| Setting | Rule |
|---|---|
| `GitHub organisation` | A GitHub name: at most 39 characters, letters, digits and hyphens, starting with a letter or digit, no hyphen at the end or next to another hyphen |
| `Pull images under the organisation` | Requires an organisation |

### Azure Container Registry

| Item | Value |
|---|---|
| Address | Editable, no default. Example: `myregistry.azurecr.io` |
| Settings | None |
| Authentication | Optional |
| User name label | `Token name or service principal ID` |
| Secret label | `Token password or service principal secret` |
| TLS options | None. Certificates are always verified |
| Secret kind | Static |

### GitLab Container Registry

| Item | Value |
|---|---|
| Address | Editable. Default: `registry.gitlab.com` |
| Settings | `GitLab instance URL`, `Project ID` (optional), `Project path` (optional) |
| Authentication | Optional |
| User name label | `User name or deploy token name` |
| Secret label | `Personal access token or deploy token`, with the `read_registry` scope |
| TLS options | Verification toggle and CA bundle |
| Secret kind | Static |

| Setting | Rule |
|---|---|
| `GitLab instance URL` | Default `https://gitlab.com`. An `https` address of at most 256 ASCII characters: a host, an optional port, an optional path of letters, digits, `.`, `_`, `~`, `-`. No credentials, query or fragment. A trailing `/` is removed. The host is normalised like a registry address |
| `Project ID` | Positive integer, at most 9007199254740991 |
| `Project path` | `group/project` or deeper; at least two segments, at most 512 characters in total. A segment starts with a letter, digit or `_`, then letters, digits, `_`, `.`, `-`; at most 255 characters |

### Amazon ECR

| Item | Value |
|---|---|
| Address | Editable, no default. `123456789012.dkr.ecr.<region>.amazonaws.com`, ending `.amazonaws.com.cn` in a `cn-` region |
| Settings | `AWS region` (required) |
| Authentication | Always on. Anonymous is refused |
| User name label | `Access key ID` |
| Secret label | `Secret access key` |
| TLS options | None. Certificates are always verified |
| Secret kind | Derived ([Amazon ECR tokens](#amazon-ecr-tokens)) |

| Rule | Value |
|---|---|
| `AWS region` | At most 32 characters: two lower-case letters, one or more `-word` groups, a number of one or two digits, as in `eu-west-2` |
| Address and region | The address must be 12 digits, `.dkr.ecr.`, the region and `.amazonaws.com`. The region in the address must equal `AWS region`. A `cn-` region requires the `.amazonaws.com.cn` form, and any other region refuses it. Refusals: `An ECR address looks like 123456789012.dkr.ecr.eu-west-2.amazonaws.com.` and `The region in the ECR address must match the region setting.` |
| User name | `AKIA` followed by 16 uppercase letters or digits. Temporary access keys are refused: `An access key ID looks like AKIAIOSFODNN7EXAMPLE. Temporary keys are not supported.` |

### Custom registry

| Item | Value |
|---|---|
| Address | Editable, no default. Example: `registry.example.com:5000` |
| Settings | None |
| Authentication | Optional |
| User name label | `User name` |
| Secret label | `Password or token` |
| TLS options | Verification toggle and CA bundle |
| Secret kind | Static |

Any registry that serves the Docker registry v2 API over HTTPS. A custom set with the address `docker.io` is contacted at `registry-1.docker.io`.

### Provider settings

| Setting refusal | Text |
|---|---|
| `GitHub organisation` | `The organisation is not a valid GitHub organisation or user name.` |
| `Pull images under the organisation` without an organisation | `Use organisation needs an organisation name.` |
| `GitLab instance URL` | `The instance URL is not a valid https address.` / `The instance URL must be an https address without credentials, query or fragment.` / `The instance URL does not have a valid host.` |
| `Project ID` | `The project ID is not valid.` / `The value is not valid.` |
| `Project path` | `The project path must look like group/project.` |
| `AWS region` | `The region is required.` / `The region is not an AWS region such as eu-west-2.` |

- Settings other than the chosen provider's are discarded.
- Settings are stored unencrypted. A setting whose name contains `secret`, `password`, `token`, `credential` or `private` is refused.
- Choosing another provider resets the address and the provider settings to the new provider's defaults. On an edit it also requires the secret again.
- Stored settings are shown on the set's page. Pulls, update checks and the connection test use the registry address; no connection reads `GitHub organisation`, `Pull images under the organisation`, `GitLab instance URL`, `Project ID` or `Project path`. A change to `GitLab instance URL` or `AWS region` requires the secret again ([Editing](#editing)).

## Address rules

An address is a host name or IP address with an optional port. It is stored in the canonical form `host[:port]`.

| Input | Result |
|---|---|
| `Registry.Example.COM` | Stored as `registry.example.com` |
| `https://registry.example.com/` | Stored as `registry.example.com` (scheme and one trailing `/` removed) |
| `registry.example.com.` | Stored as `registry.example.com` (one trailing dot removed) |
| `registry.example.com:443` | Stored as `registry.example.com` (port 443 is omitted) |
| `registry.example.com:5000` | Accepted |
| `bücher.example` | Stored as `xn--bcher-kva.example` (IDNA-encoded) |
| `192.0.2.10` | Accepted |
| `[2001:db8::1]`, `[2001:db8::1]:5000` | Accepted; IPv6 is stored in square brackets |
| `2001:db8::1` | Refused: `An IPv6 address must be written in square brackets.` |
| `http://registry.example.com` | Refused: `Plain-HTTP registries are not supported. Credentials would cross the network unencrypted; use an https address.` |
| `ftp://registry.example.com` | Refused: only the `https` scheme may be written |
| `registry.example.com/path`, `user@host`, `host?x`, `host#x`, `host%41`, `host\x` | Refused: path, credentials, query, fragment, percent-encoding, backslash |
| `host:0`, `host:65536`, `host:` | Refused: `The port must be a number from 1 to 65535.` |
| `127.1`, `0x7f.1`, `2130706433`, `host.123` | Refused: `The host looks like a number but is not a dotted IPv4 address.` |
| `localhost`, `127.0.0.1`, `169.254.169.254`, `[::1]` | Refused: `This address is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` ([Internal addresses](#internal-addresses)) |
| Spaces, control or invisible characters | Refused: `The registry address must not contain spaces or control characters.` |
| Empty | Refused: `Enter the registry's host name or IP address.` |

| Limit | Value |
|---|---|
| Stored address | At most 262 characters |
| Host name | At most 253 characters |
| Label | 1 to 63 characters from `a-z`, `0-9`, `_`, `-`; no `-` at either end |

- A fixed-address provider (Docker Hub, GitHub Container Registry) takes its address from the provider. A blank address is accepted. Any other address is refused: `The address for this provider is always docker.io.`
- Plain-HTTP registries are not supported. Credentials never cross the network unencrypted.

### Internal addresses

A registry on this server or a link-local network is refused unless `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` permits it. The refused classes, the checks at save and at connect, the token service and Amazon ECR, and the texts: [Address policy](Reference-Security#address-policy).

| Item | Behaviour |
|---|---|
| At save | The refusal is on the field `address` |
| Connection test | Fails at the TCP connection stage with the refusal text. A refused token service realm fails the Authentication stage; no credentials are sent |
| Update checks | The reference is `Unknown` with reason `address_refused` ([Images](Usage-Images#update-check)) |

## Authentication

| Setting | Behaviour |
|---|---|
| Off (anonymous) | No user name and no secret are stored. Allowed for every provider except Amazon ECR |
| On | User name and secret are stored. The connection test sends them |
| Turned off on an edit | The stored user name and secret are deleted |
| Turned on on an edit | The user name and the secret are required |

An anonymous set records that pulls from the address need no login. The registry decides per repository, when the image is pulled, whether anonymous access is allowed. The connection test does not check it.

## Anonymous entries

An entry with authentication off is required only for a registry on a private network. Unbound images are checked and updated anonymously at the registry their reference names, with or without an entry, when the registry and its token service resolve to globally routable addresses.

| Where an entry with authentication off counts | Effect |
|---|---|
| Update checks and automatic updates | A registry whose address, or whose token service, is not globally routable is checked only when an entry for its address is granted to the environment, any credential set included. Without one the check fails with `private_address`. With one, the [address policy](Reference-Security#address-policy) applies. A public registry needs no entry |
| Adoption review | The review checks unbound images at the address ([Environments](Usage-Environments#adopt-everything-on-this-host)) |
| Pull picker and pull with no choice | An entry that reads as anonymous is handled as in the table below |

| Item | Behaviour |
|---|---|
| Binding | An entry is never recorded as a binding. A pull with `Anonymous` writes no binding |
| Entry deleted | No effect on images at a public registry. Checks and updates of unbound images at a private-network registry fail with `private_address` |
| Standard user | Needs no grant on the entry. Anonymous needs none |
| Stored | No user name and no secret |

An entry reads as anonymous when authentication is off, certificate verification is on and no CA bundle is stored. The provider does not matter.

| Entry | Adoption review | Pull picker | Pull with no choice made |
|---|---|---|---|
| Reads as anonymous | Enables the one anonymous candidate. It is not counted as a second match | Not listed beside `Anonymous (no credentials)`, unless an existing binding names it. That binding keeps it listed and preselected | Anonymous, unless another set fits ([Images](Usage-Images#which-set-a-pull-uses)) |
| Authentication off with a CA bundle or with verification off | Tried as itself | Listed, with `no authentication` | Tried as itself, as any set at the address |

| Instance with | Pull with no explicit choice |
|---|---|
| An entry that reads as anonymous beside one credential set at the address | Uses that set and binds it |
| An entry that reads as anonymous, no set with authentication on | Pulls anonymously and writes no binding |

`Bind unbound images` does not count an entry that reads as anonymous as a credential set. A reference at an address where no other set is granted is not checked and reads `No credential set at this address; updates use anonymous access.` An entry with authentication off and a CA bundle or verification off is a credential set there ([Images](Usage-Images#which-references-are-checked)).

## Entries a new instance starts with

A new instance holds two entries with authentication off.

| Name | Provider | Address |
|---|---|---|
| `Docker Hub (anonymous)` | Docker Hub | `docker.io` |
| `GitHub Container Registry (anonymous)` | GitHub Container Registry | `ghcr.io` |

| Item | Behaviour |
|---|---|
| When they exist | Added once, when the database holds no user and no registry set. A database that holds a user or a registry set when it is upgraded gets neither |
| Removal | Delete them as any set ([Deleting](#deleting)). A removed entry is not created again: not at a restart, not by a restore. A restore returns the database as it was |
| Mark | `Added with this instance` on the list and as `Origin` on the set's page |
| Mark cleared | When an edit turns authentication on or changes the provider or the address. Recorded as `seeded_default_cleared` ([Recorded actions](#recorded-actions)). A name or TLS edit keeps it |
| Name, TLS settings | Editable |
| Grant | Each environment created afterwards, the local socket environment of first-run setup included, is granted every entry that still has the mark and authentication off. Recorded as `registry_grants` in `environment_created` ([Activity](Usage-Environments#recorded-actions)). Environments that exist already are not granted the entries |
| Withdraw a grant | Per environment, as for any set ([Remove a grant](#remove-a-grant)) |
| Contacted | Never at start, at setup, when an environment is created or when a page loads. Only by an administrator's pull, an administrator-run check ([Images](Usage-Images#bind-unbound-images), [Environments](Usage-Environments#adopt-everything-on-this-host)) and the update checks that already contact configured registries ([Registry connections](#registry-connections)) |
| Docker Hub limits | Unauthenticated manifest `HEAD` requests do not count against Docker Hub's pull limit. A refusal with `429` reads `rate_limited` and is not retried |

## Secret handling

| Item | Behaviour |
|---|---|
| Secret | Stored encrypted. Never returned to the browser. The secret input is never given a value. The set's page shows `Stored. It is never shown.` |
| Derived registry token | Stored encrypted. Never shown ([Amazon ECR tokens](#amazon-ecr-tokens)) |
| CA bundle | Stored as PEM certificates. Never shown. The page shows subject, issuer, expiry and SHA-256 fingerprint of each certificate |
| User name | Shown to administrators on the set's page |
| After a refused submission | The form is rendered again without the secret and without the certificates. Enter them again |
| Log lines, error messages | Never contain the secret, the registry token or certificate text |

A secret that cannot be decrypted with the current encryption key is reported as `The stored secret cannot be read with the current encryption key. Enter it again.` The connection test is refused until it is entered again. A pull answers `The stored secret of that registry credential set cannot be read. An administrator needs to enter it again.`

## Editing

1. Open the set's page and choose `Edit`.
2. Change the fields. A blank secret keeps the stored one, except where the table below requires it.
3. Choose `Save changes`.

Result: the set's page opens with `Changes saved.` When nothing changed, nothing is written and no activity entry is recorded; the page still reads `Changes saved.`

When the form returns with a problem, see [Fields](#fields). A form that lists bound images states how many are bound and that they will move.

The secret must be entered again when an edit changes any of these:

| Change | Secret |
|---|---|
| Provider | Required |
| Registry address | Required |
| Amazon ECR region | Required |
| GitLab instance URL | Required |
| CA bundle: added, replaced or removed | Required |
| Certificate verification: on or off | Required |
| Name, user name, authentication kept on, other provider settings | Kept when blank |

| Edit that changes the registry address | Effect |
|---|---|
| Images bound to the set | Each becomes `credential_missing`, `Registry address changed`, and keeps the name of the set. Checks and automatic updates of those images use anonymous access until each is pulled again with a set for the new address ([Images](Usage-Images#when-a-set-is-deleted-or-its-address-changes)) |
| Default for the address | The set's default flag is cleared |
| Form | A notice states how many images are bound and that they will move |
| A change of provider that changes the address | The same |

| Situation | Result |
|---|---|
| Secret needed and blank | `Enter the secret again. Changing the provider, address, region, GitLab instance URL, certificate bundle or certificate verification requires it.` |
| No secret stored (authentication was off) and authentication turned on | `Enter the secret.` |
| Name taken by another set (ignoring case) | `A registry credential set with this name already exists.` |
| Any saved change to a derived set | The cached registry token is deleted |

## TLS

| Item | Behaviour |
|---|---|
| Trust | The operating system's root certificates, plus the set's CA bundle when one is stored. The bundle is added to the roots; it does not replace them |
| Minimum version | TLS 1.2 |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | Not read |
| Proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`) | Not read. Connections are direct |
| Options per provider | GitLab and custom: verification toggle and CA bundle. Every other provider: always verified, no bundle |

Operating system roots are taken from the TLS library's default file or directory when it exists. If neither exists, `/etc/ssl/certs/ca-certificates.crt` and then `/etc/pki/tls/certs/ca-bundle.crt` are tried. With none, every verified connection fails ([Registry connections](#registry-connections)).

### CA bundle

| Rule | Value |
|---|---|
| Content | PEM `CERTIFICATE` blocks only. A private key or any other block type is refused |
| Count | At most 10 certificates |
| Size | At most 64 KiB, pasted or uploaded. A chosen file replaces pasted text. A file that is not UTF-8 text is refused |
| Validity | Each certificate is currently valid. Expired and not-yet-valid certificates are refused with the date |
| CA flag | Not required. A self-signed server certificate is accepted as a trust anchor |
| Loadable | The bundle must load into the TLS library together with the system roots |
| Text outside PEM blocks | Discarded |
| With verification off | A bundle is refused: `A certificate bundle is used only when certificates are verified.` |

| Refusal | Text |
|---|---|
| Too large | `The certificate bundle is larger than 64 KiB.` / `That file is larger than 64 KiB.` / `That text is longer than 64 KiB.` |
| File is not text | `That file is not text. Upload a PEM file.` |
| No PEM block | `The certificate bundle is not in PEM format.` |
| Private key | `The certificate bundle contains a private key. Provide only certificates.` |
| Other block type | `The certificate bundle must contain only CERTIFICATE blocks.` |
| Unreadable block | `The certificate bundle could not be read as PEM certificates.` |
| More than 10 | `The certificate bundle contains more than 10 certificates.` |
| Expired | `Certificate <n> of the bundle expired on <date>.` |
| Not yet valid | `Certificate <n> of the bundle is not valid until <date>.` |
| Not loadable | `The connection layer could not load this certificate bundle.` |

- On an edit, empty bundle input keeps the stored bundle. `Remove the stored CA certificates` deletes it.
- A stored certificate expiring within 30 days is marked `Expires soon`; after expiry, `Expired`.

### Turning verification off

| Rule | Value |
|---|---|
| Providers | GitLab and custom only |
| Authentication | Must be off. Credentials are never sent to a registry whose certificate is not checked |
| Refusal | `Certificate verification can be turned off only for a registry without authentication. Supply the registry's certificate as a bundle instead.` |
| Enforcement | Also enforced by the database, and by the connection test, which refuses credentials without verification |

Where an unverified set is marked:

| Place | Mark |
|---|---|
| List | `TLS not verified` badge beside the name |
| Set's page | Panel `Certificate verification is off.`; the Certificate verification entry shows the same badge |
| Form | Warning beside the toggle |
| Connection test | Server certificate stage `Skipped`: `The certificate was not checked: verification is turned off for this set.` |

## Amazon ECR tokens

An ECR set stores a long-lived IAM access key. Each use exchanges it for a short-lived registry token.

| Item | Value |
|---|---|
| Credentials | IAM user access key ID as the user name, secret access key as the secret. Long-lived keys only (`AKIA…`); temporary keys are refused |
| Permission | The key's IAM policy must allow `ecr:GetAuthorizationToken` |
| Exchange | A signed (AWS Signature Version 4) `GetAuthorizationToken` request to `https://api.ecr.<region>.amazonaws.com/` (`api.ecr.<region>.amazonaws.com.cn` in `cn-` regions) |
| Request limits | 10 s; reply at most 64 KiB; redirects not followed; certificates verified against the operating system roots; proxy variables not read |
| Token lifetime | The expiry AWS reports, capped at 12 hours from the exchange |
| Cache | The token is stored encrypted on the set, with its expiry and refresh time |
| Shown | `Registry token` section of the set's page: `Cached token` (`None yet`, `Valid`, `Expired`), `Expires`, `Last refreshed`. The token itself is never shown |

### Refresh

| Rule | Value |
|---|---|
| When | At use, when a token is needed. No timer runs |
| Threshold | The cached token is refreshed when its remaining validity is at or below the smaller of 1 hour and half of its lifetime |
| Threshold for a Swarm service credential renewal | A [renewal](Operations-Automation#credential-renewal-for-swarm-services) needs a token with at least the longer of the job's period between runs and 6 hours of validity left, never more than the token's lifetime less 1 hour. A cached token with less is replaced by a new exchange |
| No token cached | A token is requested |
| Concurrency | One exchange at a time per set |
| Edit of the set | The cached token is deleted |
| Edit during an exchange | The result is discarded and the exchange repeated; at most 2 attempts |
| Database busy | The new token is used but not cached |

### Failures

| Rule | Value |
|---|---|
| Retry | A failed exchange is not repeated for 60 s. Requests inside that window fail at once with the earlier failure |
| Cached token still valid | It is used when a refresh fails |
| No valid cached token | The set cannot be used: `A registry token could not be obtained for that credential set.`, or the message below |
| Success | Clears the failure |
| Restart | The failure window is held in memory and is lost |

| Message | Usual cause |
|---|---|
| `AWS could not be reached. Check this server's outbound access to the ECR endpoint for the region.` | No outbound path to the endpoint, TLS failure, timeout, throttling, or an AWS server error |
| `AWS rejected the access key. Check the access key ID and the secret access key.` | Wrong or deleted key, wrong secret, expired key |
| `The access key is valid but is not allowed to request an ECR authorisation token. Grant it ecr:GetAuthorizationToken.` | Missing IAM permission |
| `AWS rejected the request because this server's clock differs from AWS's by more than five minutes. Correct the clock on this server.` | Clock skew on the dockmin server |
| `AWS answered, but not in the form a token exchange returns.` | Proxy or other service answering at the endpoint; content encoding other than `identity`; a reply with no usable token, or a token already expired |
| `AWS answered with far more data than a token exchange returns.` | Reply over 64 KiB |
| `This address is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` | The ECR endpoint resolves only to internal addresses that no permit covers |
| `The credential exchange returned a token that cannot be used.` | Token expired on arrival or not in `user:password` form |

## Connection test

`Test connection` on a set's page runs a staged test of the saved set.

1. Open the set's page and find the `Connection` section.
2. Choose `Test connection`.
3. Read the result: a `Passed` or `Failed` badge with a message, the time taken, and a table of stages with `Stage`, `Outcome`, `Time` and `Detail`.

Result when it worked: `The registry is reachable and accepted the credentials.` (or `The registry is reachable.` followed by the Authentication detail for an anonymous set). When it failed, the stage that failed names the cause ([Failures by stage](#failures-by-stage)); check that stage first, then [Troubleshooting](Troubleshooting).

| Rule | Value |
|---|---|
| Runs from | The dockmin server. It does not test what a Docker host can reach |
| What is tested | The saved settings and the stored secret only. A secret is never sent to an address that was not saved |
| Request | `GET` of `/v2/` over HTTPS, one request per connection, no redirects followed, `Accept-Encoding: identity`, `User-Agent: dockmin-registry-test` |
| Reply limits | Header at most 64 KiB; body read only from a token service, at most 64 KiB |
| Credentials go to | The registry address and, for a bearer challenge, an `https` token service. Never to a plain-HTTP address. Never without a verified certificate |
| Saving | Saving never runs a test and does not require one to pass. A failed test blocks nothing |
| Result | Shown on the page and not stored. The page reads `Not tested since saved.` until a test runs. An activity entry records the outcome |
| Concurrency | Tests of an identical connection that overlap share one run |
| Derived set | Performs a fresh token exchange. The token is discarded and not cached |
| Without JavaScript | The set's page is rendered again with the result |
| Unreadable secret | The test is refused with `The stored secret cannot be read with the current encryption key. Enter it again.` (`422`) |

A stage ends as `Passed`, `Failed`, `Skipped` or `Not run`. The first failure stops the test; later stages are `Not run`.

| Stage | Runs | Limit |
|---|---|---|
| Token exchange | Amazon ECR only | 10 s |
| DNS lookup | Always. `Skipped` when the host is an IP address | 5 s |
| TCP connection | Always | 5 s per address; up to 3 resolved addresses |
| TLS handshake | Always | 10 s |
| Server certificate | Checked during the handshake. `Skipped` when verification is off | Included in the TLS handshake limit |
| Registry API | Always | 10 s |
| Authentication | Always. `Skipped` for an anonymous set | 10 s, covering the token service and the registry |
| Whole test | | 30 s |

A stage that exceeds its limit reports `<Subject> did not finish within N seconds.`, where the subject is `The credential exchange`, `The name lookup`, `The TLS handshake`, `The registry API request` or `The authentication check`. A stage cut short by the whole-test limit reports `The test reached its overall time limit of 30 seconds.` A stage that never ran reads `Not run.`

A handshake that completes with an untrusted certificate reports `Passed` for the TLS handshake and `Failed` for the server certificate.

Stage details can include: `addresses` (count resolved), `address` and `attempts` (TCP), `tls_version` and `expires` (certificate expiry date), `http_status`, `method` (`basic` or `bearer`), `token_service` (host).

### Authentication stage

| Registry reply to `GET` of `/v2/` | Behaviour |
|---|---|
| `200` | `Skipped`: `The registry did not ask for credentials.` |
| `401` with a `Bearer` challenge | Credentials are sent to the token service named by `realm`, with its `service` and no `scope`. The token is then presented to the registry. `Bearer` is preferred when both are offered |
| `401` with a `Basic` challenge | Credentials are sent to the registry |
| `401` with another scheme or none | `Failed` |
| Anonymous set | `Skipped`: `Anonymous access is decided per repository at pull time, so no credentials were tested.` |

The token service must be an `https` address. It may be on another host or port. A redirect from it is not followed and fails the stage.

### Failures by stage

Certificate and connection messages name the subject that failed: `registry` or `token service`. A certificate or connection fault on the token service is reported in the Authentication stage.

| Stage | Message | Usual cause |
|---|---|---|
| Token exchange | The messages in [Failures](#failures) | See that table |
| Token exchange | `No credential exchange is configured for this provider.` / `The credential exchange could not be completed.` | Internal fault |
| DNS lookup | `The name does not exist. Check the spelling, or use the host's IP address.` | Misspelt host, or no DNS record |
| DNS lookup | `The DNS server did not answer. Check this server's DNS configuration.` | The resolver configured on the dockmin server is down or unreachable |
| DNS lookup | `The DNS server reported a permanent failure for this name.` | The authoritative server refuses the name |
| DNS lookup | `The name resolved, but to no usable address.` | Record without an IPv4 or IPv6 address |
| DNS lookup | `The name lookup failed.` | Any other resolver error |
| TCP connection | `The connection was refused. Nothing is listening on this port, or a firewall is rejecting it.` | Nothing listens on the port, or a firewall rejects it |
| TCP connection | `There is no route to the host. Check the address and the network path.` | Wrong address or no network path |
| TCP connection | `This server's network policy blocked the connection.` | Egress policy on the dockmin server |
| TCP connection | `This address is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` | The registry resolves only to internal addresses that no permit covers |
| TCP connection | `The connection timed out after 5 seconds. The host is down or a firewall is dropping the packets.` | Host down, or a firewall drops packets |
| TCP connection | `The connection could not be established.` | Any other connect error |
| TLS handshake | `This server has no operating system root certificates to verify the registry with. Install them in the image this server runs from.` | The image has no root certificate store ([Registry connections](#registry-connections)) |
| TLS handshake | `The stored CA bundle could not be loaded. Replace it.` | Stored bundle unreadable |
| TLS handshake | `The server does not speak TLS on this port. Registries are served over https; check the port.` | Plain-HTTP port or another service |
| TLS handshake | `The server does not offer TLS 1.2 or later, which is the minimum this product uses.` | Registry or proxy restricted to older protocols |
| TLS handshake | `The server rejected the handshake; it shares no cipher suite with this server.` | No cipher in common |
| TLS handshake | `The server ended the handshake with a TLS alert.` | Server refused the connection |
| TLS handshake | `The server closed the connection during the TLS handshake. It may not support TLS 1.2 or later.` | Wrong service, or protocol restriction |
| TLS handshake | `The TLS handshake failed.` / `The TLS connection failed.` | Any other handshake or socket error |
| Server certificate | `The registry's certificate was not issued by a trusted authority. If it uses a private CA, add that CA certificate to this set's CA bundle.` | Private CA: add its certificate to the set's CA bundle (GitLab and custom) or to the image's root store |
| Server certificate | `The registry's certificate has expired.` | Registry certificate past its end date |
| Server certificate | `The registry's certificate is not valid yet. Check the clocks on its host and on this server.` | Clock skew between the registry host and the dockmin server |
| Server certificate | `The registry's certificate does not cover the host name or IP address used to reach it.` | The certificate's Subject Alternative Names lack the exact address entered |
| Server certificate | `The registry's certificate is not valid for server authentication.` | Certificate lacks `serverAuth` |
| Server certificate | `The registry's certificate, or its issuer, uses a key or digest that is too weak.` | Weak key or signature in the chain |
| Server certificate | `The registry's certificate could not be verified.` | Any other verification error |
| Registry API | `The address answered with a redirect (HTTP <code>). Redirects are not followed; use the address the registry is served from.` | Address is a front page or an alias |
| Registry API | `The address answered HTTP <code>, which is not a Docker registry API response.` | Address and port belong to another service |
| Registry API | `The address answered, but not like a Docker registry: it sent no registry API header.` | Not a registry. A registry sends `Docker-Distribution-Api-Version: registry/2.0`, or answers `401` with a `Basic` or `Bearer` challenge |
| Registry API | `The registry closed the connection without answering.` / `The registry's reply was not valid HTTP.` / `The registry's reply was far larger than a registry sends.` / `The connection to the registry was lost.` / `The TLS connection to the registry failed: <reason>` | Proxy fault, wrong service, network fault |
| Authentication | `The registry's authentication challenge is malformed or too large.` | Challenge over 4 KiB, more than 8 challenges or 16 parameters, or invalid syntax |
| Authentication | `The registry asked for credentials without naming an authentication method.` | Empty challenge |
| Authentication | `The registry asks for an authentication method this product does not support.` | Challenge is neither `Basic` nor `Bearer` |
| Authentication | `The user name cannot be sent with Basic authentication.` | User name contains a colon |
| Authentication | `The registry's challenge does not name a token service.` / `The token service address in the registry's challenge is not usable.` | Registry misconfigured |
| Authentication | `The registry's token service uses plain HTTP, so the credentials were not sent to it.` | Token service configured with an `http` realm |
| Authentication | `The token service is not used. This address is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` | The realm names an internal host that no permit covers |
| Authentication | `The token service name could not be resolved: <reason>` / `The token service name could not be resolved.` / `The token service name resolved, but to no usable address.` / `The token service could not be reached: <reason>` | DNS or network fault between this server and the token service |
| Authentication | `The token service's certificate …` / `The token service closed the connection without answering.` / `The connection to the token service was lost.` | The same faults as the registry's, on the token service |
| Authentication | `The token service rejected the credentials.` | Wrong user name or secret, expired token, missing scope |
| Authentication | `The registry rejected the credentials.` | Same, on a `Basic` registry |
| Authentication | `The token service answered HTTP <code> to the credentials.` / `The registry answered HTTP <code> to the credentials.` | Token service fault, or a redirect |
| Authentication | `The token service's reply held no usable token.` | Reply without `token` or `access_token`, over 8,192 characters, or with disallowed characters |
| Authentication | `The registry rejected the token the token service issued.` / `The registry answered HTTP <code> to the token.` | Token lacks access to the registry API |
| Any | `The connection test could not be completed.` | Internal fault |

## Access

A set is offered for a pull on an environment only when it has an environment grant for that environment. A set with no environment grant is offered nowhere.

| Grant | Meaning |
|---|---|
| Environment grant | The set may be used for a pull on that environment |
| User grant | The user may use the set |
| Team grant | Members of the team may use the set |

| Caller | Needs |
|---|---|
| Administrator | Environment grant on the set |
| Standard user | Sight of the environment, the environment grant on the set, and a user grant or a team grant on the set |
| Automation (no user) | Environment grant on the set |

### Grant a set to an environment

1. Open the set's page and find the `Environments` section.
2. Under `Grant to an environment`, enter the exact `Environment name`.
3. Choose `Grant to environment`.

Result: the environment appears in the table and the page reads `Environment granted.` When it did not work, the field shows `Enter an environment name.`, `No environment has that name.` or `That environment already has this credential set.`

### Grant a set to a user or team

1. Open the set's page and find the `Users and teams` section.
2. Under `Grant to a user or team`, choose `A user` or `A team` and enter the exact `Username or team name`.
3. Choose `Grant to user or team`.

Result: the name appears in the table and the page reads `Access granted.` When it did not work, the field shows one of `Choose whether the grant is for a user or a team.`, `Enter a username.`, `Enter a team name.`, `No user has that username.`, `No team has that name.`, `That user is disabled.`, `Administrators reach every environment and need no grant.` or `That grant already exists.`

### Remove a grant

1. In the environment table or the users and teams table, choose `Remove <name>`.
2. Review the dialog and choose `Remove environment` or `Remove access`.

| Dialog | Text |
|---|---|
| Environment | `Remove this environment?` `The credential set will no longer be offered for pulls on this environment.` |
| User or team | `Remove this access?` `This removes the direct access to this credential set held by this user. Access held through another grant is not affected.` |

Result: the page reads `Environment removed.` or `Access removed.` Removing one grant leaves access held through another.

### Rules

- Environment grants apply to administrators too.
- Grants are added by exact environment name, username or team name. Administrators and disabled users cannot be named for a user grant.
- The page lists at most 500 environments, and at most 500 users and 500 teams, with `Showing N of M` when cut.
- Offered sets are listed by name, at most 200 for one registry address. An offer carries the name, provider, address, whether authentication is on, whether certificates are verified and the secret kind. It carries no user name and no secret.
- A standard user who cannot see the environment is answered as if it did not exist.
- Grants are checked again each time a set's login is resolved, not only when the set is offered. A set that is missing, not granted to the environment, or not granted to the user all answer `That registry credential set is not available here.` A set at another address than the image's answers `That credential set is for a different registry.`
- Removing an environment grant changes no image binding. A bound set that is no longer granted to the environment refuses with `credential_unavailable`; only a deleted set or a changed address (`credential_missing`) falls back to anonymous access ([Images](Usage-Images#when-a-set-is-deleted-or-its-address-changes)).
- Deleting an environment, a user or a team removes its grants on registry sets.
- A set is offered for a pull, a push, a container create or a recreate only when its address equals the registry address of the image reference ([Images](Usage-Images#which-set-a-pull-uses)).
- Which offered set a pull uses is decided by the image's binding ([Images](Usage-Images#credential-bindings)). Container create and recreate follow the same rules ([Containers](Usage-Containers#pull)).

## The credential picker

A pull form offers `Anonymous (no credentials)` or one credential set, under the legend `Credential set`. The value chosen is sent with the request and decides the pull. Nothing is tried automatically: when the pull fails, the failure is reported with the registry's reason, no other set is used, and the user chooses again.

| Item | Value |
|---|---|
| Options | `Anonymous (no credentials)`, then every set offered for the reference's registry address, by name, at most 200. Each set shows its provider, its address, `no authentication` when it has none, and `Default for this registry.` An entry that [reads as anonymous](#anonymous-entries) is left out unless the reference is bound to it |
| No set offered | `Anonymous (no credentials)` as text with `No credential set is available for this registry.`, and no control |
| Submitted value | `anonymous` or a set id. A set that is not offered to the user for the reference is refused ([Access](#access)) |
| Binding | A pull with a set binds the reference to it ([Images](Usage-Images#credential-bindings)). A pull with `Anonymous` binds nothing. An administrator choosing a set other than the current binding rebinds it, and choosing `Anonymous` removes the binding |
| Standard user, reference already bound | The option is fixed to the binding and its name is shown. The form says `Only an administrator can change which credential set this image uses.` The binding cannot be changed from the picker |

| Binding of the reference | Preselected |
|---|---|
| Bound to a set the user may use | That set |
| Bound to a set the user may not use | Nothing. A standard user sees `This image is bound to a credential set you cannot use.` and no control |
| None or `credential_missing`, and a set is the [default for the address](#default-for-an-address) | That set |
| None or `credential_missing`, and no default | `Anonymous` |

| Page | Where the picker appears |
|---|---|
| Image pull | After `Continue` ([Images](Usage-Images#pull)) |
| Container create | The Image section, once an image is named, and the review when the image will be pulled ([Containers](Usage-Containers#credential-sets-for-a-pull)) |
| Compose stack create, edit, adopt, redeploy | One picker for each image the check reports it will pull; on the redeploy confirmation, one for each image the stored file names outright ([Stacks](Usage-Stacks#images-and-credentials)) |
| Swarm stack create, edit, adopt, redeploy | One picker for each image of the file ([Stacks](Usage-Stacks#swarm-images-and-credentials)) |
| Swarm service create and edit | The Image section of the form and the review of an edit that changes the image ([Swarm](Usage-Swarm#images-and-registry-credentials)) |

- Push, the recreate confirmation, the JSON routes and automation do not use the picker. Push and recreate preselect as set out in [Images](Usage-Images#which-set-a-pull-uses); the JSON routes and automation follow the same rules without a form.
- At most 50 pickers are shown on one stack page.

## Default for an address

| Item | Value |
|---|---|
| Scope | One set per canonical registry address. The database allows no second |
| Set | `Make the default` on the set's page. Any other default at the same address is cleared first |
| Clear | `Clear the default` |
| Shown | `Default` badge on the list beside the name. `Default for this address` on the set's page. `Default for this registry.` beside the set in the picker and on the push and recreate forms |
| Effect | Preselects the set in the picker, and on the push and recreate forms, for an image reference at the address that has no binding |
| Not used for | Deciding a pull. A bound image uses its bound set, and automatic updates use the bound set, or anonymous access when the image has none |
| Notices | `Marked as the default for its registry address.`, `No longer the default for its registry address.` |
| Unchanged | Nothing is written and no activity entry is recorded |
| Address edited | The flag is cleared |
| Set deleted | The flag goes with it |

## Duplicates

Two sets are duplicates when they have the same canonical address and the same login: both anonymous, or both authenticated with the same user name (compared ignoring case).

| Behaviour | Value |
|---|---|
| Effect | A notice on each set's page, naming the other sets (up to 50). Nothing is refused |
| Both sets | Kept. Each image is bound to the set that pulled it |
| Recorded | The number of duplicates, as `duplicates` in the activity entry |
| Names | Still unique, ignoring case |

## Deleting

1. Open the set's page and choose `Delete`. The confirmation page changes nothing.
2. Read what the page lists.
3. Choose `Delete registry`, or `Cancel`.

Result: the page `Registry deleted` states that the credential set and its grants were deleted, that pulled images and running containers were not touched and that activity history is kept. It also states how many images read `Credential set deleted`.

| The confirmation lists |
|---|
| Name, provider and address |
| Environments the set is granted to (up to 500, with the total) |
| Users and teams whose grants end |
| Images bound to the set: the total, and up to 500 as environment and reference. When the list is cut, the count per environment follows |

| Outcome | Behaviour |
|---|---|
| Deleted | The set, its environment grants and its user and team grants |
| Images bound to the set | Not deleted. Each binding becomes `credential_missing`, `Bound set deleted`, in the same transaction, and keeps the name of the deleted set. One activity row is written for each |
| Checks and automatic updates for those images | Use anonymous access until each image is pulled again with another set ([Images](Usage-Images#when-a-set-is-deleted-or-its-address-changes)) |
| Pulled images, running containers | Not touched |
| Activity history | Kept |
| Identifier | Never reissued |
| Name changed since the confirmation | Deletion refused (`422`): `Changed since it was reviewed; nothing was deleted.` |
| Number of bound images changed since the confirmation | Deletion refused (`422`) with the same message. The failure is recorded with the reason `bindings_changed` |
| Confirmation incomplete | `The confirmation was incomplete. Review it again.` |
| Set no longer exists | `404` |

The confirmation carries the set's name and the number of bound images. The delete applies only while both are unchanged. When it is refused, the confirmation page is shown again with the current values.

![Delete registry confirmation with the bound images](images/Administration-Registries-2.webp)

## Recorded actions

Target type: `registry_credential`; target id: the set's id.

| Action | Detail recorded |
|---|---|
| `registry_credential_created` | `name`, `provider`, `address`, `authentication_enabled`, `verify_tls`, `ca_bundle` (whether a bundle is stored), `duplicates` |
| `registry_credential_updated` | `name`, `changed`, `secret_replaced`, `duplicates`, `seeded_default_cleared` (`true`, only when the edit removed the `Added with this instance` mark), and when the address changed: `address`, `bindings_marked_missing` (the number of images moved to `credential_missing`) and, if the set was the default, `default_cleared`. `changed` lists the fields that differ from `name`, `provider`, `address`, `authentication`, `username`, `extras`, `verify_tls`, `ca_bundle` |
| `registry_credential_deleted` | `name`, `provider`, `address`, `bindings_marked_missing` |
| `registry_credential_default_set`, `registry_credential_default_cleared` | `name`, `address` |
| `registry_credential_tested` | `address`, `passed`, `failed_stage`, `duration_ms` |
| `registry_credential_environment_granted` | `environment_name`; the entry is scoped to the environment |
| `registry_credential_environment_revoked` | `environment_name`; the entry is scoped to the environment |
| `registry_credential_access_granted` | `grantee_kind` (`user` or `team`), `grantee_name` |
| `registry_credential_access_revoked` | `grantee_kind`, `grantee_name` |

- Each binding moved to `credential_missing` also gets an `image_binding_credential_missing` row, target type `image_binding`, target id the canonical image reference, scoped to the environment ([Images](Usage-Images#recorded-actions)). Its detail holds `credential_set_id`, `credential_set_name` and `missing_reason` (`address_changed` or `deleted`).
- `failed_stage` is one of `credential_exchange`, `dns`, `tcp`, `tls_handshake`, `certificate`, `registry_api`, `authentication`, or null when the test passed.
- The registry user name, the secret, the registry token and the CA bundle are never recorded.

## Registry connections

The dockmin server connects to registries directly, over HTTPS. It makes these connections when an administrator runs a registry connection test, when a token for an Amazon ECR set is needed, when an [image update check](Usage-Images#update-check) runs, when an administrator runs a [Bind unbound images](Usage-Images#bind-unbound-images) review or an [adoption review](Usage-Environments#adopt-everything-on-this-host), and when an administrator checks for a dockmin update ([Instance](Administration-Instance)).

| Purpose | Destination | Port |
|---|---|---|
| Connection test | The set's registry address | The address's port; 443 when none |
| Connection test | The token service named in the registry's `WWW-Authenticate` challenge. Must be `https`; may be another host | The URL's port; 443 when none |
| ECR token exchange | `api.ecr.<region>.amazonaws.com` (`api.ecr.<region>.amazonaws.com.cn` in `cn-` regions) | 443 |
| Update check: manifest `HEAD` | The registry address of the bound credential set, or, for an image with no binding or a `credential_missing` binding, of the image reference. `docker.io` is contacted at `registry-1.docker.io` | The address's port; 443 when none |
| Update check: token | The `https` token service named in the registry's `WWW-Authenticate` challenge. May be another host | The URL's port; 443 when none |
| dockmin update check: manifest `HEAD` and tag list `GET` | The registry of dockmin's own image reference | The address's port; 443 when none |

| Requirement | Detail |
|---|---|
| Direct egress | Allow outbound TCP from the dockmin server to the destinations above. A proxy is not used |
| Egress policy | Compose provides none. The default bridge network allows all outbound traffic; a restriction is applied at the host's firewall |
| Internal addresses | Refused unless permitted ([Address policy](Reference-Security#address-policy)) |
| Update check requests | On a user's request and on each run of a `container-updater` automation ([Automation](Operations-Automation#detection)). `HEAD /v2/<repository>/manifests/<tag>` to the registry address of the bound set or, for an image with no binding, of the image reference, and `GET` to its token service when the registry asks for a token. An image with no binding needs no registry entry when the registry and its token service resolve to globally routable addresses; otherwise an entry granted to the environment at the registry address is required. No image data is fetched. Nothing is sent when no check is requested and no automation runs |
| Bind unbound images and adoption reviews | On an administrator's request only. `HEAD` requests, one for each granted credential set, and an anonymous `HEAD` only when no set matched; at most 200 references, 5 requests at a time ([Images](Usage-Images#bind-unbound-images)) |
| Pulls and pushes | The Docker host connects to the registry, not dockmin. The host needs outbound access to the registry. dockmin sends the credential set's login to the host with the request |
| Proxy variables | `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and their lower-case forms are not read |
| Operating system roots | The image dockmin runs from must contain root certificates. Looked up in the TLS library's default file or directory, then `/etc/ssl/certs/ca-certificates.crt`, then `/etc/pki/tls/certs/ca-bundle.crt` |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | Not read |
| Private CAs | GitLab and custom sets take a CA bundle per set. For every other provider, and for a TLS-intercepting gateway, add the CA to the image's operating system root store |
| DNS | The resolver configured in the dockmin container |
| Clock | The ECR exchange fails when the server's clock differs from AWS's by more than five minutes |

| Symptom | Cause |
|---|---|
| Connection test fails at the TLS handshake with `This server has no operating system root certificates to verify the registry with` | No root certificate file or directory in the image |
| Connection test fails at the TCP connection with `This server's network policy blocked the connection` | Egress policy on the dockmin server or its container network |
| Connection test fails at the TCP connection with `This address is on this server or a link-local network. Set DOCKMIN_PERMITTED_INTERNAL_NETWORKS to allow it.` | The registry resolves to an internal address ([Address rules](#address-rules)) |
| ECR exchange fails with `AWS could not be reached` | No direct path to the ECR endpoint, a proxy that must be traversed, or no root certificates in the image. A warning `registry_system_trust_missing` is logged at start when there are none |

The test runs from the dockmin server and does not show what a Docker host can reach.
