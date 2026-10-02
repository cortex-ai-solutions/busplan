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


def _day(yyyymmdd):
    return datetime.date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))


def _runs_per_day_type(items):
    """[(datum, dienste)] -> Laeufe gleicher Dienstmenge, Sondertage geglaettet.

    Laeufe mit <= 2 Terminen (Feiertag, Brueckentag) werden dem vorherigen
    Lauf zugeschlagen. Ein kurzer *erster* Lauf bleibt stehen — das ist der
    auslaufende alte Fahrplan.
    """
    runs = []
    for date, services in items:
        if runs and runs[-1]["services"] == services:
            runs[-1]["end"] = date
            runs[-1]["count"] += 1
        else:
            runs.append({"start": date, "end": date, "services": services, "count": 1})

    merged = True
    while merged:
        merged = False
        for i in range(1, len(runs)):
            if runs[i]["count"] <= 2 or runs[i - 1]["services"] == runs[i]["services"]:
                runs[i - 1]["end"] = runs[i]["end"]
                runs[i - 1]["count"] += runs[i]["count"]
                del runs[i]
                merged = True
                break
    return runs


def split_versions(line_services, active, holidays, from_date, until_date):
    """Fahrplanversionen einer Linie mit Gueltigkeitszeitraum.

    Der Feed enthaelt je Linie mehrere zeitlich begrenzte Versionen (z.B.
    Ferienfahrplan bis 10.10., Regelfahrplan ab 11.10.). Je Tagesart werden die
    Laeufe gleicher Dienstmenge bestimmt; Versionswechsel sind die Startdaten
    der Folgelaeufe (Wechsel innerhalb von 7 Tagen zaehlen als einer, bei
    Wochentag/Samstag/Sonntag liegen sie sonst um Tage auseinander).

    Rueckgabe: [{"from": "YYYYMMDD", "until": "YYYYMMDD", "chosen": {tagesart: dienste}}]
    """
    by_type = defaultdict(list)
    for date in sorted(active):
        if not (from_date <= date <= until_date): continue
        todays = frozenset(active[date] & line_services)
        if todays:
            by_type[classify_date(date, holidays)].append((date, todays))

    final = {}      # tagesart -> [(datum, dienste)] nach Glaettung
    changes = []
    for day_type, items in by_type.items():
        runs = _runs_per_day_type(items)
        final[day_type] = [(d, next(r["services"] for r in runs if r["start"] <= d <= r["end"]))
                           for d, _ in items]
        changes += [r["start"] for r in runs[1:]]

    boundaries = []
    for date in sorted(changes):
        if boundaries and (_day(date) - _day(boundaries[-1])).days <= 7: continue
        boundaries.append(date)

    starts = [from_date] + boundaries
    ends   = [(_day(b) - datetime.timedelta(days=1)).strftime("%Y%m%d") for b in boundaries] + [until_date]

    versions = []
    for start, end in zip(starts, ends):
        chosen = {}
        for day_type, items in final.items():
            inside = Counter(sv for d, sv in items if start <= d <= end)
            if inside:
                chosen[day_type] = set(inside.most_common(1)[0][0])
            else:
                before = [sv for d, sv in items if d < start]
                chosen[day_type] = set(before[-1] if before else items[0][1])
        if versions and versions[-1]["chosen"] == chosen:
            versions[-1]["until"] = end
        else:
            versions.append({"from": start, "until": end, "chosen": chosen})
    return versions


def build_directions(line_trips, trip_st, trip_hs, trip_service, stop_names, chosen):
    """Richtungen/Fahrtmuster einer Linie fuer eine Fahrplanversion."""
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

        print(f"      dir{did}/{n} ({headsign}): {len(stop_names_ord)} stops, "
              + ", ".join(f"{d} {len(v)}" for d, v in sorted(schedules.items())))

        directions_out.append({
            "id": f"dir{did}_{n}",
            "direction_id": did,
            "headsign": headsign,
            "stops": stop_names_ord,
            "schedules": dict(schedules)
        })
    return directions_out


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
        versions = split_versions(line_services, active_days, holidays, from_date, until_date)

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

        versions_out = []
        for ver in versions:
            chosen = ver["chosen"]
            print(f"  Version {fmt_date(ver['from'])} bis {fmt_date(ver['until'])}")
            for day_type, services in sorted(chosen.items()):
                print(f"    {day_type}: Dienste {sorted(services)}")
            versions_out.append({
                "valid_from":  fmt_date(ver["from"]),
                "valid_until": fmt_date(ver["until"]),
                "directions":  build_directions(line_trips, trip_st, trip_hs, trip_service,
                                                stop_names, chosen)
            })

        out_path = OUTPUT_DIR / f"{line_name.lower()}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"line": line_name, "valid_from": valid_from, "valid_until": valid_until,
                       "versions": versions_out}, f, ensure_ascii=False, indent=2)
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
