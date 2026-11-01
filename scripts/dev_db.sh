#!/usr/bin/env bash
# Local Postgres on the Colima engine, isolated from Docker Desktop.
#   scripts/dev_db.sh up      start Colima (without changing the default docker context) + Postgres
#   scripts/dev_db.sh down    stop Postgres and Colima (data is kept)
#   scripts/dev_db.sh status
#   scripts/dev_db.sh reset   delete the local data volume (asks first)
set -euo pipefail
cd "$(dirname "$0")/.."

export DOCKER_CONTEXT=colima          # every docker command below talks to Colima only
COMPOSE=(docker compose -p orderops-p5 -f docker-compose.yml)

case "${1:-status}" in
  up)
    colima status >/dev/null 2>&1 || colima start --activate=false --cpu 2 --memory 1 --disk 10
    "${COMPOSE[@]}" up -d --wait
    ;;
  down)
    "${COMPOSE[@]}" stop || true
    colima stop
    ;;
  status)
    colima status 2>&1 | head -1 || true
    "${COMPOSE[@]}" ps 2>/dev/null || true
    ;;
  reset)
    read -r -p "Delete the local orderops-p5 database volume? [y/N] " ok
    [[ "$ok" == "y" ]] && "${COMPOSE[@]}" down -v
    ;;
  *) echo "usage: $0 up|down|status|reset"; exit 1 ;;
esac
echo "default docker context (unchanged): $(DOCKER_CONTEXT= docker context show)"
