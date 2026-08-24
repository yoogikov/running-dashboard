# running-dashboard

A local Tkinter desktop app for running data — no cloud, no accounts.

This repo is a from-scratch rebuild. `app/` started as just the module/host
framework with zero feature modules registered — a plain window and nothing
else. That's still the model: features get added one module at a time,
each registered explicitly in `main.py`.

The original, fuller prototype (ingestion pipeline, route-graph merging,
map/heat rendering, importer/analyzer UI, etc.) lives untouched in
`running/` for reference during the rebuild; it is gitignored and not part
of this repo's history.

## Running it

```sh
python3 app/main.py
```

Opens a single window (`WM_CLASS=running`, title "Running Dashboard",
`#17140f` background). On this machine it's also bound to **Mod4+u** via
i3 (`~/.config/i3/config`), which switches to a dedicated `running`
workspace and runs `~/.local/bin/running` — a small idempotent launcher
that checks (via `xdotool`) whether a `running`-classed window already
exists before spawning a new process, so repeated presses don't pile up
duplicate windows.

## Architecture

Four files, each with one job:

### `app/paths.py`
Shared path constants: `APP_DIR` (the `app/` folder) and `DATA_DIR`
(`app/../data`). Exists so any module — regardless of how deep it lives
under `modules/` — can find `data/` the same way, instead of each
recomputing it relative to its own `__file__`.

### `app/module.py`
Defines the `Module` base class: the contract every feature must follow.
A module declares:

- `id` — unique name; how `requires` and `host.get()` look it up
- `requires` — tuple of other module ids to have injected
- `surface` — where it wants to render:
  - `NONE` — headless, or provides its own surface (`build(None)`)
  - `ROOT` — owns the whole window (`build(<the Tk root>)`)
  - `LAYER` — draws on the map canvas *(not yet implemented)*
  - `PANEL` — floating panel over the map *(not yet implemented)*
  - `TAB` — item on the top bar *(not yet implemented)*
- `title` — panel chrome / tab label, defaults to `id`

and implements four lifecycle methods, called in this order by the host:

1. `on_deps(deps)` — receive the modules named in `requires`, keyed by id.
   Store references; don't call into them yet (their `build()` hasn't run).
2. `build(parent)` — create widgets under `parent` (whose kind is fixed by
   `surface`). Runs in dependency order, so a dependency's widgets already
   exist by the time yours are built.
3. `on_start()` — everything, everywhere, is built. First safe point to
   call into a dependency, subscribe to events, load data, start timers.
4. `on_stop()` — cancel `after` jobs and stop threads. Do not destroy
   widgets the host created for you — the host owns those.

`LAYER`/`PANEL`/`TAB` are intentionally deferred: they come back once a
module actually needs them, rather than being built speculatively ahead
of time.

### `app/host.py`
The orchestrator (`Host` class + `ModuleError` exception). Given a set of
registered module classes, it:

- **Resolves order** — depth-first topological sort of `requires`, with
  registration order as the tiebreak (this will also become map-layer
  z-order once `surface=LAYER` exists). A dependency cycle, or a
  `requires` naming an unregistered id, raises `ModuleError`.
- **Runs four passes** over every module, each pass complete before the
  next starts (so a missing dependency fails while the screen is still
  empty, not half way through drawing it):
  1. construct — instantiate every module class
  2. inject — call `on_deps` with each module's resolved dependencies
  3. build — turn each module's declared `surface` into a real Tk parent
     via `_make_surface`, then call `build(parent)`
  4. start — call `on_start()`, tracking start order for teardown
- **Tears down** in `stop()` by calling `on_stop()` in the *reverse* of
  the order modules actually started in, so nothing is torn down while
  something depending on it is still alive. One module's teardown
  exception is caught and printed, not allowed to block the rest.
