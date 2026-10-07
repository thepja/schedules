import zipfile
from datetime import datetime, timedelta

import pytest
from google.transit import gtfs_realtime_pb2

from filbleu.__main__ import format_departures
from filbleu.departures import next_departures, trip_key
from filbleu.gtfs import TIMEZONE, Gtfs
from filbleu.realtime import parse_trip_updates

FILES = {
    "stops.txt": """stop_id,stop_name,location_type,parent_station
JJ,Jean Jaurès,1,
JJ_A,Jean Jaurès,0,JJ
JJ_B,Jean Jaurès,0,JJ
JM,Jean Moulin,0,
GARE,Gare de Tours,0,
TERM,Terminus,0,
""",
    "routes.txt": """route_id,route_short_name,route_long_name,route_type,route_color
TRAM,A,Tram A,0,E30613
BUS2,2,Ligne 2,3,0066CC
""",
    "trips.txt": """route_id,service_id,trip_id,trip_headsign,direction_id
TRAM,WEEK,T1,Terminus,0
TRAM,WEEK,T2,Terminus,0
TRAM,WEEK,T3,Gare de Tours,1
TRAM,WEEKEND,T4,Terminus,0
BUS2,WEEK,T5,Terminus,0
BUS2,HOLIDAY,T6,Terminus,0
""",
    "calendar.txt": """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WEEK,1,1,1,1,1,0,0,20260101,20261231
WEEKEND,0,0,0,0,0,1,1,20260101,20261231
""",
    "calendar_dates.txt": """service_id,date,exception_type
HOLIDAY,20261007,1
""",
    "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type
T1,08:00:00,08:00:00,GARE,1,0
T1,08:05:00,08:05:00,JJ_A,2,0
T1,08:10:00,08:10:00,TERM,3,1
T2,08:10:00,08:10:00,GARE,1,0
T2,08:15:00,08:15:00,JJ_A,2,0
T2,08:20:00,08:20:00,TERM,3,1
T3,08:02:00,08:02:00,TERM,1,0
T3,08:07:00,08:07:00,JJ_B,2,0
T3,08:12:00,08:12:00,GARE,3,1
T4,08:06:00,08:06:00,JJ_A,1,0
T4,08:11:00,08:11:00,TERM,2,1
T5,24:20:00,24:20:00,GARE,1,0
T5,24:30:00,24:30:00,JJ_A,2,0
T5,24:40:00,24:40:00,TERM,3,1
T6,08:40:00,08:40:00,JJ_A,1,0
T6,08:45:00,08:45:00,TERM,2,1
""",
}

NOW = datetime(2026, 10, 7, 8, 1, tzinfo=TIMEZONE)  # un mercredi


def ts(h, m):
    return int(datetime(2026, 10, 7, h, m, tzinfo=TIMEZONE).timestamp())


@pytest.fixture
def gtfs(tmp_path):
    zip_path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)
    return Gtfs.load(str(tmp_path / "cache"), zip_path=str(zip_path))


def make_feed():
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    # T1 : 3 min de retard à la gare, à propager jusqu'à Jean Jaurès.
    e = feed.entity.add(id="1")
    e.trip_update.trip.trip_id = "T1"
    e.trip_update.trip.start_date = "20261007"
    u = e.trip_update.stop_time_update.add(stop_sequence=1, stop_id="GARE")
    u.departure.delay = 180
    # T2 : horaire absolu à Jean Jaurès (+5 min), identifié par stop_id seulement.
    e = feed.entity.add(id="2")
    e.trip_update.trip.trip_id = "T2"
    u = e.trip_update.stop_time_update.add(stop_id="JJ_A")
    u.arrival.time = ts(8, 20)
    # T3 : course supprimée.
    e = feed.entity.add(id="3")
    e.trip_update.trip.trip_id = "T3"
    e.trip_update.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.CANCELED
    # T9 : course ajoutée, inconnue du théorique.
    e = feed.entity.add(id="9")
    e.trip_update.trip.trip_id = "T9"
    e.trip_update.trip.route_id = "BUS2"
    e.trip_update.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.ADDED
    u = e.trip_update.stop_time_update.add(stop_id="JJ_B")
    u.departure.time = ts(8, 30)
    return parse_trip_updates(feed.SerializeToString())


def test_resolve_stop_by_name_groups_platforms(gtfs):
    assert gtfs.resolve_stop_ids("jean jaures") == ("Jean Jaurès", ["JJ_A", "JJ_B"])
    assert gtfs.resolve_stop_ids("JJ_B") == ("Jean Jaurès", ["JJ_B"])
    assert gtfs.resolve_stop_ids("moulin") == ("Jean Moulin", ["JM"])
    with pytest.raises(LookupError, match="Plusieurs"):
        gtfs.resolve_stop_ids("jean")
    with pytest.raises(LookupError, match="Aucun"):
        gtfs.resolve_stop_ids("inconnu")


def test_scheduled_only(gtfs):
    deps = next_departures(gtfs, ["JJ_A", "JJ_B"], {}, now=NOW)
    assert [(d.trip_id, f"{d.expected:%H:%M}", d.realtime) for d in deps] == [
        ("T1", "08:05", False),
        ("T3", "08:07", False),
        ("T2", "08:15", False),
        ("T6", "08:40", False),  # service ajouté par calendar_dates ; T4 (week-end) exclu
    ]


def test_realtime(gtfs):
    deps = next_departures(gtfs, ["JJ_A", "JJ_B"], make_feed(), now=NOW)
    got = [(d.trip_id, d.line, f"{d.expected:%H:%M}", d.realtime, d.canceled) for d in deps]
    assert got == [
        ("T3", "A", "08:07", True, True),
        ("T1", "A", "08:08", True, False),
        ("T2", "A", "08:20", True, False),
        ("T9", "2", "08:30", True, False),
        ("T6", "2", "08:40", False, False),
    ]
    assert deps[1].delay == timedelta(minutes=3)

    text = format_departures("Jean Jaurès", deps, NOW)
    assert "supprimé" in text and "(+3 min)" in text and "théorique" in text


def test_vehicle_already_past_stop(gtfs):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    e = feed.entity.add(id="1")
    e.trip_update.trip.trip_id = "T1"
    e.trip_update.stop_time_update.add(stop_sequence=3).arrival.delay = 0
    deps = next_departures(gtfs, ["JJ_A"], parse_trip_updates(feed.SerializeToString()), now=NOW)
    assert "T1" not in [d.trip_id for d in deps]


def test_line_filter_and_after_midnight(gtfs):
    now = datetime(2026, 10, 8, 0, 20, tzinfo=TIMEZONE)
    deps = next_departures(gtfs, ["JJ_A"], {}, now=now, lines={"2"})
    assert [(d.trip_id, f"{d.expected:%d %H:%M}") for d in deps] == [("T5", "08 00:30")]


def test_trip_key_ignores_dataset_variant():
    gtfs_id = "#JDD-1055#2176801#2408271-Hiver-Sco_14Sept26#0#SEMAINE#289131"
    rt_id = "#JDD-1038-1#2176801#2164643-Hiver-Sco_14Sept26#0#SEMAINE#289131"
    assert trip_key(gtfs_id) == trip_key(rt_id) == "289131"
    assert trip_key("T1") == "T1"


def test_realtime_only(gtfs):
    deps = next_departures(gtfs, ["JJ_A", "JJ_B"], make_feed(), now=NOW, realtime_only=True)
    assert "T6" not in [d.trip_id for d in deps]
    assert all(d.realtime for d in deps)
