"""Rebuilds data/route_graph.json from scratch, replaying every run in
data/running.db chronologically (see route_graph.rebuild()).

Merging is order-dependent — the earliest run to reach a spot seeds that
node's position — so the graph built by folder_watch.py's incremental,
one-run-at-a-time add_run() only comes out right when runs are dropped in
roughly the order they happened. A normal live import (one new run at a
time) is fine either way. Bulk-dropping many historical files at once is
not: folder_watch scans its directory alphabetically, which is very
unlikely to match when the runs actually happened, and the resulting
graph can come out visibly messier (spurious junctions, missing spurs)
than one built in the right order.

Run this after any bulk import to fix that — it doesn't touch running.db,
only rebuilds the graph cache from what's already stored there:

    python3 app/rebuild_graph.py
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import route_graph  # noqa: E402


def _progress(n, total, run_id, num_nodes):
    print(f"\r  {n}/{total} runs replayed (run {run_id}, {num_nodes} nodes so far)",
          end="", flush=True)


def main():
    graph = route_graph.rebuild(progress=_progress)
    saved = route_graph.load_raw()
    num_edges = len(saved["edges"]) if saved else "?"
    print(f"\ndone: {len(graph.lat)} nodes, {num_edges} edges "
          f"-> {route_graph.GRAPH_PATH}")


if __name__ == "__main__":
    main()
