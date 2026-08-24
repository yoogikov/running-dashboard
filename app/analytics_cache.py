"""Per-run cached analytics: splits, elevation profile, pace series, laps —
everything splits.py/laps.py compute, computed once per run and reused on
every later selection instead of recomputed on each click. One JSON file per
run under data/analytics/<run_id>.json, written atomically (temp file +
os.replace), matching route_graph.json's own save convention.

Stub for now: get_or_compute returns an empty dict for every run, so
run_analyzer's select_run() has something to hand to registered sections
before any of them exist. Real computation (splits, elevation, pace, laps)
and the on-disk cache land together with splits.py.
"""

CACHE_VERSION = 1   # bump whenever splits.py's/laps.py's math changes


def get_or_compute(run_id: int) -> dict:
    return {}


def invalidate(run_id: int):
    pass
