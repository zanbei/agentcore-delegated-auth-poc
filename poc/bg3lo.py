"""Reproduce the "server-held, consented, hard-expiring refresh credential" pattern
with AgentCore Identity 3LO + Token Vault.

  python3 bg3lo.py setup      user present: alice logs in + consents in a real browser; app binds the session
  python3 bg3lo.py run        user gone: scheduler role -> ForUserId -> Vault -> downstream; plus negative tests
  python3 bg3lo.py later      fetch again later (silent refresh / hard expiry)

No access or refresh token is printed or written to disk; only claims and hashes.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

import socket

import kc_check as k

socket.setdefaulttimeout(20)  # never hang on a stalled connection

ACCOUNT = os.environ["AWS_ACCOUNT_ID"]
REGION = os.environ.get("AWS_REGION", "us-west-2")
WORKLOAD = "obo-test-bg-agent"
PROVIDER = "obo-test-bg-3lo"
SCOPES = ["openid", "itsm"]
RETURN_URL = os.environ["KC_ISSUER"] + "/.well-known/openid-configuration"  # must be allow-listed on the workload identity
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "bg3lo-state.json")  # task record + token fingerprints, no secrets

_envs = {}


def env_for(role):
    """Environment for running the AWS CLI as `role` (None = my own credentials)."""
    if role is None:
        return dict(os.environ)
    if role not in _envs:
        out = subprocess.run(
            ["aws", "sts", "assume-role", "--role-arn", f"arn:aws:iam::{ACCOUNT}:role/{role}",
             "--role-session-name", "bg3lo-test", "--query", "Credentials", "--output", "json"],
            capture_output=True, text=True, check=True)
        c = json.loads(out.stdout)
        e = dict(os.environ)
        e.pop("AWS_PROFILE", None)
        e.update(AWS_ACCESS_KEY_ID=c["AccessKeyId"], AWS_SECRET_ACCESS_KEY=c["SecretAccessKey"],
                 AWS_SESSION_TOKEN=c["SessionToken"], AWS_REGION=REGION)
        _envs[role] = e
    return _envs[role]


def aws(args, role=None):
    try:
        r = subprocess.run(["aws", *args, "--region", REGION, "--output", "json",
                            "--cli-connect-timeout", "10", "--cli-read-timeout", "30"],
                           capture_output=True, text=True, env=env_for(role), timeout=60)
    except subprocess.TimeoutExpired:
        return None, "aws cli timed out"
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()
        return None, (err[-1] if err else "error")[-220:]
    return (json.loads(r.stdout) if r.stdout.strip() else {}), None


def workload_token(user_id, role=None):
    out, err = aws(["bedrock-agentcore", "get-workload-access-token-for-user-id",
                    "--workload-name", WORKLOAD, "--user-id", user_id], role)
    return (out or {}).get("workloadAccessToken"), err


def vault_token(wat, role=None, return_url=None, session_uri=None):
    args = ["bedrock-agentcore", "get-resource-oauth2-token", "--workload-identity-token", wat,
            "--resource-credential-provider-name", PROVIDER, "--scopes", *SCOPES,
            "--oauth2-flow", "USER_FEDERATION"]
    if return_url:
        args += ["--resource-oauth2-return-url", return_url]
    if session_uri:
        args += ["--session-uri", session_uri]
    return aws(args, role)


def claims(tok):
    p = tok.split(".")[1]
    c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    keep = ["sub", "preferred_username", "azp", "aud", "scope", "sid", "iat", "exp", "act"]
    out = {x: c.get(x) for x in keep if x in c}
    out["fingerprint"] = hashlib.sha256(tok.encode()).hexdigest()[:12]
    return out


def call_downstream(token, role):
    """Invoke the private Lambda over IAM (no public endpoint)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"headers": {"authorization": f"Bearer {token}"}}, f)
        payload = f.name
    out_file = payload + ".out"
    try:
        _, err = aws(["lambda", "invoke", "--function-name", "obo-test-itsm",
                      "--cli-binary-format", "raw-in-base64-out", "--payload", f"file://{payload}", out_file], role)
        if err:
            return None, err
        resp = json.load(open(out_file))
        return resp.get("statusCode"), json.loads(resp.get("body", "{}"))
    finally:
        for p in (payload, out_file):
            if os.path.exists(p):
                os.remove(p)


def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=2)


