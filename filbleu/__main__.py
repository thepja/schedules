"""Ligne de commande : python -m filbleu "Jean Jaurès"."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

from .departures import next_departures
from .gtfs import GTFS_URL, TIMEZONE, Gtfs
from .realtime import TRIP_UPDATES_URL, fetch_trip_updates

DEFAULT_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")


def format_departures(stop_name, departures, now, realtime_ok=True) -> str:
    lines = [f"Prochains passages à {stop_name} — {now:%H:%M}"]
    if not realtime_ok:
        lines.append("(temps réel indisponible : horaires théoriques uniquement)")
    if not departures:
        lines.append("Aucun passage prévu.")
        return "\n".join(lines)
    width = max(len(d.line) for d in departures)
    for d in departures:
        if d.canceled:
            when, info = f"{d.expected:%H:%M}", "supprimé"
        else:
            mins = d.minutes(now)
            when = "à l'approche" if mins == 0 else f"{mins} min"
            info = f"{d.expected:%H:%M}"
            if d.realtime:
                delay = d.delay
                if delay is not None and abs(delay) >= timedelta(minutes=1):
                    sign = "+" if delay > timedelta(0) else "-"
                    info += f" ({sign}{int(abs(delay).total_seconds() // 60)} min)"
            else:
                info += " théorique"
        lines.append(f"  {d.line:<{width}}  {d.headsign[:30]:<30}  {when:>12}  {info}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="filbleu", description="Prochains bus/trams Fil Bleu à un arrêt.")
    p.add_argument("stop", help="nom de l'arrêt (ex. « Jean Jaurès ») ou stop_id GTFS")
    p.add_argument("-n", "--limit", type=int, default=10, help="nombre de passages (défaut 10)")
    p.add_argument("-l", "--line", action="append", help="filtrer sur une ligne (répétable)")
    p.add_argument("-r", "--realtime-only", action="store_true",
                   help="n'afficher que les passages suivis en temps réel")
    p.add_argument("--horizon", type=int, default=120, help="fenêtre en minutes (défaut 120)")
    p.add_argument("--search", action="store_true", help="lister les arrêts correspondants")
    p.add_argument("--watch", type=int, metavar="SEC", help="rafraîchir toutes les SEC secondes")
    p.add_argument("--cache-dir", default=os.environ.get("FILBLEU_CACHE", DEFAULT_CACHE))
    p.add_argument("--gtfs-url", default=os.environ.get("FILBLEU_GTFS_URL", GTFS_URL))
    p.add_argument("--gtfs-zip", help="utiliser un fichier GTFS local au lieu de le télécharger")
    p.add_argument("--rt-url", default=os.environ.get("FILBLEU_RT_URL", TRIP_UPDATES_URL))
    args = p.parse_args(argv)

    gtfs = Gtfs.load(args.cache_dir, url=args.gtfs_url, zip_path=args.gtfs_zip)

    if args.search:
        for s in gtfs.search_stops(args.stop):
            kind = "zone" if s.location_type == 1 else "quai"
            print(f"{s.stop_id:<20} {kind:<5} {s.name}")
        return 0

    try:
        stop_name, stop_ids = gtfs.resolve_stop_ids(args.stop)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    lines = {l.lower() for l in args.line} if args.line else None

    while True:
        try:
            updates, rt_ok = fetch_trip_updates(args.rt_url), True
        except Exception as e:  # réseau, protobuf invalide...
            print(f"Temps réel indisponible : {e}", file=sys.stderr)
            updates, rt_ok = {}, False
        now = datetime.now(TIMEZONE)
        deps = next_departures(gtfs, stop_ids, updates, now=now, limit=args.limit,
                               horizon=timedelta(minutes=args.horizon), lines=lines,
                               realtime_only=args.realtime_only)
        output = format_departures(stop_name, deps, now, rt_ok)
        if not args.watch:
            print(output)
            return 0
        print("\033[2J\033[H" + output, flush=True)
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
