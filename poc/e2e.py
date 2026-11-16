"""End-to-end OBO test through AgentCore Gateway (MCP).

The "agent" here is a plain MCP client: it only forwards the user's token to the
gateway and never sees agent-app's secret or the downstream token.
"""
import json
import os
import sys
import urllib.request

import kc_check as k

GW = os.environ["GATEWAY_URL"]  # https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp


def mcp(token, method, params=None, rid=1):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(GW, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def run(user):
    tok, _ = k.user_token(user)
    print(f"\n=== {user}: inbound token -> {k.claims(tok)}")
    status, tools = mcp(tok, "tools/list")
    names = [t["name"] for t in tools["result"]["tools"]] if status == 200 else tools
    print("tools/list:", status, names)
    tool = next(n for n in names if "listMyIncidents" in n)
    status, res = mcp(tok, "tools/call", {"name": tool, "arguments": {}}, rid=2)
    if status != 200 or "result" not in res:
        print("tools/call failed:", status, res); return
    text = res["result"]["content"][0]["text"]
    print("tools/call isError:", res["result"].get("isError"))
    print("downstream saw   :", text)


if __name__ == "__main__":
    for u in (sys.argv[1:] or ["alice", "bob"]):
        run(u)
    print("\n=== no token")
    print(mcp("not-a-jwt", "tools/list")[0])
