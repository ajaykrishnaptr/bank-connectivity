#!/usr/bin/env bash
# Regenerate the CRL locally and push to the OCSP responder VM.
#
# Flow:
#   1. Run refresh_crl.py to produce certs/crl.crl
#   2. scp it to /tmp on the VM
#   3. ssh + sudo install to /etc/ocsp-responder/crl.crl (perms 644, root:root)
#   4. Fetch the public URL and diff the sha256 to confirm the deploy
#
# Failures at any step abort with non-zero exit so launchd surfaces them.
#
# Env overrides:
#   SSH_HOST    target host           (default: crl.fintnet.ai — uses DNS so
#                                      it survives VM IP changes)
#   SSH_USER    target user           (default: opc)
#   SSH_KEY     identity file         (default: ~/.ssh/oracle_fintnet.key)
#   REMOTE_CRL  destination path      (default: /etc/ocsp-responder/crl.crl)
#   PUBLIC_URL  verification URL      (default: http://crl.fintnet.ai/crl.crl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SSH_HOST="${SSH_HOST:-crl.fintnet.ai}"
SSH_USER="${SSH_USER:-opc}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oracle_fintnet.key}"
REMOTE_CRL="${REMOTE_CRL:-/etc/ocsp-responder/crl.crl}"
PUBLIC_URL="${PUBLIC_URL:-http://crl.fintnet.ai/crl.crl}"

LOCAL_CRL="$SCRIPT_DIR/certs/crl.crl"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
          -o ConnectTimeout=15)

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "Generating new CRL..."
python3 "$SCRIPT_DIR/refresh_crl.py"

LOCAL_HASH=$(shasum -a 256 "$LOCAL_CRL" | awk '{print $1}')
log "Local CRL sha256: $LOCAL_HASH"

log "Uploading to $SSH_USER@$SSH_HOST:/tmp/crl.crl..."
scp "${SSH_OPTS[@]}" "$LOCAL_CRL" "$SSH_USER@$SSH_HOST:/tmp/crl.crl"

log "Installing to $REMOTE_CRL..."
ssh "${SSH_OPTS[@]}" "$SSH_USER@$SSH_HOST" \
    "sudo install -o root -g root -m 644 /tmp/crl.crl '$REMOTE_CRL' && rm -f /tmp/crl.crl"

log "Verifying public URL $PUBLIC_URL..."
REMOTE_HASH=$(curl -sS --max-time 15 "$PUBLIC_URL" | shasum -a 256 | awk '{print $1}')
if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
    log "MISMATCH — local=$LOCAL_HASH remote=$REMOTE_HASH"
    exit 1
fi
log "Verified. CRL is live."
