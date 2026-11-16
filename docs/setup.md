# Setup

Placeholders in `<angle brackets>`. Region in the examples is `us-west-2`.

## 1. Identity provider

Keycloak 26.2 or later (standard token exchange is on by default from 26.2), served over HTTPS at a public hostname. AgentCore fetches the OIDC discovery document, so `localhost` does not work. The PoC ran Keycloak in `start-dev` mode behind CloudFront; the admin paths (`/admin*`, `/realms/master*`) were blocked at the CDN and administration happened from inside the host.

Set `KC_HOSTNAME` to the public URL and do **not** set `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`: with it, the discovery document advertised the internal HTTP token endpoint and AgentCore could not call it.

Then run `scripts/keycloak-setup.sh` on the host. It creates:

| Client | Purpose |
|---|---|
| `user-cli` | stands in for the app's login; audience mapper adds `agent-app` |
| `agent-app` | PoC 1 agent; confidential; standard token exchange enabled |
| `itsm-api` | downstream resource server (audience only) |
| `agent-bg` | PoC 2 background client; consent required, `fullScopeAllowed=false`, access token 60 s, session cap 300 s |

and scope `itsm` (adds `aud=itsm-api` and `preferred_username`), users `alice` and `bob`.

## 2. Downstream API

`downstream/index.mjs`: a Lambda (Node 22) that verifies RS256 signature, `iss`, `exp` and `aud=itsm-api` itself and returns the caller's incidents. Environment: `ISSUER` and `JWKS_JSON` (the realm's signing keys, pinned so the function needs no outbound network).

Do not give it a public function URL with `AuthType NONE`. That was tried first; an account-level control removed the public invoke permission within hours. The PoC finally had no public endpoint: PoC 2 invokes it with `lambda:Invoke`.

## 3. PoC 1: OBO through the gateway

```bash
# credential provider with the OBO (RFC 8693) exchange
aws bedrock-agentcore-control create-oauth2-credential-provider --cli-input-json '{
  "name": "obo-test-keycloak", "credentialProviderVendor": "CustomOauth2",
  "oauth2ProviderConfigInput": {"customOauth2ProviderConfig": {
    "oauthDiscovery": {"discoveryUrl": "<KC_ISSUER>/.well-known/openid-configuration"},
    "clientId": "agent-app", "clientSecret": "<AGENT_SECRET>",
    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
    "onBehalfOfTokenExchangeConfig": {"grantType": "TOKEN_EXCHANGE",
      "tokenExchangeGrantTypeConfig": {"actorTokenContent": "NONE"}}}}}'

# gateway, inbound JWT validation (audience agent-app)
aws bedrock-agentcore-control create-gateway --name obo-test-gateway --protocol-type MCP \
  --role-arn <gateway-role-arn> --authorizer-type CUSTOM_JWT \
  --authorizer-configuration '{"customJWTAuthorizer":{"discoveryUrl":"<KC_ISSUER>/.well-known/openid-configuration","allowedAudience":["agent-app"]}}'
```

Gateway target (OpenAPI, one operation `listMyIncidents`), outbound OAuth with `grantType: TOKEN_EXCHANGE`, `scopes: ["itsm"]`, `customParameters: {"subject_token_type": "urn:ietf:params:oauth:token-type:access_token"}`. The target's server URL must be reachable by the gateway; with the Lambda IAM-only design above, point it at an endpoint you front yourself.

Gateway service role needs, scoped to the account's `workload-identity-directory/default`, `token-vault/default` and the one credential provider:
`bedrock-agentcore:GetWorkloadAccessToken`, `GetWorkloadAccessTokenForJWT`, `GetResourceOauth2Token`, plus `secretsmanager:GetSecretValue` on the provider's client-secret ARN.

When writing the policy from a shell, avoid `$VAR:token-vault`; in zsh `:t` is a history modifier and silently truncates the ARN.

## 4. PoC 2: 3LO + Token Vault

```bash
aws bedrock-agentcore-control create-oauth2-credential-provider --cli-input-json '{
  "name": "obo-test-bg-3lo", "credentialProviderVendor": "CustomOauth2",
  "oauth2ProviderConfigInput": {"customOauth2ProviderConfig": {
    "oauthDiscovery": {"discoveryUrl": "<KC_ISSUER>/.well-known/openid-configuration"},
    "clientId": "agent-bg", "clientSecret": "<BG_SECRET>",
    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC"}}}'
# output has callbackUrl: register it as agent-bg's redirect URI in Keycloak

aws bedrock-agentcore-control create-workload-identity --name obo-test-bg-agent \
  --allowed-resource-oauth2-return-urls "<KC_ISSUER>/.well-known/openid-configuration"
```

The return URL must be listed on the workload identity, or `GetResourceOauth2Token` fails with "Callback URL ... is not registered". A new value can take a short while to apply.

Two IAM roles that your own identity can assume (`sts:AssumeRole`):

- `obo-test-scheduler-role`: allow `GetWorkloadAccessTokenForUserId` on the workload identity only; allow `GetResourceOauth2Token` on the workload identity and the one provider; `secretsmanager:GetSecretValue` on that provider's secret; `lambda:InvokeFunction` on the downstream function.
- `obo-test-noforuser-role`: same, but with an explicit `Deny` on `GetWorkloadAccessTokenForUserId` (control case).

Never attach `BedrockAgentCoreFullAccess` to the scheduler: it includes `GetWorkloadAccessTokenForUserId` on any resource.

## 5. Browser step

`poc/bg3lo.py setup` drives a local Chrome through the Keycloak login and consent page (`poc/cdp_login.mjs`). Start Chrome with `--remote-debugging-port=9222` and a throwaway `--user-data-dir`. The CDP port has no authentication; close it afterwards.
