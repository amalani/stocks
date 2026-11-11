#!/bin/sh
# Deploy on the NAS: pull the latest code and reconcile the containers.
#   ./deploy.sh              pull + `up -d web` (recreates web only if its compose config changed)
#   ./deploy.sh --run        same, then re-run the screener (fresh Tradier data + page)
#   ./deploy.sh --run-puts   same, but refresh only the Income + Wheel tabs (covered calls carried over)
#   ./deploy.sh --run-cc     same, but refresh only the Covered calls tab (fast: just tickers with shares)
#   ./deploy.sh --rerender   same, then rebuild the saved pages from stored data (no API calls);
#                            use after a template.html-only change
#   ./deploy.sh --rerender-latest   same, but only index.html (old saved pages keep their old template)
#
# Why this is enough:
#   - web (nginx) serves the bind-mounted public/, so new pages show up with no restart.
#   - screener is a one-shot job with ./ mounted, so it runs the freshly pulled screener.py next time.
set -eu

cd "$(dirname "$0")"

git pull --ff-only
docker compose up -d web

case "${1:-}" in
  --run)
    docker compose run --rm screener
    ;;
  --run-puts)
    docker compose run --rm screener sh -c \
      "pip install -q --disable-pip-version-check tzdata && python screener.py --only puts"
    ;;
  --run-cc)
    docker compose run --rm screener sh -c \
      "pip install -q --disable-pip-version-check tzdata && python screener.py --only cc"
    ;;
  --rerender)
    docker compose run --rm screener sh -c \
      "pip install -q --disable-pip-version-check tzdata && python screener.py --rerender"
    ;;
  --rerender-latest)
    docker compose run --rm screener sh -c \
      "pip install -q --disable-pip-version-check tzdata && python screener.py --rerender latest"
    ;;
  "") ;;
  *)
    echo "usage: $0 [--run | --run-puts | --run-cc | --rerender | --rerender-latest]" >&2
    exit 2
    ;;
esac

docker compose ps
