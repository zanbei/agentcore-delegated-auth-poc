# Results

Environment: us-west-2, Keycloak 26.8.2 standing in for the enterprise IdP, a Lambda standing in for the ITSM API. Everything here was run once; none of it is a benchmark.

## PoC 1: OBO through AgentCore Gateway (user present)

Flow: user token (`aud=agent-app`) -> gateway (inbound JWT check) -> AgentCore Identity exchanges at Keycloak (RFC 8693, `actorTokenContent=NONE`, scope `itsm`) -> downstream gets a token with `aud=itsm-api`.

| Case | Result |
|---|---|
| Manual exchange at Keycloak (no AgentCore), alice and bob | `aud` and `azp` change, `sub` and `preferred_username` unchanged |
| Raw user token sent straight to the downstream | 401, audience does not include `itsm-api` |
| Invalid token | 401 |
| alice via gateway | downstream sees `sub`=alice, `aud=[itsm-api,...]`, `azp=agent-app`, returns only her incident |
| bob via the same agent and tool | downstream sees bob, returns only his incident |
| Exchanged (`aud=itsm-api`) token sent to the gateway | 403: the gateway only accepts `aud=agent-app` |
| `act` claim in the exchanged token | absent |
| Downstream log in the test window | two accepted requests, alice and bob, matching the above |

The agent client only forwards the user token. It never holds the agent client secret or the downstream token.

### Why there is no `act`

Keycloak's standard token exchange narrows `aud` and `scope` and does not write the actor; its documentation classes this as impersonation in effect. Writing `act` needs the preview delegation feature (26.8), which also needs a service account, fine-grained admin permissions v2, a `delegation:client:<id>` scope on the user's login, per-session user consent, and an `actor_token` on the exchange. AgentCore sends one when `actorTokenContent` is `M2M`. Not tested here. Consequence: the downstream can use `azp` to tell the agent's client from the user's own client, but cannot see a multi-hop chain.

### User absent, naive options (no user token)

| Option | Gateway | What the downstream sees |
|---|---|---|
| E1 no token | 401 | nothing |
| E2 the agent's own client-credentials token as inbound | 200 | `service-account-agent-app`; empty data only because the downstream granted it nothing |
| E3 alice's offline refresh token, refreshed later | 200 | alice, same shape as when she is present; only `sid` differs |
| E4 alice revokes the offline token | 200 | refresh fails, but an access token already issued stays valid until it expires (900 s here) |

Mitigation for E2: restrict clients on the gateway's inbound authorizer. Not applied in this run. v1 tested a custom-claim rule on `azp` and it blocked another client's token (`results-v1.md`, P2). `allowedClients` was not tested.

## PoC 2: 3LO + Token Vault (user absent)

Client `agent-bg`: consent required, only scope `itsm`, access token 60 s, IdP session cap 300 s (stands in for a multi-day limit).

| Case | Result |
|---|---|
| alice logs in and consents in a real browser | consent page lists only `itsm`; after `CompleteResourceTokenAuth` the Vault returns a token |
| Scheduler, alice gone, `ForUserId(sub)` -> Vault -> downstream | token has `sub`=alice, `azp=agent-bg`, `aud=itsm-api`, scope `openid itsm`, no `act`; downstream returns her data |
| Access token expired (80 s after consent) | Vault refreshed silently; new token, no user action |
| Past the session cap (331 s after consent) | Vault returns no token and a new authorization URL: the user must consent again |
| `ForUserId` with bob's sub (never consented) | no token, authorization URL only |
| `ForUserId` with the username `alice` instead of the sub | no token; the id is matched literally |
| Role with explicit deny on `ForUserId` | `AccessDenied` |
| CloudTrail | records caller role and workload name, **not** the user id; `GetResourceOauth2Token` shows provider and flow only |

### Reading the results

- The credential is held server-side per (user, agent) and expires on the IdP's schedule. That reproduces "consent once, run unattended until a hard limit".
- Blast radius = users who have consented x the scopes they consented to x time remaining in their session. A user who never consented cannot be impersonated this way.
- The weak point is `GetWorkloadAccessTokenForUserId`: the platform treats the id as an opaque string. Hold the permission only on the scheduler role, derive the id only from a record written while the user was present, and keep your own audit trail, since CloudTrail will not tell you which user the scheduler acted for.
- Compared with a full-identity long-lived credential, scopes can be narrowed per task and the background client is distinguishable by `azp`. It still has no `act`.

## Not covered

- Entra ID's OBO (RFC 7523 jwt-bearer, `JWT_AUTHORIZATION_GRANT`); this repo tests the RFC 8693 mode.
- ServiceNow user mapping and admin consent.
- What happens to a token already in the Vault when the user revokes at the IdP.
- Whether tokens stored through `GetWorkloadAccessTokenForJWT` and `...ForUserId` are interchangeable.
- The scheduler hosted on AgentCore Runtime, and Runtime sessions as the "workspace" layer.
- Real multi-day session limits.
