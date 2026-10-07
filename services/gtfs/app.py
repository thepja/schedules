"""Service GTFS : données théoriques (arrêts, lignes, horaires) du réseau Fil Bleu.

Télécharge le GTFS au démarrage puis toutes les 24 h, et l'expose en JSON.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from filbleu.gtfs import DEFAULT_MAX_AGE, GTFS_URL, Gtfs

CACHE_DIR = os.environ.get("FILBLEU_CACHE", ".cache")
GTFS_URL = os.environ.get("FILBLEU_GTFS_URL", GTFS_URL)
GTFS_ZIP = os.environ.get("FILBLEU_GTFS_ZIP")  # GTFS local (tests, hors ligne)

state: dict[str, Gtfs] = {}


def gtfs() -> Gtfs:
    return state["gtfs"]


def load() -> Gtfs:
    return Gtfs.load(CACHE_DIR, url=GTFS_URL, zip_path=GTFS_ZIP)


async def refresh_daily():
    while True:
        await asyncio.sleep(DEFAULT_MAX_AGE)
        try:
            state["gtfs"] = await asyncio.to_thread(load)
        except OSError:
            pass  # on garde la version précédente


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["gtfs"] = await asyncio.to_thread(load)
    task = asyncio.create_task(refresh_daily())
    yield
    task.cancel()


app = FastAPI(title="filbleu-gtfs", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stops/search")
def search_stops(q: str = Query(min_length=2), limit: int = 15):
    """Noms d'arrêts correspondant à ``q`` (un nom regroupe tous ses quais)."""
    names: list[str] = []
    for s in gtfs().search_stops(q):
        if s.name not in names:
            names.append(s.name)
    return [{"name": n} for n in names[:limit]]


def _resolve(q: str) -> tuple[str, list[str]]:
    try:
        return gtfs().resolve_stop_ids(q)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/stops/resolve")
def resolve_stop(q: str):
    name, stop_ids = _resolve(q)
    return {"name": name, "stop_ids": stop_ids}


@app.get("/stops/directions")
def stop_directions(q: str):
    """Pour chaque quai de l'arrêt : lignes desservies et leurs destinations."""
    name, stop_ids = _resolve(q)
    placeholders = ",".join("?" * len(stop_ids))
    rows = gtfs().db.execute(
        f"""
        SELECT DISTINCT st.stop_id, r.route_id, r.short_name, r.long_name, r.color,
                        r.text_color, t.headsign
        FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        JOIN trip_last_stop l ON l.trip_id = st.trip_id
        WHERE st.stop_id IN ({placeholders})
          AND st.pickup_type != 1
          AND st.stop_sequence < l.stop_sequence
        """,
        stop_ids,
    ).fetchall()
    directions: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["stop_id"], r["route_id"])
        d = directions.setdefault(key, {
            "stop_id": r["stop_id"],
            "line": r["short_name"] or r["long_name"],
            "color": r["color"] or "",
            "text_color": r["text_color"] or "",
            "headsigns": [],
        })
        if r["headsign"] and r["headsign"] not in d["headsigns"]:
            d["headsigns"].append(r["headsign"])
    result = sorted(directions.values(), key=lambda d: (d["line"].zfill(4), d["stop_id"]))
    for d in result:
        d["headsigns"].sort()
    return {"name": name, "stop_ids": stop_ids, "directions": result}


@app.get("/routes")
def routes():
    return [dict(r) for r in gtfs().db.execute("SELECT * FROM routes")]


@app.get("/scheduled")
def scheduled(stop_id: list[str] = Query(), start: datetime = Query(),
              end: datetime = Query()):
    """Passages théoriques aux quais ``stop_id`` entre ``start`` et ``end`` (ISO 8601)."""
    return [
        {
            "trip_id": s.trip_id,
            "stop_id": s.stop_id,
            "stop_sequence": s.stop_sequence,
            "service_date": s.service_date.isoformat(),
            "time": s.time.isoformat(),
            "route_id": s.route_id,
            "headsign": s.headsign,
        }
        for s in gtfs().scheduled_departures(stop_id, start, end)
    ]


class TripIds(BaseModel):
    trip_ids: list[str]


@app.post("/trips/stop-times")
def trip_stop_times(body: TripIds):
    """Horaires théoriques complets de plusieurs courses, indexés par trip_id."""
    return {t: [dict(r) for r in gtfs().trip_stop_times(t)] for t in body.trip_ids}
