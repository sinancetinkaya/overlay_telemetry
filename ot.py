#!/usr/bin/env python3
"""
Overlay TCX speed / grade / heart-rate telemetry onto a video.

Usage:
    python3 overlay_telemetry.py --video PXL_20260726_111606330.mp4 \
                                  --tcx activity_24081337027.tcx \
                                  --output output.mp4

Parameters (everything except --video) can also live in a config file,
overlay_telemetry.conf in the script's own directory by default (--config PATH
to use another one). If it doesn't exist it is created from the parameters of
the current run; if it exists it is read, and command-line options override
it. --save-config rewrites it from the current effective parameters.

Requires: ffmpeg + ffprobe on PATH, and:
    pip install pillow --break-system-packages
"""

import argparse
import bisect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

try:
    from timezonefinder import TimezoneFinder
except ImportError:
    TimezoneFinder = None

# Matches Pixel-style filenames: PXL_20260726_111606330.mp4
#   group1 = YYYYMMDD, group2 = HHMMSS, group3 = milliseconds
FILENAME_RE = re.compile(r'(\d{8})_(\d{6})(\d{3})')

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


# --------------------------------------------------------------------------
# Video / filename helpers
# --------------------------------------------------------------------------

def parse_video_start(filename, local_tzinfo, tcx_times=None):
    """Extract the recording start time from a PXL_YYYYMMDD_HHMMSSmmm.mp4
    style filename and return it as a local datetime in `local_tzinfo`.

    Some phones (notably stock Pixel Camera) actually stamp filenames in
    UTC already, despite appearing to be local time. To handle both cases
    without guessing wrong, this computes two candidate interpretations in local time:

      1. "local"  - the filename is already local time in `local_tzinfo`.
      2. "utc"    - the filename was stamped in UTC, converted to `local_tzinfo`.

    If tcx_times (a sorted list of datetimes converted to local_tzinfo) is given,
    whichever candidate falls inside the TCX track's local time range is used.
    Otherwise, the "local" interpretation is used by default.
    """
    m = FILENAME_RE.search(os.path.basename(filename))
    if not m:
        raise ValueError(
            f"Could not parse a YYYYMMDD_HHMMSSmmm timestamp from filename: {filename}"
        )
    date_s, time_s, ms_s = m.groups()
    naive_dt = datetime.strptime(date_s + time_s, "%Y%m%d%H%M%S")
    naive_dt = naive_dt.replace(microsecond=int(ms_s) * 1000)

    candidate_local = naive_dt.replace(tzinfo=local_tzinfo)
    candidate_utc = naive_dt.replace(tzinfo=timezone.utc).astimezone(local_tzinfo)

    if tcx_times:
        tcx_start, tcx_end = tcx_times[0], tcx_times[-1]
        local_fits = tcx_start <= candidate_local <= tcx_end
        utc_fits = tcx_start <= candidate_utc <= tcx_end

        if local_fits and not utc_fits:
            print(f"  Filename interpreted as LOCAL time ({local_tzinfo}) -> matches TCX range.")
            return candidate_local
        if utc_fits and not local_fits:
            print("  Filename interpreted as UTC already (not local time) -> matches TCX range. "
                  "(Some phones, e.g. Pixel's stock Camera app, stamp filenames in UTC "
                  "even though it looks like local time.)")
            return candidate_utc
        if local_fits and utc_fits:
            # Ambiguous by range alone (can happen on long rides where both
            # candidates land inside the TCX's time span). Every real case in
            # this project has turned out to be UTC (this appears to be a
            # consistent Pixel Camera behavior), so prefer that over local
            # when both fit, rather than defaulting to local.
            print("  Both LOCAL and UTC interpretations fall inside the TCX range; "
                  "defaulting to UTC (matches Pixel's known filename behavior). "
                  "Use --tz-offset/--tz-name to force one explicitly if this is wrong.")
            return candidate_utc
        print("  WARNING: neither LOCAL nor UTC interpretation of the filename timestamp "
              "falls inside the TCX track's time range. Defaulting to LOCAL, but double-check "
              "the file/track actually correspond to each other.")
        return candidate_local

    return candidate_local


def detect_timezone_from_tcx(points):
    """Look up the IANA timezone for the first TCX track point's coordinates.
    Exits the program on failure rather than silently falling back to UTC -
    a wrong timezone produces a wrong timestamp overlay with no indication
    anything went wrong."""
    if TimezoneFinder is None:
        sys.exit(
            "ERROR: timezonefinder is not installed, and no --tz-offset/--tz-name "
            "was given, so the timezone can't be determined.\n"
            "Install it with: pip install timezonefinder\n"
            "...or pass --tz-offset <hours> / --tz-name <IANA name> explicitly."
        )
    tf = TimezoneFinder()
    first = points[0]
    tz_name = tf.timezone_at(lat=first["lat"], lng=first["lon"])
    if tz_name is None:
        sys.exit(
            "ERROR: could not resolve a timezone for the TCX's starting coordinates "
            f"({first['lat']}, {first['lon']}).\n"
            "Pass --tz-offset <hours> or --tz-name <IANA name> explicitly instead."
        )
    print(f"  Detected timezone from TCX start location: {tz_name}")
    return ZoneInfo(tz_name)


def ffprobe_video_info(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,width,height:stream_tags=rotate:stream_side_data=rotation",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    width = int(stream["width"])
    height = int(stream["height"])
    duration = float(data["format"]["duration"])

    # Detect rotation, preserving its SIGN/direction (not just "is it a quarter turn").
    # Two different metadata conventions exist in the wild:
    #   - legacy 'rotate' tag: degrees to rotate CLOCKWISE for correct display.
    #   - newer 'Display Matrix' side_data 'rotation': the opposite sign convention
    #     for the same visual effect (e.g. side_data -90 == legacy tag +90).
    # We normalize everything to the legacy tag's convention (clockwise degrees)
    # so a single, consistent value drives both our own frame-size swap AND an
    # explicit rotation filter we apply ourselves during compositing - rather
    # than depending on ffmpeg's own auto-rotate, whose behavior has differed
    # across ffmpeg versions/builds especially with -filter_complex.
    rotation = 0
    tag_rotate = stream.get("tags", {}).get("rotate")
    if tag_rotate is not None:
        try:
            rotation = int(tag_rotate)
        except ValueError:
            pass
    if rotation == 0:
        for sd in stream.get("side_data_list", []):
            if "rotation" in sd:
                try:
                    rotation = -int(sd["rotation"])
                except (TypeError, ValueError):
                    pass
                break

    rotation = rotation % 360  # normalize to 0-359, direction preserved
    if rotation in (90, 270):
        width, height = height, width
        print(f"  Detected {rotation}-degree rotation metadata - treating as {width}x{height} for overlay layout.")

    return fps, width, height, duration, rotation


def detect_vaapi_device():
    """Return a VAAPI device only if FFmpeg can encode a test frame with it."""
    candidates = []
    configured_device = os.environ.get("VAAPI_DEVICE")
    if configured_device:
        candidates.append(configured_device)
    try:
        candidates.extend(
            os.path.join("/dev/dri", name)
            for name in os.listdir("/dev/dri")
            if name.startswith("renderD")
        )
    except OSError:
        pass

    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return None, "no /dev/dri/renderD* device was found"

    failures = []
    for device in candidates:
        try:
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-vaapi_device", device,
                "-f", "lavfi", "-i", "color=c=black:s=128x128:d=0.04",
                "-frames:v", "1", "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi", "-f", "null", "-",
            ], capture_output=True, text=True)
        except OSError as error:
            failures.append(f"{device}: {error}")
            continue

        if result.returncode == 0:
            return device, ""
        error = result.stderr.splitlines()
        failures.append(f"{device}: {error[0] if error else 'FFmpeg returned no error message'}")

    return None, "; ".join(failures)


