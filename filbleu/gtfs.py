"""Données théoriques (GTFS) du réseau Fil Bleu.

Le fichier GTFS est téléchargé puis converti en base SQLite dans le dossier de
cache, afin de pouvoir interroger rapidement les horaires d'un arrêt sans
recharger tout le fichier à chaque appel.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import ssl
import time
import unicodedata
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Lien stable de transport.data.gouv.fr, redirige vers la dernière version du GTFS.
GTFS_URL = "https://transport.data.gouv.fr/resources/80694/download"
TIMEZONE = ZoneInfo("Europe/Paris")
DEFAULT_MAX_AGE = 24 * 3600


def ssl_context() -> ssl.SSLContext:
    """Contexte TLS utilisant les certificats de certifi s'il est installé.

    Le Python de python.org sous macOS n'a pas accès aux certificats système.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())

# À incrémenter quand SCHEMA change : les bases existantes sont alors reconstruites.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE stops (
    stop_id TEXT PRIMARY KEY, stop_name TEXT, norm_name TEXT,
    location_type INTEGER, parent_station TEXT, lat REAL, lon REAL
);
CREATE TABLE routes (
    route_id TEXT PRIMARY KEY, short_name TEXT, long_name TEXT,
    color TEXT, text_color TEXT
);
CREATE TABLE trips (
    trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT,
    headsign TEXT, direction_id INTEGER, shape_id TEXT
);
CREATE TABLE shapes (shape_id TEXT, seq INTEGER, lat REAL, lon REAL);
CREATE TABLE calendar (
    service_id TEXT PRIMARY KEY, days TEXT, start_date TEXT, end_date TEXT
);
CREATE TABLE calendar_dates (service_id TEXT, date TEXT, exception_type INTEGER);
CREATE TABLE stop_times (
    trip_id TEXT, stop_id TEXT, stop_sequence INTEGER,
    arrival INTEGER, departure INTEGER, pickup_type INTEGER
);
CREATE TABLE trip_last_stop (trip_id TEXT PRIMARY KEY, stop_sequence INTEGER);
"""

INDEXES = """
CREATE INDEX stop_times_stop ON stop_times (stop_id);
CREATE INDEX stop_times_trip ON stop_times (trip_id, stop_sequence);
CREATE INDEX calendar_dates_date ON calendar_dates (date);
CREATE INDEX shapes_shape ON shapes (shape_id, seq);
CREATE INDEX trips_route ON trips (route_id);
"""


def normalize(text: str) -> str:
    """Minuscules sans accents, pour la recherche d'arrêt par nom."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def parse_gtfs_time(value: str) -> int | None:
    """'25:10:00' -> secondes depuis le début du jour de service."""
    if not value:
        return None
    h, m, s = value.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def service_day_start(day: date) -> datetime:
    """Référence des horaires GTFS : midi moins 12h (gère les changements d'heure)."""
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=TIMEZONE)
    return noon - timedelta(hours=12)


def _rows(zf: zipfile.ZipFile, name: str):
    if name not in zf.namelist():
        return
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _station_trips(zf: zipfile.ZipFile, stations: set[str]) -> set[str]:
    """Courses desservant un des arrêts ``stations`` (ou un de leurs quais)."""
    stop_ids = {r["stop_id"] for r in _rows(zf, "stops.txt")
                if r["stop_id"] in stations or r.get("parent_station") in stations}
    return {r["trip_id"] for r in _rows(zf, "stop_times.txt") if r["stop_id"] in stop_ids}


