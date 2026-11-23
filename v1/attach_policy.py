"""Cedar policy for the background gateway: default deny, permit only the read tool for the background client.
Usage: python3 attach_policy.py LOG_ONLY|ENFORCE|DETACH
Policies are permit-only on purpose: a forbid that reads a missing claim tag is skipped and blocks nothing,
while a skipped permit grants nothing, so default deny still holds. Every claim is guarded with hasTag.
"""
import json
import os
import subprocess
import sys
import time

import lib

b = lib.b
GW = os.environ["BG_GATEWAY_ID"]
ENGINE = "obo_test_v1_bg_policy"


def cli(*a, check=True):
    r = subprocess.run(["aws", "bedrock-agentcore-control", *a, "--region", b.REGION, "--output", "json"], capture_output=True, text=True)
    if r.returncode:
        if check:
            sys.exit(r.stderr.strip()[-600:])
        return None
    return json.loads(r.stdout) if r.stdout.strip() else {}


def engine_id():
    for e in cli("list-policy-engines").get("policyEngines", []):
        if e["name"] == ENGINE:
            return e["policyEngineId"], e["policyEngineArn"]
    out = cli("create-policy-engine", "--name", ENGINE, "--description", "background gateway: read tools only")
    return out["policyEngineId"], out["policyEngineArn"]


gw = cli("get-gateway", "--gateway-identifier", GW)
mode = sys.argv[1]


def set_gateway(policy_cfg):
    args = ["update-gateway", "--gateway-identifier", GW, "--name", gw["name"], "--role-arn", gw["roleArn"],
            "--protocol-type", gw["protocolType"], "--authorizer-type", gw["authorizerType"],
            "--authorizer-configuration", json.dumps(gw["authorizerConfiguration"])]
    if policy_cfg:
        args += ["--policy-engine-configuration", json.dumps(policy_cfg)]
    cli(*args)
    for _ in range(40):
        time.sleep(3)
        if cli("get-gateway", "--gateway-identifier", GW)["status"] == "READY":
            return


if mode == "DETACH":
    set_gateway(None)
    print("detached; gateway has no policy engine")
    sys.exit()

eid, earn = engine_id()
for _ in range(40):
    if cli("get-policy-engine", "--policy-engine-id", eid).get("status") == "ACTIVE":
        break
    time.sleep(3)
STATEMENT = f'''permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"itsm___listMyIncidents",
  resource == AgentCore::Gateway::"{gw['gatewayArn']}"
) when {{
  principal.hasTag("azp") && principal.getTag("azp") == "agent-bg"
}};'''
existing = {p["name"]: p for p in cli("list-policies", "--policy-engine-id", eid).get("policies", [])}
old = existing.get("permit_read_for_background_client")
if old and old["status"] != "ACTIVE":
    cli("delete-policy", "--policy-engine-id", eid, "--policy-id", old["policyId"])
    for _ in range(20):
        time.sleep(3)
        if "permit_read_for_background_client" not in {p["name"] for p in cli("list-policies", "--policy-engine-id", eid).get("policies", [])}:
            break
    existing = {}
if "permit_read_for_background_client" not in existing:
    out = cli("create-policy", "--policy-engine-id", eid, "--name", "permit_read_for_background_client",
              "--definition", json.dumps({"cedar": {"statement": STATEMENT}}))
    print("policy created:", out.get("status"), out.get("statusReasons"))
for _ in range(30):
    ps = cli("list-policies", "--policy-engine-id", eid).get("policies", [])
    st = {p["name"]: p["status"] for p in ps}
    if all(v in ("ACTIVE", "CREATE_FAILED", "UPDATE_FAILED") for v in st.values()):
        break
    time.sleep(3)
print("policies:", st)
if any(v != "ACTIVE" for v in st.values()):
    for p in ps:
        print(p["name"], p.get("statusReasons"))
    sys.exit("not attaching: an engine whose policies failed leaves the gateway denying every call")
# the gateway's own role evaluates policies, so it needs these three actions (documented, and missing ones fail at attach time)
role = gw["roleArn"].split("/")[-1]
doc = {"Version": "2012-10-17", "Statement": [
    {"Sid": "PolicyEngineConfiguration", "Effect": "Allow", "Action": "bedrock-agentcore:GetPolicyEngine", "Resource": earn},
    {"Sid": "PolicyEngineAuthorization", "Effect": "Allow",
     "Action": ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"], "Resource": [earn, gw["gatewayArn"]]}]}
subprocess.run(["aws", "iam", "put-role-policy", "--role-name", role, "--policy-name", "policy-engine", "--policy-document", json.dumps(doc)], check=True)
time.sleep(10)
set_gateway({"arn": earn, "mode": mode})
print("attached in mode", mode)