def rotation_filter(rotation):
    """ffmpeg video filter fragment (with trailing comma) that applies the given
    clockwise-degrees rotation explicitly, or '' if no rotation is needed."""
    if rotation == 90:
        return "transpose=1,"
    if rotation == 270:
        return "transpose=2,"
    if rotation == 180:
        return "hflip,vflip,"
    return ""


# --------------------------------------------------------------------------
# TCX parsing / telemetry computation
# --------------------------------------------------------------------------

def _child_text(element, name):
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            return child.text
    return None


def _find_text(element, name):
    for child in element.iter():
        if child is not element and child.tag.rsplit("}", 1)[-1] == name:
            return child.text
    return None


def load_tcx(path):
    root = ElementTree.parse(path).getroot()
    points = []
    for trackpoint in root.iter():
        if trackpoint.tag.rsplit("}", 1)[-1] != "Trackpoint":
            continue

        time_text = _child_text(trackpoint, "Time")
        position = next(
            (child for child in trackpoint if child.tag.rsplit("}", 1)[-1] == "Position"),
            None,
        )
        if not time_text or position is None:
            continue

        latitude = _child_text(position, "LatitudeDegrees")
        longitude = _child_text(position, "LongitudeDegrees")
        if latitude is None or longitude is None:
            continue

        point_time = datetime.fromisoformat(time_text.replace("Z", "+00:00"))
        if point_time.tzinfo is None:
            point_time = point_time.replace(tzinfo=timezone.utc)

        def parse_number(value, cast=float):
            try:
                return cast(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        points.append({
            "time": point_time,
            "lat": float(latitude),
            "lon": float(longitude),
            "ele": parse_number(_child_text(trackpoint, "AltitudeMeters")) or 0.0,
            "hr": parse_number(_find_text(trackpoint, "Value"), int),
            "temperature": parse_number(
                _find_text(trackpoint, "Temperature") or _find_text(trackpoint, "Temp")
                or _find_text(trackpoint, "atemp")
            ),
            "speed_mps": parse_number(_find_text(trackpoint, "Speed")),
            "distance_m": parse_number(_child_text(trackpoint, "DistanceMeters")),
        })
    points.sort(key=lambda pt: pt["time"])
    return points


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def enrich_points(points, smoothing_seconds=0.0):
    """Compute per-point speed (km/h), grade (%), and cumulative distance (km),
    then smooth speed and grade with a centered TIME-window moving average
    (based on each point's actual timestamp, not point count - TCX points aren't
    necessarily evenly spaced) to reduce GPS jitter. TCX-recorded speed and
    distance are preferred, with GPS-derived values used when TCX omits them.
    Heart rate is left as-is
    (nearest-known reading, handled in sample_at) - not smoothed."""
    n = len(points)
    if not points:
        return points

    first_recorded_distance = next(
        (p["distance_m"] for p in points if p.get("distance_m") is not None), 0.0
    )
    points[0]["speed"] = (points[0].get("speed_mps") or 0.0) * 3.6
    points[0]["grade"] = 0.0
    points[0]["dist_cum"] = max(
        0.0, (points[0]["distance_m"] - first_recorded_distance) / 1000.0
    ) if points[0].get("distance_m") is not None else 0.0

    for i in range(1, n):
        p0, p1 = points[i - 1], points[i]
        gps_dist = haversine(p0["lat"], p0["lon"], p1["lat"], p1["lon"])
        dt = (p1["time"] - p0["time"]).total_seconds()
        if p1.get("distance_m") is not None:
            p1["dist_cum"] = max(0.0, (p1["distance_m"] - first_recorded_distance) / 1000.0)
        else:
            p1["dist_cum"] = p0["dist_cum"] + gps_dist / 1000.0
        dist = max(0.0, (p1["dist_cum"] - p0["dist_cum"]) * 1000.0)
        delev = p1["ele"] - p0["ele"]
        recorded_speed = p1.get("speed_mps")
        p1["speed"] = recorded_speed * 3.6 if recorded_speed is not None else (
            gps_dist / dt * 3.6 if dt > 0 else 0.0
        )
        p1["grade"] = (delev / dist * 100) if dist > 1e-3 else 0.0

    if smoothing_seconds > 0:
        times = [p["time"] for p in points]
        half = smoothing_seconds / 2.0

        def smooth_series(vals):
            out = [0.0] * n
            lo = hi = 0
            for i in range(n):
                t = times[i]
                while lo < i and (t - times[lo]).total_seconds() > half:
                    lo += 1
                if hi < lo:
                    hi = lo
                while hi < n - 1 and (times[hi + 1] - t).total_seconds() <= half:
                    hi += 1
                window = vals[lo:hi + 1]
                out[i] = sum(window) / len(window)
            return out

        speeds = smooth_series([p["speed"] for p in points])
        grades = smooth_series([p["grade"] for p in points])
        for i in range(n):
            points[i]["speed"] = speeds[i]
            points[i]["grade"] = grades[i]

    return points


def sample_at(points, times, t):
    """Return the latest recorded TCX sample at or before real-world time t.

    Sparse track recording leaves values unchanged between recorded points.
    """
    idx = max(0, bisect.bisect_right(times, t) - 1)
    return points[idx]


# --------------------------------------------------------------------------
# Wind data (Open-Meteo, no API key needed)
# --------------------------------------------------------------------------

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


OPEN_METEO_URLS = {"archive": OPEN_METEO_ARCHIVE, "forecast": OPEN_METEO_FORECAST}


WIND_VARIABLES = {"gust": "wind_gusts_10m", "mean": "wind_speed_10m"}


def _open_meteo_request(base_url, locations, start_date, end_date, speed_variable="wind_gusts_10m"):
    """One Open-Meteo call for one or more (lat, lon) locations -> a list with one
    sorted [(utc_datetime, speed_kmh, from_deg), ...] series per location, in the
    same order as `locations`. `speed_variable` is the Open-Meteo speed series to
    use (wind_gusts_10m or wind_speed_10m); the direction is always the mean
    wind direction, since Open-Meteo has no separate gust direction."""
    query = urllib.parse.urlencode({
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in locations),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in locations),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": f"{speed_variable},wind_direction_10m",
        "wind_speed_unit": "kmh",
        "timezone": "GMT",
    }, safe=",")
    req = urllib.request.Request(f"{base_url}?{query}",
                                 headers={"User-Agent": "overlay_telemetry.py"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    if isinstance(payload, dict) and payload.get("error"):
        raise ValueError(payload.get("reason", "Open-Meteo returned an error"))
    # One location -> a single object; several locations -> a list of objects.
    results = payload if isinstance(payload, list) else [payload]
    if len(results) != len(locations):
        raise ValueError(f"expected {len(locations)} locations, got {len(results)}")
    all_series = []
    for result in results:
        hourly = result.get("hourly") or {}
        series = []
        for ts, spd, deg in zip(hourly.get("time", []),
                                hourly.get(speed_variable, []),
                                hourly.get("wind_direction_10m", [])):
            if spd is None or deg is None:
                continue
            when = datetime.strptime(ts, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            series.append((when, float(spd), float(deg)))
        series.sort(key=lambda row: row[0])
        all_series.append(series)
    return all_series


def _wind_field(locations, all_series, gust):
    return [{"lat": lat, "lon": lon, "series": series, "times": [row[0] for row in series],
             "gust": gust}
            for (lat, lon), series in zip(locations, all_series)]


def pick_wind_locations(points, times, t_start, t_end, count=3, min_sep_m=1000.0):
    """Up to `count` (lat, lon) spots along the part of the track the video covers,
    evenly spaced in time. Spots closer than min_sep_m to an earlier one are
    dropped (a short video usually ends up with a single location)."""
    count = max(1, int(count))
    if count == 1:
        stamps = [t_start + (t_end - t_start) / 2]
    else:
        stamps = [t_start + (t_end - t_start) * k / (count - 1) for k in range(count)]
    locations = []
    for t in stamps:
        p = sample_at(points, times, t)
        if all(haversine(p["lat"], p["lon"], la, lo) >= min_sep_m for la, lo in locations):
            locations.append((p["lat"], p["lon"]))
    return locations


def fetch_wind_field(locations, t_start, t_end, source="auto", speed_type="gust"):
    """Hourly 10 m wind (speed km/h, direction the wind blows FROM, degrees) for
    each (lat, lon) in `locations`, covering [t_start, t_end], from Open-Meteo.

    source: "archive"  - ERA5 reanalysis only
            "forecast" - forecast API only (keeps ~3 months of past days)
            "auto"     - ERA5 archive when it fully covers the video, else the
                         forecast API.
    speed_type: "gust" - wind_gusts_10m (maximum of the preceding hour)
                "mean" - wind_speed_10m (instantaneous)
    Returns a list of {"lat", "lon", "series", "times", "gust"} (one per location),
    or None on failure so the rest of the overlay still renders."""
    utc = timezone.utc
    t0 = t_start.astimezone(utc)
    t1 = t_end.astimezone(utc)
    pad = timedelta(hours=1)
    order = ["archive", "forecast"] if source == "auto" else [source]
    speed_variable = WIND_VARIABLES[speed_type]
    gust = speed_type == "gust"

    best, last_error = None, None
    for name in order:
        try:
            all_series = _open_meteo_request(OPEN_METEO_URLS[name], locations,
                                             (t0 - pad).date(), (t1 + pad).date(),
                                             speed_variable)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            last_error = f"{name}: {error}"
            continue
        if not all_series or not all_series[0]:
            last_error = f"{name}: no wind data returned for this date"
            continue
        first = all_series[0]
        # Need a sample at/before the start and at/after the end so every frame is
        # interpolated rather than held constant (ERA5 lags real time by days, so
        # recent rides fail this and fall through to the forecast API in "auto").
        covers = first[0][0] <= t0 and first[-1][0] >= t1
        if covers:
            print(f"  Wind source: Open-Meteo {name} API, {speed_variable} "
                  f"({len(first)} hourly samples, {len(locations)} location(s))")
            return _wind_field(locations, all_series, gust)
        best = best or (name, all_series)
    if best:
        print(f"  WARNING: Open-Meteo {best[0]} data only partly covers the video; "
              "values are held constant outside it.")
        return _wind_field(locations, best[1], gust)
    print(f"  WARNING: could not get wind data ({last_error}). "
          "Wind box will be omitted. Check your internet connection.")
    return None


def wind_at(series, times, t, gust=False):
    """(speed_kmh, from_deg) at time t: direction interpolated around the circle
    (so 350 deg -> 10 deg goes via north). Speed is interpolated linearly, except
    for gusts: Open-Meteo's gust value at HH:00 is the maximum of the hour BEFORE
    it, so every moment in (HH-1:00, HH:00] takes that HH:00 value as is."""
    if not series:
        return None
    i = bisect.bisect_right(times, t)
    if i == 0:
        return series[0][1], series[0][2]
    if i >= len(series):
        return series[-1][1], series[-1][2]
    (ta, sa, da), (tb, sb, db) = series[i - 1], series[i]
    span = (tb - ta).total_seconds()
    f = (t - ta).total_seconds() / span if span > 0 else 0.0
    # gust: the sample that closes the hour t falls in (at exactly HH:00 that is
    # HH:00 itself, a moment later it is the next one).
    speed = series[bisect.bisect_left(times, t)][1] if gust else sa + (sb - sa) * f
    x = (1 - f) * math.sin(math.radians(da)) + f * math.sin(math.radians(db))
    y = (1 - f) * math.cos(math.radians(da)) + f * math.cos(math.radians(db))
    return speed, math.degrees(math.atan2(x, y)) % 360.0


def wind_at_position(field, lat, lon, t):
    """(speed_kmh, from_deg) at time t for the rider's position (lat, lon): each
    location's wind is interpolated in time (wind_at), then the locations are
    blended by inverse-square distance (directions around the circle)."""
    if not field:
        return None
    samples = []
    for loc in field:
        w = wind_at(loc["series"], loc["times"], t, loc.get("gust", False))
        if w is not None:
            samples.append((haversine(lat, lon, loc["lat"], loc["lon"]), w))
    if not samples:
        return None
    nearest = min(samples, key=lambda s: s[0])
    if len(samples) == 1 or nearest[0] < 1.0:
        return nearest[1]
    weights = [1.0 / (d * d) for d, _ in samples]
    total = sum(weights)
    speed = sum(wt * w[0] for wt, (_, w) in zip(weights, samples)) / total
    x = sum(wt * math.sin(math.radians(w[1])) for wt, (_, w) in zip(weights, samples))
    y = sum(wt * math.cos(math.radians(w[1])) for wt, (_, w) in zip(weights, samples))
    return speed, math.degrees(math.atan2(x, y)) % 360.0


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial compass bearing (0 = north, 90 = east) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def add_headings(points, window_seconds=6.0, min_move_m=4.0):
    """Give every point a GPS route heading (degrees, 0 = north): the bearing
    between the points about window_seconds/2 before and after it, which smooths
    out GPS jitter. While (almost) standing still the last heading is held, so
    the wind compass doesn't spin at red lights."""
    n = len(points)
    times = [p["time"] for p in points]
    half = timedelta(seconds=window_seconds / 2.0)
    last = None
    for i, p in enumerate(points):
        lo = bisect.bisect_left(times, times[i] - half)
        hi = bisect.bisect_right(times, times[i] + half) - 1
        if i > 0:
            lo = min(lo, i - 1)  # sparse recording: always look at a neighbour
        if i < n - 1:
            hi = max(hi, i + 1)
        a, b = points[lo], points[min(hi, n - 1)]
        if haversine(a["lat"], a["lon"], b["lat"], b["lon"]) >= min_move_m:
            last = bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])
        p["heading"] = last
    first = next((p["heading"] for p in points if p["heading"] is not None), None)
    for p in points:  # fill the stationary start of the track
        if p["heading"] is not None:
            break
        p["heading"] = first


# --------------------------------------------------------------------------
# Frame rendering
# --------------------------------------------------------------------------

def build_map_projection(points, box_w, box_h, pad=6):
    """Precompute a reusable lat/lon -> local pixel projection for a map box
    of size box_w x box_h, plus the full route already projected through it.
    Longitude is scaled by cos(mean latitude) so the route isn't stretched.
    Returns (route_px, project_fn, total_distance_km).
    """
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    mean_lat_rad = math.radians((min_lat + max_lat) / 2.0)
    lon_scale = math.cos(mean_lat_rad) or 1e-9

    lat_span = max(max_lat - min_lat, 1e-9)
    lon_span = max((max_lon - min_lon) * lon_scale, 1e-9)

    avail_w = box_w - 2 * pad
    avail_h = box_h - 2 * pad
    scale = min(avail_w / lon_span, avail_h / lat_span)

    drawn_w = lon_span * scale
    drawn_h = lat_span * scale
    off_x = pad + (avail_w - drawn_w) / 2.0
    off_y = pad + (avail_h - drawn_h) / 2.0

    def project(lat, lon):
        x = off_x + (lon - min_lon) * lon_scale * scale
        y = off_y + (max_lat - lat) * scale  # flip: north is up
        return x, y

    route_px = [project(p["lat"], p["lon"]) for p in points]
    return route_px, project, points[-1]["dist_cum"]


def get_font(size, bold=False):
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def format_pace(speed_kmh):
    """Convert km/h to a min:sec-per-km pace string, e.g. 4:32. Returns '--:--'
    for zero/near-zero speed where pace would be undefined/huge."""
    if speed_kmh is None or speed_kmh < 0.5:
        return "--:--"
    pace_min_per_km = 60.0 / speed_kmh
    minutes = int(pace_min_per_km)
    seconds = int(round((pace_min_per_km - minutes) * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"


def interpolate_color(stops, value):
    """stops: list of (value, (r,g,b)) sorted ascending. Clamps outside the range,
    linearly interpolates between the two surrounding stops otherwise."""
    if value <= stops[0][0]:
        return stops[0][1]
    if value >= stops[-1][0]:
        return stops[-1][1]
    for (v0, c0), (v1, c1) in zip(stops, stops[1:]):
        if v0 <= value <= v1:
            t = (value - v0) / (v1 - v0) if v1 > v0 else 0.0
            return tuple(int(round(c0[j] + (c1[j] - c0[j]) * t)) for j in range(3))
    return stops[-1][1]


GRADE_COLOR_STOPS = [(0.0, (70, 190, 100)), (10.0, (225, 70, 60))]  # 0%=green, 10%+=red

HR_PCT_COLOR_STOPS = [
    (0, (150, 150, 150)),   # grey
    (70, (70, 190, 100)),   # green
    (80, (150, 95, 45)),    # brown
    (90, (225, 60, 50)),    # red (also covers 100%+)
]


def render_frame(
    data, w, h, map_ctx=None, show_speed=True, show_grade=True,
    show_hr=True, show_latlong=True, show_timestamp=True, speed_type="speed",
    max_hr=None, wind=None, wind_arrow="to", wind_up="heading", wind_gust=False):

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    scale = w / 720.0
    v_offset = int(0.02 * h)  # 2% vertical offset relative to video height

    f_big = get_font(int(48 * scale), bold=True)
    f_medium = get_font(int(40 * scale), bold=True)
    f_label = get_font(int(18 * scale))
    f_small = get_font(int(20 * scale))

    def panel(xy, fill=(0, 0, 0, 130)):
        d.rounded_rectangle(xy, radius=int(12 * scale), fill=fill)

    margin = int(30 * scale)
    gap = int(15 * scale)

    # lat/lon + timestamp now at the TOP (shifted down by 2% of height)
    corner_y = int(30 * scale) + v_offset
    corner_h = int(46 * scale)

    # SPEED/GRADE/HR row now at the BOTTOM (shifted up by 2% of height)
    panel_h = int(110 * scale)
    bottom_row_y = h - margin - panel_h - v_offset
    panel_w = (w - margin * 2 - gap * 2) / 3

    def bottom_panel(i, label, value, unit, f_size=f_big, value_color=(255, 255, 255, 255)):
        x0 = margin + i * (panel_w + gap)
        x1 = x0 + panel_w
        panel((x0, bottom_row_y, x1, bottom_row_y + panel_h))
        center_x = (x0 + x1) / 2
        label_width = d.textlength(label, font=f_label)

        d.text(
            (center_x - label_width / 2, bottom_row_y + 14 * scale), 
            label,
            font=f_label, 
            fill=(255, 255, 255, 200)
        )

        value_width = d.textlength(value, font=f_size)
        unit_width = d.textlength(unit, font=f_label) if unit else 0
        unit_gap = 8 * scale if unit else 0
        group_width = value_width + unit_gap + unit_width
        value_x = center_x - group_width / 2

        d.text(
            (value_x, bottom_row_y + 42 * scale), 
            value, 
            font=f_size, 
            fill=value_color
        )

        if unit:
            d.text(
                (value_x + value_width + unit_gap, bottom_row_y + 68 * scale), 
                unit,
                font=f_label, 
                fill=(255, 255, 255, 180)
            )
            

    if show_speed:

        if speed_type == "pace":
            bottom_panel(0, "Pace", format_pace(data["speed"]), "/km", f_big)
        else:
            bottom_panel(0, "Speed", f"{data['speed']:.1f}", "km/h", f_big)
            
    if show_grade:
        
        grade_sign = "+" if data["grade"] >= 0 else ""
        grade_color = interpolate_color(GRADE_COLOR_STOPS, data["grade"])
        bottom_panel(1, "Grade", f"{grade_sign}{data['grade']:.1f}", "%", f_big,
                     value_color=(*grade_color, 255))

    if show_hr:

        if data["hr"] is None:
            hr_text = "--"
            hr_color = (255, 255, 255)

        elif max_hr:
            hr_pct = data["hr"] / max_hr * 100
            hr_text = f"{round(hr_pct)}"
            hr_color = interpolate_color(HR_PCT_COLOR_STOPS, hr_pct)
            hr_label = "% of Max HR"
            hr_unit = ""

        else:
            hr_text = f"{int(round(data['hr']))}"
            hr_color = (255, 255, 255)  # white when no --max-hr given
            hr_label = "HR"
            hr_unit = "bpm"

        bottom_panel(2, hr_label, hr_text, hr_unit, f_big, value_color=(*hr_color, 255))

    # Mini map, directly below the top-right timestamp panel (right-aligned to same margin)
    map_w = panel_w
    map_h = panel_h * 1.4
    strip_h = int(30 * scale)
    map_bottom_inset = int(6 * scale)
    map_y0 = corner_y + corner_h + gap
    map_y1 = map_y0 + map_h
    if map_ctx is not None:
        map_x1 = w - margin
        map_x0 = map_x1 - map_w
        panel((map_x0, map_y0, map_x1, map_y1), fill=(0, 0, 0, 110))

        route_px, project, total_distance = map_ctx
        if len(route_px) >= 2:
            offset_route = [(map_x0 + x, map_y0 + y) for x, y in route_px]
            d.line(offset_route, fill=(255, 255, 255, 160), width=max(1, int(2 * scale)))

        cur_x, cur_y = project(data["lat"], data["lon"])
        cx, cy = map_x0 + cur_x, map_y0 + cur_y
        dot_r = max(2, int(5 * scale))
        d.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=(255, 90, 90, 255),
                   outline=(255, 255, 255, 230), width=max(1, int(1.5 * scale)))

        # Current distance, along the bottom edge of the map panel
        dist_text = f"{data.get('dist_cum', 0.0):.1f} of {total_distance:.1f} km"
        strip_y1 = map_y1 - map_bottom_inset
        strip_y0 = strip_y1 - strip_h
        d.rounded_rectangle((map_x0, strip_y0, map_x1, strip_y1), radius=int(12 * scale),
                             corners=(False, False, True, True), fill=(0, 0, 0, 150))
        d.text(((map_x0 + map_x1) / 2, strip_y0 + strip_h / 2), dist_text,
               font=f_small, fill=(255, 255, 255, 230), anchor="mm")

    # Wind box: mirror of the minimap on the LEFT (same size/top), a compass circle
    # (north up, like the minimap) with an arrow, and the speed strip below it.
    if wind is not None:
        wind_speed, wind_from = wind
        arrow_deg = (wind_from + 180.0) % 360.0 if wind_arrow == "to" else wind_from
        # Heading-up: the GPS route direction is screen-up, so rotate everything
        # (arrow and the N marker) by -heading. Falls back to north-up if unknown.
        heading = data.get("heading") if wind_up == "heading" else None
        rot = heading or 0.0
        arrow_deg = (arrow_deg - rot) % 360.0
        wx0 = margin
        wx1 = wx0 + map_w
        panel((wx0, map_y0, wx1, map_y1), fill=(0, 0, 0, 110))

        pad = int(8 * scale)
        content_h = map_h - strip_h - map_bottom_inset
        r = min(map_w / 2 - pad, content_h / 2 - pad) - 5 * scale  # room for the 'forward' marker
        cx = (wx0 + wx1) / 2
        cy = map_y0 + content_h / 2

        ring_w = max(1, int(2 * scale))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 170),
                  width=ring_w, fill=(0, 0, 0, 50))
        tick = 5 * scale
        for deg in (0, 90, 180, 270):  # N, E, S, W ticks pointing inward (rotated with the compass)
            a = math.radians(deg - rot)
            sx, sy = math.sin(a), -math.cos(a)
            d.line((cx + sx * r, cy + sy * r, cx + sx * (r - tick), cy + sy * (r - tick)),
                   fill=(255, 110, 100, 230) if deg == 0 else (255, 255, 255, 200), width=ring_w)
        na = math.radians(-rot)
        d.text((cx + math.sin(na) * r * 0.78, cy - math.cos(na) * r * 0.78), "N",
               font=f_label, fill=(255, 110, 100, 230), anchor="mm")
        if heading is not None:  # small triangle on top = direction of travel
            d.polygon([(cx, cy - r - 5 * scale), (cx - 4 * scale, cy - r - 1 * scale),
                       (cx + 4 * scale, cy - r - 1 * scale)], fill=(255, 255, 255, 230))

        # Arrow (points where the wind blows TO, or FROM with --wind-arrow from)
        ux, uy = math.sin(math.radians(arrow_deg)), -math.cos(math.radians(arrow_deg))
        px, py = -uy, ux  # perpendicular, for the arrowhead width
        half = r * 0.58
        head_len = max(8 * scale, half * 0.7)
        head_hw = head_len * 0.55
        tip = (cx + ux * half, cy + uy * half)
        tail = (cx - ux * half, cy - uy * half)
        hb = (tip[0] - ux * head_len, tip[1] - uy * head_len)
        arrow_col = (120, 200, 255, 255)
        d.line((tail, hb), fill=arrow_col, width=max(2, int(4 * scale)))
        d.polygon([tip, (hb[0] + px * head_hw, hb[1] + py * head_hw),
                   (hb[0] - px * head_hw, hb[1] - py * head_hw)], fill=arrow_col)

        # Wind speed strip along the bottom edge, same style as the distance strip
        strip_y1 = map_y1 - map_bottom_inset
        strip_y0 = strip_y1 - strip_h
        d.rounded_rectangle((wx0, strip_y0, wx1, strip_y1), radius=int(12 * scale),
                            corners=(False, False, True, True), fill=(0, 0, 0, 150))
        d.text(((wx0 + wx1) / 2, strip_y0 + strip_h / 2), (f"Gust {wind_speed:.1f} km/h" if wind_gust else f"{wind_speed:.1f} km/h"),
               font=f_small, fill=(255, 255, 255, 230), anchor="mm")

    # lat/lon: now top-left
    if show_latlong:
        bl_text = f"{data['lat']:.5f}, {data['lon']:.5f}"
        bbox = d.textbbox((0, 0), bl_text, font=f_small)
        bl_w = (bbox[2] - bbox[0]) + int(32 * scale)
        bl_h = corner_h
        bl_x0 = margin
        bl_y0 = corner_y
        panel((bl_x0, bl_y0, bl_x0 + bl_w, bl_y0 + bl_h))
        d.text((bl_x0 + 16 * scale, bl_y0 + 12 * scale), bl_text, font=f_small, fill=(255, 255, 255, 230))

    # timestamp: now top-right
    if show_timestamp:
        br_text = data["time"].strftime("%Y-%m-%d %H:%M:%S")
        bbox = d.textbbox((0, 0), br_text, font=f_small)
        br_w = (bbox[2] - bbox[0]) + int(32 * scale)
        br_h = corner_h
        br_x1 = w - margin
        br_x0 = br_x1 - br_w
        br_y0 = corner_y
        panel((br_x0, br_y0, br_x1, br_y0 + br_h))
        d.text((br_x0 + 16 * scale, br_y0 + 12 * scale), br_text, font=f_small, fill=(255, 255, 255, 230))

    if data.get("temperature") is not None:
        temp_text = f"{data['temperature']:.1f}\u00b0C"
        bbox = d.textbbox((0, 0), temp_text, font=f_small)
        temp_w = (bbox[2] - bbox[0]) + int(32 * scale)
        temp_x0 = (w - temp_w) / 2
        panel((temp_x0, corner_y, temp_x0 + temp_w, corner_y + corner_h))
        d.text((temp_x0 + 16 * scale, corner_y + 12 * scale), temp_text,
               font=f_small, fill=(255, 255, 255, 230))

    return img


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

