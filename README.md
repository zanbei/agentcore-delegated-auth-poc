# AgentCore delegated-auth PoC

English | [简体中文](README.zh-CN.md)

Two proofs of concept for letting an agent act for a user on Amazon Bedrock AgentCore:

1. **User present: OBO token exchange.** An agent calls a downstream API through AgentCore Gateway. The gateway exchanges the user's token for a downstream token on every request, so the downstream sees the real user.
2. **User absent: 3LO + Token Vault.** The user consents once. A scheduler later fetches that user's token from AgentCore Identity's Token Vault with no user session, under a hard expiry the identity provider controls.

The second PoC simulates a pattern from workspace platforms: a per-user authentication anchor, session forwarding to workspaces, and a server-held refresh credential that lets unattended automations run until a hard expiry. This repo maps that pattern onto AgentCore and measures where it holds and where it leaks.

Status: private, experimental. Not production code.

## Architecture

Orange = an AgentCore feature (the groups are the AgentCore services). Blue = external identity provider. Green = downstream. Red dashed = the trust-critical caller and the audit gap. ◆ = an AgentCore API call.

**PoC 1: user present, OBO through the gateway**

```mermaid
flowchart LR
    U[User] -->|"login, aud=agent-app"| C["Agent client<br/>(MCP, forwards the user token only)"]
    C -->|"tools/call + user token"| G
    subgraph AC["Amazon Bedrock AgentCore"]
        G["Gateway<br/>• MCP endpoint<br/>• inbound CUSTOM_JWT authorizer<br/>• OpenAPI target"]
        I["Identity<br/>• OAuth2 credential provider<br/>• OBO: grantType TOKEN_EXCHANGE<br/>• ◆ GetResourceOauth2Token"]
        G -->|"outbound OAuth: TOKEN_EXCHANGE"| I
    end
    I -->|"RFC 8693: subject_token = user token"| K[("Keycloak<br/>external IdP, not AgentCore")]
    K -->|"new token, aud=itsm-api, same sub"| I
    G -->|"Bearer, aud=itsm-api"| D["Mock ITSM API<br/>(Lambda)"]
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    class G,I agentcore
    class K idp
    class U,C app
    class D down
    style AC fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
```

**PoC 2: user absent, 3LO + Token Vault**

```mermaid
flowchart TB
    U2["User browser<br/>(login + consent, once)"]
    subgraph setup["App side, set up once"]
        APP["App<br/>verifies login, reads sub"]
        T[("Task store<br/>task → user sub")]
    end
    subgraph AC["Amazon Bedrock AgentCore"]
        subgraph ID["Identity"]
            direction LR
            CP["OAuth2 credential provider<br/>3LO (USER_FEDERATION)"]
            WI["Workload identity<br/>obo-test-bg-agent<br/>allowed return URLs"]
            V[("Token Vault<br/>per user × agent<br/>access + refresh tokens")]
        end
    end
    K2[("Keycloak, client agent-bg<br/>consent, scope itsm, session cap<br/>external IdP, not AgentCore")]
    subgraph absent["User absent"]
        S["Scheduler<br/>IAM role, the only holder of ForUserId<br/>user sub read from the task record"]
        D2["Mock ITSM API (Lambda)<br/>IAM-only, no public endpoint"]
        CT["CloudTrail<br/>caller + workload name,<br/>no user id"]
    end
    APP -->|"◆ GetWorkloadAccessTokenForUserId(sub)<br/>◆ GetResourceOauth2Token"| CP
    CP -->|"authorization URL"| U2
    U2 -->|"login + consent"| K2
    K2 -->|"auth code callback"| V
    APP -->|"◆ CompleteResourceTokenAuth(userId=sub)"| V
    APP --> T
    S -->|"◆ GetWorkloadAccessTokenForUserId(sub from task)<br/>◆ GetResourceOauth2Token"| V
    V -->|"access token"| S
    V -.->|"refresh at the IdP, until the session cap"| K2
    S -->|"lambda:Invoke + Bearer user token"| D2
    S -.-> CT
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    classDef risk fill:#fff1f0,stroke:#b42318,stroke-dasharray:5 4,color:#0f1b2d
    class CP,WI,V agentcore
    class K2 idp
    class U2,APP,T app
    class D2 down
    class S,CT risk
    style AC fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
    style ID fill:#ffe8c2,stroke:#ff9900,color:#7a4a00
    style setup fill:#f8fafc,stroke:#cbd5e1,color:#475569
    style absent fill:#f8fafc,stroke:#cbd5e1,color:#475569
```

AgentCore features used:

