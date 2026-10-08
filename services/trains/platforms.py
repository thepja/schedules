"""Voies des trains, lues dans le flux SIRI Lite « Estimated Timetable » de la SNCF.

Le GTFS et le GTFS-RT SNCF ne publient pas les voies : seul ce flux le fait
(DeparturePlatformName / ArrivalPlatformName), en général peu avant le départ.
Le fichier fait plusieurs dizaines de Mo de XML : on le lit en flux, trajet par
trajet, en ne gardant que les passages à la gare suivie.
"""

from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import IO

from filbleu.gtfs import TIMEZONE, ssl_context

SIRI_ET_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable"

# Code UIC (8 chiffres) en fin de référence d'arrêt :
# « ScheduledStopPoint::87571000 », « StopPoint:Q:87571000:… », « StopArea:OCE87571000 »…
UIC = re.compile(r"(\d{8})(?!.*\d{8})")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

Platforms = dict[tuple[str, date], str]


def uic_of(ref: str | None) -> str | None:
    m = UIC.search(ref or "")
    return m.group(1) if m else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # sans espace de noms


def _child_text(elem: ET.Element, name: str) -> str | None:
    for c in elem:
        if _local(c.tag) == name and c.text and c.text.strip():
            return c.text.strip()
    return None


def _train_numbers(journey: ET.Element) -> set[str]:
    numbers = set()
    for e in journey.iter():
        name = _local(e.tag)
        if name in ("TrainNumberRef", "VehicleJourneyName") and e.text and e.text.strip().isdigit():
            numbers.add(e.text.strip())
    # Ex. « SNCF:2026-10-08:860594:1187:Train » : le numéro figure aussi dans la référence.
    if not numbers:
        parts = (_child_text(journey, "DatedVehicleJourneyRef") or "").split(":")
        for i, p in enumerate(parts[:-1]):
            if DATE.fullmatch(p) and parts[i + 1].isdigit():
                numbers.add(parts[i + 1])
    return numbers


def _calls(journey: ET.Element):
    for e in journey.iter():
        if _local(e.tag) in ("EstimatedCall", "RecordedCall"):
            yield e


def parse_platforms(source: IO[bytes], station_uic: str) -> Platforms:
    """Voies à la gare ``station_uic``, indexées par (numéro de train, jour du passage)."""
    result: Platforms = {}
    for _, elem in ET.iterparse(source, events=("end",)):
        if _local(elem.tag) != "EstimatedVehicleJourney":
            continue
        numbers = _train_numbers(elem)
        for call in _calls(elem):
            if uic_of(_child_text(call, "StopPointRef")) != station_uic:
                continue
            platform = (_child_text(call, "DeparturePlatformName")
                        or _child_text(call, "ArrivalPlatformName"))
            aimed = _child_text(call, "AimedDepartureTime") or _child_text(call, "AimedArrivalTime")
            if not platform or not aimed:
                continue
            day = datetime.fromisoformat(aimed).astimezone(TIMEZONE).date()
            for n in numbers:
                result[(n, day)] = platform
        elem.clear()  # libère la mémoire au fil de la lecture
    return result


def fetch_platforms(station_uic: str, url: str = SIRI_ET_URL, timeout: int = 60) -> Platforms:
    req = urllib.request.Request(url, headers={"User-Agent": "schedules-filbleu",
                                               "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return parse_platforms(resp, station_uic)
