"""Print the ids the server needs for .env: OWNER_USER_ID, SS_ACCOUNT_ID, SS_TEAM_ID. Run after login.py (python whoami.py)."""
import os
from ssapi import Client

c = Client()
u = c.ctx()
print("OWNER_USER_ID =", u["id"], f'({u.get("first_name", "")} {u.get("last_name", "")})')
accts = u.get("accounts", [])
for a in accts:
    print("SS_ACCOUNT_ID =", a["id"], f'({a.get("company", "")})')
if accts:
    os.environ.setdefault("SS_ACCOUNT_ID", str(accts[0]["id"]))
    for s in c.sites()[:5]:
        for sv in c.surveys(s["id"])[:1]:
            print("SS_TEAM_ID =", c.survey(sv["id"]).get("team_id"), "(from survey:", sv.get("title"), ")")
            raise SystemExit
print("No surveys found to read a team id from; open the web app, create one survey, and run this again.")
