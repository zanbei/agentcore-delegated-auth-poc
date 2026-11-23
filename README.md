# AgentCore delegated-auth PoC

English | [简体中文](README.zh-CN.md)

Two proofs of concept for letting an agent act for a user on Amazon Bedrock AgentCore:

1. **User present: OBO token exchange.** An agent calls a downstream API through AgentCore Gateway. The gateway exchanges the user's token for a downstream token on every request, so the downstream sees the real user.
2. **User absent: 3LO + Token Vault.** The user consents once. A scheduler later fetches that user's token from AgentCore Identity's Token Vault with no user session, under a hard expiry the identity provider controls.

The second PoC simulates a pattern from workspace platforms: a per-user authentication anchor, session forwarding to workspaces, and a server-held refresh credential that lets unattended automations run until a hard expiry. This repo maps that pattern onto AgentCore and measures where it holds and where it leaks.

Status: private, experimental. Not production code.
v1 (in progress, Chinese only): a fuller design and its checks are in `docs/design.zh-CN.md`, `docs/results-v1.md` and `v1/`. Not everything in the design is built; the results file says what was tested.

## Architecture

Orange = an AgentCore feature; the thick orange path is OBO (the groups are the AgentCore services). Blue = external identity provider. Green = downstream. Red dashed = the trust-critical caller and the audit gap. ◆ = an AgentCore API call.

**PoC 1: user present, OBO through the gateway**

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 8}, "themeVariables": {"fontSize": "13px"}}}%%
flowchart LR
    U["User"] -->|"login, aud=agent-app"| C["Agent client<br/>(forwards the user token only)"]
    C -->|"tools/call + user token<br/>aud=agent-app"| G
    subgraph AC["Amazon Bedrock AgentCore"]
        G["Gateway<br/>• inbound CUSTOM_JWT check<br/>• OpenAPI target, outbound OAuth"]
        O["🔑 OBO token exchange (AgentCore Identity)<br/>credential provider: grantType TOKEN_EXCHANGE<br/>user token in → downstream token out<br/>◆ GetResourceOauth2Token"]
        G ==>|"① request outbound token"| O
    end
    O <==>|"② RFC 8693<br/>subject_token = user token"| K[("Keycloak<br/>issues the new token<br/>external IdP, not AgentCore")]
    G ==>|"③ Bearer, aud=itsm-api"| D["Mock ITSM API<br/>(Lambda)<br/>same sub: sees the real user<br/>returns only that user's data"]
    linkStyle 2,3,4 stroke:#e65100,stroke-width:4px
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef obo fill:#ffe0b2,stroke:#e65100,stroke-width:4px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    classDef risk fill:#fff1f0,stroke:#b42318,stroke-dasharray:5 4,color:#0f1b2d
    style AC fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
    class G agentcore
    class O obo
    class K idp
    class U,C app
    class D down
```

**PoC 2: user absent, 3LO + Token Vault**

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 18, "rankSpacing": 28, "padding": 8}, "themeVariables": {"fontSize": "13px"}}}%%
flowchart LR
    APP["App (user present, once)<br/>task → user sub"] -->|"◆ ForUserId · ◆ GetResourceOauth2Token<br/>◆ CompleteResourceTokenAuth"| ID
    subgraph AC["Amazon Bedrock AgentCore"]
        ID["Identity<br/>• 3LO credential provider (USER_FEDERATION)<br/>• Workload identity<br/>• Token Vault (user × agent)<br/><b>3LO here, no OBO: no exchange, no user token needed later</b>"]
    end
    B["User browser<br/>login + consent"] -->|"consent"| K[("Keycloak agent-bg<br/>external IdP, not AgentCore")]
    K -->|"auth code"| ID
    ID -.->|"refresh, until the session cap"| K
    S["Scheduler (IAM role)<br/>only holder of ForUserId<br/>CloudTrail: no user id"] -->|"◆ ForUserId(sub from task)<br/>◆ GetResourceOauth2Token"| ID
    ID -->|"access token"| S
    S -->|"lambda:Invoke + Bearer"| D["Mock ITSM API<br/>IAM-only"]
    classDef agentcore fill:#fff7eb,stroke:#ff9900,stroke-width:2px,color:#0f1b2d
    classDef obo fill:#ffe0b2,stroke:#e65100,stroke-width:4px,color:#0f1b2d
    classDef idp fill:#eef4ff,stroke:#2563eb,color:#0f1b2d
    classDef app fill:#ffffff,stroke:#94a3b8,color:#0f1b2d
    classDef down fill:#ecfdf3,stroke:#15803d,color:#0f1b2d
    classDef risk fill:#fff1f0,stroke:#b42318,stroke-dasharray:5 4,color:#0f1b2d
    style AC fill:#fff3e0,stroke:#ff9900,stroke-width:2px,color:#7a4a00
    class ID agentcore
    class K idp
    class APP,B app
    class D down
    class S risk
```

**Where OBO is.** OBO is the thick orange path in PoC 1 only: the gateway's outbound OAuth asks AgentCore Identity for a token (①), Identity exchanges the user's token at the IdP (RFC 8693, ②), and the downstream receives a token for itself that still carries the user's `sub` (③). It needs a live user token as `subject_token`, so it does not exist in PoC 2, where the user is absent. PoC 2 gets its token from a one-time consent (3LO) and the Token Vault instead.

| | PoC 1 | PoC 2 |
|---|---|---|
| Flow | OBO token exchange | 3LO authorization code + Token Vault |
| Credential provider | `obo-test-keycloak`, with an OBO config | `obo-test-bg-3lo`, no OBO config |
| Needs a user token at call time | yes, every call | no |
| Exchange per call | yes | no, the Vault serves a stored token and refreshes it |
| `azp` the downstream sees | `agent-app` | `agent-bg` |

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

**AWS deployment view (us-west-2)**

[![AWS deployment architecture](docs/architecture-aws.png)](docs/architecture-aws.svg)

Everything runs in one account and one region. Keycloak stands in for the enterprise IdP on a single EC2 instance behind CloudFront (its security group admits only CloudFront origin traffic; there is no SSH and administration goes through Systems Manager). The mock downstream is a Lambda with no public endpoint. The gateway target of PoC 1 currently points at an endpoint that was removed, so only PoC 2 runs end to end in this deployment; the picture marks that. Click the image for the vector version.

**Pattern → AgentCore mapping**

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
docs/         setup.md, results.md, architecture-aws.svg / .png
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
- Without a user token, an agent's own client-credentials token is accepted by the gateway. An inbound restriction has to block it. v1 tested a custom-claim rule on `azp` (`docs/results-v1.md`); `allowedClients` was not tested.
- Token Vault supports "consent once, run later": silent refresh works inside the IdP session limit, and after the limit the Vault returns no token and a new authorization URL.
- `GetWorkloadAccessTokenForUserId` takes an unverified user id string. Whoever holds that IAM permission can fetch the token of any user who has already consented. The scope of that permission is the blast radius.
- CloudTrail records the `ForUserId` call but not the user id passed. The application has to log task, user and time itself.

## Security notes

- Secrets (`secrets.env`, `.env`) are git-ignored. Test users use the password grant for convenience; do not do that in production.
- The mock downstream has no public endpoint and verifies tokens itself. Keycloak runs in `start-dev` mode behind a CDN that blocks the admin paths. Treat both as test scaffolding.
- Run `scripts/cleanup.sh` (after reading it) when you are done; the test resources keep running and cost money.
