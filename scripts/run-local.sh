#!/bin/sh
# Lance les cinq services sans Docker ; le site est sur http://localhost:8000.
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python
export PYTHONPATH=. FILBLEU_CACHE=.cache

trap 'kill 0' INT TERM EXIT
$PY -m uvicorn services.gtfs.app:app --port 8001 &
$PY -m uvicorn services.realtime.app:app --port 8002 &
$PY -m uvicorn services.departures.app:app --port 8003 &
$PY -m uvicorn services.trains.app:app --port 8004 &
$PY -m uvicorn services.gateway.app:app --port 8000 &
wait
