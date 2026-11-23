"""V7: what does revoking at the identity provider actually do to a grant held in the Token Vault?
Grant alice, prime the Vault, revoke at the IdP with one of two admin operations, then poll the unattended path
(runtime 'use' mode: Vault read, then downstream call) until it reports needs-reauth.
Usage: KC_ADMIN_CMD='<command that runs a shell script file on the Keycloak host>' python3 p3_revoke.py logout|consent
  logout   POST /users/{id}/logout            ends the user's sessions (refresh tokens of those sessions stop working)
  consent  DELETE /users/{id}/consents/agent-bg   removes the user's consent for the background client
"""
import os
import shlex
import subprocess
import sys
import tempfile
import time

import lib
import rt

method = sys.argv[1]
admin = shlex.split(os.environ["KC_ADMIN_CMD"])
_, iss, sub = lib.user("alice")
uid = f"{iss}#{sub}"

ops = {"logout": f'$K create users/{sub}/logout -r obo-test -s realm=obo-test',
       "consent": f'$K delete users/{sub}/consents/agent-bg -r obo-test'}
script = f'''. /opt/kc-admin.env
K="docker exec keycloak /opt/keycloak/bin/kcadm.sh"
$K config credentials --server http://localhost:8080 --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1
{ops[method]} && echo revoked
'''


def use():
    r = rt.invoke({"mode": "use"}, uid)
    c = r.get("token_claims") or {}
    return r.get("status"), c.get("exp"), r.get("downstream_status")


print(f"## grant alice, then prime the Vault")
_, st = rt.grant_via_runtime("alice")
print("   grant:", st)
print("   primed:", use())

with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
    f.write(script)
    path = f.name
t_req = time.time()
out = subprocess.run([*admin, path], capture_output=True, text=True, timeout=120)
os.remove(path)
t0 = time.time()
print(f"## revoked with '{method}' (admin call took {int(t0 - t_req)} s): {out.stdout.strip().splitlines()[-1:] or out.stderr.strip()[-200:]}")
last = None
while time.time() - t0 < 240:
    status, exp, down = use()
    now = time.time()
    left = (exp - int(now)) if exp else None
    print(f"   t+{int(now - t0):3d}s status={status} token_exp_in={left}s downstream={down}")
    if status != "ok":
        break
    time.sleep(3)
