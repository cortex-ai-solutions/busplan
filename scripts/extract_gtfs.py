"""
GTFS Extractor for SNG Suhl — Helena's Busplan
CLI version used by GitHub Actions workflow.
Usage: python scripts/extract_gtfs.py /path/to/vmt_gtfs.zip
"""
import csv, json, sys, zipfile, os
from collections import defaultdict
from pathlib import Path

SNG_AGENCY_ID = "73"
LINES = ["D1", "D2", "S21"]
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


def get_day_type(row):
    if row.get("saturday") == "1": return "saturday"
    if row.get("sunday")   == "1": return "sunday"
    for day in ["monday","tuesday","wednesday","thursday","friday"]:
        if row.get(day) == "1": return "weekday"
    return None


def fmt_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else ""


def extract(gtfs_zip_path):
    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"Opening: {gtfs_zip_path}")

    with zipfile.ZipFile(gtfs_zip_path) as zf:
        routes   = read_csv(zf, "routes.txt")
        trips    = read_csv(zf, "trips.txt")
        stops    = read_csv(zf, "stops.txt")
        st       = read_csv(zf, "stop_times.txt")
        calendar = read_csv(zf, "calendar.txt")
        try:
            feed_info = read_csv(zf, "feed_info.txt")
        except KeyError:
            feed_info = [{}]

    valid_from  = fmt_date(feed_info[0].get("feed_start_date", ""))
    valid_until = fmt_date(feed_info[0].get("feed_end_date", ""))
    print(f"Feed validity: {valid_from} to {valid_until}")

    service_day = {r["service_id"]: get_day_type(r) for r in calendar}
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

        dir_trips = defaultdict(lambda: defaultdict(list))
        for t in line_trips:
            did = t.get("direction_id","0")
            dt  = service_day.get(t["service_id"])
            if dt: dir_trips[did][dt].append(t["trip_id"])

        directions_out = []
        for did in sorted(dir_trips.keys()):
            all_dir_trips = [t for ts in dir_trips[did].values() for t in ts]
            if not all_dir_trips: continue

            canonical     = max(all_dir_trips, key=lambda t: len(trip_st[t]))
            stop_ids_ord  = [s[1] for s in trip_st[canonical]]
            stop_names_ord = [stop_names.get(sid, sid) for sid in stop_ids_ord]

            hs_list  = [trip_hs.get(t,"") for t in all_dir_trips if trip_hs.get(t)]
            headsign = max(set(hs_list), key=hs_list.count) if hs_list else f"Richtung {did}"

            print(f"  dir{did} ({headsign}): {len(stop_names_ord)} stops")

            schedules = {}
            for dt, tid_list in dir_trips[did].items():
                matrix = []
                for tid in tid_list:
                    st_map = {s[1]: normalize_time(s[2]) for s in trip_st[tid]}
                    row    = [st_map.get(sid) for sid in stop_ids_ord]
                    if row[0]: matrix.append(row)
                matrix.sort(key=lambda r: r[0] or "99:99")
                schedules[dt] = matrix
                print(f"    {dt}: {len(matrix)} trips")

            directions_out.append({
                "id": f"dir{did}",
                "direction_id": did,
                "headsign": headsign,
                "stops": stop_names_ord,
                "schedules": schedules
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
