"""Merges every run's GPS trace into a single undirected graph — one averaged
line per physical road — cached to data/route_graph.json.

WHY THIS EXISTS
The map used to draw every raw point of every run: 93,133 points across 51
runs, ~6,180 PIL line calls per frame, 230-550ms per redraw. Since the overlay
must re-render immediately on every zoom step (heat_overlay's draw_zoom patch,
or it desyncs from the tiles), that cost landed on every scroll notch. Viewport
culling didn't help — the same local loops are run over and over, so nearly
every run's bounding box intersects the view even zoomed deep. The data was the
problem: the same stretch of road stored and drawn forty separate times.

So instead of storing runs, store the road network they trace. Road-snapping
(OSRM) was tried and dropped as far too slow; this does the job locally with
path matching plus positional averaging.

THE PRIMITIVE
Everything is add_point(). A run is just that in a loop, threading each returned
node index into the next call's incoming_from. The first run and the fifty-first
take identical code paths — an empty graph simply yields no candidates every
time, so every point becomes a new node. There is no seeding special case.

Matching is deliberately not pointwise. A single point being within d of an
existing node means very little — roads cross, and parallel streets run close.
Instead a whole local PATH around the new point must match a whole local path
through the candidate node, at the same index offset, going the same direction.
That's what makes it safe to merge aggressively.

WHY MERGING DOES NOT MOVE NODES
The obvious design — merge by weighted-averaging the new reading into the node
it matched — was tried and produces a violently vibrating line. Measured on the
real history: the median turn angle at an interior node was 26.7 degrees and the
90th percentile 134 degrees, i.e. the line doubled back on itself constantly.

The cause is not noise and not the size of the average. A run laid down on its
own is smooth (turn p50 1.8 deg); the damage appears only as later runs merge
into it, and compounds with each one (p90: 6.6 deg at one run, 23.9 at two, 93.1
at five, 113.9 at ten). Every one of the 2,000 worst spikes was a node whose two
neighbours were laid by the same run as itself, in consecutive order — a stretch
that was straight when written and got pulled crooked afterwards. Edges are born
at a p50 of 2.20m and grow by up to +12.13m, with nodes displacing up to 11.04m
from where they were created.

The reason is that node spacing is ~2.2m while a merge can pull a node several
metres, and ADJACENT NODES GET PULLED BY DIFFERENT PASSES. Once a run may hold
station on a node or skip ahead, node k might be updated by this run and node
k+1 by a different one; two passes that differ by a couple of metres cross-track
then leave a zigzag between neighbours. Nothing about the average fixes this:
projecting the motion cross-track only leaves 25.1 deg, clamping each move to
1.0m leaves 24.9, to 0.5m leaves 12.4. Only refusing to move nodes at all
restores the geometry — 2.0 deg median, 8.4 at p90, matching a single clean run.

So position is set once, by whichever run first ran that piece of road, and
later passes vote on WEIGHT (i.e. colour) alone. Averaging could be done safely,
but it needs a coherent per-pass alignment of the run's readings against the
chain's nodes — a monotone many-to-many matching — rather than each point
independently pulling whatever node it happens to be nearest.
"""
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

import db
import map_heat

GRAPH_PATH = Path(__file__).resolve().parent.parent / "data" / "route_graph.json"
VERSION = 15  # v14 had no cluster list, so a zoomed-out map showed a few specks
              # with nothing to say where they were — see _clusters;
              # v13 still drew a road recorded twice as a thin sliver between two
              # junctions — see _contract_lenses;
              # v12 drew a T-junction reached from two slightly different points
              # as a small triangle — see _contract_triangles;
              # v11 had no notion of time, so a paused-and-resumed recording drew
              # a straight edge across whatever was skipped — see _gap_breaks;
              # v10 normalized colour against the busiest road, so one lapped
             # loop flattened everything else;
             # v9 cut real road with MAX_EDGE_M=20, fragmenting the graph;
             # v8 kept short dead-end spurs hanging off junctions;
             # v7 drew edges of any length, including across recording gaps;
             # v6 added coherent per-run averaging but no smoothing pass;
             # v5 froze node positions to stop the vibration; v4 averaged them
             # per-point, which caused it; v3 marched one node per point
             # regardless of pace; v2 chorded across existing road; v1 also
             # stored directed edges

# ---------- parameters ----------
#
# Every one of these is baked into the saved file's "params" block and compared
# on load: a mismatch means the graph on disk was built under different tuning
# and rebuild() is required. Several of them affect node POSITIONS, not just
# colour, so retuning is never just a re-render.

STOP_CLUSTER_R = 5.0    # metres; radius of the stationary-cluster circle
STOP_CLUSTER_N = 5      # readings inside it before the span collapses to a centroid

D = 23.0                # metres; candidate radius — nodes this close to a new point
                        # are considered for merging. See below.
DELTA = 5.0             # metres; slack on top of D for path-proximity matching
K = 7                   # points per matched path (~13m of track at 2.2m spacing).
                        # Also the lookback/lookahead size — see add_point.
MAX_PATHS = 32          # cap on path enumeration at junctions
SELF_MIN_ARC_M = 60.0   # a run may not merge back onto a node IT CREATED less than
                        # this far back along its own track. Without it, runs collapse.
                        # Deliberately not applied to pre-existing nodes — see add_point.
W_REPEAT = 0.25         # weight added when a run merges into a node it already
                        # touched this run (a second lap), vs 1.0 for a first touch.
                        # Weight is colour only now — it no longer moves anything.
MAX_WALK_HOPS = 8       # if the previous node reaches this one within this many existing
                        # edges, the step is already-known road: record no new edge

MAX_EDGE_M = 40.0       # metres; a step longer than this is not road, it is a gap in
                        # the recording. See "WHY 40m" below.
GAP_MAX_SEC = 10.0      # seconds; a step spanning at least this much wall-clock is a
                        # paused recording, not running — see _gap_breaks.
                        # Sampling is 1Hz, so this is 10x the period.
GAP_MIN_DIST_M = 10.0   # metres; such a step is only CUT if it also displaces this
                        # far. Pausing and resuming in place is harmless to join.

HEAT_RADIUS_KM = 50.0   # colour is normalized against the reference road within this
                        # radius, not against the busiest road anywhere.
HEAT_CAP_PCT = 0.95     # that reference is this length-weighted percentile of edge
                        # weight, not the maximum. Anything above it saturates.
                        # See "WHY A CAPPED REFERENCE".

TOPO_MAX_PASSES = 8     # each reduction can expose work for the other; cap the retries
TRI_MAX_SIDE_M = D      # metres; a triangle of three junctions whose SHORTEST side is
                        # under this collapses to a T — see _contract_triangles.
LENS_MAX_SEP_M = D      # metres; two chains joining the same two junctions and never
                        # diverging further than this are one road — _contract_lenses.
SPUR_MAX_EDGES = 5      # a dead-end branch off a junction this short is GPS noise,
                        # not a road, and gets pruned. See _prune_spurs.

SMOOTH_ITERS = 2        # Laplacian passes run over the nodes a run just voted on
SMOOTH_LAMBDA = 0.5     # how far each pass pulls a node toward its neighbours' midpoint
SMOOTH_MAX_OFFSET = 2.0 # metres a node may end up from its raw vote average — see
                        # _smooth_pass; this is what stops smoothing from compounding

MAX_ANGLE_DIFF_DEG = map_heat.MAX_ANGLE_DIFF_DEG  # 30
BUCKET_DEG = map_heat.BUCKET_DEG  # ~38m, comfortably >= D so a 3x3 neighbourhood covers it

