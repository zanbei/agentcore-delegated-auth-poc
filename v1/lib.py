"""Shared helpers for the v1 checks (P0 and later). Reuses the v0 helpers in ../poc.
Needs the same environment as poc/bg3lo.py (AWS_ACCOUNT_ID, KC_ISSUER, SECRETS_FILE).
No access or refresh token is printed or written to disk; only claims and the Keycloak session id (sid).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "poc"))
import bg3lo as b  # noqa: E402
import kc_check as k  # noqa: E402

RETURN_URL = b.RETURN_URL
WORKLOAD = b.WORKLOAD


def user(name):
    """Fresh user JWT and its iss/sub."""
    tok, raw = k.user_token(name)
    if not tok:
        sys.exit(f"user token failed: {raw}")
    c = k.claims(tok)
    return tok, c["iss"], c["sub"]


def wat_for_jwt(tok, workload=WORKLOAD, role=None):
    out, err = b.aws(["bedrock-agentcore", "get-workload-access-token-for-jwt",
                      "--workload-name", workload, "--user-token", tok], role)
    return (out or {}).get("workloadAccessToken"), err


def wat_for_user_id(user_id, workload=WORKLOAD, role=None):
    out, err = b.aws(["bedrock-agentcore", "get-workload-access-token-for-user-id",
                      "--workload-name", workload, "--user-id", user_id], role)
    return (out or {}).get("workloadAccessToken"), err


def vault(wat, provider=b.PROVIDER, scopes=None, role=None, return_url=RETURN_URL):
    args = ["bedrock-agentcore", "get-resource-oauth2-token", "--workload-identity-token", wat,
            "--resource-credential-provider-name", provider, "--scopes", *(scopes or b.SCOPES),
            "--oauth2-flow", "USER_FEDERATION", "--resource-oauth2-return-url", return_url]
    return b.aws(args, role)


def brief(out, err):
    """One-line view of a vault response that never shows a token."""
    if err:
        return f"ERROR {err}"
    if out.get("accessToken"):
        c = b.claims(out["accessToken"])
        return f"TOKEN sid={c.get('sid')} sub={c.get('sub')} azp={c.get('azp')} aud={c.get('aud')}"
    return f"NO TOKEN (authorizationUrl={bool(out.get('authorizationUrl'))}, sessionStatus={out.get('sessionStatus')})"


def browser_consent(auth_url, username, return_url=RETURN_URL):
    """Drive the local CDP Chrome through login and consent. Returns the result dict."""
    r = subprocess.run(["node", os.path.join(HERE, "..", "poc", "cdp_login.mjs"), auth_url, return_url],
                       capture_output=True, text=True, timeout=150,
                       env={**os.environ, "KC_USER": username, "KC_PASS": k.env[f"{username.upper()}_PW"]})
    if not r.stdout.strip():
        sys.exit(f"browser helper produced no output: {r.stderr[-300:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def grant(username, via, workload=WORKLOAD, provider=b.PROVIDER, user_id=None):
    """User present: start 3LO, consent in the browser, bind the session. `via` is 'jwt' or 'userid'.
    Returns the token fingerprint view after binding."""
    tok, iss, sub = user(username)
    if via == "jwt":
        wat, err = wat_for_jwt(tok, workload)
        ident = {"userToken": tok}
    else:
        wat, err = wat_for_user_id(user_id or sub, workload)
        ident = {"userId": user_id or sub}
    if err:
        return f"workload token failed: {err}"
    out, err = vault(wat, provider)
    if err:
        return f"start failed: {err}"
    if out.get("accessToken"):
        return "already granted: " + brief(out, None)
    res = browser_consent(out["authorizationUrl"], username)
    if not res["ok"]:
        return f"browser flow failed: {res['steps']}"
    session_uri = res["query"].get("session_id") or out.get("sessionUri")
    _, err = b.aws(["bedrock-agentcore", "complete-resource-token-auth",
                    "--user-identifier", json.dumps(ident), "--session-uri", session_uri])
    if err:
        return f"CompleteResourceTokenAuth failed: {err}"
    out, err = vault(wat, provider)
    return "granted: " + brief(out, err)
