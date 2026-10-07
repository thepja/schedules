"""Service trains : départs SNCF (TGV, Intercités, TER) d'une gare, en temps réel.

Utilise le GTFS national SNCF, réduit aux trains desservant la gare, et le flux
GTFS-RT SNCF ; la fusion théorique / temps réel reprend ``filbleu.departures``.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Query

from filbleu.departures import next_departures
from filbleu.gtfs import DEFAULT_MAX_AGE, TIMEZONE, Gtfs
from filbleu.realtime import fetch_trip_updates

GTFS_URL = os.environ.get(
    "SNCF_GTFS_URL", "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip")
RT_URL = os.environ.get(
    "SNCF_RT_URL", "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates")
STATION = os.environ.get("SNCF_STATION", "StopArea:OCE87571000")  # Tours
CACHE_DIR = os.environ.get("FILBLEU_CACHE", ".cache")
GTFS_ZIP = os.environ.get("SNCF_GTFS_ZIP")  # GTFS local (tests, hors ligne)
POLL_SECONDS = int(os.environ.get("SNCF_RT_POLL", "30"))
RT_MAX_AGE = 180

# « StopPoint:OCETGV INOUI-87571000 » -> « TGV INOUI »
MODE = re.compile(r"^StopPoint:OCE(.+)-\d+$")

state: dict = {"gtfs": None, "updates": None, "fetched_at": None, "error": None}


def load_gtfs() -> Gtfs:
    return Gtfs.load(CACHE_DIR, url=GTFS_URL, zip_path=GTFS_ZIP, name="sncf_gtfs",
                     only_stations=[STATION])


async def refresh_gtfs():
    while True:
        await asyncio.sleep(DEFAULT_MAX_AGE)
        try:
            state["gtfs"] = await asyncio.to_thread(load_gtfs)
        except OSError:
            pass  # on garde la version précédente


async def poll_realtime():
    while True:
        try:
            state["updates"] = await asyncio.to_thread(fetch_trip_updates, RT_URL)
            state.update(fetched_at=time.time(), error=None)
        except Exception as e:  # réseau, protobuf invalide...
            state["error"] = str(e)
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["gtfs"] = await asyncio.to_thread(load_gtfs)
    tasks = [asyncio.create_task(refresh_gtfs()), asyncio.create_task(poll_realtime())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="filbleu-trains", lifespan=lifespan)


def realtime_updates():
    fresh = state["fetched_at"] is not None and time.time() - state["fetched_at"] <= RT_MAX_AGE
    return state["updates"] if fresh else None


@app.get("/health")
def health():
    age = None if state["fetched_at"] is None else time.time() - state["fetched_at"]
    return {"status": "ok" if realtime_updates() is not None else "degraded",
            "age_seconds": age, "error": state["error"]}


def mode(stop_id: str) -> str:
    m = MODE.match(stop_id)
    name = m.group(1) if m else ""
    return name.removeprefix("Train ")  # « Train TER » -> « TER »


def stops_after(gtfs: Gtfs, trip_id: str, stop_id: str) -> list[str]:
    """Noms des arrêts desservis après ``stop_id`` ; le dernier est la destination."""
    rows = gtfs.db.execute(
        "SELECT st.stop_id, s.stop_name FROM stop_times st JOIN stops s ON s.stop_id = st.stop_id "
        "WHERE st.trip_id = ? ORDER BY st.stop_sequence", (trip_id,)).fetchall()
    ids = [r["stop_id"] for r in rows]
    start = ids.index(stop_id) + 1 if stop_id in ids else 0
    return [r["stop_name"] for r in rows[start:]]


@app.get("/departures")
def departures(
    limit: int = Query(15, ge=1, le=50),
    horizon: int = Query(240, ge=10, le=24 * 60),
    realtime_only: bool = False,
):
    gtfs: Gtfs = state["gtfs"]
    station = gtfs.get_stop(STATION)
    stop_ids = [r[0] for r in gtfs.db.execute(
        "SELECT stop_id FROM stops WHERE parent_station = ?", (STATION,))] or [STATION]
    updates = realtime_updates()
    now = datetime.now(TIMEZONE)
    deps = next_departures(gtfs, stop_ids, updates or {}, now=now, limit=limit,
                           horizon=timedelta(minutes=horizon), realtime_only=realtime_only)
    result = []
    for d in deps:
        after = stops_after(gtfs, d.trip_id, d.stop_id)
        result.append({
            "mode": mode(d.stop_id),
            "number": d.headsign,  # trip_headsign = numéro de train dans le GTFS SNCF
            "line": d.line,
            "line_color": d.line_color,
            "destination": after[-1] if after else "",
            "via": after[:-1],
            "trip_id": d.trip_id,
            "expected": d.expected.isoformat(),
            "scheduled": d.scheduled.isoformat() if d.scheduled else None,
            "delay_seconds": int(d.delay.total_seconds()) if d.delay is not None else None,
            "realtime": d.realtime,
            "canceled": d.canceled,
        })
    return {
        "station": station.name if station else STATION,
        "generated_at": now.isoformat(),
        "realtime_available": updates is not None,
        "departures": result,
    }
