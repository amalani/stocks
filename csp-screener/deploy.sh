#!/bin/sh
# Deploy on the NAS: pull the latest code and reconcile the containers.
#   ./deploy.sh          pull + `up -d web` (recreates web only if its compose config changed)
#   ./deploy.sh --run    same, then re-run the screener so the page picks up code/watchlist changes
#
# Why this is enough:
#   - web (nginx) serves the bind-mounted public/, so new pages show up with no restart.
#   - screener is a one-shot job with ./ mounted, so it runs the freshly pulled screener.py next time.
set -eu

cd "$(dirname "$0")"

git pull --ff-only
docker compose up -d web

if [ "${1:-}" = "--run" ]; then
  docker compose run --rm screener
fi

docker compose ps
