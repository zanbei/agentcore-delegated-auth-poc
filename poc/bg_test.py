"""What does the downstream see when the user is NOT present?

Each scenario is a way a background job (scheduled / event-driven / offline) could
obtain an inbound token for the gateway. The gateway config is unchanged (OBO target).
"""
import json
import sys
import time

import e2e
import kc_check as k

TOOL = "itsm___listMyIncidents"
FIELDS = ["sub", "preferred_username", "azp", "scope", "sid", "login_age_s", "act"]


def call_gateway(token):
    status, res = e2e.mcp(token, "tools/call", {"name": TOOL, "arguments": {}}, rid=2)
    if status != 200 or not isinstance(res, dict) or "result" not in res:
        return status, (res if isinstance(res, str) else json.dumps(res)[:200]), None
    body = json.loads(res["result"]["content"][0]["text"])
    if "caller" not in body:  # tool returned the downstream error body
        return status, body, None
    seen = {f: body["caller"].get(f) for f in FIELDS}
    return status, [i["number"] for i in body["incidents"]], seen


def show(title, token):
    status, data, seen = call_gateway(token)
    print(f"\n## {title}")
    print("   gateway status:", status)
    print("   incidents     :", data)
    if seen:
        print("   downstream saw:", seen)
    return seen


def decode(tok):
    import base64
    p = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def revoke(refresh_token):
    import urllib.parse, urllib.request
    url = f"{k.BASE}/protocol/openid-connect/revoke"
    data = urllib.parse.urlencode({"token": refresh_token, "client_id": "user-cli",
                                   "token_type_hint": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=20) as r:
        return r.status


if __name__ == "__main__":
    wait = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    # E0: baseline, user present (fresh interactive login)
    alice_now, _ = k.user_token("alice")
    show("E0 user present: alice's fresh login token", alice_now)

    # E1: background job with no token at all
    print("\n## E1 background job, no token")
    print("   gateway status:", e2e.mcp("", "tools/list")[0])

    # E2: background job uses the agent's own identity (client_credentials)
    m2m = k.post({"grant_type": "client_credentials", "scope": "openid"},
                 auth=f"agent-app:{k.env['AGENT_SECRET']}")
    if "access_token" not in m2m:
        print("\n## E2 m2m token failed:", m2m)
    else:
        print("\n   agent m2m token:", k.claims(m2m["access_token"]))
        show("E2 background job, agent's own M2M token as inbound", m2m["access_token"])

    # E3: at "schedule time" alice grants offline access; later the job refreshes it
    sched = k.post({"grant_type": "password", "client_id": "user-cli", "username": "alice",
                    "password": k.env["ALICE_PW"], "scope": "openid offline_access"})
    rt = sched["refresh_token"]
    rtc = decode(rt)
    print(f"\n   offline refresh token: typ={rtc.get('typ')} exp={rtc.get('exp')} "
          f"(None = no fixed expiry) sid={rtc.get('sid')}")
    print(f"   ... simulating time passing ({wait}s), user is gone ...")
    time.sleep(wait)
    bg = k.post({"grant_type": "refresh_token", "client_id": "user-cli", "refresh_token": rt})
    show("E3 background job, access token refreshed from alice's offline token", bg["access_token"])

    # E4: alice revokes the offline grant
    print("\n## E4 alice revokes offline token")
    print("   revoke status :", revoke(rt))
    again = k.post({"grant_type": "refresh_token", "client_id": "user-cli", "refresh_token": rt})
    print("   refresh after revoke:", again.get("error") or again.get("http_error"), again.get("body", "")[:120])
    status, data, seen = call_gateway(bg["access_token"])
    print("   previously issued access token -> gateway:", status, data)
