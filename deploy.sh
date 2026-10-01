#!/bin/sh
# Build and (re)create the container on a remote Docker host over SSH (works on any Linux box with Docker, incl. Unraid).
# Secrets never touch the image: they go over ssh stdin into a 0600 env file that the container reads at start.
# Config comes from .env (copy .env.example). `./deploy.sh --migrate-token` also copies ~/.systemsurveyor/tokens.json
# into the container's /data (first deploy only - see README "Login").
set -e; cd "$(dirname "$0")"; . ./.env
: "${DEPLOY_HOST:?set DEPLOY_HOST in .env (e.g. root@10.0.0.5)}"
: "${REMOTE_DIR:?set REMOTE_DIR in .env (e.g. /opt/systemsurveyor-mcp)}"
K="-i ${SSH_KEY:-$HOME/.ssh/id_ed25519}"; U=$DEPLOY_HOST; B=$REMOTE_DIR; N=systemsurveyor-mcp; PORT=${PORT:-8797}
envfile() {
  for v in MCP_TOKEN PUBLIC_URL ALLOW_WRITES WRITE_SURVEYS OWNER_USER_ID SS_ACCOUNT_ID SS_TEAM_ID ALERT_WEBHOOK \
           LABOR_RATE CABLE_PER_FT MARKUP_PCT TAX_PCT KEEPALIVE_HOURS MAX_ELEMENTS_PER_WRITE TZ; do
    eval "val=\${$v:-}"
    [ -n "$val" ] && printf '%s=%s\n' "$v" "$val"
  done
  return 0
}
tar czf /tmp/ssmcp.tgz --exclude .env --exclude '*.bak' --exclude __pycache__ --exclude .git .
scp $K -q /tmp/ssmcp.tgz $U:/tmp/ssmcp.tgz && rm /tmp/ssmcp.tgz
envfile | ssh $K $U "set -e; umask 077; mkdir -p $B/secrets $B/data; chmod 700 $B/secrets $B/data
cat > $B/secrets/$N.env.tmp; mv $B/secrets/$N.env.tmp $B/secrets/$N.env"
if [ "${1:-}" = "--migrate-token" ]; then
  ssh $K $U "umask 077; cat > $B/data/tokens.json; chmod 600 $B/data/tokens.json" < "$HOME/.systemsurveyor/tokens.json"
  echo "token migrated"
fi
ssh $K $U "set -e; rm -rf $B/build && mkdir -p $B/build && tar xzf /tmp/ssmcp.tgz -C $B/build && rm /tmp/ssmcp.tgz
cd $B/build && docker build -q -t $N:latest . >/dev/null
docker rm -f $N >/dev/null 2>&1 || true
docker run -d --name $N --restart=unless-stopped -p $PORT:8797/tcp -v $B/data:/data:rw --env-file $B/secrets/$N.env $N:latest >/dev/null
docker image prune -f >/dev/null; sleep 5; docker logs --tail 3 $N; curl -s localhost:$PORT/health; echo"
