#!/usr/bin/env python3
"""
fit2tcx.py - Convert a Garmin .fit activity file to a .tcx file, keeping
every value the TCX format can carry, plus temperature (and optionally any
other per-record field) in <Extensions>.

Usage:
    python3 fit2tcx.py input.fit [output.tcx] [--all-fields]

    --all-fields   also dump every remaining record field (including
                   "unknown_*" ones) as <fit:Field name="...">value</fit:Field>
                   inside each trackpoint's <Extensions>.

Requires: pip install fitparse

Notes:
  * TCX has no native temperature element. Temperature is written as
    <gpxtpx:atemp> (Garmin TrackPointExtension v1, in degrees C) inside
    each Trackpoint's <Extensions>. Tools that don't know it ignore it.
  * Positions are converted from FIT semicircles to decimal degrees.
  * enhanced_speed / enhanced_altitude are preferred over the plain fields.
"""
import sys
import argparse
import datetime as dt
import xml.etree.ElementTree as ET

from fitparse import FitFile

NS_TCX = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_TPX2 = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
NS_GPXTPX = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
NS_FIT = "https://example.com/fit-extra/v1"  # custom, for --all-fields

ET.register_namespace("", NS_TCX)
ET.register_namespace("xsi", NS_XSI)
ET.register_namespace("tpx", NS_TPX2)
ET.register_namespace("gpxtpx", NS_GPXTPX)
ET.register_namespace("fit", NS_FIT)

SEMICIRCLE = 180.0 / 2 ** 31

SPORT_MAP = {"running": "Running", "cycling": "Biking", "e_biking": "Biking"}

# record fields we map explicitly (everything else goes to --all-fields)
HANDLED = {
    "timestamp", "position_lat", "position_long", "altitude",
    "enhanced_altitude", "distance", "heart_rate", "cadence", "speed",
    "enhanced_speed", "power", "temperature", "fractional_cadence",
}


def q(ns, tag):
    return "{%s}%s" % (ns, tag)


def sub(parent, tag, text=None, ns=NS_TCX, **attrs):
    el = ET.SubElement(parent, q(ns, tag), attrs)
    if text is not None:
        el.text = str(text)
    return el


