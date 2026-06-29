"""
Busplan SNG Suhl — Orsi Tool
============================
Claude tool definition + Python-Implementierung für KI-Agenten (z.B. Orsi auf Elestio).

Verwendung als CLI:
    python busplan_tool.py --from "Goldlauter, Suhler Straße" --to "Suhl, Zentrum"
    python busplan_tool.py --from "Waldbad" --time "07:30" --day weekday

Verwendung als Modul:
    from busplan_tool import get_next_departures
    result = get_next_departures("Goldlauter, Suhler Straße", "Suhl, Zentrum")

Claude Tool Definition (für Orsi-System-Prompt / tools=[]):
------------------------------------------------------------
{
  "name": "busplan_naechste_verbindung",
  "description": "Gibt die nächsten Busverbindungen der SNG Suhl zurück. Linien: D1, D2, S21. Nutze dieses Tool wenn der Nutzer fragt wann der nächste Bus fährt, wie lange es dauert oder wann er ankommt.",
  "input_schema": {
    "type": "object",
    "properties": {
      "von": {
        "type": "string",
        "description": "Starthaltestelle, z.B. 'Goldlauter, Suhler Straße' oder 'Waldbad'"
      },
      "nach": {
        "type": "string",
        "description": "Zielhaltestelle, z.B. 'Suhl, Zentrum'. Leer lassen für alle Richtungen."
      },
      "uhrzeit": {
        "type": "string",
        "description": "Abfahrt ab Uhrzeit im Format HH:MM, z.B. '08:30'. Standard: aktuelle Uhrzeit."
      },
      "tagestyp": {
        "type": "string",
        "enum": ["weekday", "saturday", "sunday"],
        "description": "Fahrplantyp. Standard: wird automatisch aus aktuellem Wochentag ermittelt."
      },
      "linie": {
        "type": "string",
        "description": "Linie filtern: 'D1', 'D2', 'S21'. Leer = alle Linien."
      },
      "anzahl": {
        "type": "integer",
        "description": "Anzahl der Verbindungen (Standard: 3)."
      }
    },
    "required": ["von"]
  }
}
"""

import json, urllib.request, datetime, argparse, sys
from pathlib import Path

# Datenquelle: GitHub (immer aktuell) oder lokale Dateien als Fallback
GITHUB_RAW = "https://raw.githubusercontent.com/cortex-ai-solutions/busplan/main/data"
LOCAL_DATA = Path(__file__).parent.parent / "data"

LINES = ["D1", "D2", "S21"]

THUERINGEN_HOLIDAYS = {
    "2026": [
        "2026-01-01","2026-04-03","2026-04-06","2026-05-01",
        "2026-05-14","2026-05-25","2026-10-03","2026-10-31","2026-12-25","2026-12-26"
    ]
}


def _fetch_json(line: str) -> dict | None:
    # Zuerst lokal versuchen, dann GitHub
    local = LOCAL_DATA / f"{line.lower()}.json"
    if local.exists():
        return json.loads(local.read_text("utf-8"))
    try:
        url = f"{GITHUB_RAW}/{line.lower()}.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


def _time_to_mins(t: str) -> int | None:
    if not t:
        return None
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _get_day_type(dt: datetime.date | None = None) -> str:
    if dt is None:
        dt = datetime.date.today()
    iso = dt.isoformat()
    for holidays in THUERINGEN_HOLIDAYS.values():
        if iso in holidays:
            return "sunday"
    if dt.weekday() == 6:
        return "sunday"
    if dt.weekday() == 5:
        return "saturday"
    return "weekday"


def _fuzzy_match(query: str, stops: list[str]) -> str | None:
    """Findet Haltestelle auch bei Tippfehlern oder Abkürzungen."""
    q = query.lower().strip()
    # Exakter Treffer
    for s in stops:
        if s.lower() == q:
            return s
    # Enthält-Treffer
    matches = [s for s in stops if q in s.lower() or s.lower() in q]
    return matches[0] if len(matches) == 1 else (matches[0] if matches else None)


