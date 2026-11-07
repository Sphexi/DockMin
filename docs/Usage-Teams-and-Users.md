# Teams and users

| Page | Path | Administrators | Standard users |
|---|---|---|---|
| Teams | `/teams` | Every team | The teams they belong to |
| New team | `/teams/new` | Yes | Refused (403) |
| Team | `/teams/{id}` | Every team | A team they belong to; otherwise `404` |
| Users | `/users` | Yes | Refused (403) |
| New user | `/users/new` | Yes | Refused (403) |
| User | `/users/{id}` | Yes | Refused (403) |
| Your account | `/account` | Yes | Yes |
| API tokens | `/account/tokens` | Yes | Yes |

The sidebar group `Teams and users` holds `Teams` for every role and `Users` for administrators. The user menu in the top bar holds `Account` and `Sign out`. Roles and what each role may reach: [Roles](Usage-Access-Control#roles).

## Users

### Create a user

Administrators only.

1. Open `Users` in the sidebar and choose `New user`.
2. Enter `Username`.
3. Enter `Initial password`. The help text under the field states the rule in force.
4. Choose a `Role`: `administrator` or `standard`. The default is `standard`.
5. Choose `Create user`.
6. The page of the new user opens. The account is enabled and has no access to any environment until a grant is made ([Access](Usage-Environments#access)). The new user changes the password at `/account`.

If the form answers `422`, the message sits beside the field ([User fields](#user-fields)).

### User fields

| Field | Rule | Refusal |
|---|---|---|
| `Username` | 3 to 64 characters from letters, digits, `.`, `_`, `@` and `-`, starting with a letter or digit. Unique, ignoring ASCII case | `Use 3 to 64 letters, digits, dots, hyphens, underscores or at signs, starting with a letter or digit.` `That username is taken.` |
| `Initial password`, `New password` | At least `DOCKMIN_PASSWORD_MIN_LENGTH` characters (default 12, range 8 to 128), at most 1,024. Not equal to the username, ignoring case. With `DOCKMIN_PASSWORD_REQUIRED_CLASSES` above 0, at least that many of lower case, upper case, digits and symbols | `Use at least N characters.` `Use at most 1024 characters.` `The password must not be the same as the username.` `Mix at least N of: lower case, upper case, digits, symbols.` Several refusals are joined in one message |
| `Role` | `administrator` or `standard` | `Choose administrator or standard.` |
| `Identity` | Shown while access platform sign-in is on or the user has an identity: 1 to 320 characters without control characters. Unique ignoring ASCII case ([Linking accounts](#linking-accounts)) | `Use 1 to 320 characters without control codes.` `That identity is linked to another account.` |

Passwords are stored as Argon2id hashes. The cost settings are in [Configuration](Reference-Configuration#accounts-and-sessions).

### User page

The page opens from `Users`. It shows `Status` (`Enabled` or `Disabled`, with `You` on the viewer's own account), `Role`, `Created`, `Last sign-in` (`Never` until a sign-in) and `Active sessions`. Sections in order:

| Section | Content |
|---|---|
| `Access` | `Disable user` while enabled; `Enable user` while disabled |
| `Sign-in lock` | Present only while the account is locked: `Failed sign-in attempts have locked this account.` with the wait and `Lift the lock` ([Sign-in lock](#sign-in-lock)) |
| `Profile` | `Username` and `Role`, saved with `Save changes` |
| `Access platform identity` | Present while access platform sign-in is on or the user has an identity ([Linking accounts](#linking-accounts)) |
| `Reset password` | `New password`, saved with `Reset password` |
| `Sessions` | `End all sessions` |
| `API tokens` | The user's tokens ([Token list of another user](#token-list-of-another-user)) |
| `Delete` | `Delete user` |

<!-- screenshot: Usage-Teams-and-Users-1: the user page of a standard user as an administrator, showing the facts, the Access, Profile, Reset password and Sessions sections and the API tokens table; an account with one active token -->

### Change a username or role

1. Open the user's page and edit `Username` or `Role` under `Profile`.
2. Choose `Save changes`. The notice reads `User updated.`

- A role change applies to the next request of every session and API token of the user.
- A change of role ends the reveal window of every session of the user ([Sessions and tokens](#sessions-and-tokens)).
- Demoting an administrator is refused when no other enabled administrator remains: `This is the last administrator who can sign in, so it cannot be demoted, disabled or deleted.` (`409`).
- Recorded: `user_updated`, detail `old_role` and `new_role` when the role changed.

### Disable and enable

| Action | Confirmation | Effect |
|---|---|---|
| `Disable user` | `Disable this user?`: `This ends every session and blocks sign-in. The account and its history stay.` Names the user. On the viewer's own account the section adds `This is your own account, so you will be signed out.` | Ends every session. Sign-in is refused. History stays |
| `Enable user` | None | The account can sign in again |

- Disabling the last enabled administrator is refused (`409`) with the message above.
- A disabled account cannot be named in an environment grant or added to a team.

### Deleting a user

| Item | Value |
|---|---|
| Who | Administrators, on the user's page |
| Allowed | An account that has never acted. An account with recorded activity can only be disabled: `This user has recorded activity and cannot be deleted. Disable the account instead.` (`409`) |
| Last administrator | Refused (`409`): `The last administrator cannot be deleted.` An enabled administrator is also refused when no other enabled administrator remains |
| Confirmation | A dialog `Delete this user?` that names the user: `This permanently removes the account.` |
| Binding | The form carries the username that was shown. If the username changed meanwhile, nothing is deleted: `The account changed since you reviewed it; nothing was deleted.` (`409`) |
| Result | The user list opens |
| Recorded | `user_deleted` |

### Reset a password

1. Open the user's page, enter `New password` under `Reset password`, and choose `Reset password`.
2. The notice reads `Password reset. Their sessions were ended and their API tokens and webhook URLs revoked.`

A refused password answers `422` beside the field. The password is checked against the rules in [User fields](#user-fields). The effect on sessions and credentials: [Sessions and tokens](#sessions-and-tokens).

## Your account

`/account` holds two forms for the signed-in user. The page header links `API tokens`.

| Form | Fields | Result |
|---|---|---|
| `Profile` | `Username` | `Save profile`: `Profile updated.` Recorded: `profile_updated`. Same rules as [User fields](#user-fields) |
| `Password` | `Current password`, `New password`, `Confirm new password` | `Change password`: `Password changed. Your other sessions have been signed out and your API tokens and webhook URLs revoked.` |

| Refusal | Field | Message |
|---|---|---|
| Wrong current password | `current_password` | `The current password is incorrect.` Recorded with reason `bad_current_password` |
| Account locked | `current_password` | `Too many failed attempts. Try again later.` The password is not changed. Recorded with reason `locked_out` |
| Confirmation differs | `new_password_confirm` | `The passwords do not match.` |
| New password breaks a rule | `new_password` | The messages of [User fields](#user-fields) |

## Sessions and tokens

| Action | Pages | Sessions | API tokens | Webhook URLs | Recorded |
|---|---|---|---|---|---|
| User changes own password | `/account` | Every other session of the user ends; the current one stays | Every token of the user is revoked | Every active webhook URL of the user is revoked | `password_change`, detail `tokens_revoked`, `webhook_tokens_revoked` |
| Administrator resets a password | `/users/{id}` | Every session of the user ends | Every token of the user is revoked | Every active webhook URL of the user is revoked | `password_reset`, detail `tokens_revoked`, `webhook_tokens_revoked` |
| Administrator ends a user's sessions | `/users/{id}`, confirmation `End all sessions?` | Every session of the user ends | Every token of the user is revoked | Every active webhook URL of the user is revoked | `sessions_revoked`, detail `tokens_revoked`, `webhook_tokens_revoked` |
| Administrator disables an account | `/users/{id}` | Every session of the user ends | Kept, and refused while the account is disabled | Kept, and answered `404` while the account is disabled | `user_disabled` |
| Administrator enables an account | `/users/{id}` | None | Tokens not revoked or expired work again | Active URLs work again | `user_enabled` |
| Administrator links, changes or clears an access platform identity | `/users/{id}`, section `Access platform identity` | Access platform sessions of the user end at their next request, when the token no longer maps to the user | None | None | `user_updated`, detail `external_identity_set`, `previous_external_identity`, `external_identity_cleared` |
| User revokes one of their tokens | `/account/tokens`, confirmation `Revoke this token?` | None | That token | None | `token_revoked` |
| Administrator revokes one token of a user | `/users/{id}`, confirmation `Revoke this token?` | None | That token | None | `token_revoked` |

- `tokens_revoked` is the number of API tokens the action revoked. `webhook_tokens_revoked` is the number of webhook URLs it revoked: the active URLs of the webhooks the user owns, which then show `Revoked by account recovery`. Only the owner can issue a new URL, by rotating it ([Account recovery](Operations-Automation#account-recovery)). The revocation and the password or session change happen in one transaction.
- `End all sessions` answers `Ended N session(s) and revoked the user's API tokens and webhook URLs.`
- A revoked token fails authentication from its next request.
- A change of a user's password, a reset of it, a change of the user's role, and disabling or enabling the account each end the reveal window of every session of the user, which stays signed in ([Reveal a secret variable](Usage-Stacks#reveal-a-secret-variable), [Revealing stack variables](Reference-Security#revealing-stack-variables)). A password sign-in that stores a rehashed password does the same for the user's other sessions.
- Each change on these pages answers `303` and shows its notice on the page it lands on ([Flash messages](Contributing-Frontend-Conventions#flash-messages)). A refused change renders its form again at `422` or `409`.
- A token's `Revoke` button opens a confirmation that names the token's label and prefix. It is a link to the dialog, so it works without JavaScript. Nothing is revoked until `Revoke token` is confirmed: `A revoked token stops working on its next request. It cannot be restored; create another to replace it.`
- Session lifetime and idle timeout: `DOCKMIN_SESSION_LIFETIME` and `DOCKMIN_SESSION_IDLE_TIMEOUT` ([Configuration](Reference-Configuration#accounts-and-sessions)).

## API tokens

Every signed-in user creates tokens at `/account/tokens`, for themselves. An administrator cannot create a token for another user.

### Create a token

1. Open `Account` in the user menu and choose `API tokens`.
2. Under `Create a token`, enter `Label` and, optionally, `Expires after (days)`.
3. Choose `Create token`.
4. The page shows `Copy your new token now` with the value. Copy it before leaving the page: the value is shown once and cannot be recovered.
5. Send it as `Authorization: Bearer <token>` ([Authentication](Reference-JSON-API#authentication)).

| Item | Value |
|---|---|
| Label | 1 to 64 printable characters. Unique among the user's tokens: `You already have a token with that label.` Refusal for the form: `Use 1 to 64 printable characters.` |
| Expiry | A whole number of days from 1 to 3650, or blank for none. A token with none does not expire. Refusal: `Use a whole number of days from 1 to 3650.` |
| Value | `dkm_`, 8 letters or digits, `_`, then 43 URL-safe characters. Shown once, on the page that creates it. Only a SHA-256 hash is stored |
| Acts as | The user, with the user's role and access. Read on the `/api` routes only: a token opens no page and no WebSocket |
| Checked | At every request, from the database: a revocation, an expiry, a disabled account and a changed role take effect on the next request |
| Last used | Updated at most once a minute |
| Status | `Active`, `Expired` or `Revoked` |
| Columns | `Label`, `Token` (the prefix), `Created`, `Last used`, `Expires`, `Status`, `Actions` |
| Recorded | `token_created`, target the user, detail `label`, `prefix` |

The page `API tokens` states: `A token stops working when you or an administrator changes your password or ends your sessions.`

### Token list of another user

| Item | Value |
|---|---|
| Who | Administrators, on the user's page `/users/{id}`, section `API tokens` |
| Shows | Label, token prefix, created, last used, expires, status. Never the value |
| Revoke | `POST /users/{id}/tokens/{token_id}/revoke`. A token that is not that user's answers `404`. Revoking a token that is already revoked records nothing. Notice `Token revoked.` |
| Recorded | `token_revoked`, target the token's owner; detail `label`, `prefix` |

## Sign-in lock

| Item | Value |
|---|---|
| Failures that lock a username | `DOCKMIN_LOGIN_MAX_FAILURES`, default 5 |
| Window and lock time | `DOCKMIN_LOGIN_LOCKOUT_WINDOW`, default 15 minutes |
| Lock by address | Refused only where client addresses are per-client: `DOCKMIN_TRUSTED_PROXIES` is set, or the request arrived on dockmin's own HTTPS listener. Evaluated for each request ([Behind a reverse proxy](Getting-Started-Installation#behind-a-reverse-proxy)) |
| Counted | A wrong password at sign-in, a wrong current password on the change-password form, and a wrong password on the reveal form of a secret stack variable |
| Not counted | A token from an access platform that is refused or names no account |
| Sign-in while locked | `429` with the header `Retry-After` and the message `Too many failed attempts. Try again later.` |
| Change-password form, while locked | Refused on the field `current_password`: `Too many failed attempts. Try again later.` The password is not changed. Recorded with reason `locked_out` |
| Change-password form, wrong password | `The current password is incorrect.` Recorded with reason `bad_current_password` |
| Reveal form, while locked or with a wrong password | Refused on the field `password` with the same two texts. Recorded as `reveal_window_opened` with reason `locked_out` or `bad_current_password` ([Reveal and dismissal rows](Administration-Activity#reveal-and-dismissal-rows)) |
| Lifting a lock | An administrator, on the user's page (`lockout_cleared`), or the command in [Unlock an account](#unlock-an-account) |

A successful sign-in clears the username's failures and takes back the address's count of that attempt.

### Sign-in refusals

The sign-in page answers `Invalid username or password.` (`401`) whatever the cause. The failed `login` row records the cause in `reason`:

| `reason` | Cause |
|---|---|
| `unknown_user` | No account has that username |
| `bad_password` | The password does not match |
| `account_disabled` | The account is disabled. Judged after the password verifies |
| `password_sign_in_disabled` | A standard user, while `DOCKMIN_JWT_PASSWORD_SIGN_IN` is `administrators` ([Password sign-in](#password-sign-in)) |
| `locked_out` | The username or, where enforced, the address is locked |
| `server_busy` | Password hashing was at capacity. The attempt is not counted |
| `commit_failed` | The session could not be written |

### Lift a lock from the user page

1. Open the locked user's page. The section `Sign-in lock` reads `Failed sign-in attempts have locked this account.` with the time left.
2. Choose `Lift the lock`. The notice reads `Sign-in lock lifted.`, or `This account was not locked.` when the lock had ended.

The button lifts the lock on the username. A lock held on a client address is lifted by the command below.

### Unlock an account

Use when no administrator can sign in. Run it inside the container.

1. For a container named `dockmin`, started with `docker run`:

```bash
docker exec dockmin python -m app clear-lockout <username-or-address>
```

With Compose:

```bash
docker compose exec dockmin python -m app clear-lockout <username-or-address>
```

2. Read the output. Success: `lifted N lock(s)`. When nothing was held on that subject: `no lock was held on that subject`. When the data directory holds no database: `no database at <path>` and exit status 2.
3. A lock is also held on the client address when dockmin serves HTTPS itself or trusts a proxy. Clearing the username alone leaves a sign-in refused with `429`. Run the command once for the username and once for the address. The `login_refused` log line carries the `address`.

The command lifts any lock held on that username or that address and records `lockout_cleared` in the activity log: actor `anonymous`, detail `source` `operator_command` and `subject`.

## Teams

| Item | Value |
|---|---|
| Name | 1 to 64 printable characters, unique ignoring ASCII case: `Use 1 to 64 printable characters.` `That team name is taken.` |
| Create | Administrators: `New team` on `/teams`, field `Team name`, `Create team`. `Team created.` The team page opens |
| Rename | Administrators: `Rename`, `Save name`. `Team renamed.` Recorded: `team_renamed`, detail `old_name`, `new_name` |
| Delete | Administrators. The dialog `Delete this team?` names the team and states that its memberships and the access grants made to it are removed. The users keep their accounts |
| Delete binding | The form carries the team name that was shown. If the team was renamed meanwhile, nothing is deleted: `The team changed since you reviewed it; nothing was deleted.` (`409`) |
| Recorded | `team_deleted`, detail `name` |
| List | Columns `Team` and `Created`. An administrator sees every team; anyone else sees the teams they belong to. Empty for a standard user: `You are not a member of any team yet. Ask an administrator to add you to one.` |
| Team page | `Created` and the roster `Members of <team>` with `Member`, `Role` (`Leader` or `Member`), `Joined` and, for those who manage members, `Actions` |

<!-- screenshot: Usage-Teams-and-Users-2: the team page as an administrator, showing the roster with a leader and a member, the Add a member form with "Appoint as team leader", and the Rename and Delete sections; a team with two members -->

### Members and leaders

| Action | Who | Detail |
|---|---|---|
| Add a member | An administrator, or a leader of this team | `Add a member`: `Username` (an enabled account) and, for administrators only, `Appoint as team leader`. `Added <username> to the team.` Recorded: `team_member_added`, detail `user_id`, `leader` |
| Remove a member | An administrator, or a leader for a member who is not a leader | `Remove <username>` opens the dialog `Remove this member?`: `This removes the member from the team and the access they have through it.` and the username. Nothing changes until `Remove member` is confirmed. `Member removed.` Recorded: `team_member_removed`, detail `user_id` |
| Appoint or dismiss a leader | Administrators | `Make leader` or `Remove as leader` on the member's row. `Leader appointed.` or `Leader dismissed.` Recorded: `team_leader_changed`, detail `user_id`, `leader` |

| Refusal | Message | Status |
|---|---|---|
| Name is not an enabled account | `No enabled user has that name.` | `422` |
| Already a member | `That user is already a member.` | `422` |
| Removing or changing someone who is not a member | `That user is not a member of this team.` | `422` |
| A leader appoints a leader, or removes a leader | Refused: only an administrator can appoint or remove a team leader | `403` |
| A member who is not a leader changes membership | Refused: only a team leader can change membership | `403` |

- A grant to a team reaches every member. Leaving a team removes the access held through it.
- The team page tells a member who is not a leader `Only a team leader or an administrator can change who belongs to this team.` and a leader `You lead this team, so you can add and remove members. Only an administrator can appoint or remove a leader.`
- A role change takes effect on the next request. A team leader's rights apply to the team they lead only.

## Access platform sign-in

dockmin signs a user in from a signed JWT that an access platform adds to each request. The feature is off until `DOCKMIN_JWT_HEADER` is set.

| Item | Value |
|---|---|
| Token | A compact JWS in one request header, named by `DOCKMIN_JWT_HEADER` |
| Verified | Against a key supplied in the configuration, before any claim is read ([Verification](#verification)) |
| Identity | One claim of the token, mapped to a local account that an administrator linked to it ([Linking accounts](#linking-accounts)) |
| Result | A dockmin session of that account, checked against the token on every request ([Sessions](#sessions)) |
| Roles, teams, access | The local account's. Nothing is taken from a claim, groups included |
| Surfaces | Pages and WebSocket handshakes. The `/api` routes never read the header ([API routes](#api-routes)) |
| Not supported | OIDC, SAML, LDAP, redirects to the platform, client secrets, discovery documents, userinfo calls |
| Off | With `DOCKMIN_JWT_HEADER` unset, no request reads the header and nothing is fetched |

### Set up access platform sign-in

Prerequisites: the platform adds a signed token to each request it forwards, dockmin is reachable only through the platform ([Deployment](#deployment)), and the browser reaches dockmin over HTTPS.

1. Collect the values from the platform: the header name, the issuer (`iss`), the audience (`aud`), the address of its key document or its public key, and the claim that holds the user's identity.
2. Set the variables in the environment of the container:

```bash
DOCKMIN_JWT_HEADER=<header-name>
DOCKMIN_JWT_ISSUER=<issuer>
DOCKMIN_JWT_AUDIENCE=<audience>
DOCKMIN_JWT_JWKS_URL=https://<platform-host>/<key-document-path>
DOCKMIN_JWT_IDENTITY_CLAIM=<claim-path>
```

   Use `DOCKMIN_JWT_KEY_FILE=<path-in-container>` in place of `DOCKMIN_JWT_JWKS_URL` for a key file. Variables reach the container as in [Setting a variable compose.yaml does not pass](Reference-Configuration#setting-a-variable-composeyaml-does-not-pass).
3. Recreate the container.
4. Check the start. Success: the container stays up, and the public settings list the method:

```bash
curl -k https://<dockmin-address>:8443/api/settings/public
```

   The answer holds `"authentication_methods":["internal","jwt"]`. Failure: the container exits with status `2` and the `startup_check_failed` line names the check `jwt_settings` and the setting ([Troubleshooting](Troubleshooting#jwt_settings)).
5. Link each account: open `Users`, open the user, enter the platform's value for the user in `Identity` under `Access platform identity`, and choose `Save identity`. The notice reads `Access platform identity updated.` ([Linking accounts](#linking-accounts)). With `DOCKMIN_JWT_CREATE_USERS` `true`, accounts are created on first sign-in instead ([Just-in-time accounts](#just-in-time-accounts)).
6. Open dockmin through the platform. Success: the session opens as the linked account without a password, and the activity log holds a `login` row with `method` `jwt`. When nobody is signed in, read the `jwt_refused` lines ([Log lines](#log-lines)) and [Troubleshooting](Troubleshooting#jwt_settings).

### Settings

Types, ranges, defaults and blank-value handling of every `DOCKMIN_JWT_` variable: [Access platform](Reference-Configuration#access-platform).

- With `DOCKMIN_JWT_HEADER` set, `DOCKMIN_JWT_ISSUER`, `DOCKMIN_JWT_AUDIENCE` and exactly one of `DOCKMIN_JWT_JWKS_URL` and `DOCKMIN_JWT_KEY_FILE` are required.
- `DOCKMIN_JWT_ISSUER`, `DOCKMIN_JWT_AUDIENCE`, `DOCKMIN_JWT_JWKS_URL`, `DOCKMIN_JWT_KEY_FILE` or `DOCKMIN_JWT_CREATE_USERS` set without `DOCKMIN_JWT_HEADER` is refused.
- A configuration that fails these rules, or a key file that is unreadable, empty or holds no usable key, stops the start with exit status `2`. The `startup_check_failed` line names the check `jwt_settings` and the setting ([Troubleshooting](Troubleshooting#jwt_settings)).
- A dedicated header is preferable to `Authorization`. API tokens travel in `Authorization: Bearer` ([Authentication](Reference-JSON-API#authentication)), so a gateway that overwrites `Authorization` on every request breaks API tokens sent through it. The `Bearer ` prefix is removed from the value whatever the header.
- `DOCKMIN_JWT_USERNAME_CLAIM` defaults to the identity claim. `DOCKMIN_JWT_ALGORITHMS` defaults to `ES256`. `DOCKMIN_JWT_MAX_AGE` defaults to 86,400 seconds.

### Verification

A token is checked in this order. The first failing step refuses it.

| Step | Rule | Reason |
|---|---|---|
| 1 | The header value is at most 8,192 characters | `too_long` |
| 2 | An optional `Bearer ` prefix, in any case, is removed. What remains is exactly three segments of letters, digits, `_` and `-`, separated by `.` | `malformed` |
| 3 | The first segment is at most 1,024 characters and decodes to a JSON object with a string `alg`. `kid`, when present, is a string | `malformed` |
| 4 | `alg` is in `DOCKMIN_JWT_ALGORITHMS`. `none` and every `HS*` are refused whatever the setting | `alg_not_allowed` |
| 5 | A key is chosen by `kid` ([Key selection](#key-selection)) | `unknown_kid`, `kid_required`, `keys_unavailable` |
| 6 | The key's type fits `alg`: P-256 for `ES256`, P-384 for `ES384`, P-521 for `ES512`, RSA for `RS*` and `PS*`, Ed25519 or Ed448 for `EdDSA`. A key that declares an `alg` verifies only that algorithm | `key_type_mismatch` |
| 7 | The signature verifies | `bad_signature` |
| 8 | `iss` equals `DOCKMIN_JWT_ISSUER`. `aud` is or contains `DOCKMIN_JWT_AUDIENCE`. `exp`, `iat`, `iss` and `aud` are present. `exp` is not past, and `nbf`, when present, is not ahead | `bad_issuer`, `bad_audience`, `missing_claim`, `expired`, `not_yet_valid` |
| 9 | `iat` is a number, is not ahead of now, and is not older than `DOCKMIN_JWT_MAX_AGE` | `iat_future`, `too_old`, `malformed` |
| 10 | The identity claim is present and is a string of 1 to 320 characters with no control characters | `identity_missing`, `identity_invalid` |

- The clock skew allowed on `exp`, `nbf` and `iat` is 30 seconds. It is fixed.
- `typ` is not checked. All other claims are ignored.
- The username claim is optional. A missing or invalid one counts as absent and matters only for [just-in-time accounts](#just-in-time-accounts).
- An unexpected error inside the verifier refuses the token with the reason `verifier_error_` followed by the exception's class name. It is logged as `jwt_verifier_failed` (error level, field `reason`), at most one line a minute for each reason.
- A refused token is treated as if the header were absent. Nothing falls back to another method. The reason reaches only the `jwt_refused` log line ([Log lines](#log-lines)); no activity row is written, and no page names it.
- A verified token is remembered in memory by its SHA-256 digest until the earliest of its `exp`, five minutes, and `iat` plus `DOCKMIN_JWT_MAX_AGE`. At most 1,000 are remembered.

#### Key selection

| Keys held | Token | Result |
|---|---|---|
| One key | No `kid`, or any `kid` when the key has none | That key |
| One key with a `kid` | A different `kid` | `unknown_kid`, after one refresh when the keys come from a URL |
| Several keys | A `kid` that names one key | That key |
| Several keys | No `kid` | `kid_required` |
| Several keys | A `kid` that names none | `unknown_kid`, after one refresh when the keys come from a URL |
| None usable | Any | `keys_unavailable` |

### Key sources

Exactly one source is configured.

| Source | Content | Read |
|---|---|---|
| `DOCKMIN_JWT_KEY_FILE` | A PEM public key (one key, no `kid`), or a JWKS document | At start. A change needs a restart |
| `DOCKMIN_JWT_JWKS_URL` | A JWKS document | On first need, then as described under [JWKS URL](#jwks-url). Nothing is fetched at start |

Both sources apply the same filter to a JWKS document.

| Entry | Kept |
|---|---|
| `use` | Absent or `sig` |
| `alg` | Absent, or in `DOCKMIN_JWT_ALGORITHMS` |
| `kid` | Absent, or a string of at most 256 characters |
| Key | A public EC, RSA, Ed25519 or Ed448 key. Private and symmetric keys are dropped |
| Count | The first 100 entries are read; at most 20 keys are kept |

#### JWKS URL

| Rule | Value |
|---|---|
| Scheme | `https` only |
| Addresses | The outbound [address policy](Reference-Security#address-policy) applies. An internal address is refused unless `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` permits it |
| Redirects | Not followed. Any `3xx` answer fails the fetch |
| Encoding | `Accept-Encoding: identity` is sent. An answer with any other `Content-Encoding` fails the fetch |
| Status | Only `200` |
| Time | 5 seconds for the whole fetch |
| Size | 64 KiB. A larger `Content-Length`, or a larger body, fails the fetch |
| Certificate | Verified against the operating system's root certificates. `SSL_CERT_FILE` and `SSL_CERT_DIR` are ignored |
| Proxy variables | Not read |
| Document | Must hold at least one key that passes the filter above |

| Timing | Behaviour |
|---|---|
| First fetch | At the first verification after start |
| Refresh | At the next verification once the keys are an hour old |
| Unknown `kid` | A fetch, then the lookup is repeated |
| Rate | At most one fetch attempt every 60 seconds, for refresh and unknown `kid` together. Concurrent verifications share one fetch. An unknown `kid` within 60 seconds of the last attempt is refused without a fetch |
| Failed fetch | The last good keys are kept for at most 24 hours from the fetch that returned them. After that every token is refused with `keys_unavailable` until a fetch succeeds |
| Never fetched | Every token is refused with `keys_unavailable`. The next attempt is at least 60 seconds after the last |

A failed fetch logs `jwks_fetch_failed` with a `reason`: `not_https`, `redirect`, `status`, `content_encoding`, `too_large`, `timeout`, `address_refused`, `tls`, `network`, `invalid_document` or `no_usable_keys`. The URL is not logged.

### Linking accounts

An account signs in through the platform only when an administrator linked it to an identity. A username is never matched against the identity.

| Item | Value |
|---|---|
| Where | The user's page, `/users/{id}`, section `Access platform identity`. Shown while the feature is on, and for any user that has an identity |
| Field | `Identity`: the value of the identity claim in the platform's token, such as an email address |
| Rule | 1 to 320 characters, no control characters. Unique across accounts, ignoring ASCII case |
| Comparison | The token's identity claim is compared with the linked identity ignoring ASCII case. The claim must be one the platform treats case-insensitively: an email address, or a username from a provider that does |
| Blank | Unlinks the account |
| Request | `POST /users/{id}/identity`, administrators only. Notice `Access platform identity updated.` |
| Same value | Writes no row |
| Taken | `422`: `That identity is linked to another account.` |
| Recorded | `user_updated`. Detail `external_identity_set` holds the new value, with `previous_external_identity` when one is replaced. `external_identity_cleared` holds the value removed. The value is not a secret and is recorded |
| Effect on sessions | A platform session of the user ends at its next request when the token no longer maps to the user ([Binding](#binding)) |
| User page | An account with an identity shows `Signs in through the access platform as <identity>`. A user without one shows `Not linked.` |
| Administrators | A linked administrator signs in through the platform like any other account. The role is the account's, never a claim's |

A verified identity with no linked account is refused with the reason `jwt_unlinked` unless [just-in-time accounts](#just-in-time-accounts) are on. A linked account that is disabled is refused with `jwt_account_disabled`. The caller is not signed in in either case.

### Just-in-time accounts

`DOCKMIN_JWT_CREATE_USERS=true` creates an account for a verified identity that no account is linked to.

| Item | Value |
|---|---|
| Role | `standard`. Never an administrator |
| Username | The username claim, which defaults to the identity claim. It must match `[A-Za-z0-9][A-Za-z0-9._@-]{2,63}`. A failing value is refused with `jwt_username_invalid`. A name another account has, ignoring case, is refused with `jwt_username_taken` |
| Link | The identity is set on the new account |
| Password | An Argon2id hash of 32 random bytes that are discarded. No password signs in. The account cannot open a reveal window until an administrator sets a password ([Revealing stack variables](Reference-Security#revealing-stack-variables)) |
| Access | None until an administrator grants it ([Authorisation model](Usage-Access-Control#authorisation-model)) |
| Recorded | `user_created`, actor the new account. Detail `role` (`standard`), `method` (`jwt`), `address` |
| After | The sign-in proceeds as for a linked account |

### Sessions

#### Exchange

dockmin opens a session when all of these hold.

| Condition | Value |
|---|---|
| Route | A page route. Not `/api`, not the setup page, the health and readiness routes or the icon, and not a webhook call |
| Method | `GET`. `HEAD` is also a safe method, but no page route answers it. A request with any other method is never exchanged |
| Session | The request carries no valid session cookie |
| Marker | The cookie `dockmin_signed_out` is absent ([Sign-out](#sign-out)) |
| Token | The header carries a token that verifies |
| Account | The identity is linked to an enabled account, or [just-in-time accounts](#just-in-time-accounts) are on and create one |

| The exchange | Detail |
|---|---|
| Session | A new session with the sign-in method `jwt`. Lifetime and idle timeout are those of password sessions ([Configuration](Reference-Configuration#accounts-and-sessions)) |
| Cookies | The session cookie is set on the response to the request that exchanged. The CSRF token is replaced |
| Request | Proceeds as the account |
| Last sign-in | Updated, as for a password sign-in |
| Recorded | A successful `login` row with `method` `jwt`, and the `login_succeeded` line |
| Refused | Recorded once per token, as described under [Recorded events](#recorded-events). The caller stays signed out |

A request whose session cookie belongs to a password session ignores the header.

#### Binding

Every request on a `jwt` session, page routes and WebSocket handshakes alike, must carry a token that verifies to the session's account.

| Ending | Cause |
|---|---|
| `platform_assertion_ended` | The header is absent, or its token is refused |
| `platform_identity_changed` | The token verifies to an identity that is not linked to the session's account: another account's, an unlinked identity, or a link that was changed or cleared |

| When a session ends | Detail |
|---|---|
| Session | Deleted |
| Recorded | A `logout` row, actor the account, with `reason`, `method` and `address`; the `logout_succeeded` line with the same fields |
| The request | Handled as if no session existed |
| Cookie | The session cookie is cleared in the response, unless the same response opens a new session |
| New session | A `GET` that carries a verified token for another linked, enabled account opens a session for that account in the same request |
| After | A `POST` with no session answers as for any signed-out caller |

A WebSocket handshake is bound the same way. It never opens a session from the header alone.

Revocation at the platform takes effect at the next request: the token stops arriving or stops verifying, and the session ends.

### Sign-out

`POST /logout` ends the session as for any user. For a `jwt` session it also sets a marker cookie.

| Item | Value |
|---|---|
| Cookie | `dockmin_signed_out` |
| Attributes | No expiry: it ends with the browser session. Other attributes: [Cookies](Reference-Security#cookies) |
| While present | No request is exchanged, so the browser stays signed out. A password session sets no marker |
| Login page | `Continue as <username>` is offered when the current token verifies to a linked, enabled account |
| `Continue as` | `POST /login/continue`, CSRF-protected. It verifies the current token, ends the caller's current session if there is one (reason `session_replaced`), opens the session, clears the marker and sets the cookies. While the instance is offline for a restore or re-key it is admitted, and no account is created |
| No account | `401`, the login page with `The access platform did not identify an account that can sign in here.` |

### Password sign-in

| `DOCKMIN_JWT_PASSWORD_SIGN_IN` | Behaviour |
|---|---|
| `all` | Every account may sign in with a password |
| `administrators` | A standard user's password sign-in is refused while the feature is on. Administrators sign in with a password as before |

- The refusal is recorded as a failed `login` with the reason `password_sign_in_disabled`, and logged as `login_failed`. It is judged after the password verifies, so a wrong password still reads `bad_password`.
- With `administrators`, a standard user's existing password session ends at its next request. The `logout` row carries the reason `password_sign_in_disabled` and `method` `password`.
- Administrators can always sign in with a password. It is the way in when the platform or its keys are unavailable.
- The first administrator is created on the setup page as without the feature ([Start dockmin](Getting-Started-Quick-Start#start-dockmin)).
- A password sign-in that fails still counts toward the [sign-in lock](#sign-in-lock). A refused token does not.

### API routes

| Item | Behaviour |
|---|---|
| Header | Never read on `/api` routes |
| JWT in `Authorization` on `/api` | An invalid API token: `401` |
| API tokens | Work as without the feature |
| `GET /api/settings/public` | `authentication_methods` is `["internal", "jwt"]` while the feature is on |

### Recorded events

| Event | Action | Result | Actor | Detail |
|---|---|---|---|---|
| Sign-in by exchange | `login` | success | The account | `address`, `method` (`jwt`) |
| Refused identity | `login` | failure | Anonymous | `address`, `method`, `identity`, `reason` |
| Session ended by the binding, by `Continue as` or by the password restriction | `logout` | success | The account | `reason` (`platform_assertion_ended`, `platform_identity_changed`, `session_replaced` or `password_sign_in_disabled`), `method`, `address` |
| Account created | `user_created` | success | The new account | `role`, `method`, `address` |
| Identity linked, changed or cleared | `user_updated` | success | The administrator | `external_identity_set`, `previous_external_identity`, `external_identity_cleared` |

| `login` failure reason | Cause |
|---|---|
| `jwt_unlinked` | The identity is linked to no account and just-in-time accounts are off, or the instance is offline for a restore or re-key and no account is created |
| `jwt_account_disabled` | The linked account is disabled |
| `jwt_username_invalid` | A just-in-time account's name fails the username form, or the username claim is absent |
| `jwt_username_taken` | A just-in-time account's name belongs to another account |
| `commit_failed` | The session could not be written |
| `password_sign_in_disabled` | Password sign-in of a standard user while `DOCKMIN_JWT_PASSWORD_SIGN_IN` is `administrators` |

- Every `login` row, success or failure, carries `method`: `password` or `jwt`.
- A refused identity is recorded once per distinct token, not once per request. The process remembers the last 1,000 tokens; a restart records a token's refusal once more. A refusal names the account as target when one is known.
- A token that does not verify writes no activity row.

### Log lines

| Line | Level | Fields |
|---|---|---|
| `jwt_refused` | warning | `reason`: a refusal reason from [Verification](#verification). At most one line a minute for each reason |
| `jwt_verifier_failed` | error | `reason`: `verifier_error_` and the exception class. At most one line a minute for each reason |
| `jwks_fetch_failed` | warning | `reason` ([JWKS URL](#jwks-url)) |
| `login_succeeded`, `login_failed`, `logout_succeeded` | info, warning, info | As in [Sign-in lines](Reference-Deployment#sign-in-lines), with `method`. A binding ending adds `reason` |

A token value is never logged ([Access platform tokens](Reference-Security#access-platform-tokens)).

### Deployment

| Requirement | Detail |
|---|---|
| Reachability | dockmin is reachable only through the platform. Block every other path to the listeners: bind or publish only on the platform's network, or firewall other sources |
| Transport | The platform reaches dockmin over HTTPS, or over a network no other host shares. The header is a bearer credential |
| Header | The platform sets the header on every request it forwards, and the header is named in `DOCKMIN_JWT_HEADER` |
| Cookies | The browser reaches dockmin over HTTPS. `DOCKMIN_COOKIE_SECURE` stays at its default ([Cookies](Reference-Security#cookies)) |
| Proxy | The platform is a reverse proxy. Client addresses and the scheme follow [Behind a reverse proxy](Getting-Started-Installation#behind-a-reverse-proxy) |
| Clock | The host clock is within the 30 seconds of skew that `exp`, `nbf` and `iat` allow |
| Header from elsewhere | A header that no key signed is refused. A header a client forges costs nothing but a `jwt_refused` line |

#### Token replay

| Item | Detail |
|---|---|
| Replay | A token a third party captured works against dockmin when presented directly to it, until `exp` or `DOCKMIN_JWT_MAX_AGE` after `iat`, whichever comes first |
| Cause | dockmin cannot tell which peer sent the header. Uvicorn replaces the peer address with the forwarded one for trusted proxies |
| Limits | Reachability only through the platform. A shorter `DOCKMIN_JWT_MAX_AGE`. HTTPS between the platform and dockmin |
| Ending a stolen session | Disable the account, or clear its identity: the session ends at its next request |

### Twingate

The values below are those of Twingate's published guides for web applications.

| Item | Value |
|---|---|
| Component | The Twingate Gateway adds the token to each request. The Controller signs it |
| Header | Configurable: a header name and a value template. Use `X-Twingate-Jwt` with the value `{{jwt}}` |
| JOSE header | `alg` `ES256`, `typ` `GAT`, `kid` |
| Claims used | `iss`, `aud` (the Twingate network name), `exp`, `iat`. The identity is under `user`: `user.email`, `user.username` or `user.id` |
| Claims not sent | `sub`. `nbf` is not documented |
| Keys | `https://<tenant>.twingate.com/api/v1/jwk/ec` |
| Not used | The plain headers `X-Twingate-User` and `X-Twingate-Groups`: they are not signed |

```bash
DOCKMIN_JWT_HEADER=X-Twingate-Jwt
DOCKMIN_JWT_ISSUER=twingate
DOCKMIN_JWT_AUDIENCE=<network-name>
DOCKMIN_JWT_JWKS_URL=https://<tenant>.twingate.com/api/v1/jwk/ec
DOCKMIN_JWT_IDENTITY_CLAIM=user.email
```

- `DOCKMIN_JWT_ALGORITHMS` stays at `ES256`.
- `DOCKMIN_JWT_IDENTITY_CLAIM=user.username` links accounts by Twingate username instead of email.
- `twingate` as the issuer and the network name as the audience are the values in Twingate's guides. Confirm both against a token the Gateway sends. A mismatch shows as `bad_issuer` or `bad_audience` in `jwt_refused`.
- The linked `Identity` of an account is the claim's value, such as `ana@example.com`.
- The JWKS host is public. `DOCKMIN_PERMITTED_INTERNAL_NETWORKS` is needed only when the name resolves to an internal address.
- Twingate documents no key rotation schedule. A new `kid` is picked up by the first request that names it ([JWKS URL](#jwks-url)).

## Recorded actions

| Action | Written by | Target | Detail keys |
|---|---|---|---|
| `user_created` | Create a user | user | `role`, `address` |
| `user_updated` | Username or role change; identity link | user | `old_role`, `new_role`, `address`; identity keys as in [Linking accounts](#linking-accounts) |
| `user_enabled`, `user_disabled` | Enable, disable | user | `address` |
| `user_deleted` | Delete a user | user | `address` |
| `password_reset` | Administrator reset | user | `tokens_revoked`, `webhook_tokens_revoked`, `address` |
| `sessions_revoked` | `End all sessions` | user | `tokens_revoked`, `webhook_tokens_revoked`, `address` |
| `password_change` | Own password change | the actor | `tokens_revoked`, `webhook_tokens_revoked`, `address`. Failures carry the reason `bad_current_password` or `locked_out` |
| `profile_updated` | Own username change | the actor | `address` |
| `token_created` | Create a token | the owner | `label`, `prefix`, `address` |
| `token_revoked` | Revoke a token | the owner | `label`, `prefix`, `address` |
| `lockout_cleared` | `Lift the lock`; `clear-lockout` | user | `address`; the command records `source` and `subject` |
| `login`, `logout` | Sign-in, sign-out | user | `address`, `method`; failures `reason`, `username` or `identity` |
| `team_created` | Create a team | team | `name` |
| `team_renamed` | Rename | team | `old_name`, `new_name` |
| `team_deleted` | Delete | team | `name` |
| `team_member_added` | Add a member | team | `user_id`, `leader` |
| `team_member_removed` | Remove a member | team | `user_id` |
| `team_leader_changed` | Appoint or dismiss a leader | team | `user_id`, `leader` |

Categories, actors and filters: [Activity](Administration-Activity#categories).

## Related settings

| Setting | Effect here |
|---|---|
| `DOCKMIN_PASSWORD_MIN_LENGTH`, `DOCKMIN_PASSWORD_REQUIRED_CLASSES` | Password rules |
| `DOCKMIN_LOGIN_MAX_FAILURES`, `DOCKMIN_LOGIN_LOCKOUT_WINDOW` | [Sign-in lock](#sign-in-lock) |
| `DOCKMIN_SESSION_LIFETIME`, `DOCKMIN_SESSION_IDLE_TIMEOUT` | Session length |
| `DOCKMIN_TRUSTED_PROXIES` | Whether the address lock refuses a sign-in |
| `DOCKMIN_JWT_*` | [Access platform sign-in](#access-platform-sign-in) |

All settings: [Configuration](Reference-Configuration).
