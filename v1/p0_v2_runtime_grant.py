"""V2: grant and use both go through the automation runtime (IAM inbound, runtimeUserId).
The runtime's managed workload identity cannot mint a workload token for a caller, so the console
cannot call GetWorkloadAccessTokenForUserId on it. The grant therefore runs inside the runtime.

Steps: connect (consent link) -> browser consent -> complete (session binding) -> use (Vault read, downstream call).
Negative checks: bind a session to a different user id, use as a user who never consented.
"""
import json
import sys

import lib
import rt

b = lib.b


def uid_of(name):
    _, iss, sub = lib.user(name)
    return f"{iss}#{sub}"


alice, bob = uid_of("alice"), uid_of("bob")
print("## connect (runtimeUserId = alice)")
c = rt.invoke({"mode": "connect"}, alice)
print("  ", {k: (v if k not in ("authorizationUrl", "sessionUri") else bool(v)) for k, v in c.items()})
if c.get("status") != "consent-required":
    sys.exit("connect did not return a consent link")

print("## browser consent as alice")
res = lib.browser_consent(c["authorizationUrl"], "alice")
print("   steps:", res["steps"], "ok:", res["ok"])
session_uri = res["query"].get("session_id") or c["sessionUri"]

print("## negative: bind alice's consent session to bob's user id (header says alice, payload says bob)")
n1 = rt.invoke({"mode": "complete", "sessionUri": session_uri, "userId": bob}, alice)
print("   ", n1)

print("## complete with alice's own user id")
ok = rt.invoke({"mode": "complete", "sessionUri": session_uri, "userId": alice}, alice)
print("   ", ok)

print("## use (unattended path)")
print("   alice:", json.dumps(rt.invoke({"mode": "use"}, alice)))
print("   bob  :", json.dumps(rt.invoke({"mode": "use"}, bob)))
