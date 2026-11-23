"""How fresh is the token the Vault hands out, compared with the gateway's rejection of tokens with under about a minute left?
Grants alice, then polls the runtime's gateway mode and prints, per poll, the remaining validity of the Vault token and the gateway result.
"""
import sys
import time

import rt

alice = rt.user_id_of("alice")
_, st = rt.grant_via_runtime("alice")
print("grant:", st)
if st != "completed":
    sys.exit("no fresh consent (an earlier session may still be active); wait for it to expire and rerun")
t0 = time.time()
seen = []
while time.time() - t0 < 150:
    r = rt.invoke({"mode": "gateway", "calls": []}, alice)
    sel = r.get("token_selected") or {}
    now = int(time.time())
    seen.append((int(now - t0), sel.get("iat"), (sel.get("exp") or 0) - now if sel.get("exp") else None, r.get("tools_list", {}).get("http"), r.get("status")))
    time.sleep(2)
prev = None
for t, iat, left, http, status in seen:
    print(f"t+{t:3d}s iat={iat} remaining={left}s gateway={http} status={status}{'  <- new token' if iat != prev else ''}")
    prev = iat