CONFIG_NAME = "ot.conf"
# Default config location: next to this script (not the video or the current directory).
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)
CONFIG_SKIP = {"video", "config", "save_config", "help"}  # never stored in the config
CONFIG_EXAMPLES = {  # shown as commented-out lines for free-form parameters
    "tcx": "activity.tcx",
    "output": "output.mp4",
    "tz_offset": "3",
    "tz_name": "Europe/Istanbul",
    "gps_offset": "1.5",
    "overlay_fps": "10",
    "smooth_window": "3",
    "max_hr": "190",
    "wind_points": "1",
}


def on_off(v):
    v = str(v).strip().lower()
    if v not in ("on", "off"):
        raise argparse.ArgumentTypeError("must be 'on' or 'off'")
    return v == "on"


def build_parser():
    ap = argparse.ArgumentParser(description="Overlay TCX speed/grade/HR telemetry onto a video")
    ap.add_argument("--video", required=True, help="Source video file")
    ap.add_argument("--tcx", required=True, help="TCX activity file with track points and telemetry")
    ap.add_argument("--output", required=False, default=None,
                     help="Output video file. Default: <video>_overlay<ext> next to the input video.")
    ap.add_argument("--config", default=DEFAULT_CONFIG, metavar="PATH",
                     help=f"Config file holding the parameters (everything except --video). "
                          f"Created from this run's parameters if missing, read if it exists; "
                          f"command-line options override it. Default: {CONFIG_NAME} "
                          f"in the script's directory.")
    ap.add_argument("--save-config", action="store_true",
                     help="Rewrite the config file from the current effective parameters "
                          "(config file + command line).")
    ap.add_argument("--tz-offset", type=float, default=None,
                     help="Hours to ADD to UTC for local time "
                          "(e.g. 3 for UTC+3). Overrides auto-detection. If omitted, "
                          "the timezone is auto-detected from the TCX's first coordinates.")
    ap.add_argument("--tz-name", default=None,
                     help="IANA timezone name to interpret the local time in "
                          "(e.g. Europe/Istanbul). Overrides auto-detection, but is "
                          "itself overridden by --tz-offset if both are given.")
    ap.add_argument("--gps-offset", type=float, default=0.0,
                     help="Extra seconds to shift the telemetry relative to the video, "
                          "for manual fine sync if things still look off.")
    ap.add_argument("--overlay-fps", type=float, default=5.0,
                     help="Frame rate to render the telemetry overlay at. Default: 5.")
    ap.add_argument("--smooth-window", type=float, default=0.0,
                     help="Smoothing window in seconds for speed/pace and grade. "
                          "Use 0 to disable. Default: 0.")
    ap.add_argument("--keep-temp", action=argparse.BooleanOptionalAction, default=False,
                     help="Keep the temporary overlay frames/video instead of deleting them. "
                          "Default: off.")
    ap.add_argument("--speed", type=on_off, default="on", metavar="{on,off}",
                     help="Show the speed panel. Default: on.")
    ap.add_argument("--grade", type=on_off, default="on", metavar="{on,off}",
                     help="Show the grade panel. Default: on.")
    ap.add_argument("--heartrate", type=on_off, default="on", metavar="{on,off}",
                     help="Show the heart-rate panel. Default: on.")
    ap.add_argument("--minimap", type=on_off, default="on", metavar="{on,off}",
                     help="Show the mini route map. Default: on.")
    ap.add_argument("--latlong", type=on_off, default="on", metavar="{on,off}",
                     help="Show the lat/lon readout. Default: on.")
    ap.add_argument("--timestamp", type=on_off, default="on", metavar="{on,off}",
                     help="Show the date/time readout. Default: on.")
    ap.add_argument("--speed-type", choices=["speed", "pace"], default="speed",
                     help="Display speed as 'speed' (km/h) or 'pace' (min:sec per km). Default: speed.")
    ap.add_argument("--max-hr", type=int, default=None,
                     help="Max heart rate (bpm). If given, the HR panel shows %% of max instead of raw bpm.")
    ap.add_argument("--wind", type=on_off, default="on", metavar="{on,off}",
                     help="Show the wind direction/speed box (weather from Open-Meteo, "
                          "needs internet). Default: on.")
    ap.add_argument("--wind-source", choices=["auto", "archive", "forecast"], default="auto",
                     help="Where the wind comes from: 'archive' (ERA5 reanalysis), "
                          "'forecast' (Open-Meteo forecast API's past data) or 'auto' "
                          "(archive when it fully covers the video, otherwise forecast). "
                          "Default: auto.")
    ap.add_argument("--wind-speed-type", choices=["gust", "mean"], default="gust",
                     help="Speed shown in the wind box: 'gust' (wind gusts at 10 m, the "
                          "maximum of the hour; labelled 'Gust') or 'mean' (wind speed at "
                          "10 m). The arrow always uses the mean wind direction. "
                          "Default: gust.")
    ap.add_argument("--wind-points", type=int, default=3,
                     help="How many locations along the video's part of the track to "
                          "fetch wind for; the wind at your GPS position is blended from "
                          "them (spots closer than 1 km are merged). Default: 3.")
    ap.add_argument("--wind-arrow", choices=["to", "from"], default="to",
                     help="Arrow points where the wind blows 'to' (default) or where it "
                          "comes 'from'.")
    ap.add_argument("--wind-up", choices=["heading", "north"], default="heading",
                     help="What points UP in the wind compass: the GPS route direction "
                          "('heading', default - arrow shows wind relative to your travel "
                          "direction) or true north ('north').")
    return ap


