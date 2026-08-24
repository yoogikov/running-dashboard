"""The heatmap tab and its view. Clicking the tab shows the heatmap
view — for now just an empty background; the actual heatmap comes
later.

surface=NONE because TAB isn't implemented (see module.py); the tab
packs into the topbar module's frame the same way topbar builds onto
root's, and the view is a full-window frame over root's, shown on
click and kept below the (auto-hiding) top bar.
"""
import tkinter as tk

import module as mod

TAB_BG = "#2a2a2a"    # a touch lighter than the bar, so the tab reads as
                      # a distinct control sitting on it
TAB_FG = "#cfcfcf"
TAB_PADX = 14
TAB_PADY = 8

VIEW_BG = "#12232e"   # dark navy — placeholder for the heatmap view, deliberately
                      # distinct from root's grey so it's obvious the view opened


class HeatmapTab(mod.Module):
    id = "heatmap"
    requires = ("topbar", "root")
    surface = mod.NONE

    def on_deps(self, deps: dict) -> None:
        self.topbar = deps["topbar"]
        self.root_mod = deps["root"]

    def build(self, parent) -> None:
        self.label = tk.Label(
            self.topbar.frame, text="Heatmap", bg=TAB_BG, fg=TAB_FG,
            padx=TAB_PADX, pady=TAB_PADY, cursor="hand2",
        )
        self.label.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), pady=4)
        self.label.bind("<Button-1>", self._on_click)

        # Not placed yet — stays out of the layout until the tab is clicked.
        self.view = tk.Frame(self.root_mod.frame, bg=VIEW_BG)

    def _on_click(self, _event) -> None:
        self.view.place(x=0, y=0, relwidth=1, relheight=1)
        self.topbar.frame.lift()  # the bar stays on top of whatever view is open
