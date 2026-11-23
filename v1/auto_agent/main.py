"""Automation runtime agent for the v1 checks (V2 and friends).

Runs inside AgentCore Runtime (HTTP contract: POST /invocations, GET /ping on :8080).
The runtime hands the agent a workload access token in the `WorkloadAccessToken` header.
The agent uses it to read the user's stored token from the Token Vault, calls the downstream
function with it, and returns only claims and the downstream result. It never returns a token.

Modes (JSON body):
  {"mode": "use"}                        read the Vault, call downstream            (the unattended path)
  {"mode": "connect"}                    start the 3LO flow, return the consent link
  {"mode": "complete", "sessionUri": ..} bind a finished consent to the user
  {"mode": "probe"}                      report which headers the runtime passed (names only, plus user headers)
  {"mode": "gateway", "calls": [...]}    call tools on the background gateway with the Vault token as the inbound token
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
PROVIDER = os.environ["PROVIDER_NAME"]
SCOPES = os.environ.get("SCOPES", "openid itsm").split()
RETURN_URL = os.environ["RETURN_URL"]
DOWNSTREAM_FN = os.environ["DOWNSTREAM_FN"]
GATEWAY_URL = os.environ.get("GATEWAY_URL")
dp = boto3.client("bedrock-agentcore", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)


def log(**kw):
    print(json.dumps(kw, default=str), flush=True)


def claims(tok):
    p = tok.split(".")[1]
    c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    return {k: c.get(k) for k in ("sub", "preferred_username", "azp", "aud", "scope", "sid", "exp", "act") if k in c}


def user_headers(h):
    return {k: v for k, v in h.items() if "user" in k.lower()}


def vault(wat):
    return dp.get_resource_oauth2_token(
        workloadIdentityToken=wat, resourceCredentialProviderName=PROVIDER, scopes=SCOPES,
        oauth2Flow="USER_FEDERATION", resourceOauth2ReturnUrl=RETURN_URL)


def call_downstream(token):
    r = lam.invoke(FunctionName=DOWNSTREAM_FN,
                   Payload=json.dumps({"headers": {"authorization": f"Bearer {token}"}}).encode())
    resp = json.loads(r["Payload"].read())
    body = json.loads(resp.get("body", "{}"))
    return resp.get("statusCode"), body


def mcp(token, method, params=None, rid=1):
    req = urllib.request.Request(GATEWAY_URL, method="POST", data=json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()[:400]
        out_hdr = e.headers.get("WWW-Authenticate")
        try:
            return status, {**json.loads(raw), "www_authenticate": out_hdr}
        except ValueError:
            return status, {"raw": raw[:400], "www_authenticate": out_hdr}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw[:400]}


def handle(headers, body):
    mode = body.get("mode", "use")
    h = {k: v for k, v in headers.items()}
    lower = {k.lower(): v for k, v in h.items()}
    wat = lower.get("workloadaccesstoken")
    out = {"mode": mode, "has_workload_token": bool(wat)}
    if mode == "probe":
        out["header_names"] = sorted(h)
        out["user_headers"] = user_headers(h)
        return out
    if not wat:
        out["status"] = "no-workload-token"
        return out
    if mode == "complete":
        uid = body.get("userId") or next(iter(user_headers(h).values()), None)
        out["userId_source"] = "payload" if body.get("userId") else ("header" if uid else None)
        if not uid:
            out["status"] = "no-user-id"
            return out
        dp.complete_resource_token_auth(userIdentifier={"userId": uid}, sessionUri=body["sessionUri"])
        out["status"] = "completed"
        return out
    r = vault(wat)
    if not r.get("accessToken"):
        out["status"] = "needs-reauth" if mode == "use" else "consent-required"
        out["authorizationUrl_present"] = bool(r.get("authorizationUrl"))
        if mode == "connect":
            out["authorizationUrl"] = r.get("authorizationUrl")
            out["sessionUri"] = r.get("sessionUri")
        return out
    tok = r["accessToken"]
    out["status"] = "ok"
    out["token_claims"] = claims(tok)
    if mode == "gateway":
        p = tok.split(".")[1]
        full = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        out["token_payload_keys"] = sorted(full)  # claim names only
        out["token_selected"] = {k: full.get(k) for k in ("iss", "typ", "iat", "exp", "auth_time", "aud", "azp", "scope")}
        hdr = tok.split(".")[0]
        out["token_header"] = json.loads(base64.urlsafe_b64decode(hdr + "=" * (-len(hdr) % 4)))
        st, listing = mcp(tok, "tools/list")
        out["tools_list"] = {"http": st, "tools": [t.get("name") for t in listing.get("result", {}).get("tools", [])]
                             if isinstance(listing, dict) else None, "error": listing.get("error") if isinstance(listing, dict) else None,
                             "www_authenticate": listing.get("www_authenticate") if isinstance(listing, dict) else None}
        out["calls"] = []
        for i, name in enumerate(body.get("calls", []), start=2):
            st, res = mcp(tok, "tools/call", {"name": name, "arguments": body.get("arguments", {}).get(name, {})}, rid=i)
            r = res.get("result", {}) if isinstance(res, dict) else {}
            text = " ".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict))[:300]
            out["calls"].append({"tool": name, "http": st, "isError": r.get("isError"), "error": (res.get("error") if isinstance(res, dict) else None), "text": text})
    if mode == "use":
        code, data = call_downstream(tok)
        out["downstream_status"] = code
        out["incidents"] = [i.get("number") for i in data.get("incidents", [])] if isinstance(data, dict) else None
        out["downstream_saw"] = (data.get("caller") if isinstance(data, dict) else None)
    return out


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        raw = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send(200, {"status": "Healthy"})

    def do_POST(self):
        if self.path != "/invocations":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            res = handle(dict(self.headers.items()), body)
            log(event="invocation", mode=res.get("mode"), status=res.get("status"))
            self._send(200, res)
        except Exception as e:  # noqa: BLE001 - report the error class and message, never a token
            log(event="error", error=type(e).__name__, message=str(e)[:300])
            self._send(200, {"status": "error", "error": type(e).__name__, "message": str(e)[:300]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("starting on :8080", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