# --------------------------------------------------------------------------
# Config file
# --------------------------------------------------------------------------

def _config_actions(ap):
    # ap._actions is argparse's (stable, but private) list of defined options.
    return [a for a in ap._actions if a.option_strings and a.dest not in CONFIG_SKIP]


def _config_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _typed_default(action):
    d = action.default
    if isinstance(d, str) and action.type is not None:
        return action.type(d)  # argparse converts string defaults the same way
    return d


def _is_switch(action):
    return action.type is on_off or isinstance(action, argparse.BooleanOptionalAction)


def _alternatives(action):
    if _is_switch(action):
        return ["on", "off"]
    if action.choices:
        return [str(c) for c in action.choices]
    return []


def config_file_to_args(ap, path):
    """Turn a config file into command-line style arguments (so argparse does the
    type checking). Blank values and unknown keys are ignored."""
    actions = {a.dest: a for a in _config_actions(ap)}
    out = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if "=" not in line:
                print(f"  WARNING: {path}:{lineno}: ignoring line without '='")
                continue
            key, value = (s.strip() for s in line.split("=", 1))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            action = actions.get(key.lower().replace("-", "_"))
            if action is None:
                print(f"  WARNING: {path}:{lineno}: unknown or unsupported parameter '{key}' ignored")
                continue
            if value == "":
                continue  # blank = built-in default
            flag = action.option_strings[0]
            if _is_switch(action):
                value = {"true": "on", "yes": "on", "1": "on",
                         "false": "off", "no": "off", "0": "off"}.get(value.lower(), value.lower())
            if isinstance(action, argparse.BooleanOptionalAction):
                if value not in ("on", "off"):
                    print(f"  WARNING: {path}:{lineno}: '{key}' must be on or off; ignored")
                    continue
                out.append(flag if value == "on" else f"--no-{flag[2:]}")
            else:
                out.append(f"{flag}={value}")
    return out


