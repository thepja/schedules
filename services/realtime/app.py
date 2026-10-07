"""Service temps réel : interroge le flux GTFS-RT Fil Bleu et garde la dernière version.

Les autres services lisent ce cache plutôt que de solliciter le flux à chaque requête.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException

from filbleu.realtime import TRIP_UPDATES_URL, fetch_trip_updates

RT_URL = os.environ.get("FILBLEU_RT_URL", TRIP_UPDATES_URL)
POLL_SECONDS = int(os.environ.get("FILBLEU_RT_POLL", "20"))
# Au-delà, les données sont trop anciennes pour être présentées comme du temps réel.
MAX_AGE = int(os.environ.get("FILBLEU_RT_MAX_AGE", "120"))

state: dict = {"trip_updates": None, "fetched_at": None, "error": None}


async def poll():
    while True:
        try:
            updates = await asyncio.to_thread(fetch_trip_updates, RT_URL)
            state.update(trip_updates=[asdict(tu) for tu in updates.values()],
                         fetched_at=time.time(), error=None)
        except Exception as e:  # réseau, protobuf invalide...
            state["error"] = str(e)
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll())
    yield
    task.cancel()


app = FastAPI(title="filbleu-realtime", lifespan=lifespan)


def _age() -> float | None:
    return None if state["fetched_at"] is None else time.time() - state["fetched_at"]


@app.get("/health")
def health():
    age = _age()
    return {"status": "ok" if age is not None and age <= MAX_AGE else "degraded",
            "age_seconds": age, "error": state["error"]}


@app.get("/trip-updates")
def trip_updates():
    age = _age()
    if age is None or age > MAX_AGE:
        raise HTTPException(503, state["error"] or "Flux temps réel pas encore reçu")
    return {"fetched_at": state["fetched_at"], "trip_updates": state["trip_updates"]}
