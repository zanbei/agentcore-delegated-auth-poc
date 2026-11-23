"""Helpers to invoke the automation runtime from the shell (admin credentials, for the checks)."""
import json, os, subprocess, tempfile, uuid
import lib

RID = os.environ["AUTO_RUNTIME_ID"]  # printed by deploy_auto_runtime.sh
ARN = f"arn:aws:bedrock-agentcore:{lib.b.REGION}:{lib.b.ACCOUNT}:runtime/{RID}"


def invoke(payload, user_id=None, session=None, role=None, arn=ARN):
    out = tempfile.mktemp()
    args = ["bedrock-agentcore", "invoke-agent-runtime", "--agent-runtime-arn", arn,
            "--runtime-session-id", session or ("v1-" + uuid.uuid4().hex),
            "--payload", json.dumps(payload), "--cli-binary-format", "raw-in-base64-out"]
    if user_id:
        args += ["--runtime-user-id", user_id]
    args.append(out)
    _, err = lib.b.aws(args, role)
    if err:
        return {"cli_error": err}
    try:
        return json.load(open(out))
    except Exception:
        return {"raw": open(out).read()[:400]}
    finally:
        if os.path.exists(out):
            os.remove(out)


def user_id_of(name):
    """The user id string used everywhere in v1: issuer and subject of the verified JWT."""
    _, iss, sub = lib.user(name)
    return f"{iss}#{sub}"


def grant_via_runtime(name):
    """User present: connect -> browser consent -> complete, all through the automation runtime."""
    uid = user_id_of(name)
    c = invoke({"mode": "connect"}, uid)
    if c.get("status") != "consent-required":
        return uid, f"connect: {c.get('status')} {c.get('message', '')}"
    res = lib.browser_consent(c["authorizationUrl"], name)
    if not res["ok"]:
        return uid, f"browser flow failed: {res['steps']}"
    done = invoke({"mode": "complete", "sessionUri": res["query"].get("session_id") or c["sessionUri"], "userId": uid}, uid)
    return uid, done.get("status") if done.get("status") != "error" else done.get("message")
