#!/bin/sh
# When the phone says "System Surveyor login expired": run this one command.
# It logs in on this PC (you type the captcha-proof refresh token or password), pushes the login to the container,
# then removes the local copy so only the container ever refreshes the token (two copies would knock each other out).
set -e
cd "$(dirname "$0")"
python login.py
./push_token.sh
rm -f "$HOME/.systemsurveyor/tokens.json"
echo "Done. The container picks it up within 10 minutes and sends a 'back' push."
