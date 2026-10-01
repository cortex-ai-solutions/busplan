"""
GTFS Extractor for SNG Suhl — Helena's Busplan
CLI version used by GitHub Actions workflow.
Usage: python scripts/extract_gtfs.py /path/to/vmt_gtfs.zip
"""
import csv, datetime, json, sys, zipfile, os
from collections import Counter, defaultdict
from pathlib import Path

SNG_AGENCY_ID = "73"
LINES = ["D1", "D2", "S21", "C1", "C2", "C12"]
OUTPUT_DIR = Path(__file__).parent.parent / "data"


def read_csv(zf, name):
    with zf.open(name) as f:
        content = f.read().decode("utf-8-sig")
        return list(csv.DictReader(content.splitlines()))


def normalize_time(t):
    if not t: return None
    parts = t.strip().split(":")
    h, m = int(parts[0]) % 24, int(parts[1])
    return f"{h:02d}:{m:02d}"


def fmt_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else ""


def is_subsequence(small, big):
    """Kommen alle Stops von `small` in dieser Reihenfolge auch in `big` vor?"""
    it = iter(big)
    return all(stop in it for stop in small)


def align_row(trip_stops, ref_ids):
    """Zeiten einer Fahrt positionsgenau in die Referenzsequenz einsortieren.

    Positionsgenau statt ueber ein stop_id-Dict, weil Ringlinien dieselbe
    stop_id zweimal enthalten (z.B. D1 "Suhl, Am Bahndamm" am Anfang und am
    Ende) — ein Dict wuerde die erste Zeit mit der letzten ueberschreiben.
    """
    row = [None] * len(ref_ids)
    pos = 0
    for stop_id, time in trip_stops:
        while pos < len(ref_ids) and ref_ids[pos] != stop_id:
            pos += 1
        if pos >= len(ref_ids):
            return None
        row[pos] = time
        pos += 1
    return row


def first_time(row):
    return next((t for t in row if t), "99:99")


def load_holidays():
    try:
        data = json.loads((OUTPUT_DIR / "holidays.json").read_text("utf-8"))
    except FileNotFoundError:
        return set()
    return {d for dates in data.get("years", {}).values() for d in dates}


def classify_date(yyyymmdd, holidays):
    weekday = datetime.date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]),
                            int(yyyymmdd[6:8])).weekday()
    if weekday == 6 or fmt_date(yyyymmdd) in holidays: return "sunday"
    if weekday == 5: return "saturday"
    return "weekday"


def build_service_calendar(calendar, calendar_dates):
    """Aktive service_ids je Datum (calendar.txt + calendar_dates.txt-Ausnahmen).

    Der Feed enthaelt je Linie mehrere zeitlich begrenzte Fahrplanversionen
    (z.B. Ferienfahrplan bis 09.10., Regelfahrplan ab 12.10.). Wer die
    service_ids nur nach Tagesart sortiert, wirft alle Versionen zusammen und
    zeigt dann doppelte Abfahrten bzw. Abfahrten wenige Minuten daneben.
    Deshalb wird je Datum aufgeloest, welche Dienste wirklich fahren.
    """
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    active = defaultdict(set)

    for row in calendar:
        start = datetime.datetime.strptime(row["start_date"], "%Y%m%d").date()
        end   = datetime.datetime.strptime(row["end_date"],   "%Y%m%d").date()
        flags = [row.get(d) == "1" for d in weekdays]
        if not any(flags): continue
        day = start
        while day <= end:
            if flags[day.weekday()]:
                active[day.strftime("%Y%m%d")].add(row["service_id"])
            day += datetime.timedelta(days=1)

    for row in calendar_dates:
        if row.get("exception_type") == "1":
            active[row["date"]].add(row["service_id"])
        elif row.get("exception_type") == "2":
            active[row["date"]].discard(row["service_id"])

    return active


def pick_services(line_services, active, holidays, from_date, until_date):
    """Je Tagesart die Dienstmenge, die im Zeitraum am haeufigsten gilt.

    Ab `from_date` (Extraktionstag), damit abgelaufene Ferien-/Altfahrplaene
    nicht mit dem kommenden Regelfahrplan vermischt werden. Einzelne
    Sondertage (Feiertage, Brueckentage) fallen als Minderheit heraus.
    """
    counts = defaultdict(Counter)
    for date, services in active.items():
        if not (from_date <= date <= until_date): continue
        todays = frozenset(services & line_services)
        if todays:
            counts[classify_date(date, holidays)][todays] += 1

    return {day_type: set(c.most_common(1)[0][0]) for day_type, c in counts.items()}