# WHY D = 20
# Sampling 20,400 real points and finding, for each, the nearest same-direction
# point belonging to a DIFFERENT run:
#     p50 = 0.85m   p75 = 1.97m   p90 = 5.11m   p95 = 10.53m
# Repeated traces land almost on top of each other, so D only has to cover the
# tail of that distribution — 20m clears p95 by a wide margin.
#
# While merging still moved a node the moment a point matched it, D was ALSO the
# distance a node could be dragged, so raising it made the line dramatically
# worse (turn p50 26.7 deg at D=10, 40.1 at 15, 46.7 at 20). That coupling is
# gone now that positions are settled per-run rather than per-point: D only
# decides which node a reading is credited to. Measured under the current
# algorithm, raising it is close to free:
#
#     D     nodes   edges   junctions  deg1   turn p50/p90   network
#     15    11943   12063      271      29     1.37 / 7.35    30.00km
#     18    11372   11462      216      33     1.31 / 6.76    28.50km
#     20    11111   11157      169      64     1.32 / 6.73    27.24km   <- chosen
#     25    10674   10706      146      43     1.26 / 6.22    25.93km
#
# The one number moving the wrong way is deg1 — chain endpoints, 29 at D=15 but
# 64 at D=20. A dead end appears where a trace merged into a chain from one side
# and then failed to continue, so this is the first hint of over-merging, and it
# is worth re-checking if D goes higher.
#
# THE REAL LIMIT IS NOT IN THIS TABLE. None of these metrics can see two
# genuinely separate parallel roads being fused into one line, which is what a
# large D actually costs — every one of them would call that fusion an
# improvement, since it means fewer nodes and less network length. At D=20 a
# street 20m away is a merge candidate, which is wider than a typical two-lane
# road plus footpaths. This is the point where the choice has to be checked by
# eye against a place where two roads really do run close and parallel, not
# argued from the summary statistics.
#
# BUCKET_DEG (~38m) must stay >= D or near()'s 3x3 neighbourhood stops covering
# the radius. At D=20 there is still headroom, but not much.

# WHY STOP_CLUSTER_R MUST STAY BELOW ~8m
# Sampling is 1Hz (p50 and p90 of the inter-reading time delta are both 1.0s)
# and median speed is 2.15 m/s, so STOP_CLUSTER_N=5 consecutive readings span
# ~8.6m of travel. "5+ readings within R" is therefore a SPEED CUTOFF at
# R / 4s, and the rule changes character right at running pace:
#
#     R      fires below    nodes left   reduction   spans fired
#     3m      0.75 m/s        92,520        0.7%          111
#     5m      1.25 m/s        90,083        3.3%          668     <- chosen
#     8m      2.00 m/s        67,062       28.0%        5,744
#    10m      2.50 m/s        32,837       64.7%       13,100
#    12m      3.00 m/s        18,446       80.2%       14,461
#
# The fastest run in the DB is 6.81 min/km = 2.45 m/s. So at R >= 10m the cutoff
# exceeds EVERY pace ever logged, and this silently stops being a stop-cleaner
# and becomes blanket decimation of the whole dataset. At R = 5m it fires 668
# times across all 51 runs — genuine stops and walk breaks, which is the point.


def _params() -> dict:
    return {
        "d": D, "delta": DELTA, "k": K,
        "stop_cluster_r": STOP_CLUSTER_R, "stop_cluster_n": STOP_CLUSTER_N,
        "max_paths": MAX_PATHS, "max_angle_diff_deg": MAX_ANGLE_DIFF_DEG,
        "self_min_arc_m": SELF_MIN_ARC_M, "w_repeat": W_REPEAT,
        "max_walk_hops": MAX_WALK_HOPS,
        "smooth_iters": SMOOTH_ITERS, "smooth_lambda": SMOOTH_LAMBDA,
        "smooth_max_offset": SMOOTH_MAX_OFFSET, "max_edge_m": MAX_EDGE_M,
        "gap_max_sec": GAP_MAX_SEC,
        "gap_min_dist_m": GAP_MIN_DIST_M,
        "heat_radius_km": HEAT_RADIUS_KM, "heat_cap_pct": HEAT_CAP_PCT,
        "tri_max_side_m": TRI_MAX_SIDE_M,
        "lens_max_sep_m": LENS_MAX_SEP_M,
        "spur_max_edges": SPUR_MAX_EDGES,
    }


# WHY 40m
# Readings are ~1Hz at ~2.2m apart, so a step between consecutive readings should
# be a couple of metres, and most are: p50 2.24, p90 3.3, p99 ~11.
#
# THIS CONSTANT WAS 20m AND THAT WAS WRONG. The 20m figure was measured on the
# v5 graph, before per-run averaging and the smoothing pass existed. Both of
# those MOVE nodes after their edges are created, which stretches edges that were
# ~2m at birth. On the current graph the 20-35m band holds ~44 edges carrying
# weights up to 43.25 — roads run forty times over. Cutting them severed real
# road: the graph came apart into ELEVEN components with 33 dead ends, which is
# what showed up on the map as disconnected spurs that visibly ought to join the
# path next to them.
#
# The true gap in the distribution is 34.79m -> 46.38m, and every edge above it
# is either a single-run dropout (w=1.00, 60m+ hops) or spans more than ~16s of
# missing samples at the fastest pace ever logged here (2.45 m/s). 40m sits in
# the middle of that gap. Sweeping the threshold shows a flat plateau, which is
# how you can tell it is in the right place — 30, 35, 40 and 45 all produce a
# byte-identical graph:
#
#     MAX_EDGE_M   edges  components  deg-1  turn p50   longest
#             20   10805          11     33      1.27      31.7
#             25   10791           4     13      1.26      34.8
#             30   10791           2      9      1.26      34.8
#         *** 40   10791           2      9      1.26      34.8
#           none   10801           2      7      1.26      70.1
#
# Two components is the correct answer, not a residue: the runs are in two cities
# ~2,000km apart. Line quality is unaffected (turn p50 1.27 -> 1.26).


# WHY LOCAL HEAT
# Colour is weight / max_weight, and normalizing against a single global maximum
# means the busiest road anywhere sets the scale everywhere. With runs in two
# cities that silently deletes one of them:
#
#     region      max edge weight   median edge weight
#     Chennai               52.12                 6.50
#     Singapore             13.00                 1.00
#
# Against a global 52.12, Singapore's most-run road renders at 25% of the scale
# and its median road at 2%, i.e. pinned to MIN_ALPHA — a city of real running
# drawn as uniformly cold, because a different city 2,000km away is busier.
#
# So each node carries the maximum edge weight within HEAT_RADIUS_KM of it, and
# the renderer normalizes against that. Every region is then scaled against its
# own busiest road and reaches full brightness on its own terms.
#
# Regions are found by single-linkage on a HEAT_RADIUS_KM grid rather than by a
# true per-node radius query: an exact circle for every node is O(n^2) haversines
# (~116M here) on every save. The approximation only differs from a true circle
# where two areas of running are separated by roughly one cell, which is a case
# that cannot arise for a radius as large as 50km unless you genuinely run in two
# places 50km apart -- and if you do, they are meant to be scaled separately.


# WHY A CAPPED REFERENCE
# Normalizing against a region's MAXIMUM has the same failure mode one scale down.
# A short loop run in laps accumulates weight far faster than a route run end to
# end, so it alone sets the top of the scale and compresses every other road into
# the bottom of the range:
#
#     Chennai edge weight   p50 6.50   p95 19.00   p98 40.12   max 52.12
#
# The 19-to-52 band is 40% of the whole alpha range and carries 0.34km — 2.4% of
# the network. Everything else, 95% of the roads, is squeezed below alpha 100.
#
# So the reference is the HEAT_CAP_PCT length-weighted percentile instead, and
# anything above it saturates at MAX_ALPHA. Percentile rather than a fixed
# multiple of the median, because it adapts on its own: a region with no lapped
# outlier barely moves, while a region dominated by one is rescaled hard.
#
#     cap      Chennai a_p50/a_p90    SG a_p50/a_p90   saturated
#     max            72 / 100             63 / 191         0.0%
#     p98            72 / 109             63 / 191         2.1%
# *** p95           118 / 200             63 / 209         5.4%
#     p90           136 / 237             72 / 255        10.2%
#
# p95 is where Chennai breaks open (a_p50 72 -> 118) while Singapore, which has
# no lapped loop, is left almost untouched — exactly the self-adapting behaviour
# wanted. p90 spreads a little further but pins a tenth of the network at full
# brightness, which throws away the distinction between "run often" and "run most".
#
# LENGTH-weighted, not edge-count-weighted: the percentile should mean "95% of
# the road I can see", and edges vary from 2m to 40m, so counting them instead
# would let a densely-sampled stretch outvote a sparsely-sampled one.