- **Event bus** — `subscribe(event, handler)` / `publish(event, **kw)` /
  `publish_threadsafe(event, **kw)`. Synchronous, main-thread-only,
  subscription order; `publish` raises if called off the main thread
  (Tk isn't thread-safe) — worker threads should use
  `publish_threadsafe`, which hops onto the Tk loop via `root.after(0, …)`.
  A handler that raises is caught and printed, not allowed to break
  other subscribers.

Two distinct ways modules talk to each other, each for its own job:
`requires` is for "I cannot work without this" (a traceable, steppable
method call); the event bus is for "somebody may care that this happened"
(the publisher doesn't know or care who's listening, so a listener can be
added later without touching the publisher).

### `app/main.py`
The entry point. Creates the Tk root (`className="running"` so i3's
`assign [class="running"]` rule and the launcher's `xdotool` duplicate
check both match it), calls `db.init_db()` (creates `data/running.db`
and its tables if they don't exist — without this, every import fails:
`dedupe`/`db` hit `sqlite3.OperationalError: no such table: runs`, which
the importer's broad exception handling quietly routes to `failed/`,
so the bad file is more useful to look at than the error), creates a
`Host`, calls `h.start()`, and wires `WM_DELETE_WINDOW` to `h.stop()`
then `root.destroy()`.

This is also where modules get registered. Adding a feature means
writing a `Module` subclass and adding one `h.register(...)` call here.

## Modules registered so far

### `app/modules/root.py` — `Root`
`surface=ROOT`. Owns the whole window and fills it with a single grey
(`#2b2b2b`) frame. No content of its own — it exists so every other
module has a stable, already-built parent to sit on top of instead of
the bare Tk root.

### `app/modules/topbar.py` — `TopBar`
`surface=NONE`, `requires=("root",)`. A fixed-height (40px), darker
(`#1f1f1f`) strip across the top of the window, where tab/nav buttons
will eventually live — empty for now. Builds itself directly onto the
root module's frame rather than waiting on `surface=TAB`, which isn't
implemented yet.

It's auto-hiding: positioned with `place()` at `y=-HEIGHT` (just above
the visible window) until the mouse touches the top edge
(`event.y_root - root.winfo_rooty() <= EDGE_TRIGGER`), at which point it
animates down to `y=0` in fixed steps via repeated `root.after(...)`
calls; it slides back up once the mouse moves more than `HIDE_MARGIN`
below the top. Motion is caught with `bind_all("<Motion>", ...)` so it
sees pointer movement anywhere in the window, not just over the bar
itself.

### `app/modules/heatmap.py` — `HeatmapTab`
`surface=NONE`, `requires=("topbar", "background")`. Packs a "Heatmap"
label into the top bar as a tab (`surface=TAB` isn't implemented yet, so
this is a plain click-bound label rather than a real tab widget). On
click, calls the background module's `show()` and lifts the top bar
back above it, so the auto-hiding bar stays on top of the map.

### `app/modules/background.py` — `BackgroundModule`
`surface=NONE`, `requires=("root",)`. Owns the map: a `tkintermapview`
widget (CartoDB Dark Matter basemap, no labels, no API key) with tile
caching (`tile_cache.py`) and a heat-colored route overlay
(`heat_overlay.py`) drawn on top of it. `show()`/`hide()` place/unplace
the widget (idempotent); the first `show()` also draws the route graph
and zooms to the most recent run. Subscribes to `run.imported` and
redraws automatically when a new run lands.

Adapted from the original for this repo's root design: it builds its
map widget onto `root_mod.frame` instead of the bare Tk root, so it
stacks correctly under the (also `root.frame`-mounted) top bar — in the
original, `root.py` itself was the bare-root owner and everything else,
map included, built straight onto `host.root`.

### `app/modules/importer.py` — `ImporterModule`
`surface=NONE`. Watches `data/imports/` for dropped GPX/TCX files and
imports them automatically. Thin by design: `on_start`/`on_stop` just
start and stop a `FolderWatcher` (see below); the only other thing it
does is turn that watcher's single `on_import` callback into a
`run.imported` bus event, so any future module can react without this
file or `folder_watch.py` changing again.

Not a UI module — it has no visible surface, just runs in the background.

## The data-import pipeline

Brought in as-is from `running/` (already-working, tested code — ported
whole rather than rebuilt, unlike the framework files above):

- **`app/gpx_parser.py`** — stdlib-only GPX/TCX parsing.
- **`app/gps_smooth.py`** — drops GPS outliers, then a moving-average
  smooth over lat/lon.
- **`app/track_processing.py`** — runs the above, then re-summarizes
  distance/pace/elevation/date from the smoothed points.
