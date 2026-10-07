"""Alertes de perturbation (flux GTFS-RT « service alerts »)."""

from __future__ import annotations

import html
import re
import time
import urllib.request
from dataclasses import dataclass, field

from google.transit import gtfs_realtime_pb2

from .gtfs import ssl_context

SERVICE_ALERTS_URL = "https://data.filbleu.fr/ws-tr/gtfs-rt/opendata/service-alerts"

Alert_ = gtfs_realtime_pb2.Alert
EFFECTS = {
    Alert_.NO_SERVICE: "Service interrompu",
    Alert_.REDUCED_SERVICE: "Service réduit",
    Alert_.SIGNIFICANT_DELAYS: "Retards importants",
    Alert_.DETOUR: "Déviation",
    Alert_.ADDITIONAL_SERVICE: "Service supplémentaire",
    Alert_.MODIFIED_SERVICE: "Service modifié",
    Alert_.STOP_MOVED: "Arrêt déplacé",
}
SEVERITIES = {Alert_.INFO: "info", Alert_.WARNING: "warning", Alert_.SEVERE: "severe"}


@dataclass
class Alert:
    id: str
    header: str
    description: str
    url: str
    effect: str  # libellé français, vide si inconnu
    severity: str  # info, warning, severe ou unknown
    periods: list[tuple[int | None, int | None]] = field(default_factory=list)
    route_ids: list[str] = field(default_factory=list)
    stop_ids: list[str] = field(default_factory=list)
    trip_ids: list[str] = field(default_factory=list)
    network_wide: bool = False  # ne vise ni ligne, ni arrêt, ni course

    def active(self, now: float | None = None) -> bool:
        if not self.periods:
            return True
        now = time.time() if now is None else now
        return any((s is None or s <= now) and (e is None or now <= e) for s, e in self.periods)


def _text(translated, lang: str = "fr") -> str:
    """Traduction ``lang`` (sinon la première), convertie en texte brut."""
    translations = list(translated.translation)
    if not translations:
        return ""
    chosen = next((t for t in translations if t.language.lower().startswith(lang)), None)
    if chosen is None:
        chosen = next((t for t in translations if not t.language), translations[0])
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", chosen.text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def parse_alerts(data: bytes) -> list[Alert]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    alerts = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        a = entity.alert
        routes, stops, trips = [], [], []
        for ie in a.informed_entity:
            if ie.route_id:
                routes.append(ie.route_id)
            if ie.stop_id:
                stops.append(ie.stop_id)
            if ie.HasField("trip") and ie.trip.trip_id:
                trips.append(ie.trip.trip_id)
            elif ie.HasField("trip") and ie.trip.route_id:
                routes.append(ie.trip.route_id)
        alerts.append(Alert(
            id=entity.id,
            header=_text(a.header_text),
            description=_text(a.description_text),
            url=_text(a.url),
            effect=EFFECTS.get(a.effect, ""),
            severity=SEVERITIES.get(a.severity_level, "unknown"),
            periods=[(p.start or None, p.end or None) for p in a.active_period],
            route_ids=routes, stop_ids=stops, trip_ids=trips,
            network_wide=not (routes or stops or trips),
        ))
    return alerts


def fetch_alerts(url: str = SERVICE_ALERTS_URL, timeout: int = 15) -> list[Alert]:
    req = urllib.request.Request(url, headers={"User-Agent": "schedules-filbleu"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return parse_alerts(resp.read())


def public(alert: Alert) -> dict:
    """Champs utiles à l'affichage."""
    return {"id": alert.id, "header": alert.header, "description": alert.description,
            "url": alert.url, "effect": alert.effect, "severity": alert.severity}