# ---------- preprocessing ----------

def collapse_stops(points: list[tuple], radius: float = STOP_CLUSTER_R,
                   min_count: int = STOP_CLUSTER_N) -> list[tuple]:
    """points: [(lat, lon, time), ...] in seq order for ONE run, time optional.
    Returns [(lat, lon, t_first, t_last), ...] with stationary clusters replaced
    by their centroid, so add_point can assume every consecutive pair is
    genuinely moving.

    Anchors a span at point i and extends while each following point is within
    `radius` of the anchor. A span of >= min_count points collapses to one point
    at their centroid; anything shorter emits point i and advances by one.

    The two timestamps are the span's first and last, not one value, because
    _gap_breaks measures the interval BETWEEN consecutive output points and a
    collapsed span can cover minutes. Reporting a single time would turn every
    long stop into a spurious gap. Everything downstream reads only [0] and [1].
    """
    out: list[tuple] = []
    i, n = 0, len(points)
    while i < n:
        j = i + 1
        while j < n and map_heat._haversine_m(
            points[i][0], points[i][1], points[j][0], points[j][1]
        ) <= radius:
            j += 1
        span = points[i:j]
        t0 = span[0][2] if len(span[0]) > 2 else None
        t1 = span[-1][2] if len(span[-1]) > 2 else None
        if len(span) >= min_count:
            out.append((sum(p[0] for p in span) / len(span),
                        sum(p[1] for p in span) / len(span), t0, t1))
            i = j
        else:
            out.append((points[i][0], points[i][1], t0, t0))
            i += 1
    return out


def _gap_breaks(pts: list[tuple]) -> set[int]:
    """Indices in a collapse_stops output where the recording was interrupted, so
    the point there begins a fresh segment and gets no edge back to the previous.

    WHY THIS EXISTS. Pausing a recording and resuming somewhere else leaves two
    readings a few seconds apart in the file and tens of metres apart on the
    ground, with nothing in between. Joined blindly they draw a straight line
    across whatever the runner actually walked around. That is what put a
    diagonal through the infield of the track at IIT Madras: runs 48 and 59 each
    paused mid-session (78s/15.8m and 81s/17.8m) and the chord was the join. It
    survived MAX_EDGE_M because at 15-18m it is shorter than a legitimate step
    between two sparse readings, so length alone cannot separate the two cases.
    Time can, and only time can.

    WHY 10 SECONDS. Sampling is 1Hz and extremely regular: across 95,471
    consecutive pairs the median, p90 and p99.9 deltas are 1.0s, 1.0s and 2.0s.
    Only 41 pairs exceed 3s. So anything at 10s is ten times the sampling period
    and far outside the normal regime; there is no continuum to cut through.

    Applying the test AFTER stop collapse rather than before is what keeps this
    from firing on ordinary pauses. Standing at a light for a minute produces a
    long delta too, but those readings are all within STOP_CLUSTER_R, so the gap
    ends up INSIDE one collapsed span and never becomes an interval between two
    output points. Of the 29 raw gaps >= 3s, collapse absorbs 12 that way. Every
    one of the 17 survivors at >= 10s involves real displacement, which is
    exactly the population that draws a false edge.

    WHY DISTANCE TOO. Time alone is not enough, and this was measured the hard
    way: cutting on the time test by itself split Singapore from one component
    into five, the pieces meeting at 3.2m, 3.4m and 5.1m. Those are pauses taken
    and resumed on the spot — a stop just short of what collapse absorbs. Joining
    them draws a 3m straight line where the runner moved 3m, which is inside the
    graph's own positional accuracy, so the join is free while the cut orphans
    everything downstream of it. Only once a pause displaces the runner can the
    straight join cross ground they did not cover, so GAP_MIN_DIST_M is where
    breaking starts being worth its cost. At 10m the rule fires on 6 of the 17
    time-gaps, including both halves of the stadium chord, and Chennai and
    Singapore each stay a single component.
    """
    breaks: set[int] = set()
    for i in range(len(pts) - 1):
        end, start = _parse_ts(pts[i][3]), _parse_ts(pts[i + 1][2])
        if not end or not start:
            continue
        if (start - end).total_seconds() < GAP_MAX_SEC:
            continue
        if map_heat._haversine_m(pts[i][0], pts[i][1],
                                 pts[i + 1][0], pts[i + 1][1]) < GAP_MIN_DIST_M:
            continue
        breaks.add(i + 1)
    return breaks


def _parse_ts(t):
    if not t:
        return None
    if isinstance(t, datetime):
        return t
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------- the graph ----------

