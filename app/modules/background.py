"""The background: the map, its tile cache, and the route-heat overlay drawn on
top of it.

This is the app's ground layer — everything else floats above it. surface=NONE:
root.py is the ROOT owner now, so this builds straight onto self.host.root
instead of taking a parent, the same way topbar.py does.
"""
import threading

import tkintermapview
from PIL import Image, ImageTk

import db
import heat_overlay
import map_heat
import module
import paths
import route_graph
import tile_cache
from ui import cards

DEFAULT_POSITION = (20.0, 0.0)  # fallback world-ish view when no runs logged yet
DEFAULT_ZOOM = 2
TILE_CACHE_PATH = paths.DATA_DIR / "tile_cache.db"
TILE_CACHE_MAX_BYTES = 300 * 1024 * 1024  # 300 MB cap, oldest-used tiles evicted first
TILE_CACHE_PRUNE_INTERVAL_MS = 5 * 60 * 1000  # re-check the cap every 5 minutes while running


class BackgroundModule(module.Module):
    id = "background"
    surface = module.NONE
    title = "Map"

    def build(self, parent):
        self.map_widget = tkintermapview.TkinterMapView(
            self.host.root, width=1280, height=820, corner_radius=0,
            database_path=str(TILE_CACHE_PATH),
            bg_color=cards.APP_BG,
        )
        # CartoDB Dark Matter, no-labels: dark basemap, no place-name clutter, no API key.
        self.map_widget.set_tile_server(
            "https://basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png", max_zoom=20
        )
        # tkintermapview's database_path only *reads* an existing tiles table (meant
        # for its separate OfflineLoader pre-download feature) — it never writes tiles
        # fetched during normal browsing back to disk. Patch in write-through caching
        # with a size cap so tiles you've actually viewed persist across restarts.
        tile_cache.enable_write_through_cache(
            self.map_widget, str(TILE_CACHE_PATH), max_bytes=TILE_CACHE_MAX_BYTES
        )
        # tkintermapview hardcodes its own placeholder tiles (light gray while
        # panning/zooming, near-white when a tile fails to load), and separately
        # hardcodes its raw Canvas bg to a cream color (#F1EFEA) that shows through
        # wherever no tile has been drawn at all yet — bg_color is only used to
        # paint corner-radius arcs, never wired to the canvas. Override all three
        # so a loading/uncovered region reads dark instead of flashing white.
        # Held on self as well as on the widget: the widget's own reference is what
        # keeps it alive today, and an explicit one removes the GC footgun.
        self._dark_tile = ImageTk.PhotoImage(
            Image.new("RGB", (self.map_widget.tile_size, self.map_widget.tile_size), (23, 20, 15))
        )
        self.map_widget.empty_tile_image = self._dark_tile
        self.map_widget.not_loaded_tile_image = self._dark_tile
        self.map_widget.canvas.configure(bg=cards.APP_BG)

        # Not placed here — hidden until root.py's heatmap tab calls show().
        # Placing needs a mapped widget with real dimensions, so the initial
        # zoom_to_last fit happens in show() instead of here.
        self.visible = False
        self.map_widget.set_position(*DEFAULT_POSITION)
        self.map_widget.set_zoom(DEFAULT_ZOOM)

        # No pan inertia. tkintermapview's mouse_release() starts fading_move(),
        # which keeps applying a decaying move_velocity for ~a second after the
        # button is up. The map should stop where it is let go. Neutering the
        # animation itself rather than mouse_release, because mouse_release also
        # dispatches map_click_callback for a click that didn't move.
        self.map_widget.fading_move = lambda: None

        self.canvas = self.map_widget.canvas
        self.overlay = heat_overlay.HeatOverlay(self.map_widget)
        self._prune_job = None
        self._drawn = False   # has draw_routes ever run? see show()

    def on_start(self):
        self._schedule_tile_cache_prune()
        self.host.subscribe("run.imported", self._on_run_imported)

    def on_stop(self):
        if self._prune_job is not None:
            self.host.root.after_cancel(self._prune_job)
            self._prune_job = None
        self.overlay.destroy()

    # ---------- tile cache maintenance ----------

    def _schedule_tile_cache_prune(self):
        def prune_in_background():
            try:
                tile_cache.prune_cache(str(TILE_CACHE_PATH), TILE_CACHE_MAX_BYTES)
            except Exception:
                pass  # cache maintenance is best-effort; never worth crashing over

        threading.Thread(target=prune_in_background, daemon=True).start()
        self._prune_job = self.host.root.after(
            TILE_CACHE_PRUNE_INTERVAL_MS, self._schedule_tile_cache_prune
        )

    # ---------- visibility ----------

    def show(self):
        """Called by root.py's heatmap tab. Idempotent."""
        if self.visible:
            return
        self.map_widget.place(x=0, y=0, relwidth=1, relheight=1)
        self.visible = True
        if not self._drawn:
            self.draw_routes(zoom_to_last=True)   # first reveal: fit the last run
            self._drawn = True

    def hide(self):
        """Called by root.py's heatmap tab. Idempotent."""
        if not self.visible:
            return
        self.map_widget.place_forget()
        self.visible = False

    # ---------- map heatmap ----------

    def _on_run_imported(self, run_id, run):
        self.draw_routes(zoom_to_last=self.visible)

    def draw_routes(self, zoom_to_last: bool = False):
        """Draws the merged route graph (route_graph.py) as a heat-colored
        overlay: one averaged line per physical road, brightness set by how
        many runs traced it.

        This used to load every run's every raw GPS point — 93k of them — and
        hand them all to the overlay, which then redrew the lot on every single
        zoom step. The graph is built incrementally on import instead, so
        there's nothing to compute here beyond reading one JSON file.

        zoom_to_last: fit the view to just the most recent run instead of the
        whole graph (which can be zoomed way out if your runs span different
        cities).

        Public because it is this module's API — the thing other modules call
        when they change what should be on the map."""
        self.overlay.set_graph(route_graph.load_raw())

        if not zoom_to_last:
            return
        runs = [dict(r) for r in db.get_all_runs()]  # ordered by date ASC
        if not runs:
            return
        tps = db.get_track_points(runs[-1]["id"])
        flat_points = [(tp["lat"], tp["lon"]) for tp in tps if tp["lat"] is not None]
        bounds = map_heat.bounding_box(flat_points)
        if bounds:
            self.map_widget.fit_bounding_box(bounds[0], bounds[1])
