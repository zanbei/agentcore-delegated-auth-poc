"""Change the inbound authorizer of the background gateway and wait until it is READY again.
Usage: python3 gw_authorizer.py none                 audience only
       python3 gw_authorizer.py claim <claim> <value> [OPERATOR]   audience plus a custom claim rule on a string claim
"""
import json
import os
import subprocess
import sys
import time

import lib

b = lib.b
GW = os.environ["BG_GATEWAY_ID"]
DISCOVERY = os.environ["KC_ISSUER"] + "/.well-known/openid-configuration"


def cli(*a):
    r = subprocess.run(["aws", "bedrock-agentcore-control", *a, "--region", b.REGION, "--output", "json"], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr.strip()[-500:])
    return json.loads(r.stdout) if r.stdout.strip() else {}


cur = cli("get-gateway", "--gateway-identifier", GW)
auth = {"discoveryUrl": DISCOVERY, "allowedAudience": os.environ.get("AUD", "agent-bg-ex").split(",")}
if sys.argv[1] == "claim":
    op = sys.argv[4] if len(sys.argv) > 4 else "EQUALS"
    auth["customClaims"] = [{"inboundTokenClaimName": sys.argv[2], "inboundTokenClaimValueType": "STRING",
                             "authorizingClaimMatchValue": {"claimMatchValue": {"matchValueString": sys.argv[3]}, "claimMatchOperator": op}}]
cli("update-gateway", "--gateway-identifier", GW, "--name", cur["name"], "--role-arn", cur["roleArn"],
    "--protocol-type", cur["protocolType"], "--authorizer-type", "CUSTOM_JWT",
    "--authorizer-configuration", json.dumps({"customJWTAuthorizer": auth}))
for _ in range(40):
    time.sleep(3)
    if cli("get-gateway", "--gateway-identifier", GW)["status"] == "READY":
        break
print("authorizer now:", json.dumps(cli("get-gateway", "--gateway-identifier", GW)["authorizerConfiguration"]["customJWTAuthorizer"].get("customClaims", "audience only")))
