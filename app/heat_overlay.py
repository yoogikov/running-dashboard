"""Renders the merged route graph as a single Pillow-composited, genuinely
alpha-blended overlay image on top of the map.

tkintermapview's CanvasPath draws each line as a fully-opaque Tk canvas item —
Canvas has no real alpha compositing at all (stipple only fakes transparency
via a dither mask, see map_heat.py's earlier approach). So the overlay is
rasterized in Pillow instead and blitted as one image.

WHAT IS DRAWN
Not runs — the merged graph from route_graph.py: one averaged line per physical
road, with a weight saying how many runs traced it. Drawing raw runs meant
93,133 points and ~6,180 line calls per frame, 230-550ms per redraw, paid on
every single zoom step (see _patch_map_widget). The graph collapses all the
repeated traces of a road into one chain, which is both the honest picture and
vastly less to draw.

Brightness comes from edge weight as a fraction of the graph's maximum, floored
at MIN_ALPHA so a road run once still reads on the map instead of vanishing.

The overlay has to be manually kept in sync with the map on pan/zoom —
tkintermapview's own path objects get their coordinates shifted cheaply on every
move; a rasterized image doesn't get that for free. Two-tier fix, mirroring how
CanvasPath itself handles `move=True` draws:
  - on every pan event (draw_move fires per mouse-move pixel), immediately
    *shift* the existing rendered image by the same pixel delta the tiles moved
    by — cheap, so it never visibly lags behind while dragging.
  - only fully re-render the bitmap (new content, correct edges) once movement
    settles (debounced) or on zoom (always), since shifting alone can't reveal
    newly-panned-into map area.
"""
import math

from PIL import Image, ImageDraw, ImageTk
from tkintermapview.utility_functions import osm_to_decimal

ROUTE_RGB = (230, 130, 40)  # fixed hue; only alpha (brightness) scales with weight
MIN_ALPHA = 45    # a road traced by a single run: dim, but still visible
MAX_ALPHA = 255   # a road at the graph's maximum edge weight
ALPHA_LEVELS = 24  # quantization steps — see _alpha_for_weight
LINE_WIDTH = 4
DEBOUNCE_MS = 120  # full re-render this long after the last pan event, not on every one
CLUSTER_PIN_MAX_PX = 90     # a cluster whose on-screen bounding box is smaller than this
                            # across is pinned instead of relied on to be visible. Measured
                            # in pixels rather than by zoom level so it adapts to the window
                            # and to how spread out a place's roads actually are: 13km of
                            # road inside 90px is at most a few dozen lit pixels.
PIN_CIRCLE = "#8C3D14"      # pin fill / outline / label, keyed to ROUTE_RGB so the pins
PIN_OUTSIDE = "#E68228"     # read as the same data as the lines they stand in for
PIN_TEXT = "#E68228"

OVERSCAN = 0.25         # the bitmap is rendered this much larger than the canvas on every
                        # side, as a fraction of canvas size, and anchored at a negative
                        # offset — so a pan slides real content in instead of blank. See
                        # redraw() and _shift().
                        #
                        # Cost is bitmap area, i.e. (1+2s)^2, so this is bought
                        # rather than free. Measured at the settled zoom:
                        #
                        #     s      median redraw
                        #     0.00      10.1 ms      (no margin: blank on every pan)
                        #     0.15      15.1 ms
                        #     0.25      17.9 ms
                        #     0.35      20.0 ms
                        #     0.50      26.7 ms
                        #
                        # 0.25 buys a quarter-viewport of slack in each direction
                        # for +7.8ms, leaving the frame far inside the budget that
                        # made the immediate draw_zoom re-render viable. Raising it
                        # pays quadratically for slack that _shift's margin check
                        # already handles by re-rendering.
VIEWPORT_PADDING = 0.5  # extra margin around the visible area, as a fraction of its size —
                        # keeps nearby-but-offscreen routes ready before a pan/zoom needs them

