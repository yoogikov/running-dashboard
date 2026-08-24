"""A vertically-scrolling frame. tkinter has no such widget built in — the usual
recipe is a Canvas holding one child Frame, with a Scrollbar driving the canvas's
view — used wherever content can run longer than the space for it (the run list
sidebar, the run analyzer's detail column).
"""
import tkinter as tk

from ui import cards


class ScrollableFrame(tk.Frame):
    """`.inner` is a plain tk.Frame — pack/grid children into that, not into
    self. Everything else here is the canvas/scrollbar machinery that makes
    `.inner` scroll when its content overflows the visible height.
    """

    def __init__(self, parent, bg=cards.CARD_BG, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)

        self._canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self._scrollbar = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        self.inner = tk.Frame(self._canvas, bg=bg)
        # window, not pack/place: the canvas is what actually scrolls, so its
        # content has to be a canvas item, not a normal packed child.
        self._inner_window = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Only scroll this widget's content while the pointer is actually
        # over it — bound/unbound on enter/leave rather than left as a global
        # binding, so hovering this frame doesn't steal wheel events from
        # whatever else is on screen (e.g. the map, once this sits over it).
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

    def _on_inner_configure(self, event):
        # inner's content changed size — recompute how far the canvas can scroll.
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # keep inner exactly as wide as the visible canvas, so its children
        # can fill=x without guessing a width.
        self._canvas.itemconfigure(self._inner_window, width=event.width)

    def _bind_mousewheel(self, event):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)   # Windows/macOS
        self._canvas.bind_all("<Button-4>", self._on_mousewheel)     # X11 scroll up
        self._canvas.bind_all("<Button-5>", self._on_mousewheel)     # X11 scroll down

    def _unbind_mousewheel(self, event):
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
