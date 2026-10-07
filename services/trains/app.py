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

from filbleu.alerts import fetch_alerts, public
from filbleu.departures import next_departures
from filbleu.gtfs import DEFAULT_MAX_AGE, TIMEZONE, Gtfs, normalize
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
ALERTS_URL = os.environ.get(
    "SNCF_ALERTS_URL", "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts")
ALERTS_POLL_SECONDS = 120

# « StopPoint:OCETGV INOUI-87571000 » -> « TGV INOUI »
MODE = re.compile(r"^StopPoint:OCE(.+)-\d+$")
# Les alertes citent « OCESN860594F », début du trip_id GTFS « OCESN860594F1187_F:… ».
TRAIN_KEY = re.compile(r"^OCESN\d+F")

state: dict = {"gtfs": None, "updates": None, "fetched_at": None, "error": None, "alerts": []}


def train_key(trip_id: str) -> str:
    m = TRAIN_KEY.match(trip_id)
    return m.group(0) if m else trip_id


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


async def poll_alerts():
    while True:
        try:
            state["alerts"] = await asyncio.to_thread(fetch_alerts, ALERTS_URL)
        except Exception:
            pass  # on garde les dernières alertes connues
        await asyncio.sleep(ALERTS_POLL_SECONDS)


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
    tasks = [asyncio.create_task(refresh_gtfs()), asyncio.create_task(poll_realtime()),
             asyncio.create_task(poll_alerts())]
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


def station_stop_ids(gtfs: Gtfs) -> list[str]:
    return [r[0] for r in gtfs.db.execute(
        "SELECT stop_id FROM stops WHERE parent_station = ?", (STATION,))] or [STATION]


def stops_after(gtfs: Gtfs, trip_id: str, stop_id: str) -> list[dict]:
    """Arrêts desservis après ``stop_id`` (nom, décalage d'arrivée en secondes depuis
    le départ de ``stop_id``) ; le dernier est la destination."""
    rows = gtfs.db.execute(
        "SELECT st.stop_id, s.stop_name, st.arrival, st.departure FROM stop_times st "
        "JOIN stops s ON s.stop_id = st.stop_id WHERE st.trip_id = ? ORDER BY st.stop_sequence",
        (trip_id,)).fetchall()
    ids = [r["stop_id"] for r in rows]
    if stop_id not in ids:
        return []
    i = ids.index(stop_id)
    origin = rows[i]["departure"]
    return [{"name": r["stop_name"], "offset": r["arrival"] - origin} for r in rows[i + 1:]]


def city_match(query: str, stop_name: str) -> bool:
    """« Paris » correspond à « Paris Austerlitz » ou « Paris Montparnasse 1 et 2 »."""
    q, name = normalize(query), normalize(stop_name)
    return name == q or name.startswith(q + " ") or name.startswith(q + "-")


@app.get("/destinations")
def destinations():
    """Gares desservies après la gare, pour proposer des destinations."""
    gtfs: Gtfs = state["gtfs"]
    ids = station_stop_ids(gtfs)
    rows = gtfs.db.execute(
        f"""
        SELECT DISTINCT s.stop_name FROM stop_times a
        JOIN stop_times b ON b.trip_id = a.trip_id AND b.stop_sequence > a.stop_sequence
        JOIN stops s ON s.stop_id = b.stop_id
        WHERE a.stop_id IN ({",".join("?" * len(ids))})
        ORDER BY s.stop_name
        """, ids).fetchall()
    return [r[0] for r in rows]


@app.get("/departures")
def departures(
    limit: int = Query(15, ge=1, le=50),
    horizon: int = Query(240, ge=10, le=24 * 60),
    realtime_only: bool = False,
    to: str | None = Query(None, description="ville ou gare desservie après le départ"),
):
    gtfs: Gtfs = state["gtfs"]
    station = gtfs.get_stop(STATION)
    updates = realtime_updates()
    now = datetime.now(TIMEZONE)
    if to:
        # Les trains vers une ville donnée sont rares : on cherche plus loin.
        horizon = max(horizon, 16 * 60)
    deps = next_departures(gtfs, station_stop_ids(gtfs), updates or {}, now=now,
                           limit=500 if to else limit, horizon=timedelta(minutes=horizon),
                           realtime_only=realtime_only)
    result = []
    for d in deps:
        after = stops_after(gtfs, d.trip_id, d.stop_id)
        target = next((a for a in after if city_match(to, a["name"])), None) if to else None
        if to and target is None:
            continue
        # Arrivée estimée : même retard qu'au départ.
        arrival = d.expected + timedelta(seconds=target["offset"]) if target else None
        result.append({
            "mode": mode(d.stop_id),
            "number": d.headsign,  # trip_headsign = numéro de train dans le GTFS SNCF
            "line": d.line,
            "line_color": d.line_color,
            "destination": after[-1]["name"] if after else "",
            "via": [a["name"] for a in after[:-1]],
            "arrival": arrival.isoformat() if arrival else None,
            "arrival_stop": target["name"] if target else None,
            "trip_id": d.trip_id,
            "expected": d.expected.isoformat(),
            "scheduled": d.scheduled.isoformat() if d.scheduled else None,
            "delay_seconds": int(d.delay.total_seconds()) if d.delay is not None else None,
            "realtime": d.realtime,
            "canceled": d.canceled,
        })
        if len(result) >= limit:
            break

    # Alertes : celles de la gare et celles des trains affichés, sans doublons
    # (la SNCF répète la même alerte pour chaque train concerné).
    station_ids = set(station_stop_ids(gtfs)) | {STATION}
    shown = {train_key(d["trip_id"]): d for d in result}
    alerts, canonical = [], {}
    for a in state["alerts"]:
        trains = {train_key(t) for t in a.trip_ids} & shown.keys()
        if not a.active() or not (trains or station_ids.intersection(a.stop_ids)):
            continue
        key = (a.header, a.description)
        if key not in canonical:
            canonical[key] = a.id
            alerts.append(public(a))
        for t in trains:
            ids = shown[t].setdefault("alert_ids", [])
            if canonical[key] not in ids:
                ids.append(canonical[key])
    for d in result:
        d.setdefault("alert_ids", [])
    return {
        "station": station.name if station else STATION,
        "to": to,
        "generated_at": now.isoformat(),
        "realtime_available": updates is not None,
        "alerts": alerts,
        "departures": result,
    }
