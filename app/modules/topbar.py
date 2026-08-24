"""The top bar: a fixed-height strip across the top of the window, where
tab/nav buttons will eventually live. Empty for now — no buttons yet.

Auto-hides: it lives just above the window (y=-HEIGHT) until the mouse
touches the top edge, then slides down; it slides back up once the mouse
moves below it. Uses place() rather than pack() so it can be positioned
by a y-offset independent of the rest of the layout, and floats over
whatever root ends up holding instead of taking up permanent space.

surface=NONE because TAB isn't implemented (see module.py); it builds
itself directly onto the root module's frame instead of waiting for a
surface kind that doesn't exist yet.
"""
import tkinter as tk

import module as mod

BG = "#1f1f1f"      # slightly darker than the root grey, so the strip reads
                    # as a distinct bar rather than blending into the window
HEIGHT = 40
EDGE_TRIGGER = 3    # px from the window's top edge that counts as "at the edge"
HIDE_MARGIN = HEIGHT + 4  # give a little slack below the bar before hiding,
                          # so leaving it doesn't flicker right at the boundary
SLIDE_STEP = 6      # px per animation tick
SLIDE_INTERVAL_MS = 12


class TopBar(mod.Module):
    id = "topbar"
    requires = ("root",)
    surface = mod.NONE

    def on_deps(self, deps: dict) -> None:
        self.root_mod = deps["root"]

    def build(self, parent) -> None:
        self.frame = tk.Frame(self.root_mod.frame, bg=BG, height=HEIGHT)
        self.frame.place(x=0, y=-HEIGHT, relwidth=1)
        self._y = -HEIGHT       # current position
        self._target = -HEIGHT  # where the animation is heading
        self._anim_job = None

    def on_start(self) -> None:
        # bind_all so motion anywhere in the window is seen, not just over
        # whatever widget happens to be under the pointer.
        self.host.root.bind_all("<Motion>", self._on_motion, add="+")

    def on_stop(self) -> None:
        if self._anim_job is not None:
            self.host.root.after_cancel(self._anim_job)
            self._anim_job = None

    def _on_motion(self, event) -> None:
        y = event.y_root - self.host.root.winfo_rooty()
        if y <= EDGE_TRIGGER:
            self._set_target(0)
        elif y > HIDE_MARGIN:
            self._set_target(-HEIGHT)

    def _set_target(self, target: int) -> None:
        if target == self._target:
            return
        self._target = target
        if self._anim_job is None:
            self._animate()

    def _animate(self) -> None:
        if self._y < self._target:
            self._y = min(self._y + SLIDE_STEP, self._target)
        else:
            self._y = max(self._y - SLIDE_STEP, self._target)
        self.frame.place_configure(y=self._y)
        if self._y == self._target:
            self._anim_job = None
        else:
            self._anim_job = self.host.root.after(SLIDE_INTERVAL_MS, self._animate)
