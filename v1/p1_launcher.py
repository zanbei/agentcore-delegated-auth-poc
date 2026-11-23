"""P1: unattended run through EventBridge Scheduler -> launcher -> automation runtime -> Vault -> downstream.
Covers: happy path, hard cap (token gone after the IdP session cap), a user who never consented,
a disabled grant (launcher must not invoke the runtime).
"""
import datetime
import json
import os
import time

import lib
import rt

b = lib.b
ACCOUNT, REGION = b.ACCOUNT, b.REGION
TABLE = "obo-test-v1-registry"
FN = "obo-test-v1-launcher"
FN_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FN}"
SCH_ROLE = f"arn:aws:iam::{ACCOUNT}:role/obo-test-v1-scheduler-invoke-role"


def ddb_put(item):
    _, err = b.aws(["dynamodb", "put-item", "--table-name", TABLE, "--item", json.dumps(item)])
    if err:
        raise SystemExit(f"put-item failed: {err}")


def ddb_get(pk):
    out, err = b.aws(["dynamodb", "get-item", "--table-name", TABLE, "--key", json.dumps({"pk": {"S": pk}})])
    item = (out or {}).get("Item", {})
    return {k: list(v.values())[0] for k, v in item.items()}


def seed(uid, task_id, status="active", ttl=7 * 86400):
    ddb_put({"pk": {"S": f"grant#{uid}"}, "status": {"S": status}, "expires_at": {"N": str(int(time.time()) + ttl)},
             "confirm_text_version": {"S": "v1"}})
    ddb_put({"pk": {"S": f"task#{task_id}"}, "user_id": {"S": uid}})


def trigger(task_id):
    out, err = b.aws(["lambda", "invoke", "--function-name", FN, "--cli-binary-format", "raw-in-base64-out",
                      "--payload", json.dumps({"task_id": task_id}), "/tmp/v1-launcher-out.json"])
    if err:
        return {"error": err}
    return json.load(open("/tmp/v1-launcher-out.json"))


def show(label, task_id, uid):
    t, g = ddb_get(f"task#{task_id}"), ddb_get(f"grant#{uid}")
    print(f"   {label}: task.last_result={t.get('last_result')} reason={t.get('last_reason')} "
          f"downstream={t.get('last_downstream')} | grant.status={g.get('status')}")


alice = rt.user_id_of("alice")
bob = rt.user_id_of("bob")

print("## grant alice through the runtime (user present)")
_, st = rt.grant_via_runtime("alice")
print("   ", st)
t0 = time.time()
seed(alice, "digest-alice")
seed(bob, "digest-bob")

print("## scheduled run (EventBridge Scheduler, one-time, ~90 s from now)")
at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S")
name = f"obo-test-v1-digest-{int(time.time())}"
_, err = b.aws(["scheduler", "create-schedule", "--name", name, "--schedule-expression", f"at({at})",
                "--schedule-expression-timezone", "UTC", "--flexible-time-window", "Mode=OFF",
                "--action-after-completion", "DELETE",
                "--target", json.dumps({"Arn": FN_ARN, "RoleArn": SCH_ROLE, "Input": json.dumps({"task_id": "digest-alice"})})])
print("   schedule created:", err or name, "at", at, "UTC")
time.sleep(110)
show("after scheduled run", "digest-alice", alice)

print("## user who never consented under this runtime (bob)")
print("   launcher:", trigger("digest-bob"))
show("bob", "digest-bob", bob)

wait = 330 - (time.time() - t0)
print(f"## hard cap: wait {int(max(wait, 0))} s so that the IdP session cap (300 s) passes")
time.sleep(max(wait, 0))
print("   launcher:", trigger("digest-alice"))
show("after cap", "digest-alice", alice)
print("   launcher again (grant is now needs-reauth, runtime must not be invoked):", trigger("digest-alice"))

print("## disabled grant")
seed(alice, "digest-alice", status="disabled")
print("   launcher:", trigger("digest-alice"))
show("disabled", "digest-alice", alice)
