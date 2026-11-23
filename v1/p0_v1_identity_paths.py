"""V1: do the JWT path and the user-id path address the same Token Vault entry?
Grant through one path, read through the other, for two users (one per direction).
"""
import lib
import sys

b, k = lib.b, lib.k


def read_jwt(name):
    tok, _, _ = lib.user(name)
    wat, err = lib.wat_for_jwt(tok)
    return lib.brief(*lib.vault(wat)) if wat else f"WAT failed {err}"


def read_uid(user_id):
    wat, err = lib.wat_for_user_id(user_id)
    return lib.brief(*lib.vault(wat)) if wat else f"WAT failed {err}"


# Direction 1: alice grants through the JWT path
tok, iss, sub = lib.user("alice")
print("## D1: alice grants via JWT path")
print("   ", lib.grant("alice", "jwt"))
print("   read via JWT path          :", read_jwt("alice"))
print("   read via userId=sub        :", read_uid(sub))
for label, uid in [("iss|sub", f"{iss}|{sub}"), ("iss#sub", f"{iss}#{sub}"), ("iss:sub", f"{iss}:{sub}"),
                   ("iss/sub", f"{iss}/{sub}")]:
    print(f"   read via userId={label:8s}:", read_uid(uid))

# Direction 2: bob grants through the user-id path
tokb, issb, subb = lib.user("bob")
print("## D2: bob grants via userId=sub")
print("   ", lib.grant("bob", "userid"))
print("   read via userId=sub        :", read_uid(subb))
print("   read via JWT path          :", read_jwt("bob"))
