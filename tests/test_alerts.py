from google.transit import gtfs_realtime_pb2 as pb

from filbleu.alerts import parse_alerts
from services.departures.app import relevant_alerts


def make_alerts():
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    a = feed.entity.add(id="travaux").alert
    a.informed_entity.add(route_id="TRAM")
    a.effect = pb.Alert.DETOUR
    a.severity_level = pb.Alert.WARNING
    a.header_text.translation.add(text="Bauarbeiten", language="de")
    a.header_text.translation.add(text="Travaux", language="fr")
    a.description_text.translation.add(text="<p>Ligne A d&eacute;vi&eacute;e.</p><p>Bus de substitution.</p>",
                                       language="fr")
    p = a.active_period.add(start=1000, end=2000)
    b = feed.entity.add(id="greve").alert
    b.header_text.translation.add(text="Grève")
    c = feed.entity.add(id="course").alert
    c.informed_entity.add().trip.trip_id = "T1"
    c.header_text.translation.add(text="Retard", language="fr")
    return parse_alerts(feed.SerializeToString())


def test_parse_alerts():
    travaux, greve, course = make_alerts()
    assert travaux.header == "Travaux"  # traduction française, pas la première
    assert travaux.description == "Ligne A déviée.\nBus de substitution."
    assert (travaux.effect, travaux.severity) == ("Déviation", "warning")
    assert travaux.route_ids == ["TRAM"] and not travaux.network_wide
    assert travaux.active(1500) and not travaux.active(2500)
    assert greve.network_wide and greve.active()
    assert course.trip_ids == ["T1"]


def test_relevant_alerts():
    alerts = make_alerts()
    ids = lambda **kw: [a.id for a in relevant_alerts(alerts, **kw)]
    assert ids(stop_ids=set(), route_ids={"TRAM"}, trip_keys=set()) == ["travaux", "greve"]
    assert ids(stop_ids=set(), route_ids={"BUS2"}, trip_keys={"T1"}) == ["greve", "course"]