class RouteGraph:
    """Nodes are identified by index into parallel lists. Edges are UNDIRECTED.

    A road is one line whichever way you ran it, so which direction a trace was
    laid down in carries no meaning worth storing. This has to be undirected
    rather than merely ignored at match time: a run heading the reverse way
    along an existing chain has to be able to walk that chain to build its
    candidate path P1 at all. With directed edges it would find no traversable
    path, fail every match, and lay a duplicate line right beside the road it
    was supposed to merge into — the exact thing the graph exists to prevent."""

    def __init__(self):
        self.lat: list[float] = []
        self.lon: list[float] = []
        self.w: list[float] = []
        self.adj: list[set] = []        # node -> set of neighbour indices (undirected)
        self.buckets: dict = {}         # bucket -> set of node indices
        self.node_bucket: list[tuple] = []  # node -> the bucket it currently sits in
        self.run_ids: list[int] = []
        # Positional accumulators. A node's position is the MEAN OVER RUNS of
        # each run's vote for it, so slat/slon hold the running sum of those
        # votes and votes[] their count. Kept separate from w: w counts passes
        # for colour and takes fractional increments for repeat laps, while
        # these must stay an honest sum and count or the mean drifts.
        self.slat: list[float] = []
        self.slon: list[float] = []
        self.votes: list[int] = []

    # --- node/bucket bookkeeping ---

    def _add_node(self, lat: float, lon: float, weight: float = 1.0) -> int:
        idx = len(self.lat)
        self.lat.append(lat)
        self.lon.append(lon)
        self.w.append(weight)
        self.adj.append(set())
        b = map_heat._bucket(lat, lon)
        self.buckets.setdefault(b, set()).add(idx)
        self.node_bucket.append(b)
        self.slat.append(lat)
        self.slon.append(lon)
        self.votes.append(1)   # the reading that created it is its first vote
        return idx

    def _move_node(self, idx: int, lat: float, lon: float):
        """Moving a node can push it into a different spatial-hash bucket, so it
        has to be re-filed or later lookups miss it."""
        self.lat[idx] = lat
        self.lon[idx] = lon
        b = map_heat._bucket(lat, lon)
        old = self.node_bucket[idx]
        if b != old:
            self.buckets[old].discard(idx)
            if not self.buckets[old]:
                del self.buckets[old]
            self.buckets.setdefault(b, set()).add(idx)
            self.node_bucket[idx] = b

    def _add_edge(self, a: int, b: int):
        self.adj[a].add(b)
        self.adj[b].add(a)

    def connected_within(self, a: int, b: int, max_hops: int) -> bool:
        """Is b reachable from a in at most max_hops edges?

        Used to decide whether a step from a to b needs a new edge at all. When
        the answer is yes, the two are already joined by real road and drawing a
        direct edge would chord across it — closing a spurious loop rather than
        recording anything new."""
        if a == b:
            return True
        frontier = {a}
        seen = {a}
        for _ in range(max_hops):
            nxt = set()
            for x in frontier:
                for y in self.adj[x]:
                    if y == b:
                        return True
                    if y not in seen:
                        seen.add(y)
                        nxt.add(y)
            if not nxt:
                return False
            frontier = nxt
        return False

    def shortest_path(self, a: int, b: int, max_hops: int) -> list[int] | None:
        """Node list from a to b inclusive, or None if further than max_hops.

        Used to recover the nodes a run ran straight past without stopping on —
        they are on the road it ran, so they get a vote like any other."""
        if a == b:
            return [a]
        prev: dict[int, int] = {a: a}
        frontier = [a]
        for _ in range(max_hops):
            nxt = []
            for x in frontier:
                for y in self.adj[x]:
                    if y in prev:
                        continue
                    prev[y] = x
                    if y == b:
                        out = [b]
                        while out[-1] != a:
                            out.append(prev[out[-1]])
                        return out[::-1]
                    nxt.append(y)
            if not nxt:
                return None
            frontier = nxt
        return None

    def near(self, lat: float, lon: float, radius: float) -> list[tuple]:
        """[(distance, node_idx), ...] within radius, nearest first. The 3x3
        bucket neighbourhood always fully covers `radius` because BUCKET_DEG
        is kept >= D."""
        bx, by = map_heat._bucket(lat, lon)
        found = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx in self.buckets.get((bx + dx, by + dy), ()):
                    dist = map_heat._haversine_m(lat, lon, self.lat[idx], self.lon[idx])
                    if dist <= radius:
                        found.append((dist, idx))
        found.sort()
        return found

    # --- path enumeration ---

    def _paths_through(self, s: int, i: int, k: int, cap: int = MAX_PATHS) -> list[list[int]]:
        """All simple paths of k nodes with s at index i — i steps out one way
        and k-1-i steps out the other, along undirected edges.

        On an ordinary road stretch every node has two neighbours, so one of
        those two walks retraces the other; the simple-path filter below drops
        those, leaving essentially one path per (s, i). At a junction it
        branches, hence the cap: without it a cluster of degree-3+ nodes blows
        up exponentially in k, and this runs per candidate per point across
        ~90k points."""
        back = self._walk(s, i, self.adj, cap)
        if not back:
            return []
        fwd = self._walk(s, k - 1 - i, self.adj, cap)
        if not fwd:
            return []
        paths = []
        for b in back:
            for f in fwd:
                # b is [s, nb, nb-nb, ...]; reverse it and drop the duplicated
                # s before joining onto the other half.
                path = list(reversed(b)) + f[1:]
                # Both halves start at s and walk the same undirected
                # adjacency, so the pair that doubles back on itself shows up
                # as a repeated node and is rejected here.
                if len(set(path)) == len(path):
                    paths.append(path)
                    if len(paths) >= cap:
                        return paths
        return paths

    @staticmethod
    def _walk(start: int, steps: int, adj: list[set], cap: int) -> list[list[int]]:
        """All node-sequences of length steps+1 starting at `start` and
        following `adj`. Returns [] if no branch reaches full length — a path
        shorter than k is not a valid P1, since P2 is always k long."""
        paths = [[start]]
        for _ in range(steps):
            nxt = []
            for p in paths:
                for n in adj[p[-1]]:
                    if n in p:
                        continue  # keep paths simple
                    nxt.append(p + [n])
                    if len(nxt) >= cap:
                        break
                if len(nxt) >= cap:
                    break
            if not nxt:
                return []
            paths = nxt
        return paths


# ---------- matching ----------

def _net_bearing(pts: list[tuple]) -> float | None:
    """Direction of a path, taken end to end. A chord rather than a per-segment
    average: for two traces of the same road the chords agree even where the
    road curves, and it's immune to per-segment GPS jitter."""
    if len(pts) < 2:
        return None
    (lat1, lon1), (lat2, lon2) = pts[0], pts[-1]
    if lat1 == lat2 and lon1 == lon2:
        return None
    return map_heat._bearing_deg(lat1, lon1, lat2, lon2)


def _bearing_diff(b1: float, b2: float) -> float:
    """Angular difference folded mod 180, giving 0-90: two paths lying along
    the same road agree whichever way each was travelled, so exactly-opposite
    reads as identical. An out-and-back is one line, not two.

    What this still rejects is a genuinely different alignment — a crossing
    street meeting this one at 60 degrees. That check has to stay: over a
    k-point path spanning only ~13m, a perpendicular crossing can otherwise
    pass the proximity test and fuse two real roads into one."""
    return map_heat._orientation_diff(b1, b2)


def _covers(a: list[tuple], b: list[tuple], tol: float) -> bool:
    """Every point of `a` has some point of `b` within tol (directed Hausdorff)."""
    for lat1, lon1 in a:
        if not any(map_heat._haversine_m(lat1, lon1, lat2, lon2) <= tol
                   for lat2, lon2 in b):
            return False
    return True


def _paths_match(p1: list[tuple], p2: list[tuple], tol: float,
                 max_angle: float = MAX_ANGLE_DIFF_DEG) -> bool:
    """Proximity in EITHER direction (so one path being denser or longer than
    the other is fine), plus agreeing net bearing."""
    if not (_covers(p1, p2, tol) or _covers(p2, p1, tol)):
        return False
    b1, b2 = _net_bearing(p1), _net_bearing(p2)
    if b1 is None or b2 is None:
        return False
    return _bearing_diff(b1, b2) <= max_angle


# ---------- the core primitive ----------

class RunContext:
    """Per-run state. `created` drives the self guard and `touch_arc` decides
    whether a merge is worth a full +1 or only +W_REPEAT — they are separate on
    purpose, because the guard must cover only the nodes this run laid down
    itself, not every node it has passed over."""

    def __init__(self):
        self.touch_arc: dict[int, float] = {}  # node idx -> arc length at last touch
        self.created: dict[int, float] = {}    # nodes THIS run created -> arc when created


