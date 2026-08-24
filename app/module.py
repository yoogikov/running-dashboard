"""What a module is.

A module is one feature of the dashboard, packaged so that main.py can add it
with a single register() line. It says what it needs (`requires`) and where it
wants to appear (`surface`); the host resolves the first and creates the second.

See host.py for who calls these methods and in what order.
"""

# Surface kinds. A module DECLARES one; the host CREATES it and hands over a
# bare parent to build into. A module never makes its own Toplevel and never
# places itself on the root — so a panel can become a tab later without the
# module changing.
NONE = "none"    # headless, or provides its own surface.  build(None)
ROOT = "root"    # owns the whole window.                  build(<the Tk root>)
LAYER = "layer"  # draws on the map canvas.                build(<map canvas>)
PANEL = "panel"  # floating panel over the map.            build(<content frame>)
TAB = "tab"      # item on the top bar.                    build(<pane frame> or None)


class Module:
    id: str = ""                      # unique; what `requires` and host.get() use
    requires: tuple = ()              # module ids to inject, by id
    surface: str = NONE
    title: str = ""                   # panel chrome / tab label; defaults to id

    def __init__(self, host):
        self.host = host

    # ---------- lifecycle, in the order the host calls it ----------

    def on_deps(self, deps: dict) -> None:
        """The modules named in `requires`, keyed by id. Store what you need.

        Do NOT call into them yet — their build() has not run, so their widgets
        do not exist."""

    def build(self, parent) -> None:
        """Create your widgets under `parent`, whose kind is fixed by `surface`
        (see the constants above). Runs in dependency order, so a dependency's
        widgets already exist by the time yours are built."""

    def on_start(self) -> None:
        """Everything, everywhere, is built. First point at which calling into a
        dependency is legal. Subscribe to events, load data, start timers."""

    def on_stop(self) -> None:
        """Cancel your `after` jobs and stop your threads. Do NOT destroy widgets
        the host created for you — the host owns those."""
