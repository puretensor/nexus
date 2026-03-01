#!/bin/bash
# deploy.sh — Build and deploy Nexus to fox-n1 K3s
#
# Run from tensor-core:
#   cd ~/nexus && bash k8s/deploy.sh
#
# Phases:
#   1. Stage build context on fox-n1
#   2. Build container image (docker build → k3s ctr import)
#   3. Create K8s resources (secrets, configmap, pvcs)
#   4. Seed persistent data (DB, memory, observer state)
#   5. Deploy pod + service

set -euo pipefail

NEXUS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FOX_N1="${FOX_N1:-root@fox-n1}"
BUILD_DIR="/tmp/nexus-build"
VERSION="v2.0.0"

echo "=== Nexus K8s Deploy ==="
echo "Source: $NEXUS_DIR"
echo "Target: $FOX_N1"
echo ""

# -------------------------------------------------------
# Phase 1: Stage build context
# -------------------------------------------------------
echo "[1/5] Staging build context on fox-n1..."

ssh "$FOX_N1" "rm -rf $BUILD_DIR && mkdir -p $BUILD_DIR/claude-bin"

rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='.env' \
  --exclude='nexus.db' --exclude='observers/.state' --exclude='tests' \
  --exclude='k8s' --exclude='.pytest_cache' --exclude='claude-bin' \
  "$NEXUS_DIR/" "$FOX_N1:$BUILD_DIR/"

# Claude CLI binary (resolve symlink)
CLAUDE_BIN="$(readlink -f ~/.local/bin/claude)"
echo "  Copying Claude CLI ($CLAUDE_BIN, $(du -h "$CLAUDE_BIN" | cut -f1))..."
scp "$CLAUDE_BIN" "$FOX_N1:$BUILD_DIR/claude-bin/claude"

# Utility scripts — place where container expects them (HOME=/app)
ssh "$FOX_N1" "mkdir -p $BUILD_DIR/.config/puretensor"
scp ~/.config/puretensor/{gmail,gcalendar,gdrive,privateemail}.py \
  "$FOX_N1:$BUILD_DIR/.config/puretensor/"
# Also copy credentials config if it exists
scp ~/.config/puretensor/privateemail.conf "$FOX_N1:$BUILD_DIR/.config/puretensor/" 2>/dev/null || true
scp ~/.config/puretensor/credentials.json "$FOX_N1:$BUILD_DIR/.config/puretensor/" 2>/dev/null || true

echo "  Build context staged."

# -------------------------------------------------------
# Phase 2: Build container image
# -------------------------------------------------------
echo "[2/5] Building container image on fox-n1..."

ssh "$FOX_N1" "cd $BUILD_DIR && docker build -t nexus:$VERSION . 2>&1" | tail -10

# Import into k3s containerd
echo "  Importing image into k3s containerd..."
ssh "$FOX_N1" "docker save nexus:$VERSION | k3s ctr images import -"

echo "  Image nexus:$VERSION ready."

# -------------------------------------------------------
# Phase 3: Create K8s resources
# -------------------------------------------------------
echo "[3/5] Creating K8s resources..."

# Namespace
scp "$NEXUS_DIR/k8s/namespace.yaml" "$FOX_N1:/tmp/nexus-ns.yaml"
ssh "$FOX_N1" "kubectl apply -f /tmp/nexus-ns.yaml"

# --- Secrets ---
echo "  Creating secrets..."

# nexus-env: use --from-env-file (handles all quoting correctly)
scp "$NEXUS_DIR/.env" "$FOX_N1:/tmp/nexus.env"
ssh "$FOX_N1" "kubectl -n nexus delete secret nexus-env 2>/dev/null || true"
ssh "$FOX_N1" "kubectl -n nexus create secret generic nexus-env --from-env-file=/tmp/nexus.env && rm /tmp/nexus.env"

# Claude CLI credentials
ssh "$FOX_N1" "kubectl -n nexus delete secret claude-credentials 2>/dev/null || true"
scp ~/.claude/.credentials.json "$FOX_N1:/tmp/claude-creds.json"
ssh "$FOX_N1" "kubectl -n nexus create secret generic claude-credentials \
  --from-file=.credentials.json=/tmp/claude-creds.json && rm /tmp/claude-creds.json"