# Background fill for the overlay bitmap. Alpha 1 rather than 0 looks like a typo
# and is not: it is worth ~29x on every single frame.
#
# Tk keeps a "valid region" for any photo image carrying an alpha channel — the
# set of pixels that aren't fully transparent — as a union of rectangles, and
# rebuilds it on every update. With a genuinely transparent background, thousands
# of scattered short diagonal route segments explode that region into thousands
# of tiny rectangles, and the region arithmetic dominates everything else:
# measured at 329ms per paste, against 11ms for the identical pixels drawn as a
# few long horizontal lines, and 12ms for a fully opaque image. It is neither the
# pixel count (6% coverage was the slowest case) nor the colour count (2 distinct
# values) — purely how fragmented the non-transparent area is.
#
# Alpha 1/255 makes every pixel technically non-transparent, so the region
# collapses to one rectangle. The cost is 0.4% black over the map, which is
# imperceptible against a basemap already at #17140F.
OVERLAY_BG = (0, 0, 0, 1)


def _alpha_for_weight(w_edge: float, max_edge_weight: float) -> int:
    """Fraction of the LOCAL REFERENCE weight, remapped onto [MIN_ALPHA, MAX_ALPHA].

    max_edge_weight here is not a maximum at all: it is the reference weight for
    this edge's surroundings — the HEAT_CAP_PCT length-weighted percentile within
    route_graph.HEAT_RADIUS_KM. Edges above it clamp to MAX_ALPHA, which is
    deliberate. See "WHY LOCAL HEAT" (what a global maximum does to a second
    city) and "WHY A CAPPED REFERENCE" (what a lapped loop does to every other
    road) in route_graph.py.

    The floor matters: without it, a road run once on a map whose busiest road
    has weight 40 would render at 6/255 — invisible. With it, a lone road sits
    at ~50 and the busiest at 255.

    Quantized to ALPHA_LEVELS so that neighbouring edges of near-identical
    weight collapse to the same value and can be drawn as one polyline instead
    of one call per ~2m edge. That batching is most of the render win."""
    if max_edge_weight <= 0:
        frac = 1.0
    else:
        frac = min(1.0, max(0.0, w_edge / max_edge_weight))
    level = round(frac * (ALPHA_LEVELS - 1))
    frac = level / (ALPHA_LEVELS - 1)
    return int(round(MIN_ALPHA + frac * (MAX_ALPHA - MIN_ALPHA)))


