#!/bin/sh
# After `python login.py` on this PC: push the fresh login to the container (no restart needed; it re-reads within 10 min).
set -e; cd "$(dirname "$0")"; . ./.env
K="-i ${SSH_KEY:-$HOME/.ssh/id_ed25519}"
ssh $K "$DEPLOY_HOST" "umask 077; cat > $REMOTE_DIR/data/tokens.json.new && mv $REMOTE_DIR/data/tokens.json.new $REMOTE_DIR/data/tokens.json" < "$HOME/.systemsurveyor/tokens.json"
echo "token pushed"