def fetch_and_call(user_sub, label, role="obo-test-scheduler-role"):
    wat, err = workload_token(user_sub, role)
    if err:
        print(f"   [{label}] ForUserId -> DENIED/ERROR: {err}")
        return None
    out, err = vault_token(wat, role, return_url=RETURN_URL)
    if err:
        print(f"   [{label}] Vault -> ERROR: {err}")
        return None
    if not out.get("accessToken"):
        print(f"   [{label}] Vault returned NO token; user must re-authorize "
              f"(authorizationUrl present: {bool(out.get('authorizationUrl'))}, sessionStatus: {out.get('sessionStatus')})")
        return None
    c = claims(out["accessToken"])
    status, body = call_downstream(out["accessToken"], role)
    print(f"   [{label}] Vault token claims: {c}")
    if isinstance(body, dict):
        print(f"   [{label}] downstream {status}: incidents={[i['number'] for i in body.get('incidents', [])]}, "
              f"saw={ {x: body.get('caller', {}).get(x) for x in ('preferred_username', 'azp', 'act')} if 'caller' in body else body}")
    else:
        print(f"   [{label}] downstream call failed: {body}")
    return c


def setup():
    # The app verifies who is scheduling the task while the user is present.
    tok, _ = k.user_token("alice")
    sub = k.claims(tok)["sub"]
    print(f"## setup: alice is present, sub={sub}")
    wat, err = workload_token(sub)  # app's own credentials
    if err:
        sys.exit(f"workload token failed: {err}")
    out, err = vault_token(wat, return_url=RETURN_URL)
    if err:
        sys.exit(f"start 3LO failed: {err}")
    if out.get("accessToken"):
        print("   vault already holds a token for alice (previous consent)")
    else:
        print(f"   3LO started, sessionStatus={out.get('sessionStatus')}; opening browser for alice ...")
        r = subprocess.run(["node", os.path.join(HERE, "cdp_login.mjs"), out["authorizationUrl"], RETURN_URL],
                           capture_output=True, text=True, timeout=120,
                           env={**os.environ, "KC_USER": "alice", "KC_PASS": k.env["ALICE_PW"]})
        if not r.stdout.strip():
            sys.exit(f"browser helper produced no output: {r.stderr[-300:]}")
        res = json.loads(r.stdout.strip().splitlines()[-1])
        print(f"   browser steps: {res['steps']}")
        if not res["ok"]:
            sys.exit("browser flow did not reach the return URL")
        print(f"   returned to {res['returnedTo']} with query keys {list(res['query'])}")
        session_uri = res["query"].get("session_id") or out.get("sessionUri")
        _, err = aws(["bedrock-agentcore", "complete-resource-token-auth",
                      "--user-identifier", json.dumps({"userId": sub}), "--session-uri", session_uri])
        print(f"   CompleteResourceTokenAuth: {'ok' if not err else err}")
        if err:
            sys.exit("session binding failed, task not saved")
        out, err = vault_token(workload_token(sub)[0], return_url=RETURN_URL)
        print(f"   vault now holds a token for alice: {bool((out or {}).get('accessToken'))} {err or ''}")
        if not (out or {}).get("accessToken"):
            sys.exit("no token in vault after binding, task not saved")
    st = {"task": {"id": "daily-incident-digest", "user_sub": sub, "created_at": int(time.time())}}
    save_state(st)
    print(f"   task record saved: {st['task']}")


def run():
    st = load_state()
    task = st["task"]
    bob_sub = k.claims(k.user_token("bob")[0])["sub"]
    print(f"## run: user is gone. task={task['id']} user_sub={task['user_sub']}")
    first = fetch_and_call(task["user_sub"], "R1 scheduler, alice's task")
    if first:
        st["first"] = first
        save_state(st)
    print("\n## negative tests")
    fetch_and_call(bob_sub, "N1 scheduler, bob's sub (bob never consented)")
    fetch_and_call("alice", "N2 scheduler, userId='alice' instead of sub")
    fetch_and_call(task["user_sub"], "N3 role without ForUserId", role="obo-test-noforuser-role")


def later():
    st = load_state()
    task = st["task"]
    age = int(time.time()) - task["created_at"]
    print(f"## later: {age}s after alice consented (access token lifespan 60s, client session max 300s)")
    c = fetch_and_call(task["user_sub"], f"L t+{age}s")
    if c and st.get("first"):
        print(f"   same token as first fetch? {c['fingerprint'] == st['first']['fingerprint']} "
              f"(first iat={st['first']['iat']}, now iat={c['iat']})")


if __name__ == "__main__":
    {"setup": setup, "run": run, "later": later}[sys.argv[1]]()
