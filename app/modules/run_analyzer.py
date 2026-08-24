"""Run Analyzer: the tab that shows one run at a time in detail.

Deliberately narrow. This module owns the tab pane's layout (collapsible
sidebar + scrollable detail column) and run-selection state, and exposes a
small registration API for satellite modules to plug into — run_search and
run_list build into the sidebar slots, run_header/run_actions/run_splits/etc.
register detail sections. This file knows nothing about splits, elevation,
laps, or any other analysis; see whichever module declares
requires=("run_analyzer",) for those.

The registration API is a plain method call, not the event bus, because every
satellite module has a hard dependency on this one — see host.py's own
"two mechanisms" note on when each is the right tool.
"""
import tkinter as tk

import analytics_cache
import db
import module
from ui import cards
from ui.scrollable import ScrollableFrame

SIDEBAR_WIDTH = 300


class RunAnalyzerModule(module.Module):
    id = "run_analyzer"
    surface = module.TAB
    title = "Analyze"
    tab_pane = True
    tab_height = 750

    def build(self, parent):
        self._sections = []          # ordered list of build_fn(frame, run, analytics)
        self.selected_run_id = None
        self._sidebar_open = True

        # ---- collapse/expand toggle: always visible, even with the sidebar
        # hidden, so it can always be reopened. ----
        self._toggle = tk.Label(
            parent, text="»", bg=cards.CARD_BG, fg=cards.CARD_MUTED,
            font=("TkDefaultFont", 11), width=2, cursor="hand2",
        )
        self._toggle.pack(side="left", fill="y")
        self._toggle.bind("<Button-1>", lambda e: self.toggle_sidebar())
        self._toggle.bind("<Enter>", lambda e: self._toggle.configure(fg=cards.ACCENT_HOVER))
        self._toggle.bind("<Leave>", lambda e: self._toggle.configure(fg=cards.CARD_MUTED))

        # ---- sidebar: search_slot (run_search builds into this) above
        # list_slot (run_list builds into this) ----
        self._sidebar = tk.Frame(parent, bg=cards.CARD_BG, width=SIDEBAR_WIDTH)
        self._sidebar.pack_propagate(False)
        self._sidebar.pack(side="left", fill="y")

        self.search_slot = tk.Frame(self._sidebar, bg=cards.CARD_BG)
        self.search_slot.pack(side="top", fill="x")

        tk.Frame(self._sidebar, bg=cards.CARD_BORDER, height=1).pack(side="top", fill="x")

        list_scroll = ScrollableFrame(self._sidebar, bg=cards.CARD_BG)
        list_scroll.pack(side="top", fill="both", expand=True)
        self.list_slot = list_scroll.inner

        # A fixed anchor between sidebar and detail column, so expand_sidebar()
        # has something stable to re-pack the sidebar in front of.
        self._detail_border = tk.Frame(parent, bg=cards.CARD_BORDER, width=1)
        self._detail_border.pack(side="left", fill="y")

        # ---- detail column: torn down and rebuilt on every select_run() ----
        detail_scroll = ScrollableFrame(parent, bg=cards.CARD_BG)
        detail_scroll.pack(side="left", fill="both", expand=True)
        self._detail_column = detail_scroll.inner

        self._render_empty_state()

    def on_start(self):
        self.host.root.bind("<Escape>", self._on_escape)

    # ---------- API for satellite modules ----------

    def register_detail_section(self, build_fn):
        """build_fn(frame, run, analytics) -> None. Appended in registration
        order, which is what controls vertical position in the detail
        column — same convention main.py's register() order already uses for
        map layer z-order, reused here for section order instead."""
        self._sections.append(build_fn)

    def get_selected_run_id(self):
        return self.selected_run_id

    def select_run(self, run_id):
        """Tears down and rebuilds the detail column: fetches the run and its
        cached analytics ONCE, then calls every registered section with the
        same pair — no satellite re-fetches or re-computes what another one
        already needed."""
        self.selected_run_id = run_id
        for child in self._detail_column.winfo_children():
            child.destroy()

        run = db.get_run(run_id) if run_id is not None else None
        if run is None:
            self.selected_run_id = None
            self._render_empty_state()
            return

        analytics = analytics_cache.get_or_compute(run_id)
        for build_fn in self._sections:
            build_fn(self._detail_column, run, analytics)

    def expand_sidebar(self):
        if self._sidebar_open:
            return
        self._sidebar.pack(side="left", fill="y", before=self._detail_border)
        self._sidebar_open = True

    def collapse_sidebar(self):
        if not self._sidebar_open:
            return
        self._sidebar.pack_forget()
        self._sidebar_open = False

    def toggle_sidebar(self):
        self.collapse_sidebar() if self._sidebar_open else self.expand_sidebar()

    def is_sidebar_open(self) -> bool:
        return self._sidebar_open

    # ---------- internal ----------

    def _render_empty_state(self):
        tk.Label(
            self._detail_column, text="Select a run from the list to see its details.",
            bg=cards.CARD_BG, fg=cards.CARD_MUTED, font=("TkDefaultFont", 9),
        ).pack(padx=16, pady=16, anchor="w")

    def _on_escape(self, event):
        if self.host.is_visible(self.id) and self._sidebar_open:
            self.collapse_sidebar()
