# TODO

Backlog of known issues/ideas, not scheduled — so they don't get lost between
sessions.

- **Smoother map scrolling.** Panning/zooming the heatmap map (`modules/background.py`,
  `tkintermapview` + `heat_overlay.py`'s overlay redraw) doesn't feel smooth. Needs
  profiling to find out whether it's tile fetch/draw latency, the overlay's
  redraw/shift logic, or something else.