| Feature | Service | PoC |
|---|---|---|
| MCP gateway, inbound `CUSTOM_JWT` authorizer | Gateway | 1 |
| OpenAPI target with outbound OAuth `TOKEN_EXCHANGE` | Gateway | 1 |
| OAuth2 credential provider with OBO config (RFC 8693, `actorTokenContent=NONE`) | Identity | 1 |
| OAuth2 credential provider, 3LO (`USER_FEDERATION`) | Identity | 2 |
| Workload identity with allowed return URLs | Identity | 2 |
| Token Vault: tokens per user x agent, silent refresh | Identity | 2 |
| `GetWorkloadAccessTokenForUserId` | Identity | 2 |
| `GetResourceOauth2Token` | Identity | 1 (called by the gateway's service role), 2 |
| `CompleteResourceTokenAuth` | Identity | 2 |

Not AgentCore: Keycloak (issues the tokens, decides consent and the session cap), the IAM roles, the task store, the downstream API, CloudTrail. AgentCore Runtime, Memory, Policy (Cedar) and the `GetWorkloadAccessTokenForJWT` path are not used.

| Pattern component | AgentCore mapping | Built here |
|---|---|---|
| Per-user auth anchor | IdP session + AgentCore workload identity | yes |
| Session forwarding to workspaces (user present) | Gateway OBO exchange per request | yes (PoC 1) |
| Server-held refresh credential | Token Vault, 3LO authorization-code flow | yes (PoC 2) |
| Hard expiry, re-consent by the user | IdP refresh-token / session lifetime | yes (PoC 2) |
| Restrict who can use the credential | IAM: `ForUserId` only for the scheduler role, explicit deny elsewhere | yes (PoC 2) |
| Workspaces (compute) | AgentCore Runtime sessions (one microVM per session) | **no** |

## Layout

```
poc/          kc_check.py   manual Keycloak token exchange and direct downstream checks
              e2e.py        PoC 1: MCP calls through the gateway as alice and bob
              bg3lo.py      PoC 2: setup / run / later (3LO consent, scheduler fetch, expiry)
              bg_test.py    "no user token" scenarios against the OBO gateway (E1-E4)
              cdp_login.mjs drives a Chrome (CDP) through login + consent for PoC 2 setup
downstream/   index.mjs     mock ITSM API (Lambda): verifies JWT iss/exp/aud/signature, returns the caller's data
scripts/      keycloak-setup.sh, cleanup.sh
docs/         setup.md, results.md
```

## Run

Prerequisites: an AWS account with AgentCore enabled, AWS CLI v2, Python 3, Node 22+, a Keycloak 26.2+ reachable over HTTPS (not `localhost`; AgentCore must reach the discovery URL), and Chrome started with `--remote-debugging-port=9222` for the consent step.

```bash
cp .env.example .env && $EDITOR .env && set -a && . ./.env && set +a
# follow docs/setup.md to create Keycloak clients, the OAuth2 credential providers,
# the gateway and target, the workload identity, and the IAM roles
python3 poc/e2e.py                 # PoC 1
python3 poc/bg3lo.py setup         # PoC 2: alice consents in the browser
python3 poc/bg3lo.py run           # scheduler fetches her token; negative tests
python3 poc/bg3lo.py later         # after the token lifespan, then after the session cap
```

## Findings (details in `docs/results.md`)

- OBO works for a present user: the downstream sees the same `sub` with `aud` rewritten, and each user gets only their own data. The agent client never holds the downstream credential.
- Keycloak's standard token exchange does not write an `act` claim. The downstream can only tell an agent made the call from `azp`. Writing `act` needs Keycloak's preview delegation feature, which requires per-session user consent and so does not fit unattended runs.
- Without a user token, an agent's own client-credentials token is accepted by the gateway unless `allowedClients` restricts it.
- Token Vault supports "consent once, run later": silent refresh works inside the IdP session limit, and after the limit the Vault returns no token and a new authorization URL.
- `GetWorkloadAccessTokenForUserId` takes an unverified user id string. Whoever holds that IAM permission can fetch the token of any user who has already consented. The scope of that permission is the blast radius.
- CloudTrail records the `ForUserId` call but not the user id passed. The application has to log task, user and time itself.

## Security notes

- Secrets (`secrets.env`, `.env`) are git-ignored. Test users use the password grant for convenience; do not do that in production.
- The mock downstream has no public endpoint and verifies tokens itself. Keycloak runs in `start-dev` mode behind a CDN that blocks the admin paths. Treat both as test scaffolding.
- Run `scripts/cleanup.sh` (after reading it) when you are done; the test resources keep running and cost money.