def build_database(zip_path: str, db_path: str, only_stations: list[str] | None = None) -> None:
    """Convertit un fichier GTFS (zip) en base SQLite.

    ``only_stations`` limite la base aux courses desservant ces arrêts : utile pour
    un GTFS national dont on ne consulte qu'une gare.
    """
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    db = sqlite3.connect(tmp_path)
    db.executescript(SCHEMA)
    with zipfile.ZipFile(zip_path) as zf:
        keep = _station_trips(zf, set(only_stations)) if only_stations else None
        db.executemany(
            "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (r["stop_id"], r.get("stop_name", ""), normalize(r.get("stop_name", "")),
                 _int(r.get("location_type"), 0), r.get("parent_station") or None,
                 _float(r.get("stop_lat")), _float(r.get("stop_lon")))
                for r in _rows(zf, "stops.txt")
            ),
        )
        db.executemany(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
            (
                (r["route_id"], r.get("route_short_name", ""), r.get("route_long_name", ""),
                 r.get("route_color", ""), r.get("route_text_color", ""))
                for r in _rows(zf, "routes.txt")
            ),
        )
        shape_ids: set[str] = set()

        def trips():
            for r in _rows(zf, "trips.txt"):
                if keep is None or r["trip_id"] in keep:
                    if r.get("shape_id"):
                        shape_ids.add(r["shape_id"])
                    yield (r["trip_id"], r["route_id"], r["service_id"], r.get("trip_headsign", ""),
                           _int(r.get("direction_id")), r.get("shape_id") or None)

        db.executemany("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)", trips())
        db.executemany(
            "INSERT INTO shapes VALUES (?, ?, ?, ?)",
            (
                (r["shape_id"], _int(r.get("shape_pt_sequence"), 0), _float(r["shape_pt_lat"]),
                 _float(r["shape_pt_lon"]))
                for r in _rows(zf, "shapes.txt") if r["shape_id"] in shape_ids
            ),
        )
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        db.executemany(
            "INSERT INTO calendar VALUES (?, ?, ?, ?)",
            (
                (r["service_id"], "".join(r.get(d, "0") or "0" for d in weekdays),
                 r["start_date"], r["end_date"])
                for r in _rows(zf, "calendar.txt")
            ),
        )
        db.executemany(
            "INSERT INTO calendar_dates VALUES (?, ?, ?)",
            (
                (r["service_id"], r["date"], _int(r.get("exception_type")))
                for r in _rows(zf, "calendar_dates.txt")
            ),
        )

        def stop_times():
            for r in _rows(zf, "stop_times.txt"):
                if keep is not None and r["trip_id"] not in keep:
                    continue
                arrival = parse_gtfs_time(r.get("arrival_time", ""))
                departure = parse_gtfs_time(r.get("departure_time", ""))
                yield (r["trip_id"], r["stop_id"], int(r["stop_sequence"]),
                       arrival if arrival is not None else departure,
                       departure if departure is not None else arrival,
                       _int(r.get("pickup_type"), 0))

        db.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?)", stop_times())
    db.execute(
        "INSERT INTO trip_last_stop "
        "SELECT trip_id, MAX(stop_sequence) FROM stop_times GROUP BY trip_id"
    )
    db.executescript(INDEXES)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()
    db.close()
    os.replace(tmp_path, db_path)