def add_point(graph: RouteGraph, A: tuple, incoming_from: int | None,
              lookback: list[tuple], lookahead: list[tuple],
              run_ctx: RunContext, arc: float) -> int:
    """Adds one point of a run to the graph, returning the node index it
    resolved to — either an existing node it merged into, or a newly created one.

    A: (lat, lon). lookback/lookahead: up to K points either side of A in its
    own run, truncated at run boundaries. arc: distance travelled along this
    run so far, used by the self guard.

    Sizing the window to K (rather than some independent constant) is what makes
    every index placement i available: P2 needs i points behind A and K-1-i
    ahead, and both are <= K by construction. Only a run's own start and end
    truncate it."""
    lat, lon = A
    tol = D + DELTA

    # 1. candidates within D, nearest first
    candidates = graph.near(lat, lon, D)

    # 2. self guard — against nodes THIS RUN CREATED only.
    #
    #    In virgin territory the guard is what stops a run collapsing to a single
    #    node: A's successor sits ~2.2m away, well inside D, and its window
    #    matches the trace just laid almost perfectly, so it would merge straight
    #    back into A, and so would the next point, and the next. It's measured in
    #    arc length rather than points so it doesn't depend on sampling rate.
    #
    #    But applying it to nodes that ALREADY EXISTED forces something wrong:
    #    every point then has to consume a fresh node, so a run marches through
    #    an existing chain at exactly one node per reading no matter how fast it
    #    was actually moving. Run the same road slower than the run that laid it
    #    and you out-run your own position — measured on run 28, the merge target
    #    drifted 2.9m, 3.5m, 4.7m, 5.7m, 7.0m, 8.2m behind over six consecutive
    #    readings while a node under 2m away was available every single time —
    #    then the drift passes D, the chain drops out of candidates entirely, and
    #    the run jumps six nodes forward at once. Every metre of that drift is
    #    also dragged into the node's position by the merge average, which is
    #    what bent straight roads into zigzags and split clean chains into spurs.
    #
    #    A pre-existing node may therefore be re-used by consecutive readings
    #    freely: standing still relative to the graph is the correct answer when
    #    the graph is denser than the run. Across the full history this holds for
    #    36,287 steps — 40% of them — and drops backward steps from 7.05% to
    #    0.01% and junctions from 1,754 to 529.
    candidates = [
        (dist, idx) for dist, idx in candidates
        if idx not in run_ctx.created or arc - run_ctx.created[idx] >= SELF_MIN_ARC_M
    ]

    matched = None
    for _dist, s in candidates:
        if _matches(graph, s, lookback, lookahead, A, tol):
            matched = s
            break

    # 4. merge or create
    if matched is not None and matched == incoming_from:
        # Still standing on the node the previous reading resolved to. That's not
        # a second observation of anything — moving it would drag it toward a
        # reading it already accounts for, and weighting it would make a slow
        # runner look like a frequent one. Colour has to keep meaning passes.
        node_id = matched
    elif matched is not None:
        # Weight only. The position update is deliberately NOT done here — it
        # happens once for the whole run in _average_in_run, because moving a
        # node the instant one point matches it is what made the line vibrate.
        node_id = matched
        graph.w[node_id] += W_REPEAT if node_id in run_ctx.touch_arc else 1.0
    else:
        node_id = graph._add_node(lat, lon, 1.0)
        run_ctx.created[node_id] = arc

    run_ctx.touch_arc[node_id] = arc

    # 5. edge — but only where the step isn't already covered by existing road.
    #
    #    Joining every consecutive pair unconditionally is what produced the
    #    "triangle at a T junction" artifact. Node spacing along a road is ~2.2m
    #    while GPS noise is 1-2m, so when a run comes back along a stretch it
    #    already laid, which of several near-identical nodes it snaps to is
    #    decided by noise. One backward pick is enough: joining it directly
    #    chords across the nodes in between and closes the chain into a loop.
    #    Measured over the real history, 88.6% of such chords joined nodes that
    #    were ALREADY connected by existing edges (40% only two hops apart), and
    #    essentially every cycle in the graph came from one.
    #
    #    So if the previous node can already reach this one over existing road,
    #    the runner just walked that road — the connection is recorded already and
    #    an edge would invent topology. Only a genuinely unreachable step is new
    #    road and earns an edge.
    #
    #    Resolving back to incoming_from is now an ordinary outcome rather than
    #    the impossibility it was when the self guard covered every node — it
    #    means the reading didn't leave the node. There is no edge to draw.
    #    A step longer than MAX_EDGE_M is a gap in the recording rather than a
    #    piece of road, so it gets no edge and the chain is simply left broken
    #    there — which is the honest shape, since nothing is known about the
    #    ground in between.
    if incoming_from is not None and incoming_from != node_id:
        if map_heat._haversine_m(graph.lat[incoming_from], graph.lon[incoming_from],
                                 lat, lon) > MAX_EDGE_M:
            return node_id
        if not graph.connected_within(incoming_from, node_id, MAX_WALK_HOPS):
            graph._add_edge(incoming_from, node_id)

    return node_id


def _matches(graph: RouteGraph, s: int, lookback: list[tuple],
             lookahead: list[tuple], A: tuple, tol: float) -> bool:
    """Is there an index i and a graph path P1 (s at index i) matching
    the run's own window path P2 (A at index i)?

    s and A must sit at the SAME index in their respective paths — that's what
    keeps the two paths in step along-track, instead of letting an arbitrary
    stretch of one road match an offset stretch of another."""
    for i in range(K):
        if i > len(lookback) or (K - 1 - i) > len(lookahead):
            continue
        p2 = lookback[len(lookback) - i:] + [A] + lookahead[:K - 1 - i]
        if len(p2) < K:
            continue
        for path in graph._paths_through(s, i, K):
            p1 = [(graph.lat[n], graph.lon[n]) for n in path]
            if _paths_match(p1, p2, tol):
                return True
    return False


# ---------- run-level driver ----------

def add_run_to_graph(graph: RouteGraph, run_id: int, points: list[tuple]):
    """points: [(lat, lon, time), ...] raw, in seq order. Preprocesses and folds
    the whole run in, in two phases — see _average_in_run for why the positions
    are not updated during the first one.

    A run interrupted by a paused recording is folded in as several segments
    rather than one chain: _gap_breaks says where, and at each break prev_node
    resets to None so no edge spans the interruption. The segments still share
    one RunContext, so the self-guard and the once-per-run weighting continue to
    treat the whole thing as a single run — only the edges are cut."""
    timed = collapse_stops(points)
    if len(timed) < 2:
        return
    breaks = _gap_breaks(timed)
    # Times have done their job; the geometry below is all plain (lat, lon).
    pts = [(p[0], p[1]) for p in timed]

    ctx = RunContext()
    n_before = len(graph.lat)
    resolved: list[int] = []
    prev_node = None
    arc = 0.0
    for i, p in enumerate(pts):
        if i > 0:
            arc += map_heat._haversine_m(pts[i - 1][0], pts[i - 1][1], p[0], p[1])
        if i in breaks:
            prev_node = None    # recording was paused here; do not bridge it
        lo = max([0] + [b for b in breaks if b <= i])
        hi = min([len(pts)] + [b for b in breaks if b > i])
        lookback = pts[max(lo, i - K):i]
        lookahead = pts[i + 1:min(hi, i + 1 + K)]
        prev_node = add_point(graph, p, prev_node, lookback, lookahead, ctx, arc)
        resolved.append(prev_node)

    voted = _average_in_run(graph, pts, resolved, n_before, breaks)
    _smooth_pass(graph, voted)
    _prune_spurs(graph)
    graph.run_ids.append(run_id)


def _average_in_run(graph: RouteGraph, pts: list[tuple], resolved: list[int],
                    n_before: int, breaks: set[int] | None = None) -> set:
    """Move every node this run passed over toward this run's estimate of where
    the road is — all of them together, once the whole run is known.

    Doing it per-point as the run streams in is what tore the line apart: node
    spacing is ~2.2m but a merge shifts a node metres, and with each point
    independently pulling whichever node it was nearest, node k could be moved by
    this run while node k+1 was moved by some other one. Two passes a couple of
    metres apart cross-track then leave a permanent zigzag between neighbours.

    Coherence is the fix, and it needs three things:

      * ANCHORS. resolved[i] already pairs reading i with a node, so the two
        sequences are aligned wherever the run actually stopped on a node.
      * NO GAPS. A run moving faster than the chain is dense skips nodes
        entirely. Those get no anchor, so they would sit still while their
        neighbours moved — the same tear, one node wide. Walking the graph path
        between consecutive anchors recovers them, and each takes the nearer of
        the two readings that bracket it. This one detail is worth more than
        anything else here: without it the median turn angle is 11.0 degrees and
        the 90th percentile 131.3; with it, 4.3 and 49.1.
      * ONE VOTE PER RUN. Not per reading — a slow pass leaves more readings on
        a node and would otherwise outvote a fast one. And not an incremental
        weighted blend either: a node's blend fraction is d/(w+d), so a node with
        w=1 moves halfway toward the reading while its neighbour with w=10 moves
        a tenth of the way, and the difference between the two is another tear.
        Accumulating a plain sum and count and taking the mean removes the blend
        fraction entirely, and takes 4.3/49.1 down to 2.4/14.1.

    For reference, never moving nodes at all scores 2.0/8.3, and the naive
    per-point average scores 26.7/134.0."""
    votes: dict[int, list] = {}

    def vote(n: int, lat: float, lon: float):
        v = votes.get(n)
        if v is None:
            votes[n] = [lat, lon, 1]
        else:
            v[0] += lat
            v[1] += lon
            v[2] += 1

    for i, n in enumerate(resolved):
        vote(n, pts[i][0], pts[i][1])

    # nodes the run ran straight past without ever being nearest to
    breaks = breaks or set()
    for i in range(len(resolved) - 1):
        a, b = resolved[i], resolved[i + 1]
        if a == b or (i + 1) in breaks:
            continue    # nothing was run between these two, so nothing to fill in
        path = graph.shortest_path(a, b, MAX_WALK_HOPS)
        if not path or len(path) <= 2:
            continue
        for nd in path[1:-1]:
            d0 = map_heat._haversine_m(pts[i][0], pts[i][1], graph.lat[nd], graph.lon[nd])
            d1 = map_heat._haversine_m(pts[i + 1][0], pts[i + 1][1],
                                       graph.lat[nd], graph.lon[nd])
            p = pts[i] if d0 <= d1 else pts[i + 1]
            vote(nd, p[0], p[1])

    for n, (slat, slon, cnt) in votes.items():
        if n >= n_before:
            continue  # this run created it, and _add_node already counted that vote
        graph.slat[n] += slat / cnt
        graph.slon[n] += slon / cnt
        graph.votes[n] += 1
        graph._move_node(n, graph.slat[n] / graph.votes[n],
                         graph.slon[n] / graph.votes[n])
    return set(votes)