def extract(gtfs_zip_path):
    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"Opening: {gtfs_zip_path}")

    with zipfile.ZipFile(gtfs_zip_path) as zf:
        routes   = read_csv(zf, "routes.txt")
        trips    = read_csv(zf, "trips.txt")
        stops    = read_csv(zf, "stops.txt")
        st       = read_csv(zf, "stop_times.txt")
        calendar = read_csv(zf, "calendar.txt")
        calendar_dates = read_csv(zf, "calendar_dates.txt")
        try:
            feed_info = read_csv(zf, "feed_info.txt")
        except KeyError:
            feed_info = [{}]

    valid_from  = fmt_date(feed_info[0].get("feed_start_date", ""))
    valid_until = fmt_date(feed_info[0].get("feed_end_date", ""))
    print(f"Feed validity: {valid_from} to {valid_until}")

    holidays    = load_holidays()
    active_days = build_service_calendar(calendar, calendar_dates)
    # Ab heute — bzw. Feedbeginn, falls der Feed in der Zukunft startet
    from_date   = max(datetime.date.today().strftime("%Y%m%d"),
                      feed_info[0].get("feed_start_date", ""))
    until_date  = feed_info[0].get("feed_end_date") or "99999999"
    stop_names  = {s["stop_id"]: s["stop_name"] for s in stops}

    target_routes = {r["route_short_name"]: r["route_id"]
                     for r in routes
                     if r.get("agency_id") == SNG_AGENCY_ID
                     and r.get("route_short_name") in LINES}
    print(f"Found routes: {target_routes}")

    for line_name in LINES:
        route_id = target_routes.get(line_name)
        if not route_id:
            print(f"WARNING: {line_name} not found!")
            continue

        print(f"\nProcessing {line_name}...")
        line_trips   = [t for t in trips if t["route_id"] == route_id]
        all_trip_ids = {t["trip_id"] for t in line_trips}
        trip_hs      = {t["trip_id"]: t.get("trip_headsign","") for t in line_trips}
        trip_service = {t["trip_id"]: t["service_id"] for t in line_trips}

        line_services = {t["service_id"] for t in line_trips}
        chosen = pick_services(line_services, active_days, holidays, from_date, until_date)
        for day_type, services in sorted(chosen.items()):
            print(f"  {day_type}: Dienste {sorted(services)}")

        trip_st = defaultdict(list)
        for row in st:
            if row["trip_id"] in all_trip_ids:
                trip_st[row["trip_id"]].append((
                    int(row["stop_sequence"]),
                    row["stop_id"],
                    row.get("departure_time") or row.get("arrival_time","")
                ))
        for tid in trip_st:
            trip_st[tid].sort(key=lambda x: x[0])

        # Fahrten nach exakter Stopfolge gruppieren
        patterns = defaultdict(list)
        for t in line_trips:
            tid = t["trip_id"]
            if not trip_st[tid]: continue
            if not any(t["service_id"] in svcs for svcs in chosen.values()): continue
            did = t.get("direction_id", "0")
            patterns[(did, tuple(s[1] for s in trip_st[tid]))].append(tid)

        # Kurzlaeufer der laengsten passenden Sequenz zuordnen. Linien wie C1/C2
        # haben ein gutes Dutzend Fahrtvarianten — mit nur einer Referenzfahrt
        # je Richtung fielen Fahrten und ganze Haltestellen unter den Tisch.
        refs = []
        for (did, pattern), tids in sorted(patterns.items(), key=lambda kv: -len(kv[0][1])):
            match = next((r for r in refs
                          if r["direction_id"] == did and is_subsequence(pattern, r["ref_ids"])), None)
            if match:
                match["trips"].extend(tids)
            else:
                refs.append({"direction_id": did, "ref_ids": list(pattern), "trips": list(tids)})

        refs.sort(key=lambda r: (r["direction_id"], -len(r["ref_ids"])))

        directions_out = []
        for n, ref in enumerate(refs):
            did, ref_ids = ref["direction_id"], ref["ref_ids"]
            stop_names_ord = [stop_names.get(sid, sid) for sid in ref_ids]

            hs_list  = [trip_hs.get(t,"") for t in ref["trips"] if trip_hs.get(t)]
            headsign = max(set(hs_list), key=hs_list.count) if hs_list else f"Richtung {did}"

            print(f"  dir{did}/{n} ({headsign}): {len(stop_names_ord)} stops")

            schedules = defaultdict(list)
            for tid in ref["trips"]:
                row = align_row([(s[1], normalize_time(s[2])) for s in trip_st[tid]], ref_ids)
                if not (row and any(row)): continue
                for day_type, services in chosen.items():
                    if trip_service[tid] in services:
                        schedules[day_type].append(row)

            for day_type in schedules:
                # Identische Fahrten (gleiche Zeiten) nur einmal behalten
                unique = {tuple(r): r for r in schedules[day_type]}
                schedules[day_type] = sorted(unique.values(), key=first_time)
                print(f"    {day_type}: {len(schedules[day_type])} trips")

            directions_out.append({
                "id": f"dir{did}_{n}",
                "direction_id": did,
                "headsign": headsign,
                "stops": stop_names_ord,
                "schedules": dict(schedules)
            })

        out_path = OUTPUT_DIR / f"{line_name.lower()}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"line": line_name, "valid_from": valid_from, "valid_until": valid_until,
                       "directions": directions_out}, f, ensure_ascii=False, indent=2)
        print(f"  Written: {out_path} ({out_path.stat().st_size/1024:.1f} KB)")

    # Generate data-bundle.js — includes all extracted lines dynamically
    bundle_path = OUTPUT_DIR.parent / "data-bundle.js"
    lines_data = []
    for line_name in LINES:
        json_path = OUTPUT_DIR / f"{line_name.lower()}.json"
        if json_path.exists():
            lines_data.append(json.loads(json_path.read_text("utf-8")))
    try:
        holidays_data = json.loads((OUTPUT_DIR / "holidays.json").read_text("utf-8"))
    except FileNotFoundError:
        holidays_data = {"years": {}}
    bundle = "// Auto-generated by extract_gtfs.py — do not edit manually.\n"
    bundle += "window.BUSPLAN_LINES = " + json.dumps(lines_data, ensure_ascii=False, separators=(',', ':')) + ";\n"
    bundle += "window.BUSPLAN_HOLIDAYS = " + json.dumps(holidays_data, ensure_ascii=False, separators=(',', ':')) + ";\n"
    bundle_path.write_text(bundle, encoding="utf-8")
    print(f"  Bundle: {bundle_path} ({bundle_path.stat().st_size/1024:.1f} KB)")

    print("\nDone!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_gtfs.py /path/to/vmt_gtfs.zip")
        sys.exit(1)
    extract(sys.argv[1])