def _schema_ok(db_path: str) -> bool:
    if not os.path.exists(db_path):
        return False
    with sqlite3.connect(db_path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def download(url: str, dest: str, timeout: int = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "schedules-filbleu"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp, \
            open(tmp, "wb") as out:
        while chunk := resp.read(1 << 16):
            out.write(chunk)
    os.replace(tmp, dest)


@dataclass
class Stop:
    stop_id: str
    name: str
    location_type: int
    parent_station: str | None


@dataclass
class ScheduledStop:
    trip_id: str
    stop_id: str
    stop_sequence: int
    service_date: date
    time: datetime  # départ théorique
    route_id: str
    headsign: str


class Gtfs:
    """Accès aux données théoriques stockées dans la base SQLite."""

    def __init__(self, db_path: str):
        # Accès en lecture seule, partagé entre les threads d'un serveur web.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    @classmethod
    def load(cls, cache_dir: str, url: str = GTFS_URL, max_age: int = DEFAULT_MAX_AGE,
             zip_path: str | None = None, name: str = "filbleu_gtfs",
             only_stations: list[str] | None = None) -> "Gtfs":
        """Ouvre la base, en (re)téléchargeant le GTFS s'il est absent ou trop ancien.

        ``zip_path`` permet d'utiliser un fichier GTFS local à la place du téléchargement ;
        ``name`` nomme les fichiers du cache et ``only_stations`` filtre les courses
        (voir ``build_database``).
        """
        os.makedirs(cache_dir, exist_ok=True)
        db_path = os.path.join(cache_dir, f"{name}.sqlite")
        if zip_path is not None:
            if not _schema_ok(db_path) or os.path.getmtime(db_path) < os.path.getmtime(zip_path):
                build_database(zip_path, db_path, only_stations)
            return cls(db_path)
        fresh = _schema_ok(db_path) and time.time() - os.path.getmtime(db_path) < max_age
        if not fresh:
            zip_dest = os.path.join(cache_dir, f"{name}.zip")
            try:
                download(url, zip_dest)
                build_database(zip_dest, db_path, only_stations)
            except OSError:
                # Pas de réseau : on garde l'ancienne base, reconstruite depuis le
                # dernier zip téléchargé si son schéma est périmé.
                if not _schema_ok(db_path) and os.path.exists(zip_dest):
                    build_database(zip_dest, db_path, only_stations)
                elif not os.path.exists(db_path):
                    raise
        return cls(db_path)

    # --- arrêts -------------------------------------------------------------

    def get_stop(self, stop_id: str) -> Stop | None:
        row = self.db.execute("SELECT * FROM stops WHERE stop_id = ?", (stop_id,)).fetchone()
        return Stop(row["stop_id"], row["stop_name"], row["location_type"],
                    row["parent_station"]) if row else None

    def search_stops(self, query: str) -> list[Stop]:
        """Arrêts dont le nom contient ``query`` (sans tenir compte des accents/majuscules)."""
        rows = self.db.execute(
            "SELECT * FROM stops WHERE norm_name LIKE ? AND location_type IN (0, 1) "
            "ORDER BY stop_name, stop_id",
            (f"%{normalize(query)}%",),
        ).fetchall()
        return [Stop(r["stop_id"], r["stop_name"], r["location_type"], r["parent_station"])
                for r in rows]

    def resolve_stop_ids(self, query: str) -> tuple[str, list[str]]:
        """Trouve les quais correspondant à un identifiant ou à un nom d'arrêt.

        Renvoie (nom affiché, liste des stop_id de quais). Un nom regroupe tous les
        quais portant exactement ce nom (les deux sens), ainsi que les quais rattachés
        à une zone d'arrêt (parent_station) portant ce nom.
        """
        stop = self.get_stop(query)
        if stop is not None:
            matches = [stop]
        else:
            candidates = self.search_stops(query)
            if not candidates:
                raise LookupError(f"Aucun arrêt ne correspond à « {query} »")
            names = sorted({c.name for c in candidates})
            exact = [n for n in names if normalize(n) == normalize(query)]
            if exact:
                name = exact[0]
            elif len(names) == 1:
                name = names[0]
            else:
                raise LookupError(
                    f"Plusieurs arrêts correspondent à « {query} » : " + ", ".join(names)
                )
            matches = [c for c in candidates if c.name == name]
        ids: list[str] = []
        for s in matches:
            if s.location_type == 1:
                ids += [r[0] for r in self.db.execute(
                    "SELECT stop_id FROM stops WHERE parent_station = ? AND location_type = 0",
                    (s.stop_id,))]
            else:
                ids.append(s.stop_id)
        return matches[0].name, sorted(set(ids))

    def route(self, route_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM routes WHERE route_id = ?", (route_id,)).fetchone()

    def trip(self, trip_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()

    # --- calendrier -----------------------------------------------------------

    def active_services(self, day: date) -> set[str]:
        ymd = day.strftime("%Y%m%d")
        services = {
            r["service_id"]
            for r in self.db.execute(
                "SELECT service_id, days FROM calendar WHERE start_date <= ? AND end_date >= ?",
                (ymd, ymd))
            if r["days"][day.weekday()] == "1"
        }
        for r in self.db.execute(
                "SELECT service_id, exception_type FROM calendar_dates WHERE date = ?", (ymd,)):
            if r["exception_type"] == 1:
                services.add(r["service_id"])
            elif r["exception_type"] == 2:
                services.discard(r["service_id"])
        return services

    # --- horaires -------------------------------------------------------------

    def scheduled_departures(self, stop_ids: list[str], start: datetime,
                             end: datetime) -> list[ScheduledStop]:
        """Passages théoriques aux quais ``stop_ids`` entre ``start`` et ``end``.

        Les terminus et les arrêts sans montée (pickup_type = 1) sont exclus.
        """
        placeholders = ",".join("?" * len(stop_ids))
        result: list[ScheduledStop] = []
        local_start = start.astimezone(TIMEZONE).date()
        # La veille aussi : les courses après minuit (horaires > 24:00:00).
        day = local_start - timedelta(days=1)
        while day <= end.astimezone(TIMEZONE).date():
            base = service_day_start(day)
            lo = int((start - base).total_seconds())
            hi = int((end - base).total_seconds())
            services = self.active_services(day)
            if services and hi >= 0:
                rows = self.db.execute(
                    f"""
                    SELECT st.trip_id, st.stop_id, st.stop_sequence, st.departure,
                           t.route_id, t.headsign, t.service_id
                    FROM stop_times st
                    JOIN trips t ON t.trip_id = st.trip_id
                    JOIN trip_last_stop l ON l.trip_id = st.trip_id
                    WHERE st.stop_id IN ({placeholders})
                      AND st.departure BETWEEN ? AND ?
                      AND st.pickup_type != 1
                      AND st.stop_sequence < l.stop_sequence
                    """,
                    (*stop_ids, lo, hi),
                )
                for r in rows:
                    if r["service_id"] in services:
                        result.append(ScheduledStop(
                            r["trip_id"], r["stop_id"], r["stop_sequence"], day,
                            base + timedelta(seconds=r["departure"]),
                            r["route_id"], r["headsign"]))
            day += timedelta(days=1)
        return result

    def trip_stop_times(self, trip_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT stop_id, stop_sequence, arrival, departure FROM stop_times "
            "WHERE trip_id = ? ORDER BY stop_sequence", (trip_id,)).fetchall()
