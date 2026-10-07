"""Passerelle : sert le site et relaie /api/* vers les services internes.

Seul ce service est exposé publiquement ; les autres restent sur le réseau interne.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

SERVICES = {
    "gtfs": os.environ.get("GTFS_SERVICE_URL", "http://localhost:8001"),
    "realtime": os.environ.get("REALTIME_SERVICE_URL", "http://localhost:8002"),
    "departures": os.environ.get("DEPARTURES_SERVICE_URL", "http://localhost:8003"),
}
# Préfixe public -> (service, chemin interne)
ROUTES = {
    "stops": ("gtfs", "/stops"),
    "departures": ("departures", "/departures"),
}

app = FastAPI(title="filbleu-gateway")
client = httpx.AsyncClient(timeout=15)


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
        resp = await client.get(f"{SERVICES[service]}{path}{rest}",
                                params=request.query_params.multi_items())
    except httpx.HTTPError:
        raise HTTPException(502, f"Service {service} injoignable")
    return Response(resp.content, resp.status_code,
                    media_type=resp.headers.get("content-type"),
                    headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"),
                           html=True), name="static")