def _prune_spurs(graph: RouteGraph) -> int:
    """Cut off short dead-end branches hanging off a junction.

    A trace that wobbles off the road for a second and comes back leaves a stub:
    a couple of nodes reaching out from a junction to a dead end and stopping.
    It is GPS noise, not a road, and it shows up as a whisker on the drawn line.

    Measured at D=23 the two populations are cleanly separated. Stubs exist at 1,
    2, 3 and 4 edges (11 of them) and then at 10, 11 and 15 (6 of them) — nothing
    at all in between, so any cutoff from 4 to 9 prunes exactly the same set.
    Weight says the same thing independently: every stub of <= 5 edges has a peak
    node weight of 1.00, i.e. it was run once, ever, while the long ones sit at
    2.00-3.00. So this removes things seen once and keeps things seen repeatedly.

    Only branches ending at a JUNCTION are cut. A chain that is free at both ends
    is not a spur, it is an isolated fragment of real road, and removing it would
    delete road that was genuinely run. When MAX_EDGE_M was 20 that guard was
    load-bearing: it saved 18 fragments the length filter had severed from the
    road they belonged to. At 40 there are none left to save — every remaining
    dead end (9 of them) is a genuine branch off a junction, kept because it runs
    longer than SPUR_MAX_EDGES. The guard stays because the reasoning still holds,
    not because anything currently depends on it.

    Nodes are left in place and only their edges dropped: the parallel arrays are
    indexed by node id throughout, so actually deleting one means reindexing
    everything. An edgeless node draws nothing. It does stay a merge candidate,
    which is the right behaviour — if that branch turns out to be real and gets
    run again, it re-forms, and once it exceeds SPUR_MAX_EDGES it survives."""
    total = 0
    while True:                     # cutting a spur can expose another behind it
        removed = 0
        for n in range(len(graph.lat)):
            if len(graph.adj[n]) != 1:
                continue
            path = [n]
            prev, cur = None, n
            while True:
                nxt = [x for x in graph.adj[cur] if x != prev]
                if len(nxt) != 1:
                    break
                prev, cur = cur, nxt[0]
                path.append(cur)
                if len(graph.adj[cur]) != 2:
                    break
                if len(path) > SPUR_MAX_EDGES + 1:
                    break
            if len(path) - 1 > SPUR_MAX_EDGES:
                continue
            if len(graph.adj[path[-1]]) <= 2:
                continue            # free at both ends: an isolated chain, not a spur
            for i in range(len(path) - 1):
                graph.adj[path[i]].discard(path[i + 1])
                graph.adj[path[i + 1]].discard(path[i])
            removed += 1
        total += removed
        if not removed:
            return total


def _smooth_pass(graph: RouteGraph, voted: set):
    """Laplacian smoothing over the nodes this run just moved, plus their
    neighbours — those are the ones whose local shape changed.

    Only degree-2 nodes move. Junctions and chain ends are anchors, so topology
    and the geometry of every intersection are left exactly as they were.

    The important part is that this is a BOUNDED FILTER, not an accumulating
    mutation. The vote accumulators are never touched, so slat/slon/votes always
    hold the honest mean of the runs; _average_in_run resets each voted node to
    that mean, and this then displaces it by at most SMOOTH_MAX_OFFSET metres.
    Two things follow:

      * It cannot compound. A node smoothed on all 51 runs is still within the
        leash of its true average, so the drawn line can never creep away from
        the data it came from. In practice the leash is slack — the measured
        offset is 0.12m at p50 and 0.43m at p90 against a 2m limit.
      * Real corners survive. Iterating plain Laplacian over the WHOLE graph once
        per run does compound, and it drove the 99th-percentile turn angle from
        65 degrees to 15 — that is not jitter being removed, that is every real
        bend in every road being rounded off.

    Measured effect at the chosen settings: turn angle p50 2.41 -> 1.37 deg and
    p90 12.73 -> 7.36, for 1.3% of network length lost to the usual Laplacian
    shrinkage."""
    if SMOOTH_ITERS <= 0 or not voted:
        return
    scope = set(voted)
    for n in tuple(scope):
        scope |= graph.adj[n]
    scope = [n for n in scope if len(graph.adj[n]) == 2]
    if not scope:
        return

    for _ in range(SMOOTH_ITERS):
        # computed against the current positions, then applied together: an
        # in-place sweep would let a node be pulled by an already-moved
        # neighbour and propagate the shift along the chain.
        upd = []
        for n in scope:
            a, b = tuple(graph.adj[n])
            upd.append((
                n,
                graph.lat[n] + ((graph.lat[a] + graph.lat[b]) / 2 - graph.lat[n]) * SMOOTH_LAMBDA,
                graph.lon[n] + ((graph.lon[a] + graph.lon[b]) / 2 - graph.lon[n]) * SMOOTH_LAMBDA,
            ))
        for n, la, lo in upd:
            graph._move_node(n, la, lo)

    for n in scope:
        rlat = graph.slat[n] / graph.votes[n]
        rlon = graph.slon[n] / graph.votes[n]
        d = map_heat._haversine_m(rlat, rlon, graph.lat[n], graph.lon[n])
        if d > SMOOTH_MAX_OFFSET:
            f = SMOOTH_MAX_OFFSET / d
            graph._move_node(n, rlat + (graph.lat[n] - rlat) * f,
                             rlon + (graph.lon[n] - rlon) * f)


# ---------- persistence ----------

