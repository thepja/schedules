"""Passerelle : sert le site et relaie /api/* vers les services internes.

Seul ce service est exposé publiquement ; les autres restent sur le réseau interne.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from services.http import request as wake_request

SERVICES = {
    "gtfs": os.environ.get("GTFS_SERVICE_URL", "http://localhost:8001"),
    "realtime": os.environ.get("REALTIME_SERVICE_URL", "http://localhost:8002"),
    "departures": os.environ.get("DEPARTURES_SERVICE_URL", "http://localhost:8003"),
    "trains": os.environ.get("TRAINS_SERVICE_URL", "http://localhost:8004"),
}
# Préfixe public -> (service, chemin interne)
ROUTES = {
    "stops": ("gtfs", "/stops"),
    "departures": ("departures", "/departures"),
    "trains": ("trains", ""),  # /api/trains/departures, /api/trains/destinations
}

client = httpx.AsyncClient(timeout=60)
WARMUP_INTERVAL = 60  # secondes
last_warmup = 0.0
background: set[asyncio.Task] = set()


def warm_up():
    """Réveille tous les services en parallèle plutôt qu'en cascade au fil des appels."""
    global last_warmup
    if time.monotonic() - last_warmup < WARMUP_INTERVAL:
        return
    last_warmup = time.monotonic()
    for url in SERVICES.values():
        task = asyncio.create_task(wake_request(client, "GET", f"{url}/health"))
        background.add(task)
        task.add_done_callback(lambda t: (background.discard(t), t.exception()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    warm_up()
    yield


app = FastAPI(title="filbleu-gateway", lifespan=lifespan)


@app.middleware("http")
async def wake_services(request: Request, call_next):
    warm_up()
    return await call_next(request)


@app.get("/api/health")
async def health():
    async def check(name, url):
        try:
            resp = await client.get(f"{url}/health", timeout=3)
            return name, resp.json() if resp.status_code == 200 else {"status": "down"}
        except httpx.HTTPError:
            return name, {"status": "down"}

    results = dict(await asyncio.gather(*(check(n, u) for n, u in SERVICES.items())))
    ok = all(r.get("status") == "ok" for r in results.values())
    return {"status": "ok" if ok else "degraded", "services": results}


@app.get("/api/{prefix}{rest:path}")
async def proxy(prefix: str, rest: str, request: Request):
    if prefix not in ROUTES:
        raise HTTPException(404, "Route inconnue")
    service, path = ROUTES[prefix]
    try:
        resp = await wake_request(client, "GET", f"{SERVICES[service]}{path}{rest}",
                                  params=request.query_params.multi_items())
    except httpx.HTTPError:
        raise HTTPException(502, f"Service {service} injoignable")
    return Response(resp.content, resp.status_code,
                    media_type=resp.headers.get("content-type"),
                    headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"),
                           html=True), name="static")