class HeatOverlay:
    def __init__(self, map_widget):
        self.map_widget = map_widget
        # chains: [(merc, alphas, bbox), ...] where merc is [(mx, my), ...] in
        # normalized [0,1] Mercator space, alphas is one quantized alpha per
        # segment (len(merc) - 1), and bbox is (min_lat, max_lat, min_lon, max_lon)
        self.chains: list[tuple] = []
        self._photo_image = None
        self._photo_size = None  # size the current PhotoImage was allocated at
        self._canvas_image_id = None
        self._overscan = (0, 0)  # px of rendered margin on each side; set by redraw()
        self.clusters = []
        self._pins = {}          # cluster index -> marker, only while it is shown
        self._shift_dx = self._shift_dy = 0.0  # px panned since that render
        self._debounce_job = None
        self._last_upper_left_tile_pos = None  # tile position the current bitmap was rendered at
        self._last_zoom = None
        self._patch_map_widget()

    # ---------- building the drawable form ----------

    def set_graph(self, graph: dict | None):
        """graph: the dict from route_graph.load_raw() — flat node arrays,
        undirected edge pairs, and max_edge_weight.

        Everything expensive happens here, once per graph change, rather than
        per redraw: edge weights, alpha quantization, chain extraction and
        per-chain bounding boxes. redraw() fires on every zoom step, so it must
        stay a pure draw loop."""
        self.chains = []
        self.clusters = graph.get("clusters") or [] if graph else []
        self._clear_pins()
        if graph and graph.get("edges"):
            self.chains = self._build_chains(graph)
        self._last_zoom = None      # invalidate: redraw()'s early-out only knows
        self.redraw()               # about the view, not about the content

    # ---------- cluster pins ----------

    def _clear_pins(self):
        for marker in self._pins.values():
            marker.delete()
        self._pins = {}

    def _update_pins(self):
        """Show a pin over any cluster too small on screen to read as roads.

        Zoomed out far enough, a place collapses to a handful of lit pixels that
        are easy to miss entirely and impossible to identify. The test is the
        cluster's own on-screen size rather than a zoom threshold, so it fires at
        whatever zoom actually makes that place too small — which differs between
        a compact set of loops and one spread across a city, and changes again
        when the window is resized.

        Pins are created and destroyed only as visibility changes. redraw() runs
        on every zoom step, and rebuilding markers there would cost more than the
        drawing does."""
        if not self.clusters:
            return
        transform = self._transform()
        if transform is None:
            return
        ax, bx, ay, by = transform
        w, h = self.map_widget.width, self.map_widget.height
        for i, c in enumerate(self.clusters):
            min_lat, max_lat, min_lon, max_lon = c["bbox"]
            x0, y0 = self._merc(max_lat, min_lon)
            x1, y1 = self._merc(min_lat, max_lon)
            span = max(abs(x1 - x0) * ax, abs(y1 - y0) * ay)
            # Off-screen clusters get no pin: a pin pointing outside the window
            # is invisible anyway, and drawing it still costs a canvas item.
            cx, cy = self._merc(c["lat"], c["lon"])
            on = (-w <= cx * ax + bx <= 2 * w) and (-h <= cy * ay + by <= 2 * h)
            want = span < CLUSTER_PIN_MAX_PX and on
            if want and i not in self._pins:
                runs = c["runs"]
                self._pins[i] = self.map_widget.set_marker(
                    c["lat"], c["lon"],
                    text=f"{runs} run{'' if runs == 1 else 's'} · {c['km']:.0f} km",
                    text_color=PIN_TEXT, marker_color_circle=PIN_CIRCLE,
                    marker_color_outside=PIN_OUTSIDE,
                )
            elif not want and i in self._pins:
                self._pins.pop(i).delete()

    @staticmethod
    def _merc(lat: float, lon: float) -> tuple:
        rl = math.radians(lat)
        return ((lon + 180.0) / 360.0,
                (1.0 - math.log(math.tan(rl) + 1.0 / math.cos(rl)) / math.pi) / 2.0)

    @staticmethod
    def _build_chains(graph: dict) -> list[tuple]:
        """Collapses the graph into maximal chains — runs of nodes where every
        interior node has exactly two neighbours, i.e. an uninterrupted stretch
        of road between junctions or dead ends.

        Without this the renderer would issue one draw.line call per edge, and
        edges span only ~2m of road, so a single street would cost hundreds of
        calls. As chains, that same street is one polyline."""
        lat, lon, w = graph["nodes"]["lat"], graph["nodes"]["lon"], graph["nodes"]["w"]
        n = len(lat)
        # Per-node reference weight: the road brightness is measured against,
        # local to route_graph.HEAT_RADIUS_KM and capped at a percentile rather
        # than a maximum — so a quieter city still reaches full brightness on its
        # own scale, and one lapped loop does not flatten everything around it.
        # Older graph files predate the array and fall back to the global maximum.
        local_ref = graph["nodes"].get("local_ref_w")
        max_ew = graph.get("max_edge_weight") or 1.0

        adj: list[list[int]] = [[] for _ in range(n)]
        for a, b in graph["edges"]:
            adj[a].append(b)
            adj[b].append(a)

        def interior(i: int) -> bool:
            return len(adj[i]) == 2

        used = set()  # undirected edges already placed in a chain
        chains = []

        def key(a: int, b: int) -> tuple:
            return (a, b) if a < b else (b, a)

        def walk(start: int, nxt: int) -> list[int]:
            """Follows the chain from `start` through `nxt` until it reaches a
            junction, a dead end, or an edge already consumed."""
            path = [start, nxt]
            used.add(key(start, nxt))
            prev, cur = start, nxt
            while interior(cur):
                step = adj[cur][0] if adj[cur][1] == prev else adj[cur][1]
                k = key(cur, step)
                if k in used:
                    break
                used.add(k)
                path.append(step)
                prev, cur = cur, step
            return path

        # Start from every node that isn't a chain interior — junctions and
        # dead ends. Everything reachable from them gets covered.
        for i in range(n):
            if interior(i):
                continue
            for b in adj[i]:
                if key(i, b) not in used:
                    chains.append(walk(i, b))

        # Anything left is a pure cycle (every node interior), so it has no
        # junction or endpoint to start from and the sweep above never reached it.
        for i in range(n):
            for b in adj[i]:
                if key(i, b) not in used:
                    chains.append(walk(i, b))

        # Precompute each node's position in normalized [0,1] Web Mercator space.
        # The projection is linear in 2**zoom, so the expensive part — a log, a
        # tan and a cos per point — depends only on latitude and can be hoisted
        # out of redraw() entirely. redraw() then reduces to a multiply and a
        # subtract per point, which was worth ~126ms of every frame.
        merc_x = [(lo + 180.0) / 360.0 for lo in lon]
        merc_y = []
        for la in lat:
            rl = math.radians(la)
            merc_y.append((1.0 - math.log(math.tan(rl) + 1.0 / math.cos(rl)) / math.pi) / 2.0)

        result = []
        for path in chains:
            merc = [(merc_x[i], merc_y[i]) for i in path]
            alphas = [
                _alpha_for_weight(
                    (w[path[j]] + w[path[j + 1]]) / 2,
                    # Both ends of an edge are at most MAX_EDGE_M apart, so they
                    # always share a region; either endpoint gives the same value.
                    (local_ref[path[j]] or max_ew) if local_ref else max_ew,
                )
                for j in range(len(path) - 1)
            ]
            lats = [lat[i] for i in path]
            lons = [lon[i] for i in path]
            result.append((merc, alphas, (min(lats), max(lats), min(lons), max(lons))))
        return result

    # --- previous approach: one entry per run, every raw GPS point drawn ---
    # Replaced by set_graph() above. Kept rather than deleted in case the
    # graph merge needs reverting.
    #
    # def set_routes(self, runs_points: dict[int, list[tuple]]):
    #     self.runs_points = runs_points
    #     self._run_bboxes = {
    #         run_id: (
    #             min(lat for lat, _lon, _d in pts), max(lat for lat, _lon, _d in pts),
    #             min(lon for _lat, lon, _d in pts), max(lon for _lat, lon, _d in pts),
    #         )
    #         for run_id, pts in runs_points.items() if pts
    #     }
    #     self.redraw()

    # ---------- projection / viewport ----------

    def _transform(self):
        """(ax, bx, ay, by) such that a node at normalized Mercator (mx, my)
        lands at canvas (mx*ax + bx, my*ay + by). Returns None if the map isn't
        laid out yet.

        Reducing the projection to a pair of affine coefficients is what lets
        redraw() avoid any trigonometry — see _build_chains."""
        zoom = round(self.map_widget.zoom)
        n = 2.0 ** zoom
        ul = self.map_widget.upper_left_tile_pos
        lr = self.map_widget.lower_right_tile_pos
        tile_w = lr[0] - ul[0]
        tile_h = lr[1] - ul[1]
        if tile_w == 0 or tile_h == 0:
            return None
        sx = self.map_widget.width / tile_w
        sy = self.map_widget.height / tile_h
        return (n * sx, -ul[0] * sx, n * sy, -ul[1] * sy)

    def _latlon_to_canvas(self, lat: float, lon: float) -> tuple:
        t = self._transform()
        if t is None:
            return (0.0, 0.0)
        ax, bx, ay, by = t
        rl = math.radians(lat)
        mx = (lon + 180.0) / 360.0
        my = (1.0 - math.log(math.tan(rl) + 1.0 / math.cos(rl)) / math.pi) / 2.0
        return (mx * ax + bx, my * ay + by)

    def _visible_bounds(self) -> tuple:
        """(min_lat, max_lat, min_lon, max_lon) of the current viewport, padded
        by VIEWPORT_PADDING so a small pan/zoom doesn't need an immediate
        rebuild to reveal routes just outside the strict visible edge."""
        zoom = round(self.map_widget.zoom)
        lat1, lon1 = osm_to_decimal(*self.map_widget.upper_left_tile_pos, zoom)
        lat2, lon2 = osm_to_decimal(*self.map_widget.lower_right_tile_pos, zoom)
        min_lat, max_lat = min(lat1, lat2), max(lat1, lat2)
        min_lon, max_lon = min(lon1, lon2), max(lon1, lon2)
        # Culling is per-chain and cheap, so this only has to be AT LEAST the
        # overscan; anything less would cull chains that the bigger bitmap has
        # room to draw, and the margin would come out blank.
        pad = max(VIEWPORT_PADDING, OVERSCAN + 0.05)
        pad_lat = (max_lat - min_lat) * pad
        pad_lon = (max_lon - min_lon) * pad
        return (min_lat - pad_lat, max_lat + pad_lat, min_lon - pad_lon, max_lon + pad_lon)

    @staticmethod
    def _bbox_intersects(a: tuple, b: tuple) -> bool:
        a_min_lat, a_max_lat, a_min_lon, a_max_lon = a
        b_min_lat, b_max_lat, b_min_lon, b_max_lon = b
        return not (
            a_max_lat < b_min_lat or a_min_lat > b_max_lat
            or a_max_lon < b_min_lon or a_min_lon > b_max_lon
        )

    # ---------- drawing ----------

    def redraw(self):
        """Full re-render: regenerates the bitmap from scratch at the map's
        current position/zoom and re-anchors it at (0, 0) — canvas.move() calls
        from panning shift the item away from (0, 0) over time, so this must
        reset that, not just swap the image content.

        Chains outside the padded viewport are skipped wholesale, and each
        surviving chain is emitted as runs of constant alpha rather than one
        call per edge."""
        w, h = self.map_widget.width, self.map_widget.height
        if w <= 0 or h <= 0:
            return

        ox, oy = int(w * OVERSCAN), int(h * OVERSCAN)

        # Nothing moved since the last render, so the bitmap is already correct
        # and regenerating it would be pure waste. This is not a rare case: one
        # zoom step arrives here TWICE, because tkintermapview's draw_zoom ends
        # by calling draw_move(called_after_zoom=True) — which is the patched
        # draw_move, which sees the changed zoom, cannot _shift() across a scale
        # change, and re-renders. patched_draw_zoom then re-renders the identical
        # view. Skipping the second halves the cost of every zoom step.
        #
        # The restack still has to happen: the map draws its tiles as canvas
        # images too, and a redrawn tile lands above the overlay.
        if (self._photo_image is not None
                and self._photo_size == (w + 2 * ox, h + 2 * oy)
                and self._shift_dx == 0.0 and self._shift_dy == 0.0
                and self._last_zoom == round(self.map_widget.zoom)
                and self._last_upper_left_tile_pos == self.map_widget.upper_left_tile_pos):
            self._restack()
            return

        overlay = Image.new("RGBA", (w + 2 * ox, h + 2 * oy), OVERLAY_BG)
        draw = ImageDraw.Draw(overlay, "RGBA")
        transform = self._transform()
        if transform is not None and self.chains:
            ax, bx, ay, by = transform
            # _transform lands a node on CANVAS pixels; the bitmap extends ox/oy
            # further up and left, so shift the origin to match. The image is then
            # placed at (-ox, -oy) below, which cancels it back out on screen.
            bx += ox
            by += oy
            min_lat, max_lat, min_lon, max_lon = self._visible_bounds()
            line = draw.line
            for merc, alphas, bbox in self.chains:
                # Inlined bbox rejection: this runs 20k+ times per frame, and
                # the method-call overhead alone was measurable.
                if (bbox[1] < min_lat or bbox[0] > max_lat
                        or bbox[3] < min_lon or bbox[2] > max_lon):
                    continue
                xy = [(mx * ax + bx, my * ay + by) for mx, my in merc]
                # Emit each maximal stretch of equal alpha as one polyline.
                start = 0
                cur = alphas[0]
                for j in range(1, len(alphas)):
                    if alphas[j] != cur:
                        line(xy[start:j + 1], fill=(*ROUTE_RGB, cur), width=LINE_WIDTH)
                        start = j
                        cur = alphas[j]
                line(xy[start:], fill=(*ROUTE_RGB, cur), width=LINE_WIDTH)

        # Reuse the Tk image object rather than building a new one. Constructing
        # an ImageTk.PhotoImage registers a fresh image with the Tk interpreter
        # and costs ~100ms at this size regardless of content; paste() writes
        # into the existing one and is far cheaper. Only reallocate if the
        # window has been resized.
        if self._photo_image is not None and self._photo_size == overlay.size:
            self._photo_image.paste(overlay)
        else:
            self._photo_image = ImageTk.PhotoImage(overlay)
            self._photo_size = overlay.size
            if self._canvas_image_id is not None:
                self.map_widget.canvas.itemconfigure(
                    self._canvas_image_id, image=self._photo_image
                )
        canvas = self.map_widget.canvas
        if self._canvas_image_id is None:
            self._canvas_image_id = canvas.create_image(
                -ox, -oy, image=self._photo_image, anchor="nw", tag="path"
            )
        else:
            # Re-anchor: undoes any accumulated move() shifts from panning.
            canvas.coords(self._canvas_image_id, -ox, -oy)
        self._update_pins()
        self._restack()

        self._overscan = (ox, oy)
        self._shift_dx = self._shift_dy = 0.0
        self._last_upper_left_tile_pos = self.map_widget.upper_left_tile_pos
        self._last_zoom = round(self.map_widget.zoom)

    def _restack(self):
        """Put the overlay back above the tiles, then the cluster pins back above
        the overlay. Both raises are required and the order is load-bearing: the
        overlay is raised over everything, markers included, so the markers have
        to go back on top or the pins render behind the routes bitmap."""
        if self._canvas_image_id is None:
            return
        canvas = self.map_widget.canvas
        canvas.tag_raise(self._canvas_image_id)
        canvas.tag_raise("marker")

    def _shift(self):
        """Cheap immediate reposition for a pan event — moves the existing
        rendered image by the same pixel delta the map moved by, exactly like
        CanvasPath's own move=True path does for individual line coordinates.

        Returns False, meaning the caller must fall back to a full redraw, in two
        cases: the zoom changed (shifting cannot handle a scale change), or the
        pan has consumed the overscan margin. The second is not optional. The
        debounce is cancelled and rescheduled by every pan event, so during one
        continuous drag a debounced redraw never fires at all — without this
        check the margin would be exhausted a fraction of a second into the drag
        and the leading edge would go blank exactly as it did before, just
        slightly later. Panning past the margin is the one moment a redraw is
        genuinely required, so it is triggered by distance rather than by time."""
        if self._canvas_image_id is None or self._last_upper_left_tile_pos is None:
            return False
        if round(self.map_widget.zoom) != self._last_zoom:
            return False

        tile_w = self.map_widget.lower_right_tile_pos[0] - self.map_widget.upper_left_tile_pos[0]
        tile_h = self.map_widget.lower_right_tile_pos[1] - self.map_widget.upper_left_tile_pos[1]
        if tile_w == 0 or tile_h == 0:
            return False

        dx = ((self._last_upper_left_tile_pos[0] - self.map_widget.upper_left_tile_pos[0])
              / tile_w) * self.map_widget.width
        dy = ((self._last_upper_left_tile_pos[1] - self.map_widget.upper_left_tile_pos[1])
              / tile_h) * self.map_widget.height

        ox, oy = self._overscan
        if abs(self._shift_dx + dx) > ox or abs(self._shift_dy + dy) > oy:
            return False  # margin used up; only a re-render has the missing content

        self.map_widget.canvas.move(self._canvas_image_id, dx, dy)
        self._shift_dx += dx
        self._shift_dy += dy
        self._last_upper_left_tile_pos = self.map_widget.upper_left_tile_pos
        return True

    def _redraw_debounced(self):
        if self._debounce_job is not None:
            self.map_widget.after_cancel(self._debounce_job)
        self._debounce_job = self.map_widget.after(DEBOUNCE_MS, self.redraw)

    def destroy(self):
        """Shutdown only. Cancels the pending redraw and drops the cluster pins
        so nothing fires against a dead interpreter. Deliberately does NOT
        un-patch the map widget: the widget is being destroyed too, and
        restoring the originals would only re-enable draws during teardown."""
        if self._debounce_job is not None:
            self.map_widget.after_cancel(self._debounce_job)
            self._debounce_job = None
        self._clear_pins()

    def _patch_map_widget(self):
        """Wraps the three tkintermapview methods that reposition/redraw
        everything on zoom and pan, so the overlay stays in sync.

        draw_zoom (every scroll-wheel/button zoom step) matters most and was
        originally missed: it ends by calling draw_move(called_after_zoom=True)
        rather than draw_initial_array, and _shift() correctly refuses to shift
        on a zoom change (it can't handle a scale change) — so without this
        patch, draw_move's *debounced* fallback would leave the overlay frozen
        at the old scale for up to DEBOUNCE_MS on every zoom step, a visible
        desync. This forces an immediate (non-debounced) re-render right after
        every zoom instead, which is exactly why redraw() has to be cheap.
        """
        widget = self.map_widget
        original_draw_initial_array = widget.draw_initial_array
        original_draw_move = widget.draw_move
        original_draw_zoom = widget.draw_zoom

        def patched_draw_initial_array(*args, **kwargs):
            result = original_draw_initial_array(*args, **kwargs)
            self.redraw()
            return result

        def patched_draw_move(*args, **kwargs):
            result = original_draw_move(*args, **kwargs)
            if self._shift():
                # Shifted within the rendered margin, so the screen is already
                # correct; the debounced pass just refills the margin once the
                # drag settles.
                self._redraw_debounced()
            else:
                # Margin exhausted (or zoom changed): nothing to slide in, so
                # re-render now rather than showing a blank edge until the
                # debounce fires.
                if self._debounce_job is not None:
                    self.map_widget.after_cancel(self._debounce_job)
                    self._debounce_job = None
                self.redraw()
            return result

        def patched_draw_zoom(*args, **kwargs):
            result = original_draw_zoom(*args, **kwargs)
            if self._debounce_job is not None:  # supersede draw_move's debounced call above
                self.map_widget.after_cancel(self._debounce_job)
                self._debounce_job = None
            self.redraw()
            return result

        # All three patched_* closures already capture the bound original method
        # and `self` (this overlay) — assigning them directly as instance
        # attributes (not via types.MethodType) is correct here, unlike
        # tile_cache.py's patch, since these don't take `widget` as an explicit
        # first argument.
        widget.draw_initial_array = patched_draw_initial_array
        widget.draw_move = patched_draw_move
        widget.draw_zoom = patched_draw_zoom
