# running-dashboard

A local Tkinter desktop app for running data — no cloud, no accounts.

This repo is a from-scratch rebuild. `app/` currently contains only the
module/host framework with **zero feature modules registered** — so running
it opens a plain window and nothing else. That's deliberate: it's the
staging point every feature gets added onto, one module at a time.

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
check both match it), creates a `Host`, calls `h.start()`, and wires
`WM_DELETE_WINDOW` to `h.stop()` then `root.destroy()`.

This is also where modules get registered — currently none are, so
`Host` resolves an empty module list and you get the bare window. Adding
a feature means writing a `Module` subclass and adding one
`h.register(...)` (or equivalent) call here.

## Notes for editors

- `app/` has no `__init__.py` and relies on
  `sys.path.insert(0, APP_DIR)` in `main.py` for flat top-level imports
  (`import host`, `import module as mod`, etc.). Editors/type-checkers
  that don't know about this will flag `host`/`module`/`paths` as
  unresolved imports — expected and harmless.
- `data/` is gitignored; it holds runtime output (e.g. `launch.log` from
  the i3 launcher) and is created on demand, not tracked.