- **`app/dedupe.py`** — decides whether a parsed run is already stored
  (start-time match within tolerance, or date+distance+duration
  fallback).
- **`app/db.py`** — SQLite storage (`data/running.db`): `runs` and
  `track_points` tables, plus the accessors the rest of the pipeline
  needs.
- **`app/route_graph.py`** + **`app/map_heat.py`** — merges every run
  into one shared, undirected route graph (`data/route_graph.json`) for
  future heatmap rendering. This is the most involved piece by far; see
  its own docstrings for the matching/merging algorithm.
- **`app/folder_watch.py`** — the actual watcher. Polls `data/imports/`
  every 2 seconds; for each new file, on its own worker thread: parse →
  reject to `failed/` if empty → smooth → dedupe-check → insert into the
  db → fold into the route graph → move to `imported/`/`duplicates/`.
  Manual fields (RPE, pulse, sleep, soreness) are left blank on
  auto-import; only `modules/importer.py`'s callback hops back onto the
  main thread, since that's the only part that touches Tk state.

### ⚠️ Bulk imports and `app/rebuild_graph.py`

Route merging is order-dependent — whichever run reaches a spot first
seeds that node's position (see `route_graph.py`'s own docstrings) — so
the graph only comes out right if runs are folded in roughly the order
they actually happened. `folder_watch.py`'s incremental `add_run()`
handles a normal one-at-a-time live import fine either way, but it scans
its directory **alphabetically**, not chronologically. Bulk-dropping many
historical GPX files into `data/imports/` at once therefore merges them
in the wrong order, and the resulting graph can come out visibly wrong —
spurious tangled junctions, missing spurs that should connect to the
rest of the road.

`app/rebuild_graph.py` fixes this: it doesn't touch `running.db`, just
rebuilds `data/route_graph.json` from scratch by replaying every stored
run **chronologically by date** (`route_graph.rebuild()`, which already
existed for exactly this). Run it after any bulk import:

```sh
python3 app/rebuild_graph.py
```

## The map / heatmap rendering

Also brought in as-is, and now wired into `modules/background.py` (see
above):

- **`app/heat_overlay.py`** — `HeatOverlay`: rasterizes the merged route
  graph into one Pillow-composited, genuinely alpha-blended image and
  blits it over the map, rather than drawing thousands of individual Tk
  canvas lines (Canvas has no real alpha compositing). Brightness per
  road scales with how many runs traced it. Patches the map widget's
  pan/zoom internals so the overlay stays in sync as you move around —
  cheaply shifted pixel-for-pixel on every pan, fully re-rendered on
  zoom or once panning settles.
- **`app/tile_cache.py`** — write-through SQLite cache for the map's own
  basemap tiles (`data/tile_cache.db`, 300MB cap, oldest evicted first).
  `tkintermapview` only *reads* a pre-downloaded tile cache by default;
  this patches it to also *write* every tile fetched during normal use.
- **`app/ui/cards.py`** — the color palette (warm charcoal / desaturated
  terracotta) and `make_card`/`card_title` helpers for floating panels
  over the map. Not yet used by anything wired in.

## Data directory

`data/` holds everything the app produces at runtime:

```
data/
  running.db            # sqlite: runs + track points
  route_graph.json       # merged route graph cache
  tile_cache.db           # map tile cache
  launch.log              # i3 launcher output
  imports/
    imported/             # successfully-imported GPX/TCX files land here
    failed/                # files that failed to parse
    duplicates/            # files matching a run already stored
```

The folder *structure* is tracked in git via `.gitkeep` files (so a
fresh clone has the right layout), but none of the actual contents are —
`.gitignore` excludes `data/*.db`, `data/*.json`, `data/*.log`, and any
`.gpx`/`.tcx` under `data/imports/*/` by pattern, rather than
blanket-ignoring `data/` itself.

## Notes for editors

- `app/` has no `__init__.py` and relies on
  `sys.path.insert(0, APP_DIR)` in `main.py` for flat top-level imports
  (`import host`, `import module as mod`, etc.). Editors/type-checkers
  that don't know about this will flag `host`/`module`/`paths` and the
  pipeline modules as unresolved imports — expected and harmless.