def _regions(lat: list, lon: list, radius_km: float) -> list:
    """Group nodes into regions by single-linkage over a `radius_km` grid:
    occupied cells that touch, diagonals included, join the same region, so two
    regions are always more than one cell apart. Returns a region key per node.

    A degree of latitude is ~111.32km everywhere; a degree of longitude shrinks
    by cos(lat), so sizing the cell by the widest latitude in the data keeps
    every cell at least radius_km across on both axes."""
    n = len(lat)
    cell_lat = radius_km / 111.32
    widest = max(abs(x) for x in lat)
    cell_lon = cell_lat / max(0.05, math.cos(math.radians(widest)))

    cells: dict[tuple, list[int]] = {}
    for i in range(n):
        cells.setdefault((int(lat[i] / cell_lat),
                          int(lon[i] / cell_lon)), []).append(i)

    parent: dict[tuple, tuple] = {c: c for c in cells}

    def find(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for (ci, cj) in cells:
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nb = (ci + di, cj + dj)
                if nb in parent:
                    rx, ry = find((ci, cj)), find(nb)
                    if rx != ry:
                        parent[rx] = ry

    region_of = [None] * n
    for c, members in cells.items():
        r = find(c)
        for i in members:
            region_of[i] = r
    return region_of


def _clusters(lat: list, lon: list, edges: list) -> list[dict]:
    """One entry per separated area of the map, for the pins the renderer drops
    when a place is too small on screen to show its roads — see heat_overlay's
    CLUSTER_PIN_MAX_PX.

    Grouped at HEAT_RADIUS_KM, the same grouping the local heat reference uses,
    so a pin covers exactly the area that shares a brightness scale. Only nodes
    that still carry an edge count: an edgeless node draws nothing, and a pin
    over empty map would point at nothing.

    Each entry carries the centroid to pin, the bbox the renderer measures on
    screen to decide whether to show it, the run count, and the road length.
    Run counts come from each run's first reading assigned to the nearest
    centroid — a single run never spans two areas 50km apart."""
    drawn = sorted({n for e in edges for n in e})
    if not drawn:
        return []
    region_of = _regions([lat[i] for i in drawn], [lon[i] for i in drawn],
                         HEAT_RADIUS_KM)
    groups: dict = {}
    for k, i in enumerate(drawn):
        groups.setdefault(region_of[k], []).append(i)

    idx = {node: region_of[k] for k, node in enumerate(drawn)}
    km: dict = {}
    for a, b in edges:
        km[idx[a]] = km.get(idx[a], 0.0) + map_heat._haversine_m(
            lat[a], lon[a], lat[b], lon[b]) / 1000.0

    out = []
    for r, members in groups.items():
        clat = sum(lat[i] for i in members) / len(members)
        clon = sum(lon[i] for i in members) / len(members)
        out.append({
            "lat": clat, "lon": clon,
            "bbox": [min(lat[i] for i in members), max(lat[i] for i in members),
                     min(lon[i] for i in members), max(lon[i] for i in members)],
            "km": round(km.get(r, 0.0), 1),
            "runs": 0,
        })

    starts = db.get_run_start_points()
    for _rid, slat, slon in starts:
        if slat is None:
            continue
        best = min(out, key=lambda c: map_heat._haversine_m(slat, slon,
                                                            c["lat"], c["lon"]))
        best["runs"] += 1
    out.sort(key=lambda c: -c["runs"])
    return out


def _local_ref_weights(edges: list, lat: list, lon: list, w: list) -> list[float]:
    """Per node, the weight that should render as full brightness nearby: the
    HEAT_CAP_PCT length-weighted percentile of edge weight within
    HEAT_RADIUS_KM. See "WHY LOCAL HEAT" and "WHY A CAPPED REFERENCE" above for
    why this is neither a global figure nor a maximum.

    Nodes are grouped by single-linkage over a HEAT_RADIUS_KM grid: occupied
    cells that touch (including diagonally) join the same region, so two regions
    are always more than one cell apart. Each node then takes its region's
    reference. Returns 1.0 for edgeless nodes, which draw nothing anyway.
    """
    if not lat:
        return []
    region_of = _regions(lat, lon, HEAT_RADIUS_KM)

    # (weight, length) per edge, grouped by region
    per_region: dict[tuple, list[tuple[float, float]]] = {}
    for a, b in edges:
        per_region.setdefault(region_of[a], []).append((
            (w[a] + w[b]) / 2,
            map_heat._haversine_m(lat[a], lon[a],
                                  lat[b], lon[b]),
        ))

    region_ref: dict[tuple, float] = {}
    for r, items in per_region.items():
        items.sort()
        total = sum(length for _w, length in items)
        if total <= 0:
            region_ref[r] = items[-1][0] if items else 1.0
            continue
        target, acc = HEAT_CAP_PCT * total, 0.0
        region_ref[r] = items[-1][0]
        for weight, length in items:
            acc += length
            if acc >= target:
                region_ref[r] = weight
                break

    return [region_ref.get(region_of[i], 0.0) or 1.0 for i in range(len(lat))]



def _junction_chains(adj: dict, lat: list, lon: list) -> tuple:
    """(junctions, chains) of an adjacency map. A junction is a node whose degree
    is anything but 2; a chain is the run of degree-2 nodes between two of them,
    returned as (end_a, end_b, length_m, path)."""
    junc = {n for n in adj if len(adj[n]) != 2}
    chains, seen = [], set()
    for j in sorted(junc):
        for nb in sorted(adj[j]):
            if (j, nb) in seen:
                continue
            path, prev, cur = [j, nb], j, nb
            while cur not in junc:
                nxt = [x for x in adj[cur] if x != prev]
                if len(nxt) != 1:
                    break
                prev, cur = cur, nxt[0]
                path.append(cur)
            seen.add((j, nb))
            seen.add((path[-1], path[-2]))
            chains.append((j, path[-1], _path_len(path, lat, lon), path))
    return junc, chains


def _path_len(path: list, lat: list, lon: list) -> float:
    return sum(map_heat._haversine_m(lat[path[i]], lon[path[i]],
                                     lat[path[i + 1]], lon[path[i + 1]])
               for i in range(len(path) - 1))


def _adj_of(edges: list) -> dict:
    adj: dict = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _rewire(edges: list, remap: dict, drop: set) -> list:
    """Rebuild an edge list with nodes remapped and some edges dropped, removing
    the self-loops and duplicates a contraction leaves behind."""
    out, seen = [], set()
    for a, b in edges:
        if (a, b) in drop or (b, a) in drop:
            continue
        a, b = remap.get(a, a), remap.get(b, b)
        if a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        out.append([key[0], key[1]])
    return out


def _hausdorff_m(p1: list, p2: list, lat: list, lon: list) -> float:
    """Symmetric Hausdorff distance between two paths, in metres — how far apart
    the two lines are at their worst point."""
    def directed(a, b):
        return max(min(map_heat._haversine_m(lat[x], lon[x], lat[y], lon[y])
                       for y in b) for x in a)
    return max(directed(p1, p2), directed(p2, p1))


def _contract_lenses(lat: list, lon: list, w: list, edges: list) -> list:
    """Fuse two chains that join the SAME pair of junctions and run alongside
    each other — one road recorded twice, which the merger failed to unify.

    This, not the triangle, is what actually draws the slivers near the running
    track. Two arrivals along nearly the same line become two separate chains
    between the same two junctions, and the gap between them renders as a thin
    wedge. Because both ends are shared, it is a two-junction cycle, so the
    three-junction triangle test below never sees it.

    A two-junction cycle is not automatically an artifact: running out one way
    and back another is one too. What separates them is how far apart the two
    lines run. Measured over the 40 such pairs in the history, the symmetric
    Hausdorff separation is continuous from 5.0m to 27.8m, then jumps to 36.4,
    36.7 and finally 94.1 and 151.7 — the last of which are genuine alternate
    routes hundreds of metres long. The cutoff is D again, and for the same
    reason as in _reduce_triangles: within D the builder already considers two
    readings to be the same road, so two chains closer than that are one road.

    The surviving chain is the one with the higher mean weight — more runs
    agreed on that line, so it is the better estimate of where the road is. The
    dropped chain's weight is carried over to the nearest surviving node with a
    max rather than a sum: these are duplicate recordings, and a sum would
    invent a brighter road than any single stretch was ever run."""
    pairs: dict = {}
    _junc, chains = _junction_chains(_adj_of(edges), lat, lon)
    for a, b, _length, path in chains:
        if a != b:
            pairs.setdefault((a, b) if a < b else (b, a), []).append(path)

    remap, drop, done = {}, set(), set()
    for key in sorted(pairs):
        group = pairs[key]
        if len(group) < 2 or (set(key) & done):
            continue
        group.sort(key=lambda p: -sum(w[n] for n in p) / len(p))
        keeper = group[0]
        for other in group[1:]:
            if _hausdorff_m(keeper, other, lat, lon) >= LENS_MAX_SEP_M:
                continue
            done |= set(key)
            for n in other[1:-1]:
                near = min(keeper, key=lambda k: map_heat._haversine_m(
                    lat[n], lon[n], lat[k], lon[k]))
                w[near] = max(w[near], w[n])
            for i in range(len(other) - 1):
                drop.add((other[i], other[i + 1]))
    return _rewire(edges, remap, drop) if drop else edges


def _contract_triangles(lat: list, lon: list, w: list, edges: list) -> list:
    """Collapse three junctions joined in a triangle down to a single T
    junction, by contracting the triangle's SHORTEST side.

    WHY. Where a path meets a road, different runs arrive at slightly different
    points. Each arrival becomes its own junction, and the stretch of road
    between them closes the loop, so one T-junction is drawn as a small triangle
    with a sliver of road for its base. It is not a road feature, it is two
    junctions that should have been one.

    WHY THE SHORTEST SIDE. Contracting it is the edit that moves the least: the
    two arrival points merge into one and the third junction is left exactly
    where it is, so what was a triangle becomes three chains meeting at a point.
    Contracting a longer side instead would drag real road geometry with it.

    WHY A SIZE LIMIT. Not every triangle is an artifact. Measured across the
    history there were 24, and the shortest sides separated cleanly:

        1.7 .. 19.6 m   22 triangles   arrival-point splits
             32.4 m      1 triangle    a real fork, 32/179/397 m
             69.9 m      1 triangle    a real road block, ~70-80 m a side

    The cutoff is D, the candidate radius, which lands in the 19.6-32.4 gap for
    a reason rather than by luck: D is the distance within which the builder
    already treats two readings as the same piece of road, so two junctions
    closer together than D are the same junction. Larger cycles are never
    touched — a four-junction loop is a block, not a T-junction seen twice."""
    _junc, chains = _junction_chains(_adj_of(edges), lat, lon)
    jadj: dict = {}
    for a, b, length, path in chains:
        if a != b:
            jadj.setdefault(a, {}).setdefault(b, []).append((length, path))
            jadj.setdefault(b, {}).setdefault(a, []).append((length, path))

    tris = set()
    for a in jadj:
        nbrs = sorted(jadj[a])
        for i, b in enumerate(nbrs):
            for c in nbrs[i + 1:]:
                if c in jadj.get(b, {}):
                    tris.add(tuple(sorted((a, b, c))))

    # Shortest side first, so the most clear-cut artifact is settled before any
    # triangle overlapping it.
    todo = []
    for a, b, c in tris:
        sides = sorted(min(jadj[x][y]) for x, y in ((a, b), (b, c), (a, c)))
        if sides[0][0] < TRI_MAX_SIDE_M:
            todo.append(sides[0])
    todo.sort(key=lambda t: (t[0], t[1]))

    remap, touched = {}, set()
    for _length, path in todo:
        if touched & set(path):
            continue
        touched |= set(path)
        survivor = max(path, key=lambda n: (w[n], -n))
        tot = sum(w[n] for n in path) or float(len(path))
        lat[survivor] = sum(lat[n] * (w[n] or 1.0) for n in path) / tot
        lon[survivor] = sum(lon[n] * (w[n] or 1.0) for n in path) / tot
        # Max, not sum: the junction was crossed by the union of the passes that
        # used either arm, and adding them would invent a bright spot.
        w[survivor] = max(w[n] for n in path)
        for n in path:
            if n != survivor:
                remap[n] = survivor
    return _rewire(edges, remap, set()) if remap else edges


def _simplify_topology(lat: list, lon: list, w: list, edges: list) -> list:
    """Run both reductions to a fixed point. They feed each other: fusing a lens
    can leave three junctions in a triangle, and contracting a triangle can put
    two chains between the same pair. Runs on the serialized edge list, not the
    live graph, exactly as the MAX_EDGE_M filter does, and leaves absorbed nodes
    in place with their edges rewired away, exactly as _prune_spurs does.
    lat/lon/w must be private copies — both reductions write to them."""
    for _ in range(TOPO_MAX_PASSES):
        before = len(edges)
        edges = _contract_lenses(lat, lon, w, edges)
        edges = _contract_triangles(lat, lon, w, edges)
        if len(edges) == before:
            break
    return edges


def _serialize(graph: RouteGraph) -> dict:
    # Undirected: emit each pair once, normalized low-index first.
    #
    # The length limit is re-checked here as well as at creation, because
    # averaging and smoothing move nodes AFTER their edges were made and an edge
    # can cross the limit on the way. Measured on the real history that is one
    # edge out of 12,064 — but it only takes one to draw a line across the map.
    edges = [[a, b]
             for a in range(len(graph.lat)) for b in sorted(graph.adj[a])
             if a < b and map_heat._haversine_m(graph.lat[a], graph.lon[a],
                                                graph.lat[b], graph.lon[b]) <= MAX_EDGE_M]
    # Private copies: _reduce_triangles moves and merges nodes, and the live
    # graph's arrays must keep the positions its accumulators were built from.
    lat, lon, w = list(graph.lat), list(graph.lon), list(graph.w)
    edges = _simplify_topology(lat, lon, w, edges)

    max_ew = 0.0
    for a, b in edges:
        ew = (w[a] + w[b]) / 2
        if ew > max_ew:
            max_ew = ew
    local_ref = _local_ref_weights(edges, lat, lon, w)
    bbox = None
    if lat:
        bbox = [min(lat), max(lat), min(lon), max(lon)]
    return {
        "version": VERSION,
        "params": _params(),
        # slat/slon/votes are the positional accumulators: without them an
        # incrementally-added run would restart the mean from the current
        # position and quietly weight itself as heavily as all history combined.
        # local_ref_w is per-node and exists only for rendering: the weight
        # that renders as full brightness nearby. Local so a second city is not
        # scaled against this one, and a capped percentile rather than a maximum
        # so one lapped loop does not flatten every other road.
        "nodes": {"lat": lat, "lon": lon, "w": w,
                  "slat": graph.slat, "slon": graph.slon, "votes": graph.votes,
                  "local_ref_w": local_ref},
        "edges": edges,
        "clusters": _clusters(lat, lon, edges),
        "max_edge_weight": max_ew or 1.0,
        "bbox": bbox,
        "run_ids": graph.run_ids,
    }


def _deserialize(data: dict) -> RouteGraph:
    g = RouteGraph()
    n = data["nodes"]
    g.lat, g.lon, g.w = list(n["lat"]), list(n["lon"]), list(n["w"])
    g.slat = list(n.get("slat") or g.lat)
    g.slon = list(n.get("slon") or g.lon)
    g.votes = list(n.get("votes") or [1] * len(g.lat))
    g.adj = [set() for _ in g.lat]
    for i, (la, lo) in enumerate(zip(g.lat, g.lon)):
        b = map_heat._bucket(la, lo)
        g.buckets.setdefault(b, set()).add(i)
        g.node_bucket.append(b)
    for a, b in data["edges"]:
        g.adj[a].add(b)
        g.adj[b].add(a)
    g.run_ids = list(data.get("run_ids", []))
    return g


def save(graph: RouteGraph, path: Path = GRAPH_PATH):
    """Atomic: a crash mid-write must not leave a truncated graph on disk,
    since it's the only copy and rebuilding is expensive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(_serialize(graph), f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load(path: Path = GRAPH_PATH) -> RouteGraph:
    """Returns an empty graph if the file is missing, or if it was built under
    different parameters — several of them affect node positions, so a stale
    graph can't just be re-coloured, it has to be rebuilt."""
    if not path.exists():
        return RouteGraph()
    with open(path) as f:
        data = json.load(f)
    if data.get("version") != VERSION or data.get("params") != _params():
        return RouteGraph()
    return _deserialize(data)


def load_raw(path: Path = GRAPH_PATH) -> dict | None:
    """The serialized dict as-is, for the renderer — it wants flat arrays and
    max_edge_weight, not the mutable RouteGraph with its bucket index."""
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


# ---------- public entry points ----------

def add_run(run_id: int, path: Path = GRAPH_PATH):
    """Folds one newly-imported run into the saved graph. Call right after the
    run and its track points exist in the DB."""
    graph = load(path)
    if run_id in graph.run_ids:
        return  # already folded in; re-adding would double-count its weight
    points = db.get_points_for_graph(run_id)
    if len(points) < 2:
        return
    add_run_to_graph(graph, run_id, points)
    save(graph, path)


def rebuild(path: Path = GRAPH_PATH, progress=None):
    """Replays every run chronologically into a fresh graph. Needed once, and
    again whenever any parameter changes — merging is order-dependent (the
    earliest run seeds the node positions), so chronological replay is what
    makes a rebuild deterministic."""
    graph = RouteGraph()
    runs = [dict(r) for r in db.get_all_runs()]  # date ASC
    for n, r in enumerate(runs, 1):
        points = db.get_points_for_graph(r["id"])
        if len(points) < 2:
            continue
        add_run_to_graph(graph, r["id"], points)
        if progress:
            progress(n, len(runs), r["id"], len(graph.lat))
    save(graph, path)
    return graph
