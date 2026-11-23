"""V2: which IAM actions does the caller need to invoke the automation runtime with a runtimeUserId?"""
import json
import lib
import rt

_, iss, sub = lib.user("alice")
uid = f"{iss}#{sub}"
for role in ("obo-test-v1-launcher-role", "obo-test-v1-noforuser-role"):
    for label, u in (("with runtimeUserId", uid), ("without runtimeUserId", None)):
        r = rt.invoke({"mode": "use"}, u, role=role)
        short = {k: r.get(k) for k in ("status", "has_workload_token", "downstream_status", "cli_error") if k in r}
        print(f"{role:30s} {label:22s} -> {short}")
