"""Service des passages : combine le théorique (service GTFS) et le temps réel.

Ne possède aucune donnée : il interroge les deux autres services à chaque requête
et applique la logique de ``filbleu.departures``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query

from filbleu.alerts import Alert, public
from filbleu.departures import LATE_MARGIN, next_departures, trip_key
from filbleu.gtfs import TIMEZONE, ScheduledStop
from filbleu.realtime import trip_update_from_dict
from services.http import request

GTFS_SERVICE = os.environ.get("GTFS_SERVICE_URL", "http://localhost:8001")
REALTIME_SERVICE = os.environ.get("REALTIME_SERVICE_URL", "http://localhost:8002")

app = FastAPI(title="filbleu-departures")
clients = {
    "gtfs": httpx.AsyncClient(base_url=GTFS_SERVICE, timeout=30),
    "realtime": httpx.AsyncClient(base_url=REALTIME_SERVICE, timeout=5),
}
routes_cache: dict[str, dict] = {}


class PrefetchedGtfs:
    """Données théoriques déjà récupérées, avec l'interface attendue par next_departures."""

    def __init__(self, scheduled: list[ScheduledStop], trip_times: dict[str, list[dict]],
                 routes: dict[str, dict]):
        self.scheduled = scheduled
        self.trip_times = trip_times
        self.routes = routes

    def scheduled_departures(self, stop_ids, start, end):
        return self.scheduled

    def trip_stop_times(self, trip_id):
        return self.trip_times.get(trip_id, [])

    def route(self, route_id):
        return self.routes.get(route_id)


async def _get(client: str, path: str, **params):
    try:
        resp = await request(clients[client], "GET", path, params=params)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Service {client} injoignable : {e}")
    if resp.status_code == 404:
        raise HTTPException(404, resp.json().get("detail", "Introuvable"))
    if resp.status_code != 200:
        raise HTTPException(502, f"Service {client} : erreur {resp.status_code}")
    return resp.json()


async def _routes() -> dict[str, dict]:
    if not routes_cache:
        routes_cache.update({r["route_id"]: r for r in await _get("gtfs", "/routes")})
    return routes_cache


async def _trip_updates() -> dict | None:
    """Mises à jour temps réel par trip_id, ou None si le flux est indisponible."""
    try:
        resp = await clients["realtime"].get("/trip-updates")
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return {tu["trip_id"]: trip_update_from_dict(tu) for tu in resp.json()["trip_updates"]}


async def _alerts() -> list[Alert]:
    """Alertes en cours ; liste vide si le service temps réel ne répond pas."""
    try:
        resp = await clients["realtime"].get("/alerts")
    except httpx.HTTPError:
        return []
    return [Alert(**a) for a in resp.json()] if resp.status_code == 200 else []


def relevant_alerts(alerts: list[Alert], stop_ids: set[str], route_ids: set[str],
                    trip_keys: set[str]) -> list[Alert]:
    """Alertes visant tout le réseau, un des arrêts, une des lignes ou une des courses."""
    return [
        a for a in alerts
        if a.network_wide or stop_ids.intersection(a.stop_ids) or route_ids.intersection(a.route_ids)
        or trip_keys.intersection(trip_key(t) for t in a.trip_ids)
    ]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/departures")
async def departures(
    stop: str,
    line: list[str] | None = Query(None),
    realtime_only: bool = False,
    limit: int = Query(10, ge=1, le=50),
    horizon: int = Query(120, ge=5, le=24 * 60),
):
    """Prochains passages à ``stop`` (nom d'arrêt ou stop_id d'un quai)."""
    now = datetime.now(TIMEZONE)
    end = now + timedelta(minutes=horizon)
    resolved, updates, routes, alerts = await asyncio.gather(
        _get("gtfs", "/stops/resolve", q=stop), _trip_updates(), _routes(), _alerts())
    rows = await _get("gtfs", "/scheduled", stop_id=resolved["stop_ids"],
                      start=(now - LATE_MARGIN).isoformat(), end=end.isoformat())
    scheduled = [
        ScheduledStop(r["trip_id"], r["stop_id"], r["stop_sequence"],
                      date.fromisoformat(r["service_date"]), datetime.fromisoformat(r["time"]),
                      r["route_id"], r["headsign"])
        for r in rows
    ]

    # Horaires complets uniquement pour les courses suivies en temps réel.
    trip_times: dict[str, list[dict]] = {}
    if updates:
        live = {trip_key(t) for t in updates}
        tracked = sorted({s.trip_id for s in scheduled if trip_key(s.trip_id) in live})
        if tracked:
            resp = await request(clients["gtfs"], "POST", "/trips/stop-times",
                                 json={"trip_ids": tracked})
            resp.raise_for_status()
            trip_times = resp.json()

    deps = next_departures(
        PrefetchedGtfs(scheduled, trip_times, routes), resolved["stop_ids"], updates or {},
        now=now, limit=limit, horizon=timedelta(minutes=horizon),
        lines={l.lower() for l in line} if line else None, realtime_only=realtime_only,
    )

    def shown(route_id):
        r = routes.get(route_id)
        name = (r["short_name"] or r["long_name"]) if r else route_id
        return not line or (name or "").lower() in {l.lower() for l in line}

    alerts = relevant_alerts(
        alerts,
        set(resolved["stop_ids"]) | set(resolved.get("parent_ids", [])),
        {s.route_id for s in scheduled if shown(s.route_id)},
        {trip_key(s.trip_id) for s in scheduled if shown(s.route_id)},
    )
    trip_alerts = {}
    for a in alerts:
        for t in a.trip_ids:
            trip_alerts.setdefault(trip_key(t), []).append(a.id)
    return {
        "stop_name": resolved["name"],
        "stop_ids": resolved["stop_ids"],
        "generated_at": now.isoformat(),
        "realtime_available": updates is not None,
        "alerts": [public(a) for a in alerts],
        "departures": [
            {
                "line": d.line,
                "line_color": d.line_color,
                "headsign": d.headsign,
                "stop_id": d.stop_id,
                "trip_id": d.trip_id,
                "expected": d.expected.isoformat(),
                "scheduled": d.scheduled.isoformat() if d.scheduled else None,
                "delay_seconds": int(d.delay.total_seconds()) if d.delay is not None else None,
                "realtime": d.realtime,
                "canceled": d.canceled,
                "alert_ids": trip_alerts.get(trip_key(d.trip_id), []),
            }
            for d in deps
        ],
    }