def iso(ts):
    """fitparse returns naive UTC datetimes."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ts.microsecond // 1000)


def num(v, ndigits=None):
    if isinstance(v, float):
        return repr(round(v, ndigits)) if ndigits is not None else repr(v)
    return str(v)


def get(msg, *names):
    for n in names:
        v = msg.get_value(n)
        if v is not None:
            return v
    return None


def read_fit(path):
    fit = FitFile(path)
    fit.parse()
    records, laps, sessions = [], [], []
    for m in fit.get_messages():
        if m.name == "record":
            records.append(m)
        elif m.name == "lap":
            laps.append(m)
        elif m.name == "session":
            sessions.append(m)
    return fit, records, laps, sessions


def build_lap_element(act, lap_msg, lap_records, sport, extra=False):
    start = get(lap_msg, "start_time") if lap_msg else lap_records[0].get_value("timestamp")
    lap = sub(act, "Lap", StartTime=iso(start))

    def lv(*n):
        return get(lap_msg, *n) if lap_msg else None

    total_time = lv("total_timer_time", "total_elapsed_time")
    total_dist = lv("total_distance")
    if total_dist is None and lap_records:
        total_dist = get(lap_records[-1], "distance")
    if total_time is None and lap_records:
        total_time = (lap_records[-1].get_value("timestamp") - lap_records[0].get_value("timestamp")).total_seconds()

    sub(lap, "TotalTimeSeconds", num(float(total_time or 0), 3))
    sub(lap, "DistanceMeters", num(float(total_dist or 0), 2))
    max_speed = lv("enhanced_max_speed", "max_speed")
    if max_speed is not None:
        sub(lap, "MaximumSpeed", num(float(max_speed), 3))
    sub(lap, "Calories", int(lv("total_calories") or 0))
    avg_hr, max_hr = lv("avg_heart_rate"), lv("max_heart_rate")
    if avg_hr is not None:
        sub(sub(lap, "AverageHeartRateBpm"), "Value", int(avg_hr))
    if max_hr is not None:
        sub(sub(lap, "MaximumHeartRateBpm"), "Value", int(max_hr))
    intensity = lv("intensity")
    sub(lap, "Intensity", "Resting" if str(intensity) == "rest" else "Active")
    avg_cad = lv("avg_cadence")
    if avg_cad is not None and sport == "Biking":
        sub(lap, "Cadence", int(avg_cad))
    trig = str(lv("lap_trigger") or "manual").lower()
    trig_map = {"manual": "Manual", "distance": "Distance", "time": "Time",
                "position_start": "Location", "position_lap": "Location",
                "position_waypoint": "Location", "position_marked": "Location",
                "session_end": "Manual", "fitness_equipment": "Manual"}
    sub(lap, "TriggerMethod", trig_map.get(trig, "Manual"))

    track = sub(lap, "Track")
    for r in lap_records:
        add_trackpoint(track, r, sport, extra)

    # lap-level extension: lap averages that TCX has no slot for
    ext_vals = {}
    if lap_msg is not None:
        ext_vals["AvgSpeed"] = lv("enhanced_avg_speed", "avg_speed")
        ext_vals["AvgWatts"] = lv("avg_power")
        ext_vals["MaxWatts"] = lv("max_power")
        if sport == "Running":
            ext_vals["AvgRunCadence"] = lv("avg_cadence")
            ext_vals["MaxRunCadence"] = lv("max_cadence")
    ext_vals = {k: v for k, v in ext_vals.items() if v is not None}
    temps = {"AvgTemperature": lv("avg_temperature"), "MaxTemperature": lv("max_temperature")}
    temps = {k: v for k, v in temps.items() if v is not None}
    if ext_vals or temps:
        lx = sub(lap, "Extensions")
        if ext_vals:
            lx_el = ET.SubElement(lx, q(NS_TPX2, "LX"))
            for k, v in ext_vals.items():
                ET.SubElement(lx_el, q(NS_TPX2, k)).text = num(v)
        for k, v in temps.items():
            ET.SubElement(lx, q(NS_FIT, k)).text = num(v)
    return lap


def add_trackpoint(track, r, sport, extra=False):
    tp = sub(track, "Trackpoint")
    sub(tp, "Time", iso(r.get_value("timestamp")))

    lat, lon = r.get_value("position_lat"), r.get_value("position_long")
    if lat is not None and lon is not None:
        pos = sub(tp, "Position")
        sub(pos, "LatitudeDegrees", num(lat * SEMICIRCLE, 8))
        sub(pos, "LongitudeDegrees", num(lon * SEMICIRCLE, 8))

    alt = get(r, "enhanced_altitude", "altitude")
    if alt is not None:
        sub(tp, "AltitudeMeters", num(float(alt), 2))
    dist = r.get_value("distance")
    if dist is not None:
        sub(tp, "DistanceMeters", num(float(dist), 2))
    hr = r.get_value("heart_rate")
    if hr is not None:
        sub(sub(tp, "HeartRateBpm"), "Value", int(hr))
    cad = r.get_value("cadence")
    if cad is not None and sport != "Running":
        sub(tp, "Cadence", int(cad))

    # ---- extensions ----
    speed = get(r, "enhanced_speed", "speed")
    power = r.get_value("power")
    temp = r.get_value("temperature")
    ext = ET.Element(q(NS_TCX, "Extensions"))

    if speed is not None or power is not None or (sport == "Running" and cad is not None):
        tpx = ET.SubElement(ext, q(NS_TPX2, "TPX"))
        if speed is not None:
            ET.SubElement(tpx, q(NS_TPX2, "Speed")).text = num(float(speed), 3)
        if sport == "Running" and cad is not None:
            ET.SubElement(tpx, q(NS_TPX2, "RunCadence")).text = str(int(cad))
        if power is not None:
            ET.SubElement(tpx, q(NS_TPX2, "Watts")).text = str(int(power))

    if temp is not None:
        g = ET.SubElement(ext, q(NS_GPXTPX, "TrackPointExtension"))
        ET.SubElement(g, q(NS_GPXTPX, "atemp")).text = num(float(temp))

    if extra:
        for f in r:
            if f.name in HANDLED or f.value is None:
                continue
            if f.name in ("altitude", "speed"):
                continue
            el = ET.SubElement(ext, q(NS_FIT, "Field"), {"name": f.name})
            el.text = str(f.value)
            if f.units:
                el.set("units", str(f.units))

    if len(ext):
        tp.append(ext)


def convert(in_path, out_path, extra=False):
    fit, records, laps, sessions = read_fit(in_path)
    if not records:
        sys.exit("No 'record' messages found in %s" % in_path)

    sport_raw = str(get(sessions[0], "sport") if sessions else "other").lower()
    sport = SPORT_MAP.get(sport_raw, "Other")

    root = ET.Element(q(NS_TCX, "TrainingCenterDatabase"))
    root.set(q(NS_XSI, "schemaLocation"),
             NS_TCX + " http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd")
    acts = sub(root, "Activities")
    act = sub(acts, "Activity", Sport=sport)

    first_ts = records[0].get_value("timestamp")
    sub(act, "Id", iso(first_ts))

    # assign each record to the last lap whose start_time <= record time
    laps = sorted(laps, key=lambda m: get(m, "start_time") or first_ts)
    if laps:
        buckets = [[] for _ in laps]
        starts = [get(l, "start_time") for l in laps]
        for r in records:
            ts = r.get_value("timestamp")
            idx = 0
            for i, s in enumerate(starts):
                if s is not None and s <= ts:
                    idx = i
            buckets[idx].append(r)
        for lap_msg, recs in zip(laps, buckets):
            if recs:
                build_lap_element(act, lap_msg, recs, sport, extra)
    else:
        build_lap_element(act, None, records, sport, extra)

    # creator info from the first Garmin device_info that has a product
    for m in fit.get_messages("device_info"):
        prod = m.get_value("garmin_product") or m.get_value("product")
        if m.get_value("manufacturer") and prod is not None and m.get_value("device_index") in (0, "creator"):
            creator = sub(act, "Creator")
            creator.set(q(NS_XSI, "type"), "Device_t")
            sub(creator, "Name", str(prod))
            sub(creator, "UnitId", int(m.get_value("serial_number") or 0))
            sub(creator, "ProductID", 0)
            break

    author = sub(root, "Author")
    author.set(q(NS_XSI, "type"), "Application_t")
    sub(author, "Name", "fit2tcx.py")
    sub(author, "LangID", "EN")
    sub(author, "PartNumber", "000-00000-00")

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)

    n_temp = sum(1 for r in records if r.get_value("temperature") is not None)
    print("Wrote %s" % out_path)
    print("  sport: %s | laps: %d | trackpoints: %d | with temperature: %d"
          % (sport, max(len(laps), 1), len(records), n_temp))


def main():
    ap = argparse.ArgumentParser(description="Convert Garmin .fit to .tcx (keeps temperature)")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--all-fields", action="store_true",
                    help="also write every other record field into <Extensions>")
    a = ap.parse_args()
    out = a.output or (a.input.rsplit(".", 1)[0] + ".tcx")
    convert(a.input, out, a.all_fields)


if __name__ == "__main__":
    main()
