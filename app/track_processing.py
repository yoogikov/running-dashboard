"""Pipeline applied to every imported run: smooth the raw GPS points (drop
outliers, reduce jitter) and recompute distance/pace/elevation from the
smoothed result before storing.

Road-snapping is deliberately NOT done here — it only happens transiently
when rendering routes on the map (see main.py), never stored. Everything
else (stats, pace, elevation, the DB itself) always uses this smoothed-but-
unsnapped track, since OSRM's matched geometry carries no elevation or
timestamps and would only lose data if treated as the source of truth.
"""
import gps_smooth
import gpx_parser


def process(parsed: dict) -> dict:
    raw_points = parsed.get("track_points") or []
    if len(raw_points) < 2:
        return parsed

    smoothed_points = gps_smooth.clean_track(raw_points)
    summary = gpx_parser.summarize_points(smoothed_points, parsed.get("source_file", ""))

    result = dict(parsed)
    result["track_points"] = smoothed_points
    result["distance_km"] = summary["distance_km"]
    result["duration_sec"] = summary["duration_sec"] or parsed["duration_sec"]
    result["avg_pace_sec_per_km"] = summary["avg_pace_sec_per_km"]
    result["elevation_gain_m"] = summary["elevation_gain_m"]
    result["date"] = summary["date"] or parsed["date"]
    return result
