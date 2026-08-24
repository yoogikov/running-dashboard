"""Auto-hiding top bar: slides down when the mouse nears the top edge of the
window, slides back up once the mouse moves away. Sits above the map as a
plain `place()`-positioned overlay — no content yet, that gets added next.
"""
import tkinter as tk

from ui import cards

HEIGHT = 56
TRIGGER_ZONE = 6     # px from the very top of the window that triggers a slide-in
HIDE_MARGIN = 12     # extra px below the bar's bottom edge before it counts as "left"
STEP_MS = 12
STEP_PX = 8


class TopBar:
    def __init__(self, root: tk.Tk, map_canvas: tk.Widget):
        self.root = root
        self.visible = False
        self.target_y = -HEIGHT
        self._animating = False

        self._step_job = None

        self.frame = tk.Frame(root, bg=cards.CARD_BG, height=HEIGHT)
        self.frame.place(x=0, y=-HEIGHT, relwidth=1, height=HEIGHT)

        # thin bottom edge so the bar reads as a distinct layer over the map,
        # not a hard accent-colored box (same reasoning as the card borders)
        border = tk.Frame(self.frame, bg=cards.CARD_BORDER, height=1)
        border.pack(side="bottom", fill="x")

        # Tabs live here. Packed AFTER the border, so pack gives the border its
        # strip first and this takes what's left above it. Created even with no
        # tabs: an empty frame with no children requests 0x0, so the bar renders
        # exactly as it did before tabs existed.
        self._tabs = tk.Frame(self.frame, bg=cards.CARD_BG)
        self._tabs.pack(side="left", fill="y", padx=8)

        # The map's canvas fills the whole window, so it — not the root — is
        # what actually receives mouse motion. add="+" so we don't clobber
        # tkintermapview's own drag/zoom bindings on the same widget.
        map_canvas.bind("<Motion>", self._on_motion, add="+")

    def add_tab(self, label: str, on_click=None) -> "Tab":
        """Adds a clickable item to the bar. The host calls this for modules
        that declare surface="tab"; it owns the returned handle and whatever
        content pane goes with it."""
        return Tab(self._tabs, label, on_click)

    def attach_motion_source(self, widget):
        """Forwards a widget's mouse motion to the bar's trigger.

        Anything placed over the map hides the map canvas from the pointer, and
        the canvas binding below is what makes the bar slide down — so without
        this the bar would go dead wherever a panel happened to sit."""
        widget.bind("<Motion>", self._on_motion, add="+")

    def _on_motion(self, event):
        # y relative to the WINDOW, not to whichever widget received the event,
        # so panels and tab panes can forward their motion here and still be
        # measured against the same top edge. Identical to event.y for the map
        # canvas, which sits at the window origin.
        y = event.y_root - self.root.winfo_rooty()
        if y <= TRIGGER_ZONE:
            self._show()
        elif y > HEIGHT + HIDE_MARGIN:
            self._hide()

    def _show(self):
        if self.target_y == 0:
            return
        self.target_y = 0
        self.visible = True
        self._animate()

    def _hide(self):
        if self.target_y == -HEIGHT:
            return
        self.target_y = -HEIGHT
        self.visible = False
        self._animate()

    def _animate(self):
        if self._animating:
            return
        self._animating = True
        self._step()

    def _step(self):
        current = self.frame.place_info().get("y")
        current = int(current) if current is not None else -HEIGHT

        diff = self.target_y - current
        if abs(diff) <= STEP_PX:
            new_y = self.target_y
        else:
            new_y = current + (STEP_PX if diff > 0 else -STEP_PX)

        self.frame.place_configure(y=new_y)

        if new_y != self.target_y:
            self._step_job = self.root.after(STEP_MS, self._step)
        else:
            self._step_job = None
            self._animating = False

    def destroy(self):
        """Stops the slide animation before the interpreter goes away — a
        pending after() firing against a destroyed root is what produces Tk's
        `invalid command name` traceback on exit."""
        if self._step_job is not None:
            self.root.after_cancel(self._step_job)
            self._step_job = None
        self._animating = False


class Tab:
    """One item on the bar. The host holds these; modules never touch them."""

    def __init__(self, parent, label: str, on_click=None):
        self.active = False
        self.label = tk.Label(parent, text=label, bg=cards.CARD_BG, fg=cards.CARD_FG,
                              font=("TkDefaultFont", 9), padx=12, cursor="hand2")
        self.label.pack(side="left")
        self.label.bind("<Enter>", lambda e: self.label.configure(fg=cards.ACCENT_HOVER))
        self.label.bind("<Leave>", lambda e: self.label.configure(fg=self._rest_fg()))
        if on_click is not None:
            self.label.bind("<Button-1>", lambda e: on_click())

    def _rest_fg(self):
        return cards.ACCENT if self.active else cards.CARD_FG

    def set_active(self, active: bool):
        self.active = active
        self.label.configure(fg=self._rest_fg())

    def destroy(self):
        self.label.destroy()
