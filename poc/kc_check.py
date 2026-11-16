"""Manual token-exchange check against Keycloak (no AgentCore involved)."""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ["KC_ISSUER"]  # e.g. https://<host>/realms/<realm>
TOKEN_URL = f"{BASE}/protocol/openid-connect/token"
SECRETS_FILE = os.environ.get("SECRETS_FILE", "secrets.env")  # AGENT_SECRET, ALICE_PW, BOB_PW, BG_SECRET
env = dict(l.strip().split("=", 1) for l in open(os.path.expanduser(SECRETS_FILE)) if "=" in l)


def post(data, auth=None):
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:400]}


def claims(tok):
    p = tok.split(".")[1]
    d = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    keep = ["iss", "sub", "aud", "azp", "preferred_username", "scope", "act"]
    return {k: d.get(k) for k in keep if k in d}


def user_token(name):
    r = post({"grant_type": "password", "client_id": "user-cli", "username": name,
              "password": env[f"{name.upper()}_PW"], "scope": "openid"})
    return r.get("access_token"), r


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "alice"
    tok, raw = user_token(name)
    if not tok:
        print("user token failed:", raw); sys.exit(1)
    print("user token:", claims(tok))
    ex = post({"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
               "subject_token": tok,
               "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
               "scope": "itsm"}, auth=f"agent-app:{env['AGENT_SECRET']}")
    if "access_token" not in ex:
        print("exchange failed:", ex); sys.exit(1)
    print("exchanged  :", claims(ex["access_token"]))


def call_api(token, url=None):
    url = url or os.environ["DOWNSTREAM_URL"]  # only for direct checks when the API is reachable over HTTP
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
