"""launcher: triggered by EventBridge Scheduler with {"task_id": ...}.

1. Read the task record (it holds the user id written while the user was present).
2. Read the grant record. Run only if it is active and not expired.
3. Invoke the automation runtime with runtimeUserId. The runtime reads the user's token from the Vault.
4. Write the outcome back (last_run, needs-reauth).
The launcher never sees a token. It has InvokeAgentRuntime + InvokeAgentRuntimeForUser on one runtime, nothing else in AgentCore.
"""
import hashlib
import json
import os
import time
import uuid

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["REGISTRY"])
RUNTIME_ARN = os.environ["RUNTIME_ARN"]
agent = boto3.client("bedrock-agentcore")


def now():
    return int(time.time())


def record(pk, **fields):
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    TABLE.update_item(Key={"pk": pk}, UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def handler(event, context):
    task_id = event["task_id"]
    task = TABLE.get_item(Key={"pk": f"task#{task_id}"}).get("Item")
    if not task:
        return {"task_id": task_id, "result": "skipped", "reason": "unknown task"}
    user_id = task["user_id"]
    grant = TABLE.get_item(Key={"pk": f"grant#{user_id}"}).get("Item")
    if not grant:
        reason = "no grant"
    elif grant.get("status") != "active":
        reason = f"grant {grant.get('status')}"
    elif int(grant.get("expires_at", 0)) <= now():
        reason = "grant expired"
    else:
        reason = None
    if reason:
        record(f"task#{task_id}", last_run_at=now(), last_result="skipped", last_reason=reason)
        return {"task_id": task_id, "result": "skipped", "reason": reason}
    # CloudTrail data events do not carry runtimeUserId (checked in V3). They do carry runtimeSessionId,
    # so the session id embeds a hash of the user and a run record in the registry maps it back.
    session_id = f"task-{task_id}-{now()}-u{hashlib.sha256(user_id.encode()).hexdigest()[:12]}-{uuid.uuid4().hex}"[:100]
    TABLE.put_item(Item={"pk": f"run#{session_id}", "user_id": user_id, "task_id": task_id, "started_at": now()})
    resp = agent.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=session_id,
        runtimeUserId=user_id, contentType="application/json", accept="application/json",
        payload=json.dumps({"mode": "use", "task_id": task_id}).encode())
    out = json.loads(resp["response"].read())
    status = out.get("status")
    if status == "needs-reauth":
        record(f"grant#{user_id}", status="needs-reauth", needs_reauth_at=now())
    elif status == "ok":
        record(f"grant#{user_id}", last_used_at=now())
    record(f"task#{task_id}", last_run_at=now(), last_result=status or "unknown",
           last_downstream=out.get("downstream_status") or 0)
    return {"task_id": task_id, "result": status, "downstream_status": out.get("downstream_status"),
            "incidents": out.get("incidents")}
