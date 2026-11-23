#!/usr/bin/env bash
# Background gateway: inbound accepts only tokens whose `azp` is the background client (custom claim rule),
# the target exchanges that token (RFC 8693, OBO) with a separate exchange client so the downstream sees a different `azp`.
# Needs: AWS_ACCOUNT_ID, AWS_REGION, KC_ISSUER, BGEX_SECRET (secret of the Keycloak client agent-bg-ex), DOWNSTREAM_URL (https://...).
set -euo pipefail
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}" "${KC_ISSUER:?}" "${BGEX_SECRET:?}" "${DOWNSTREAM_URL:?}"
PROVIDER="obo-test-v1-bg-ex"
ROLE="obo-test-v1-bg-gateway-role"
GW="obo-test-v1-bg-gateway"
ROOT="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}"
DISCOVERY="${KC_ISSUER}/.well-known/openid-configuration"

aws bedrock-agentcore-control get-oauth2-credential-provider --name "$PROVIDER" >/dev/null 2>&1 || \
aws bedrock-agentcore-control create-oauth2-credential-provider --name "$PROVIDER" --credential-provider-vendor CustomOauth2 \
  --oauth2-provider-config-input "$(cat <<EOF
{"customOauth2ProviderConfig":{"oauthDiscovery":{"discoveryUrl":"${DISCOVERY}"},"clientId":"agent-bg-ex","clientSecret":"${BGEX_SECRET}",
"clientAuthenticationMethod":"CLIENT_SECRET_BASIC","onBehalfOfTokenExchangeConfig":{"grantType":"TOKEN_EXCHANGE",
"tokenExchangeGrantTypeConfig":{"actorTokenContent":"NONE"}}}}
EOF
)" >/dev/null
SECRET_ARN=$(aws bedrock-agentcore-control get-oauth2-credential-provider --name "$PROVIDER" --query 'clientSecretArn.secretArn' --output text)

aws iam get-role --role-name "$ROLE" >/dev/null 2>&1 || aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
 "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"bedrock-agentcore.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"${AWS_ACCOUNT_ID}\"}}}]}" >/dev/null
aws iam put-role-policy --role-name "$ROLE" --policy-name inline --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
 {\"Sid\":\"TokenVault\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:GetWorkloadAccessToken\",\"bedrock-agentcore:GetWorkloadAccessTokenForJWT\",\"bedrock-agentcore:GetResourceOauth2Token\"],
  \"Resource\":[\"${ROOT}:workload-identity-directory/default\",\"${ROOT}:workload-identity-directory/default/workload-identity/*\",\"${ROOT}:token-vault/default\",\"${ROOT}:token-vault/default/oauth2credentialprovider/${PROVIDER}\"]},
 {\"Sid\":\"ReadClientSecret\",\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"${SECRET_ARN}\"}]}"
sleep 10

GW_ID=$(aws bedrock-agentcore-control list-gateways --query "items[?name=='${GW}'].gatewayId | [0]" --output text)
if [ "$GW_ID" = "None" ] || [ -z "$GW_ID" ]; then
  GW_ID=$(aws bedrock-agentcore-control create-gateway --name "$GW" --protocol-type MCP --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE}" \
    --authorizer-type CUSTOM_JWT --authorizer-configuration "$(cat <<EOF
{"customJWTAuthorizer":{"discoveryUrl":"${DISCOVERY}","allowedAudience":["agent-bg-ex"],
 "customClaims":[{"inboundTokenClaimName":"azp","inboundTokenClaimValueType":"STRING",
  "authorizingClaimMatchValue":{"claimMatchValue":{"matchValueString":"agent-bg"},"claimMatchOperator":"EQUALS"}}]}}
EOF
)" --query gatewayId --output text)
fi
for i in $(seq 1 30); do
  S=$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" --query status --output text); [ "$S" = "READY" ] && break; sleep 4
done
echo "gateway=$GW_ID status=$S"

SPEC=$(python3 - <<EOF
import json
op = lambda oid, summ, params=None: {"operationId": oid, "summary": summ, **({"parameters": params} if params else {}),
     "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}}}
print(json.dumps({"openapi": "3.0.1", "info": {"title": "Mock ITSM API", "version": "1.0"}, "servers": [{"url": "${DOWNSTREAM_URL}"}],
  "paths": {"/incidents": {"get": op("listMyIncidents", "List incidents visible to the calling user")},
            "/incidents/{id}/approve": {"post": op("approveIncident", "Approve an incident (write)",
               [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}])}}}))
EOF
)
python3 - "$GW_ID" "$PROVIDER" "$ROOT" "$SPEC" <<'EOF'
import json, subprocess, sys
gw, provider, root, spec = sys.argv[1:5]
cfg = {"mcp": {"openApiSchema": {"inlinePayload": spec}}}
creds = [{"credentialProviderType": "OAUTH", "credentialProvider": {"oauthCredentialProvider": {
    "providerArn": f"{root}:token-vault/default/oauth2credentialprovider/{provider}", "grantType": "TOKEN_EXCHANGE", "scopes": ["itsm"],
    "customParameters": {"subject_token_type": "urn:ietf:params:oauth:token-type:access_token"}}}}]
ex = subprocess.run(["aws", "bedrock-agentcore-control", "list-gateway-targets", "--gateway-identifier", gw, "--query",
                     "items[?name=='itsm'].targetId | [0]", "--output", "text"], capture_output=True, text=True).stdout.strip()
if ex and ex != "None":
    print("target exists:", ex); sys.exit()
r = subprocess.run(["aws", "bedrock-agentcore-control", "create-gateway-target", "--gateway-identifier", gw, "--name", "itsm",
                    "--target-configuration", json.dumps(cfg), "--credential-provider-configurations", json.dumps(creds),
                    "--query", "[targetId,status]", "--output", "text"], capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip()[-400:])
EOF
aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" --query '{url:gatewayUrl,id:gatewayId}' --output json