def write_config(ap, args, path):
    """Write every parameter except --video as 'key = value' (the value this run
    uses), each preceded by its help text and followed by the other possible
    values as commented-out lines, so switching is just moving the '#'."""
    lines = [
        "# overlay_telemetry.py parameters",
        "#",
        "# Read automatically when this file exists; command-line options override it.",
        "# One 'key = value' per line; a blank value means the built-in default.",
        "# Commented-out 'key = value' lines are the other (non-active) values:",
        "# move the '#' to switch to one. The --video file is never stored here.",
        "",
    ]
    for action in _config_actions(ap):
        key = action.dest.replace("_", "-")
        shown = _config_value(getattr(args, action.dest))
        default = _config_value(_typed_default(action))
        lines.extend(textwrap.wrap((action.help or "").replace("%%", "%"), width=78,
                                   initial_indent="# ", subsequent_indent="# "))
        alts = _alternatives(action)
        if not alts and action.dest in CONFIG_EXAMPLES:
            alts = [CONFIG_EXAMPLES[action.dest]]
        if alts and "default" not in (action.help or "").lower():
            lines.append(f"# Built-in default: {default or '(none)'}")
        lines.append(f"{key} = {shown}".rstrip())
        lines.extend(f"# {key} = {alt}" for alt in alts if alt != shown)
        lines.append("")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        print(f"Config file written: {path}")
    except OSError as error:
        print(f"  WARNING: could not write config file {path}: {error}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = build_parser()

    # Find the config file path first, then feed its contents to the real parser
    # ahead of the command line so command-line options win.
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--config", default=DEFAULT_CONFIG)
    config_path = pre.parse_known_args()[0].config
    config_existed = os.path.isfile(config_path)
    config_args = config_file_to_args(ap, config_path) if config_existed else []
    args = ap.parse_args(config_args + sys.argv[1:])
    if config_existed:
        print(f"Loaded parameters from {config_path}")
    if args.save_config or not config_existed:
        write_config(ap, args, config_path)

    if args.output is None:
        video_dir, video_name = os.path.split(args.video)
        stem, ext = os.path.splitext(video_name)
        args.output = os.path.join(video_dir, f"{stem}_overlay{ext}")
        print(f"No --output given, defaulting to: {args.output}")

    print("Reading video info...")
    fps, width, height, duration, rotation = ffprobe_video_info(args.video)
    print(f"  {width}x{height} @ {fps:.2f}fps, duration {duration:.1f}s")

    print("Parsing TCX...")
    points = load_tcx(args.tcx)
    if not points:
        sys.exit("No track points with time and position found in TCX file.")
    points = enrich_points(points, smoothing_seconds=args.smooth_window)
    add_headings(points)

    print("Resolving timezone...")
    if args.tz_offset is not None:
        local_tz = timezone(timedelta(hours=args.tz_offset))
        print(f"  Using explicit --tz-offset: UTC+{args.tz_offset}")
    elif args.tz_name:
        local_tz = ZoneInfo(args.tz_name)
        print(f"  Using explicit --tz-name: {args.tz_name}")
    else:
        local_tz = detect_timezone_from_tcx(points)  # exits on failure, never returns None

    # Convert all TCX points to local timezone
    for p in points:
        p["time"] = p["time"].astimezone(local_tz)

    times = [p["time"] for p in points]
    print(f"  {len(points)} track points, local time: {times[0].strftime('%Y-%m-%d %H:%M:%S')} -> {times[-1].strftime('%Y-%m-%d %H:%M:%S')}")

    video_start = parse_video_start(args.video, local_tz, tcx_times=times)
    print(f"Video start (Local Time): {video_start.strftime('%Y-%m-%d %H:%M:%S')}")

    if not (times[0] <= video_start <= times[-1]):
        print("  WARNING: video start time falls outside the TCX track's time range "
              "under both interpretations. Check the file pairing, or pass --tz-offset/--gps-offset "
              "manually.")

    wind_field = None
    if args.wind:
        w_start = video_start + timedelta(seconds=args.gps_offset)
        w_end = w_start + timedelta(seconds=duration)
        locations = pick_wind_locations(points, times, w_start, w_end, count=args.wind_points)
        print(f"Fetching wind data from Open-Meteo (source: {args.wind_source}) "
              f"for {len(locations)} location(s)...")
        for lat, lon in locations:
            print(f"  {lat:.4f}, {lon:.4f}")
        wind_field = fetch_wind_field(locations, w_start, w_end, source=args.wind_source,
                                      speed_type=args.wind_speed_type)
        if wind_field:
            for label, t in (("start", w_start), ("end", w_end)):
                p = sample_at(points, times, t)
                w = wind_at_position(wind_field, p["lat"], p["lon"], t)
                if w:
                    heading = (f", route heading {p['heading']:.0f}\u00b0"
                               if p.get("heading") is not None else "")
                    print(f"  Wind {args.wind_speed_type} at video {label}: {w[0]:.1f} km/h from {w[1]:.0f}\u00b0 "
                          f"(arrow to {(w[1] + 180) % 360:.0f}\u00b0{heading})")

    tmpdir = tempfile.mkdtemp(prefix="telemetry_overlay_")
    frames_dir = os.path.join(tmpdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    # Precompute the mini-map's route projection once (it doesn't change frame to frame) -
    # box size must match render_frame's own layout math exactly (margin/gap/panel_w/panel_h).
    map_ctx = None
    
    if args.minimap:
        scale = width / 720.0
        margin = int(30 * scale)
        gap = int(15 * scale)
        panel_h = int(110 * scale)
        panel_w = (width - margin * 2 - gap * 2) / 3
        map_h = panel_h * 1.4
        map_strip_h = int(30 * scale)
        map_bottom_inset = int(6 * scale)
        map_content_h = map_h - map_strip_h - map_bottom_inset
        map_ctx = build_map_projection(points, box_w=panel_w, box_h=map_content_h)

    try:
        n_overlay_frames = int(duration * args.overlay_fps) + 1
        print(f"Rendering {n_overlay_frames} overlay frames at {args.overlay_fps}fps...")
        for i in range(n_overlay_frames):
            t_video = i / args.overlay_fps
            t_real = video_start + timedelta(seconds=t_video + args.gps_offset)
            data = sample_at(points, times, t_real)
            wind_now = wind_at_position(wind_field, data["lat"], data["lon"], t_real)
            frame = render_frame(
                data, width, height, map_ctx=map_ctx,
                show_speed=args.speed, show_grade=args.grade, show_hr=args.heartrate,
                show_latlong=args.latlong, show_timestamp=args.timestamp,
                speed_type=args.speed_type,
                max_hr=args.max_hr,
                wind=wind_now, wind_arrow=args.wind_arrow, wind_up=args.wind_up,
                wind_gust=args.wind_speed_type == "gust",
            )
            frame.save(os.path.join(frames_dir, f"frame_{i:06d}.png"))
            if i % 50 == 0:
                print(f"  {i}/{n_overlay_frames}")

        overlay_video = os.path.join(tmpdir, "overlay.mov")
        print("Encoding overlay frames to an alpha-channel video...")
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(args.overlay_fps),
            "-i", os.path.join(frames_dir, "frame_%06d.png"),
            "-c:v", "qtrle",
            overlay_video,
        ], check=True)

        print("Compositing overlay onto source video...")
        # -noautorotate + -display_rotation 0: disable ffmpeg's own implicit rotation
        # handling AND override the input's rotation metadata to 0 before it's read.
        # Without this, ffmpeg carries the source's original rotation side-data (e.g.
        # -90) through to the output even though we've already physically rotated the
        # pixels ourselves via the transpose filter below - causing a double rotation
        # that plays back sideways/landscape in metadata-aware players.
        # fps={fps}: pins the output to the source's actual frame rate, rather than
        # whatever ffmpeg derives from mixing the source's fps with the overlay's
        # (lower) --overlay-fps.
        # -video_track_timescale: derived from fps (matches what real camera-recorded
        # MP4s typically use) rather than a hardcoded device-specific value, so this
        # stays correct for any source frame rate, not just 30fps Pixel footage.
        rot_filter = rotation_filter(rotation)
        timescale = max(1, round(fps * 512))
        vaapi_device, vaapi_error = detect_vaapi_device()
        if vaapi_device:
            print(f"  Using VAAPI hardware encoding: {vaapi_device}")
            filter_complex = (
                f"[0:v]{rot_filter}setpts=PTS-STARTPTS[base];"
                f"[base][1:v]overlay=shortest=1,fps={fps}[composited];"
                "[composited]format=nv12,hwupload[v]"
            )
        else:
            print("  VAAPI unavailable; using FFmpeg's software encoder.")
            print(f"  VAAPI probe: {vaapi_error}")
            filter_complex = (
                f"[0:v]{rot_filter}setpts=PTS-STARTPTS[base];"
                f"[base][1:v]overlay=shortest=1,fps={fps}[v]"
            )

        composite_cmd = ["ffmpeg", "-y", "-noautorotate", "-display_rotation", "0"]
        if vaapi_device:
            composite_cmd.extend(["-vaapi_device", vaapi_device])
        composite_cmd.extend([
            "-i", args.video,
            "-i", overlay_video,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a?",
            "-video_track_timescale", str(timescale),
        ])
        if vaapi_device:
            composite_cmd.extend(["-c:v", "h264_vaapi", "-qp", "23"])
        composite_cmd.extend(["-c:a", "copy", args.output])
        subprocess.run(composite_cmd, check=True)
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"Temp files kept at: {tmpdir}")

    print(f"Done: {args.output}")


if __name__ == "__main__":
    main()