# SSH keys
ssh "$FOX_N1" "kubectl -n nexus delete secret ssh-keys 2>/dev/null || true"
scp ~/.ssh/id_ed25519 "$FOX_N1:/tmp/nexus-ssh-key"
scp ~/.ssh/config.puretensor "$FOX_N1:/tmp/nexus-ssh-config"
ssh "$FOX_N1" "kubectl -n nexus create secret generic ssh-keys \
  --from-file=id_ed25519=/tmp/nexus-ssh-key \
  --from-file=config=/tmp/nexus-ssh-config && \
  rm /tmp/nexus-ssh-key /tmp/nexus-ssh-config"

# OAuth tokens
ssh "$FOX_N1" "kubectl -n nexus delete secret oauth-tokens 2>/dev/null || true"
OAUTH_DIR="$HOME/.config/puretensor/gdrive_tokens"
OAUTH_ARGS=""
for f in "$OAUTH_DIR"/*.json; do
  [ -f "$f" ] || continue
  fname="$(basename "$f")"
  scp "$f" "$FOX_N1:/tmp/nexus-oauth-$fname"
  OAUTH_ARGS="$OAUTH_ARGS --from-file=$fname=/tmp/nexus-oauth-$fname"
done
if [ -n "$OAUTH_ARGS" ]; then
  ssh "$FOX_N1" "kubectl -n nexus create secret generic oauth-tokens $OAUTH_ARGS"
  ssh "$FOX_N1" "rm -f /tmp/nexus-oauth-*.json"
else
  echo "  WARNING: No OAuth tokens found, creating empty secret"
  ssh "$FOX_N1" "kubectl -n nexus create secret generic oauth-tokens"
fi

# ConfigMap (env vars)
scp "$NEXUS_DIR/k8s/configmap.yaml" "$FOX_N1:/tmp/nexus-cm.yaml"
ssh "$FOX_N1" "kubectl apply -f /tmp/nexus-cm.yaml"

# Claude context ConfigMap (CLAUDE.md for Claude CLI fallback)
ssh "$FOX_N1" "kubectl -n nexus delete configmap claude-context 2>/dev/null || true"
if [ -f "$NEXUS_DIR/CLAUDE.md" ]; then
  scp "$NEXUS_DIR/CLAUDE.md" "$FOX_N1:/tmp/nexus-claude-md"
else
  # Create minimal CLAUDE.md from system prompt
  echo "# PureClaw Context" > /tmp/nexus-claude-md
  cat "$NEXUS_DIR/prompts/pureclaw_context.md" >> /tmp/nexus-claude-md 2>/dev/null || true
  scp /tmp/nexus-claude-md "$FOX_N1:/tmp/nexus-claude-md"
  rm /tmp/nexus-claude-md
fi
ssh "$FOX_N1" "kubectl -n nexus create configmap claude-context \
  --from-file=CLAUDE.md=/tmp/nexus-claude-md && rm /tmp/nexus-claude-md"

# Claude memory ConfigMap (project memory for Claude CLI)
ssh "$FOX_N1" "kubectl -n nexus delete configmap claude-memory 2>/dev/null || true"
MEMORY_ARGS=""
if [ -d "$NEXUS_DIR/prompts" ]; then
  for f in "$NEXUS_DIR/prompts"/*.md; do
    [ -f "$f" ] || continue
    fname="$(basename "$f")"
    scp "$f" "$FOX_N1:/tmp/nexus-mem-$fname"
    MEMORY_ARGS="$MEMORY_ARGS --from-file=$fname=/tmp/nexus-mem-$fname"
  done
fi
if [ -n "$MEMORY_ARGS" ]; then
  ssh "$FOX_N1" "kubectl -n nexus create configmap claude-memory $MEMORY_ARGS"
  ssh "$FOX_N1" "rm -f /tmp/nexus-mem-*.md"
else
  ssh "$FOX_N1" "kubectl -n nexus create configmap claude-memory"
fi

# PVCs
scp "$NEXUS_DIR/k8s/pvcs.yaml" "$FOX_N1:/tmp/nexus-pvcs.yaml"
ssh "$FOX_N1" "kubectl apply -f /tmp/nexus-pvcs.yaml"

echo "  K8s resources created."

# -------------------------------------------------------
# Phase 4: Seed persistent data
# -------------------------------------------------------
echo "[4/5] Seeding persistent data..."

# Delete leftover seed pod if present
ssh "$FOX_N1" "kubectl delete pod -n nexus nexus-seed --grace-period=0 2>/dev/null || true"
sleep 2

# Create temp pod with PVC mounted
ssh "$FOX_N1" 'cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: nexus-seed
  namespace: nexus
spec:
  restartPolicy: Never
  containers:
    - name: seed
      image: busybox:1.36
      command: ["sleep", "300"]
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: nexus-data
EOF'

ssh "$FOX_N1" "kubectl -n nexus wait --for=condition=Ready pod/nexus-seed --timeout=120s"

# Copy SQLite DB
echo "  Copying nexus.db..."
scp "$NEXUS_DIR/nexus.db" "$FOX_N1:/tmp/nexus.db"
ssh "$FOX_N1" "kubectl cp /tmp/nexus.db nexus/nexus-seed:/data/nexus.db && rm /tmp/nexus.db"

# Copy memory
if [ -f "$HOME/.hal/memory.json" ]; then
  echo "  Copying memory.json..."
  scp "$HOME/.hal/memory.json" "$FOX_N1:/tmp/memory.json"
  ssh "$FOX_N1" "kubectl exec -n nexus nexus-seed -- mkdir -p /data/hal"
  ssh "$FOX_N1" "kubectl cp /tmp/memory.json nexus/nexus-seed:/data/hal/memory.json && rm /tmp/memory.json"
fi

# Copy observer state
if [ -d "$NEXUS_DIR/observers/.state" ] && [ "$(ls -A "$NEXUS_DIR/observers/.state" 2>/dev/null)" ]; then
  echo "  Copying observer state..."
  ssh "$FOX_N1" "kubectl exec -n nexus nexus-seed -- mkdir -p /data/state/observers"
  for f in "$NEXUS_DIR/observers/.state"/*; do
    [ -f "$f" ] || continue
    fname="$(basename "$f")"
    scp "$f" "$FOX_N1:/tmp/obs-$fname"
    ssh "$FOX_N1" "kubectl cp /tmp/obs-$fname nexus/nexus-seed:/data/state/observers/$fname && rm /tmp/obs-$fname"
  done
fi

# Fix ownership — seed pod runs as root but nexus runs as uid 1000
ssh "$FOX_N1" "kubectl exec -n nexus nexus-seed -- chown -R 1000:1000 /data"

ssh "$FOX_N1" "kubectl delete pod -n nexus nexus-seed --grace-period=0"
echo "  Data seeded."

# -------------------------------------------------------
# Phase 5: Deploy
# -------------------------------------------------------
echo "[5/5] Deploying..."

scp "$NEXUS_DIR/k8s/deployment.yaml" "$FOX_N1:/tmp/nexus-deploy.yaml"
scp "$NEXUS_DIR/k8s/service.yaml" "$FOX_N1:/tmp/nexus-svc.yaml"
ssh "$FOX_N1" "kubectl apply -f /tmp/nexus-deploy.yaml && kubectl apply -f /tmp/nexus-svc.yaml"

echo ""
echo "Waiting for pod to start..."
ssh "$FOX_N1" "kubectl -n nexus rollout status deployment/nexus --timeout=120s" || true

echo ""
echo "=== Deploy complete ==="
ssh "$FOX_N1" "kubectl get pods -n nexus -o wide"
echo ""
echo "Next steps:"
echo "  1. Check logs: ssh fox-n1 'kubectl logs -n nexus deploy/nexus --tail=50'"
echo "  2. Test via Telegram: send 'status' to @puretensor_claude_bot"
echo "  3. Stop TC nexus: sudo systemctl stop nexus && sudo systemctl disable nexus"
echo "  4. Update Gitea webhook to http://YOUR_FOX_N1_IP:30876/"
