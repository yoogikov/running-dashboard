"""Holds the modules, resolves what they need from each other, creates the
surfaces they ask for, and carries events between them.

TWO MECHANISMS, EACH FOR ITS OWN JOB
`requires` is for "I cannot work without this" — the host injects the module
object itself, so the call is a plain method call you can grep for and step
through. The event bus is for "somebody may care that this happened" — the
publisher does not know or care who is listening, which is what lets a module
be added later without editing anything that already exists.

Reaching for the bus where a dependency belongs gives you an app whose control
flow you cannot follow; reaching for a dependency where the bus belongs means
editing the publisher every time a new listener shows up.

Only surface=NONE and surface=ROOT are implemented so far — LAYER/PANEL/TAB
come back once a module actually needs them, rather than speculatively.
"""
import threading
import tkinter as tk
import traceback

import module as mod


class ModuleError(RuntimeError):
    """A registry or dependency failure. Always fatal, and always raised before
    mainloop() — a wiring mistake should stop the app, not degrade it."""


class Host:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._classes = {}      # id -> Module subclass, in registration order
        self._mods = {}         # id -> instance
        self._order = []        # topologically sorted ids
        self._subs = {}         # event name -> [handler]
        self._started = []      # ids that reached on_start, in the order they did

    # ---------- registry ----------

    def register(self, cls):
        """Add a module class. Registration ORDER MATTERS: it is the tiebreak in
        the dependency sort, which will become the z-order of map layers once
        surface=LAYER exists."""
        if not cls.id:
            raise ModuleError(f"{cls.__name__} has no id")
        if cls.id in self._classes:
            raise ModuleError(
                f"duplicate module id {cls.id!r}: "
                f"{self._classes[cls.id].__name__} and {cls.__name__}"
            )
        self._classes[cls.id] = cls
        return cls

    def get(self, module_id: str):
        return self._mods[module_id]

    def _resolve_order(self) -> list:
        """Depth-first topological sort. Ties break toward registration order,
        so the sequence of register() calls in main.py is the build order."""
        order, state = [], {}

        def visit(mid, path):
            if state.get(mid) == "done":
                return
            if state.get(mid) == "visiting":
                raise ModuleError("dependency cycle: " + " -> ".join(path + [mid]))
            state[mid] = "visiting"
            for dep in self._classes[mid].requires:
                if dep not in self._classes:
                    raise ModuleError(
                        f"module {mid!r} requires {dep!r}, which is not registered. "
                        f"Registered: {sorted(self._classes)}"
                    )
                visit(dep, path + [mid])
            state[mid] = "done"
            order.append(mid)

        for mid in self._classes:   # dicts preserve insertion order
            visit(mid, [])
        return order

    # ---------- lifecycle ----------

    def start(self):
        """Five passes, each over every module, in dependency order.

        They are separate passes rather than one loop because two different
        guarantees are needed and a single pass can only give one of them:
        every module must exist before any dependency is injected, and every
        module must be injected before any module builds a widget. That second
        one is what makes a missing dependency fail while the screen is still
        empty, instead of half way through drawing it."""
        self._order = self._resolve_order()

        roots = [m for m in self._order if self._classes[m].surface == mod.ROOT]
        if len(roots) > 1:
            raise ModuleError(f"more than one surface={mod.ROOT!r} module: {roots}")

        for mid in self._order:                                     # 1. construct
            self._mods[mid] = self._classes[mid](self)

        for mid in self._order:                                     # 2. inject
            deps = {d: self._mods[d] for d in self._classes[mid].requires}
            self._mods[mid].on_deps(deps)

        for mid in self._order:                                     # 3. build
            m = self._mods[mid]
            m.build(self._make_surface(m))

        for mid in self._order:                                     # 4. start
            self._mods[mid].on_start()
            self._started.append(mid)

    def stop(self):
        """Reverse of the order modules actually started in, so nothing is torn
        down while something that depends on it is still live."""
        for mid in reversed(self._started):
            try:
                self._mods[mid].on_stop()
            except Exception:
                traceback.print_exc()   # one bad teardown must not block the rest
        self._started.clear()

    # ---------- event bus ----------

    def subscribe(self, event: str, handler):
        """Returns a closure that unsubscribes, for a module that needs to stop
        listening in on_stop. Most don't — teardown ends the process."""
        self._subs.setdefault(event, []).append(handler)

        def unsubscribe():
            handlers = self._subs.get(event)
            if handlers and handler in handlers:
                handlers.remove(handler)
        return unsubscribe

    def publish(self, event: str, **payload):
        """Synchronous, in subscription order, main thread only.

        Handlers are isolated: one that raises is reported and skipped, because
        a module misbehaving should not take down the modules after it in the
        list — let alone the app."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                f"publish({event!r}) called off the main thread. Tk is not thread "
                f"safe; use publish_threadsafe() instead."
            )
        for handler in list(self._subs.get(event, ())):  # copy: a handler may subscribe
            try:
                handler(**payload)
            except Exception:
                traceback.print_exc()

    def publish_threadsafe(self, event: str, **payload):
        """For worker threads: hops onto the Tk main loop and publishes there."""
        self.root.after(0, lambda: self.publish(event, **payload))

    # ---------- surfaces ----------

    def _make_surface(self, m):
        kind = m.surface
        if kind == mod.NONE:
            return None
        if kind == mod.ROOT:
            return self.root
        raise ModuleError(
            f"module {m.id!r}: surface {kind!r} has no implementation yet"
        )
