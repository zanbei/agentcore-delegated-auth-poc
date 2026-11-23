"""P2: the unattended path goes through the background gateway.
Vault token (azp=agent-bg) -> gateway inbound (azp rule) -> OBO exchange with agent-bg-ex -> downstream.
Run once without a policy engine (baseline), and again after attach_policy.py (read allowed, write denied).
Usage: python3 p2_gateway.py [--skip-grant]
"""
import json
import sys

import rt

alice = rt.user_id_of("alice")
if "--skip-grant" not in sys.argv:
    _, st = rt.grant_via_runtime("alice")
    print("grant alice:", st)
r = rt.invoke({"mode": "gateway", "calls": ["itsm___listMyIncidents", "itsm___approveIncident"],
               "arguments": {"itsm___approveIncident": {"id": "INC0001"}}}, alice)
print("vault token claims:", r.get("token_claims"))
print("tools/list:", r.get("tools_list"))
for c in r.get("calls", []):
    print("call:", json.dumps(c)[:700])
if "calls" not in r:
    print(r)
