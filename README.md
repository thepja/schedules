# schedules

schedules display filbleu sncf ...

## Fil Bleu (Tours) — prochains passages à un arrêt

Utilise les données ouvertes du réseau Fil Bleu publiées sur
[transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/fil-bleu-syndicat-des-mobilites-gtfs-gtfs-rt) :

- **GTFS** (horaires théoriques, arrêts, lignes) : `https://transport.data.gouv.fr/resources/80694/download`
  — téléchargé puis converti en base SQLite dans `.cache/`, rafraîchi toutes les 24 h ;
- **GTFS-RT trip-updates** (temps réel) : `https://data.filbleu.fr/ws-tr/gtfs-rt/opendata/trip-updates`.

Aucune clé d'API n'est nécessaire.

### Installation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Utilisation

```sh
.venv/bin/python -m filbleu "Jean Jaurès"            # prochains passages (tous quais, tous sens)
.venv/bin/python -m filbleu "Jean Jaurès" -l A -n 5  # tram A uniquement, 5 passages
.venv/bin/python -m filbleu --search jaures          # lister les arrêts / stop_id correspondants
.venv/bin/python -m filbleu <stop_id>                # un quai précis (un seul sens)
.venv/bin/python -m filbleu "Jean Jaurès" --watch 30 # affichage rafraîchi toutes les 30 s
```

Exemple de sortie :

```
Prochains passages à Jean Jaurès — 08:01
  A  Gare de Tours                          08:07  supprimé
  A  Terminus                               7 min  08:08 (+3 min)
  A  Terminus                              19 min  08:20 (+5 min)
  2  Terminus                              39 min  08:40 théorique
```

Les horaires sans données temps réel sont marqués « théorique ». Si le flux temps
réel est indisponible, l'outil affiche les horaires théoriques.

Les URLs peuvent être surchargées via `--gtfs-url` / `--rt-url` (ou les variables
`FILBLEU_GTFS_URL` / `FILBLEU_RT_URL`), et `--gtfs-zip` permet d'utiliser un GTFS local.

### En Python

```python
from filbleu import Gtfs, fetch_trip_updates, next_departures

gtfs = Gtfs.load(".cache")
name, stop_ids = gtfs.resolve_stop_ids("Jean Jaurès")
for d in next_departures(gtfs, stop_ids, fetch_trip_updates(), limit=5):
    print(d.line, d.headsign, d.expected, d.realtime, d.canceled)
```

### Tests

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```
