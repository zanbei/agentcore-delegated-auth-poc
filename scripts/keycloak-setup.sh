#!/bin/bash
# Create the Keycloak realm, clients, scopes and users used by the PoCs.
# Run INSIDE the Keycloak container host, e.g. over SSM or ssh:
#   KC_CONTAINER=keycloak KC_ADMIN_ENV=/opt/kc-admin.env AGENT_SECRET=... BG_SECRET=... \
#   ALICE_PW=... BOB_PW=... AGENTCORE_CALLBACK=https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<id> \
#   bash keycloak-setup.sh
# Derived from the commands used while building the PoC; read it before running.
# Note: AGENTCORE_CALLBACK is the callbackUrl returned by create-oauth2-credential-provider
# for the 3LO provider, so create that provider first with a placeholder client secret and
# update it, or run only the PoC 1 part (set SKIP_BG=1).
set -euo pipefail
: "${AGENT_SECRET:?}" "${ALICE_PW:?}" "${BOB_PW:?}"
KC_CONTAINER=${KC_CONTAINER:-keycloak}
. "${KC_ADMIN_ENV:-/opt/kc-admin.env}"
K="docker exec $KC_CONTAINER /opt/keycloak/bin/kcadm.sh"
R=obo-test

$K config credentials --server http://localhost:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null

id_of() { python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])'; }

$K get realms/$R >/dev/null 2>&1 || $K create realms -s realm=$R -s enabled=true -s accessTokenLifespan=900

# Scope that makes tokens valid for the downstream API
$K create client-scopes -r $R -s name=itsm -s protocol=openid-connect \
  -s 'attributes."include.in.token.scope"=true' 2>/dev/null || true
SID=$($K get client-scopes -r $R --fields id,name | python3 -c 'import sys,json; print([s["id"] for s in json.load(sys.stdin) if s["name"]=="itsm"][0])')
$K create client-scopes/$SID/protocol-mappers/models -r $R -s name=aud-itsm-api -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper -s 'config."included.client.audience"=itsm-api' \
  -s 'config."access.token.claim"=true' 2>/dev/null || true
$K create client-scopes/$SID/protocol-mappers/models -r $R -s name=itsm-username -s protocol=openid-connect \
  -s protocolMapper=oidc-usermodel-property-mapper -s 'config."user.attribute"=username' \
  -s 'config."claim.name"=preferred_username' -s 'config."jsonType.label"=String' \
  -s 'config."access.token.claim"=true' 2>/dev/null || true

# Downstream resource server
$K create clients -r $R -s clientId=itsm-api -s publicClient=false -s standardFlowEnabled=false \
  -s directAccessGrantsEnabled=false 2>/dev/null || true

# PoC 1: the agent. Standard token exchange (RFC 8693) enabled.
$K create clients -r $R -s clientId=agent-app -s publicClient=false -s secret="$AGENT_SECRET" \
  -s standardFlowEnabled=false -s directAccessGrantsEnabled=false -s serviceAccountsEnabled=true \
  -s 'attributes."standard.token.exchange.enabled"=true' 2>/dev/null || true
AID=$($K get clients -r $R -q clientId=agent-app --fields id | id_of)
$K update clients/$AID/optional-client-scopes/$SID -r $R
$K create clients/$AID/protocol-mappers/models -r $R -s name=aud-self -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper -s 'config."included.client.audience"=agent-app' \
  -s 'config."access.token.claim"=true' 2>/dev/null || true

# PoC 1: stand-in for the app's login (password grant only for tests)
$K create clients -r $R -s clientId=user-cli -s publicClient=true -s standardFlowEnabled=false \
  -s directAccessGrantsEnabled=true 2>/dev/null || true
UID_=$($K get clients -r $R -q clientId=user-cli --fields id | id_of)
$K create clients/$UID_/protocol-mappers/models -r $R -s name=aud-agent-app -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper -s 'config."included.client.audience"=agent-app' \
  -s 'config."access.token.claim"=true' 2>/dev/null || true

# Users
for u in alice:"$ALICE_PW" bob:"$BOB_PW"; do
  n=${u%%:*}; p=${u#*:}
  $K create users -r $R -s username=$n -s enabled=true -s email=$n@example.com -s emailVerified=true \
    -s firstName=$n -s lastName=test 2>/dev/null || true
  $K set-password -r $R --username $n --new-password "$p"
done

# PoC 2: background client. Consent required, narrow scopes, short sessions to simulate a hard expiry.
if [ -z "${SKIP_BG:-}" ]; then
  : "${BG_SECRET:?}" "${AGENTCORE_CALLBACK:?}"
  $K create clients -r $R -s clientId=agent-bg -s publicClient=false -s secret="$BG_SECRET" \
    -s standardFlowEnabled=true -s directAccessGrantsEnabled=false -s serviceAccountsEnabled=false \
    -s consentRequired=true -s fullScopeAllowed=false -s "redirectUris=[\"$AGENTCORE_CALLBACK\"]" 2>/dev/null || true
  CID=$($K get clients -r $R -q clientId=agent-bg --fields id | id_of)
  # kcadm's -s drops some attributes on create; set them through a full update
  $K get clients/$CID -r $R > /tmp/agent-bg.json
  python3 - <<'EOF'
import json
d = json.load(open("/tmp/agent-bg.json"))
d.setdefault("attributes", {}).update({
    "access.token.lifespan": "60",        # access token 60 s
    "client.session.max.lifespan": "300",  # hard cap 300 s, stands in for a multi-day limit
    "client.session.idle.timeout": "300",
})
json.dump(d, open("/tmp/agent-bg.json", "w"))
EOF
  docker cp /tmp/agent-bg.json $KC_CONTAINER:/tmp/agent-bg.json
  $K update clients/$CID -r $R -f /tmp/agent-bg.json
  rm -f /tmp/agent-bg.json; docker exec -u 0 $KC_CONTAINER rm -f /tmp/agent-bg.json
  # only the basic scope by default; itsm is requested explicitly and shown on the consent page
  for sc in $($K get clients/$CID/default-client-scopes -r $R --fields id,name | python3 -c 'import sys,json; print(" ".join(s["id"] for s in json.load(sys.stdin) if s["name"]!="basic"))'); do
    $K delete clients/$CID/default-client-scopes/$sc -r $R
  done
  $K update clients/$CID/optional-client-scopes/$SID -r $R
fi
echo "keycloak setup done"
