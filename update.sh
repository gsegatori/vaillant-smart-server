#!/bin/bash
#
# Deploy / auto-update di vaillant-smart-server su un host Docker.
#
# Prima esecuzione:
#   git clone git@github.com:gsegatori/vaillant-smart-server.git ~/vaillant-smart-server
#   cd ~/vaillant-smart-server && ./update.sh
#
# Aggiornamenti successivi:
#   cd ~/vaillant-smart-server && ./update.sh
#
set -euo pipefail

echo "== Aggiornamento vaillant-smart-server =="

# --- 1) sync codice (auto-stash di eventuali mod locali, es. chmod +x manuale) ---
if [ -d .git ]; then
  echo "[1/6] git pull..."
  if ! git diff --quiet HEAD 2>/dev/null; then
    STASH_MSG="auto-stash-update.sh-$(date +%Y%m%d-%H%M%S)"
    echo "       mod locali presenti, stash come '$STASH_MSG' (recuperabile con git stash list)"
    git stash push -q -m "$STASH_MSG"
  fi
  git pull --ff-only
else
  echo "[1/6] non sono dentro un repo git, salto pull"
fi

# --- 2) .env ---
if [ ! -f .env ]; then
  echo "[2/6] creo .env da .env.example (DA CONFIGURARE: VAILLANT_USER/PASSWORD)"
  cp .env.example .env
  echo "       MODIFICA .env e imposta VAILLANT_USER + VAILLANT_PASSWORD prima di startare"
else
  echo "[2/6] .env esiste, lasciato com'e'"
fi

# --- 3) data dir scrivibile dall'uid 1000 del container ---
mkdir -p data
if [ "$(stat -c '%u' data)" != "1000" ]; then
  echo "[3/6] chown 1000:1000 data (sudo)..."
  sudo chown -R 1000:1000 data
else
  echo "[3/6] data/ gia' owned by uid 1000"
fi

# --- 4) stop container precedente ---
echo "[4/6] stop container precedente..."
docker compose down 2>/dev/null || true

# --- 5) build + up + cleanup immagini orfane ---
echo "[5/6] build + up (host network)..."
docker compose up -d --build
echo "       cleanup immagini dangling..."
docker image prune -f >/dev/null 2>&1 || true

# --- 6) attendi healthy ---
echo "[6/6] attendo healthy..."
for i in $(seq 1 20); do
  status=$(docker compose ps --format '{{.Status}}' 2>/dev/null | head -1 || true)
  case "$status" in
    *"(healthy)"*) echo "       healthy ✓"; break ;;
    *"unhealthy"*) echo "       UNHEALTHY, vedi logs"; docker compose logs --tail 30; exit 1 ;;
  esac
  sleep 2
done

echo
echo "=== status ==="
docker compose ps
echo
LAN_IP=$(ip -4 route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
PORT="${BIND_PORT:-5000}"
if [ -n "${LAN_IP:-}" ]; then
  echo "Endpoint live:"
  echo "   http://${LAN_IP}:${PORT}/healthz"
  echo "   http://${LAN_IP}:${PORT}/zones"
  echo "   http://${LAN_IP}:${PORT}/boiler-consumption-current-month"
  echo "   http://${LAN_IP}:${PORT}/admin/cache  (debug)"
fi

echo
echo "== vaillant-smart-server aggiornato! ✅ =="
