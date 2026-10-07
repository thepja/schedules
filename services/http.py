"""Appels HTTP entre services, tolérants au réveil d'un service endormi.

Sur l'offre gratuite de Render, un service inactif s'endort ; pendant son réveil
(jusqu'à ~1 min), le proxy de Render répond 502/503/504 ou la connexion échoue.
"""

from __future__ import annotations

import asyncio
import time

import httpx

WAKE_STATUSES = {502, 503, 504}
WAKE_TIMEOUT = 90  # secondes
RETRY_DELAY = 3


async def request(client: httpx.AsyncClient, method: str, url: str, *,
                  retry_statuses=WAKE_STATUSES, **kwargs) -> httpx.Response:
    """Comme ``client.request``, en réessayant tant que le service se réveille."""
    deadline = time.monotonic() + WAKE_TIMEOUT
    while True:
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code not in retry_statuses:
                return resp
        except httpx.TransportError:
            if time.monotonic() >= deadline:
                raise
        else:
            if time.monotonic() >= deadline:
                return resp
        await asyncio.sleep(RETRY_DELAY)
