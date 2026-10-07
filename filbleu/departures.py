"""Calcul des prochains passages à un arrêt, en combinant théorique et temps réel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .gtfs import TIMEZONE, Gtfs, ScheduledStop, service_day_start
from .realtime import StopUpdate, TripUpdate

# Marge pour retrouver les bus en retard dont l'horaire théorique est déjà passé.
LATE_MARGIN = timedelta(minutes=30)

# Les trip_id Fil Bleu (« #JDD-1055#2176801#2408271-Hiver-…#SEMAINE#289131 ») varient
# selon le jeu de données et la période : le flux temps réel peut citer une autre
# variante que celle active dans le GTFS. Seul le numéro de course final est stable.
_FILBLEU_TRIP = re.compile(r"^#JDD-.*#(\d+)$")


def trip_key(trip_id: str) -> str:
    """Identifiant de course stable entre GTFS et temps réel."""
    m = _FILBLEU_TRIP.match(trip_id)
    return m.group(1) if m else trip_id


@dataclass
class Departure:
    line: str
    line_color: str
    headsign: str
    stop_id: str
    trip_id: str
    expected: datetime  # horaire estimé (= théorique sans temps réel)
    scheduled: datetime | None
    realtime: bool
    canceled: bool = False

    @property
    def delay(self) -> timedelta | None:
        if self.scheduled is None or not self.realtime:
            return None
        return self.expected - self.scheduled

    def minutes(self, now: datetime) -> int:
        return max(0, int((self.expected - now).total_seconds() // 60))


class _TripTimes:
    """Horaires théoriques d'une course, pour situer les mises à jour temps réel."""

    def __init__(self, gtfs: Gtfs, trip_id: str):
        rows = gtfs.trip_stop_times(trip_id)
        self.departure = {r["stop_sequence"]: r["departure"] for r in rows}
        self.seq_by_stop: dict[str, int] = {}
        for r in rows:
            self.seq_by_stop.setdefault(r["stop_id"], r["stop_sequence"])

    def sequence(self, upd: StopUpdate) -> int | None:
        if upd.stop_sequence is not None:
            return upd.stop_sequence
        return self.seq_by_stop.get(upd.stop_id)


def _estimate(sched: ScheduledStop, tu: TripUpdate, times: _TripTimes):
    """Renvoie (horaire estimé | None, annulé, temps réel) pour un passage théorique.

    Un horaire None signifie que le bus est déjà passé à cet arrêt.
    """
    if tu.canceled:
        return sched.time, True, True
    base = service_day_start(sched.service_date)
    located = sorted(
        ((seq, u) for u in tu.updates if (seq := times.sequence(u)) is not None),
        key=lambda x: x[0],
    )
    exact = next((u for seq, u in located if seq == sched.stop_sequence), None)
    if exact is not None:
        if exact.skipped:
            return sched.time, True, True
        if exact.no_data:
            return sched.time, False, False
        if exact.time is not None:
            return datetime.fromtimestamp(exact.time, TIMEZONE), False, True
        if exact.delay is not None:
            return sched.time + timedelta(seconds=exact.delay), False, True
        return sched.time, False, False

    previous = [(seq, u) for seq, u in located if seq < sched.stop_sequence]
    if not previous:
        if located:
            # Les mises à jour ne concernent que des arrêts situés après le nôtre :
            # le véhicule a déjà dépassé l'arrêt.
            return None, False, True
        return sched.time, False, False
    # Propagation du retard du dernier arrêt mis à jour avant le nôtre (spec GTFS-RT).
    seq, upd = previous[-1]
    if upd.no_data:
        return sched.time, False, False
    delay = upd.delay
    if delay is None and upd.time is not None and seq in times.departure:
        delay = upd.time - int((base + timedelta(seconds=times.departure[seq])).timestamp())
    if delay is None:
        return sched.time, False, False
    return sched.time + timedelta(seconds=delay), False, True


def next_departures(
    gtfs: Gtfs,  # ou tout objet offrant scheduled_departures, trip_stop_times et route
    stop_ids: list[str],
    trip_updates: dict[str, TripUpdate] | None = None,
    now: datetime | None = None,
    limit: int = 10,
    horizon: timedelta = timedelta(hours=2),
    lines: set[str] | None = None,
    realtime_only: bool = False,
) -> list[Departure]:
    """Prochains passages aux quais ``stop_ids``, triés par horaire estimé."""
    now = now or datetime.now(TIMEZONE)
    trip_updates = {trip_key(t): tu for t, tu in (trip_updates or {}).items()}
    stop_set = set(stop_ids)
    routes: dict[str, tuple[str, str]] = {}

    def route_info(route_id: str | None) -> tuple[str, str]:
        if route_id not in routes:
            r = gtfs.route(route_id) if route_id else None
            routes[route_id] = (r["short_name"] or r["long_name"], r["color"]) if r else (route_id or "?", "")
        return routes[route_id]

    result: list[Departure] = []
    seen_trips: set[str] = set()
    for sched in gtfs.scheduled_departures(stop_ids, now - LATE_MARGIN, now + horizon):
        seen_trips.add(trip_key(sched.trip_id))
        tu = trip_updates.get(trip_key(sched.trip_id))
        if tu is not None and tu.start_date and tu.start_date != sched.service_date.strftime("%Y%m%d"):
            tu = None
        if tu is None:
            expected, canceled, realtime = sched.time, False, False
        else:
            expected, canceled, realtime = _estimate(sched, tu, _TripTimes(gtfs, sched.trip_id))
        if expected is None:
            continue
        line, color = route_info(sched.route_id)
        result.append(Departure(line, color, sched.headsign, sched.stop_id, sched.trip_id,
                                expected, sched.time, realtime, canceled))

    # Courses ajoutées en temps réel, absentes du théorique.
    for key, tu in trip_updates.items():
        if key in seen_trips or not tu.added:
            continue
        for upd in tu.updates:
            if upd.stop_id in stop_set and upd.time is not None and not upd.skipped:
                line, color = route_info(tu.route_id)
                result.append(Departure(line, color, "", upd.stop_id, tu.trip_id,
                                        datetime.fromtimestamp(upd.time, TIMEZONE), None,
                                        True, tu.canceled))
                break

    result = [
        d for d in result
        if d.expected >= now and d.expected <= now + horizon
        and (lines is None or d.line.lower() in lines)
        and (not realtime_only or d.realtime)
    ]
    result.sort(key=lambda d: d.expected)
    return result[:limit]