def get_next_departures(
    von: str,
    nach: str = "",
    uhrzeit: str = "",
    tagestyp: str = "",
    linie: str = "",
    anzahl: int = 3,
) -> dict:
    """
    Gibt die nächsten Busverbindungen zurück.
    Rückgabe: {"verbindungen": [...], "fehler": None | str, "alle_haltestellen": [...]}
    """
    now = datetime.datetime.now()
    day_type  = tagestyp or _get_day_type()
    from_mins = _time_to_mins(uhrzeit) if uhrzeit else (now.hour * 60 + now.minute)

    results = []
    all_stops = set()
    lines_to_check = [linie.upper()] if linie else LINES

    for line_name in lines_to_check:
        data = _fetch_json(line_name)
        if not data:
            continue

        for dir in data["directions"]:
            stops = dir["stops"]
            for s in stops:
                all_stops.add(s)

            # Fuzzy-Match Starthaltestelle
            from_stop = _fuzzy_match(von, stops)
            if not from_stop:
                continue
            from_idx = stops.index(from_stop)

            # Fuzzy-Match Zielhaltestelle
            to_stop = None
            to_idx  = -1
            if nach:
                to_stop = _fuzzy_match(nach, stops)
                if to_stop:
                    to_idx = next(
                        (i for i, s in enumerate(stops) if s == to_stop and i > from_idx),
                        -1
                    )
                    if to_idx == -1:
                        continue  # Ziel in dieser Richtung nicht erreichbar

            trips = dir["schedules"].get(day_type, [])
            for trip in trips:
                dep = trip[from_idx] if from_idx < len(trip) else None
                if not dep:
                    continue
                dep_mins = _time_to_mins(dep)
                if dep_mins is None or dep_mins < from_mins:
                    continue

                arr = trip[to_idx] if (to_idx >= 0 and to_idx < len(trip)) else None
                wait = dep_mins - from_mins

                results.append({
                    "linie":      line_name,
                    "richtung":   dir["headsign"],
                    "abfahrt":    dep,
                    "ankunft":    arr,
                    "von":        from_stop,
                    "nach":       to_stop or dir["headsign"],
                    "wartezeit":  f"in {wait} Min" if wait > 0 else "jetzt",
                    "dep_mins":   dep_mins,
                })

    results.sort(key=lambda r: r["dep_mins"])
    for r in results:
        del r["dep_mins"]

    if not results:
        # Hilfreiche Fehlermeldung mit verfügbaren Stops
        stop_list = sorted(all_stops)
        hint = ""
        if von:
            matches = [s for s in stop_list if von.lower() in s.lower()]
            if matches:
                hint = f" Meintest du: {', '.join(matches[:3])}?"
        return {
            "verbindungen": [],
            "fehler": f"Keine Verbindungen gefunden ab '{von}' nach '{nach or 'alle Richtungen'}' ({day_type}, ab {uhrzeit or 'jetzt'}).{hint}",
            "alle_haltestellen": stop_list
        }

    return {
        "verbindungen": results[:anzahl],
        "fehler": None,
        "alle_haltestellen": []
    }


def list_stops(linie: str = "") -> list[str]:
    """Gibt alle Haltestellen zurück (optional gefiltert nach Linie)."""
    lines_to_check = [linie.upper()] if linie else LINES
    stops = set()
    for line_name in lines_to_check:
        data = _fetch_json(line_name)
        if data:
            for dir in data["directions"]:
                stops.update(dir["stops"])
    return sorted(stops)


# ── CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Busplan SNG Suhl — Abfragetools")
    parser.add_argument("--from", dest="von",  required=True, help="Starthaltestelle")
    parser.add_argument("--to",   dest="nach", default="",    help="Zielhaltestelle")
    parser.add_argument("--time", dest="time", default="",    help="Uhrzeit HH:MM")
    parser.add_argument("--day",  dest="day",  default="",    choices=["weekday","saturday","sunday",""])
    parser.add_argument("--line", dest="line", default="",    help="Linie: D1, D2, S21")
    parser.add_argument("--n",    dest="n",    default=3,     type=int, help="Anzahl Ergebnisse")
    parser.add_argument("--stops",action="store_true",        help="Alle Haltestellen auflisten")
    args = parser.parse_args()

    if args.stops:
        stops = list_stops(args.line)
        print("\n".join(stops))
        sys.exit(0)

    result = get_next_departures(
        von=args.von, nach=args.nach, uhrzeit=args.time,
        tagestyp=args.day, linie=args.line, anzahl=args.n
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
