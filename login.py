"""Run this yourself:  python login.py   (input is hidden, nothing is logged)."""
import getpass, sys
from ssapi import Client, SSError, TOKEN_FILE

c = Client()
print("1) email + password   2) paste refresh token (use this if option 1 complains about captcha)")
mode = input("choice [1]: ").strip() or "1"
try:
    if mode == "1":
        c.login(input("email: ").strip(), getpass.getpass("password: "))
    else:
        print("In app.systemsurveyor.com DevTools console run:  copy(localStorage.refreshToken)")
        c.set_refresh_token(getpass.getpass("refresh token: ").strip())
    print("OK - tokens saved to", TOKEN_FILE)
    print("sites visible:", len(c.sites()))
except SSError as e:
    print("FAILED:", e)
    sys.exit(1)
