"""Call the background gateway directly with a token from Keycloak (no Vault involved), to isolate the inbound rules.
Usage: python3 gw_direct.py [tool ...]    tokens: user-cli password grant (azp=user-cli); needs aud agent-bg-ex mapper on user-cli.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import lib

k = lib.k
GW = os.environ["BG_GATEWAY_ID"]
URL = f"https://{GW}.gateway.bedrock-agentcore.{lib.b.REGION}.amazonaws.com/mcp"


def mcp(tok, method, params=None, rid=1):
    req = urllib.request.Request(URL, method="POST", data=json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return e.code, body, e.headers.get("WWW-Authenticate")


tok, _ = k.user_token("alice")
print("token claims:", {x: k.claims(tok).get(x) for x in ("azp", "aud", "scope")})
print("tools/list:", mcp(tok, "tools/list"))
