"""Flux temps réel GTFS-RT du réseau Fil Bleu."""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field

from google.transit import gtfs_realtime_pb2

from .gtfs import ssl_context

TRIP_UPDATES_URL = "https://data.filbleu.fr/ws-tr/gtfs-rt/opendata/trip-updates"

TripDescriptor = gtfs_realtime_pb2.TripDescriptor
StopTimeUpdate = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate


@dataclass
class StopUpdate:
    stop_sequence: int | None
    stop_id: str | None
    arrival_time: int | None  # timestamp POSIX
    departure_time: int | None
    arrival_delay: int | None  # secondes
    departure_delay: int | None
    skipped: bool = False
    no_data: bool = False

    @property
    def time(self) -> int | None:
        return self.departure_time if self.departure_time is not None else self.arrival_time

    @property
    def delay(self) -> int | None:
        return self.departure_delay if self.departure_delay is not None else self.arrival_delay


@dataclass
class TripUpdate:
    trip_id: str
    route_id: str | None
    start_date: str | None  # AAAAMMJJ
    canceled: bool = False
    added: bool = False
    updates: list[StopUpdate] = field(default_factory=list)


def _event(stu, name):
    if not stu.HasField(name):
        return None, None
    ev = getattr(stu, name)
    return (ev.time if ev.HasField("time") else None,
            ev.delay if ev.HasField("delay") else None)


def parse_trip_updates(data: bytes) -> dict[str, TripUpdate]:
    """Décode un flux GTFS-RT TripUpdates (protobuf) et l'indexe par trip_id."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    result: dict[str, TripUpdate] = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip = tu.trip
        if not trip.trip_id:
            continue
        rel = trip.schedule_relationship
        update = TripUpdate(
            trip_id=trip.trip_id,
            route_id=trip.route_id or None,
            start_date=trip.start_date or None,
            canceled=rel == TripDescriptor.CANCELED,
            added=rel == TripDescriptor.ADDED,
        )
        for stu in tu.stop_time_update:
            arr_time, arr_delay = _event(stu, "arrival")
            dep_time, dep_delay = _event(stu, "departure")
            update.updates.append(StopUpdate(
                stop_sequence=stu.stop_sequence if stu.HasField("stop_sequence") else None,
                stop_id=stu.stop_id or None,
                arrival_time=arr_time or None,
                departure_time=dep_time or None,
                arrival_delay=arr_delay,
                departure_delay=dep_delay,
                skipped=stu.schedule_relationship == StopTimeUpdate.SKIPPED,
                no_data=stu.schedule_relationship == StopTimeUpdate.NO_DATA,
            ))
        result[trip.trip_id] = update
    return result


def fetch_trip_updates(url: str = TRIP_UPDATES_URL, timeout: int = 15) -> dict[str, TripUpdate]:
    req = urllib.request.Request(url, headers={"User-Agent": "schedules-filbleu"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return parse_trip_updates(resp.read())


def trip_update_from_dict(data: dict) -> TripUpdate:
    """Inverse de ``dataclasses.asdict`` : reconstruit une TripUpdate reçue en JSON."""
    data = dict(data)
    updates = [StopUpdate(**u) for u in data.pop("updates", [])]
    return TripUpdate(**data, updates=updates)
