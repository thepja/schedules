"""Le service des passages, branché sur le vrai service GTFS et un flux temps réel simulé."""

from dataclasses import asdict
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from filbleu.gtfs import TIMEZONE
from services.departures import app as departures
from services.gtfs import app as gtfs_service
from test_departures import NOW, gtfs, make_feed  # noqa: F401 (fixture gtfs)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def client(gtfs, monkeypatch):
    gtfs_service.state["gtfs"] = gtfs
    feed = {"fetched_at": 0, "trip_updates": [asdict(tu) for tu in make_feed().values()]}
    monkeypatch.setattr(departures, "datetime", FixedDatetime)
    monkeypatch.setattr(departures, "clients", {
        "gtfs": httpx.AsyncClient(transport=httpx.ASGITransport(app=gtfs_service.app),
                                  base_url="http://gtfs"),
        "realtime": httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=feed)), base_url="http://realtime"),
    })
    monkeypatch.setattr(departures, "routes_cache", {})
    return TestClient(departures.app)


def test_departures_combines_services(client):
    body = client.get("/departures", params={"stop": "Jean Jaurès"}).json()
    assert body["stop_name"] == "Jean Jaurès"
    assert body["realtime_available"] is True
    got = [(d["trip_id"], d["line"], datetime.fromisoformat(d["expected"]).astimezone(TIMEZONE)
            .strftime("%H:%M"), d["realtime"], d["canceled"]) for d in body["departures"]]
    assert got == [
        ("T3", "A", "08:07", True, True),
        ("T1", "A", "08:08", True, False),
        ("T2", "A", "08:20", True, False),
        ("T9", "2", "08:30", True, False),
        ("T6", "2", "08:40", False, False),
    ]
    assert body["departures"][1]["delay_seconds"] == 180


def test_departures_filters(client):
    body = client.get("/departures", params={"stop": "JJ_A", "line": "A",
                                             "realtime_only": "true"}).json()
    assert [d["trip_id"] for d in body["departures"]] == ["T1", "T2"]


def test_unknown_stop(client):
    resp = client.get("/departures", params={"stop": "inconnu"})
    assert resp.status_code == 404
    assert "Aucun arrêt" in resp.json()["detail"]


def test_train_mode():
    from services.trains.app import mode
    assert mode("StopPoint:OCETGV INOUI-87571000") == "TGV INOUI"
    assert mode("StopPoint:OCETrain TER-87571000") == "TER"
    assert mode("StopPoint:OCECar TER-87571000") == "Car TER"
    assert mode("autre") == ""


def test_city_match():
    from services.trains.app import city_match
    assert city_match("Paris", "Paris Austerlitz")
    assert city_match("paris", "Paris Montparnasse Hall 1 - 2")
    assert city_match("Orleans", "Orléans")
    assert not city_match("Paris", "Orléans Avenue de Paris")
    assert not city_match("Tours", "Joué-lès-Tours")
