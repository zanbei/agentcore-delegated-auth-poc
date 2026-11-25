#!/bin/bash
# Keycloak changes the v1 background path needs, on top of scripts/keycloak-setup.sh.
# Run INSIDE the Keycloak container host (same way as keycloak-setup.sh):
#   KC_CONTAINER=keycloak KC_ADMIN_ENV=/opt/kc-admin.env BGEX_SECRET=... bash keycloak-v1.sh
#
#  1. agent-bg-ex: a second, confidential client that performs the token exchange (RFC 8693) for the background gateway.
#     A different client than the interactive path, so the downstream sees a different `azp`.
#  2. agent-bg tokens get agent-bg-ex as an extra audience. Keycloak's standard token exchange requires the requesting
#     client to be in the audience of the subject token.
#  3. agent-bg access token lifespan 120 s. The gateway rejects inbound tokens with under about a minute of validity left,
#     so the 60 s used in the first PoC cannot work behind a gateway.
set -euo pipefail
: "${BGEX_SECRET:?}"
KC_CONTAINER=${KC_CONTAINER:-keycloak}
. "${KC_ADMIN_ENV:-/opt/kc-admin.env}"
K="docker exec $KC_CONTAINER /opt/keycloak/bin/kcadm.sh"
R=obo-test
$K config credentials --server http://localhost:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null
id_of() { python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])'; }

# 1. exchange client
$K create clients -r $R -s clientId=agent-bg-ex -s publicClient=false -s secret="$BGEX_SECRET" \
  -s standardFlowEnabled=false -s directAccessGrantsEnabled=false -s serviceAccountsEnabled=true \
  -s 'attributes."standard.token.exchange.enabled"=true' 2>/dev/null || true
EX=$($K get clients -r $R -q clientId=agent-bg-ex --fields id | id_of)
SCOPE=$($K get client-scopes -r $R --fields id,name | python3 -c 'import sys,json; print([s["id"] for s in json.load(sys.stdin) if s["name"]=="itsm"][0])')
$K update clients/$EX/optional-client-scopes/$SCOPE -r $R

# 2. audience
BG=$($K get clients -r $R -q clientId=agent-bg --fields id | id_of)
$K create clients/$BG/protocol-mappers/models -r $R -s name=aud-bg-ex -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper -s 'config."included.client.audience"=agent-bg-ex' \
  -s 'config."access.token.claim"=true' 2>/dev/null || true

# 3. token lifespan (kcadm update merges this attribute into the existing ones)
$K update clients/$BG -r $R -s 'attributes."access.token.lifespan"=120'
echo "agent-bg-ex ready; agent-bg access token lifespan 120 s"
