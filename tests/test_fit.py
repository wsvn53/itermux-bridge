"""Pane sizing: iTerm2 panes follow the attached client, like a tmux window.

The regression this guards: a client narrower than the pane on the Mac saw
every row cut off at its right edge. Drives fit.py against fakes — no iTerm2.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.fit import SizeFitter, target_sizes

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and bool(cond)


class Grid:
    def __init__(self, w, h):
        self.width, self.height = w, h


class Sess:
    def __init__(self, sid, w=100, h=40):
        self.session_id = sid
        self.grid_size = Grid(w, h)


class Split:
    def __init__(self, vertical, children):
        self.vertical, self.children = vertical, children


print("=== target_sizes ===")

sizes = target_sizes(False, None, "p", 90, 30)
check("pane mode: the pane takes the whole client", sizes == {"p": (90, 30)})

a, b = Sess("a"), Sess("b")
sizes = target_sizes(True, Split(True, [a, b]), "a", 101, 30)
check("window mode: every pane gets a size", set(sizes) == {"a", "b"})
check("...side by side they fill the client less one divider column",
      sizes["a"][0] + sizes["b"][0] == 100, f"({sizes})")
check("...one row shorter than the region, for the pane's title bar",
      sizes["a"][1] == 29 and sizes["b"][1] == 29, f"({sizes})")


print("\n=== SizeFitter ===")


class TTY:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows

    def size(self):
        return self.cols, self.rows


class Peer:
    def __init__(self, cols=80, rows=24, tty="/dev/ttys099"):
        self.tty = TTY(cols, rows)
        self.ttyname = tty
        self.window_mode = False
        self.fit_key = self.fit_task = self.fit_window = None


class Tab:
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions
        self.all_sessions = list(sessions)

    @property
    def root(self):
        # Like iTerm2: a zoom collapses the split tree to the visible pane.
        return Split(True, self.sessions)


class Window:
    def __init__(self, window_id, tabs):
        self.window_id, self.tabs = window_id, tabs


class API:
    """Records what the fitter asked iTerm2 to do."""

    def __init__(self, windows, ttys=None, fits=True, fullscreen=False):
        self.windows_ = windows
        self.ttys = ttys or {}           # session_id -> tty
        self.fits = fits
        self.fullscreen = fullscreen
        self.layouts = []
        self.grids, self.restored = [], []
        self.calls = []                  # order of grid/frame changes

    def tab_of(self, sid):
        return next((t for w in self.windows_ for t in w.tabs
                     if any(s.session_id == sid for s in t.sessions)), None)

    def window_of(self, tab):
        return next((w for w in self.windows_ if tab in w.tabs), None)

    async def frame(self, window):
        return f"frame-of-{window.window_id}"

    async def set_frame(self, wid, frame):
        self.restored.append((wid, frame))
        self.calls.append("frame")

    async def set_grid_sizes(self, sizes):
        # Yield like the real RPC does: a cancellation (e.g. a fit cancelling
        # itself) is only delivered at a real suspension point.
        await asyncio.sleep(0)
        self.grids.append(dict(sizes))
        self.calls.append(("grid", dict(sizes)))
        return self.fits

    async def variable(self, s, name, default=None):
        return self.ttys.get(s.session_id, default)

    def tab_by_id(self, tab_id):
        return next((t for w in self.windows_ for t in w.tabs
                     if t.tab_id == tab_id), None)

    def is_zoomed(self, tab):
        return bool(getattr(tab, "zoomed", False))

    async def is_fullscreen(self, window):
        return self.fullscreen

    async def set_layout(self, tab_id, sizes):
        await asyncio.sleep(0)
        self.layouts.append((tab_id, dict(sizes)))
        self.calls.append(("layout", dict(sizes)))


class Backend(SizeFitter):
    def __init__(self, api):
        self.api = api
        self.loop = asyncio.new_event_loop()

    def _spawn(self, coro, what):
        return self.loop.create_task(coro)

    def settle(self):
        self.loop.run_until_complete(asyncio.sleep(0.01))


def rig(**kw):
    pane = Sess("p")
    win = Window("w1", [Tab("t1", [pane])])
    return Backend(API([win], **kw)), pane


be, pane = rig()
peer = Peer(80, 24)
be._maybe_fit(peer, pane); be.settle()
check("attach fits the pane to the client", be.api.grids == [{"p": (80, 24)}],
      f"({be.api.grids})")

be._maybe_fit(peer, pane); be.settle()
check("a steady view does not refit every poll", len(be.api.grids) == 1)

peer.tty.cols = 60
be._maybe_fit(peer, pane); be.settle()
check("client resize refits", be.api.grids[-1] == {"p": (60, 24)},
      f"({be.api.grids[-1]})")

be._release_fit(peer); be.settle()
# One restore sets the frame twice (before and after the layout).
check("last client leaving restores the Mac window's original frame",
      be.api.restored == [("w1", "frame-of-w1")] * 2, f"({be.api.restored})")
# The frame alone rescales a split proportionally and leaves dividers moved.
# Frame, then the whole layout, then the frame again: measured on iTerm2,
# ending on the layout call leaves the window a row short.
check("...frame, original layout, frame -- in that order",
      be.api.calls[-3:] == ["frame", ("layout", {"p": (100, 40)}), "frame"],
      f"({be.api.calls[-3:]})")

# Two clients on one window: the first to leave must NOT restore it under the
# one still attached; that one refits to its own size instead.
be, pane = rig()
p1, p2 = Peer(80, 24, "/dev/ttys001"), Peer(120, 40, "/dev/ttys002")
be._maybe_fit(p1, pane); be.settle()
be._maybe_fit(p2, pane); be.settle()
be._release_fit(p2); be.settle()
check("one of two clients leaving keeps the window fitted",
      be.api.restored == [])
be._maybe_fit(p1, pane); be.settle()
check("...and the remaining client refits to its own size",
      be.api.grids[-1] == {"p": (80, 24)}, f"({be.api.grids[-1]})")
be._release_fit(p1); be.settle()
check("...restored once both are gone", len(be.api.restored) == 2)

# A client running inside the same iTerm2 window would be resized by the fit,
# report a new size, and refit forever. Leave that window alone.
be, pane = rig(ttys={"p": "/dev/ttys050"})
peer = Peer(80, 24, "/dev/ttys050")
be._maybe_fit(peer, pane); be.settle()
check("client inside the same window: no fit", be.api.grids == [])
be._release_fit(peer); be.settle()
check("...and nothing to restore", be.api.restored == [])

# Fullscreen windows refuse set_grid_size: fall back to cropping, and still
# hand the (unchanged) frame back cleanly.
be, pane = rig(fits=False)
peer = Peer(80, 24)
be._maybe_fit(peer, pane); be.settle()
check("unfittable window doesn't raise", len(be.api.grids) == 1)

# Moving to another iTerm2 window (attach elsewhere, cross-window select)
# gives the first one back.
a, b = Sess("a"), Sess("b")
api = API([Window("w1", [Tab("t1", [a])]), Window("w2", [Tab("t2", [b])])])
be = Backend(api)
peer = Peer(80, 24)
be._maybe_fit(peer, a); be.settle()
be._maybe_fit(peer, b); be.settle()
check("switching windows restores the one we left",
      api.restored and {w for w, _f in api.restored} == {"w1"},
      f"({api.restored})")
# The old window's restore and the new window's fit run concurrently and touch
# different windows, so check the fit happened, not that it came last.
check("...and fits the new one", {"b": (80, 24)} in api.grids, f"({api.grids})")

print("\n=== window mode: only a single-pane view is fitted ===")

# The regression: every zoom/unzoom cycle refitted the whole split, pane by
# pane, from its CURRENT proportions -- one cell of drift per cycle, and
# nested splits never converged at all. Now a multi-pane view is left alone,
# a zoom fits the one visible pane, and unzoom restores the layout captured
# before the zoom.
a, b, c = Sess("a", 60, 20), Sess("b", 120, 20), Sess("c", 180, 19)
tab = Tab("t1", [a, b, c])
api = API([Window("w1", [tab])])
be = Backend(api)
peer = Peer(100, 30)
peer.window_mode = True
be._maybe_fit(peer, a); be.settle()
check("whole split tab in view: panes are not resized", api.grids == [])
original = {"a": (60, 20), "b": (120, 20), "c": (180, 19)}

restores = []
for cycle in range(5):
    tab.sessions, tab.zoomed = [a], True          # Ctrl-B z: zoomed on a
    be._maybe_fit(peer, a); be.settle()
    fitted = api.grids[-1] if api.grids else None
    a.grid_size = Grid(100, 29)                    # the fit took effect
    tab.sessions, tab.zoomed = [a, b, c], False    # Ctrl-B z again
    be._maybe_fit(peer, a); be.settle()
    restores.append(api.layouts[-1][1] if api.layouts else None)
    a.grid_size = Grid(60, 20)                     # the restore took effect
check("zoom fits the one visible pane (title row off the height)",
      fitted == {"a": (100, 29)}, f"({fitted})")
check("unzoom restores the layout from before the zoom",
      restores[0] == original, f"({restores[0]})")
check("five zoom cycles restore the identical layout every time (no drift)",
      restores == [original] * 5, f"({restores})")

# A fullscreen window can't be resized; holding it would only "restore" (and
# set the frame of) a window we never changed.
be, pane = rig(fullscreen=True)
peer = Peer(80, 24)
be._maybe_fit(peer, pane); be.settle()
be._release_fit(peer); be.settle()
check("fullscreen window: no fit, nothing restored",
      be.api.grids == [] and be.api.restored == [], f"({be.api.calls})")


print("\n=== a remembered layout goes stale ===")

import time as _time  # noqa: E402

# What happened live: the layout was recorded during an earlier visit, the
# user then re-arranged the tab while nobody was attached, and later attached
# to it while it was ALREADY zoomed. Unzooming "restored" the old record,
# undoing the user's re-arrangement.
a, b = Sess("a", 60, 20), Sess("b", 120, 20)
tab = Tab("t1", [a, b])
api = API([Window("w1", [tab])])
be = Backend(api)
be._layouts()["t1"] = (_time.monotonic() - 3600, {"a": (10, 5), "b": (170, 35)})
tab.sessions, tab.zoomed = [a], True           # attach lands on a zoomed tab
peer = Peer(100, 30); peer.window_mode = True
be._maybe_fit(peer, a); be.settle()
tab.sessions, tab.zoomed = [a, b], False       # unzoom
be._maybe_fit(peer, a); be.settle()
check("stale record is never restored", api.layouts == [], f"({api.layouts})")
check("...only the window frame is put back", api.restored != [])

# The normal case still restores exactly: zoom while the client watches.
a, b = Sess("a", 60, 20), Sess("b", 120, 20)
tab = Tab("t1", [a, b])
api = API([Window("w1", [tab])])
be = Backend(api)
peer = Peer(100, 30); peer.window_mode = True
be._maybe_fit(peer, a); be.settle()           # watching it unzoomed: recorded
tab.sessions, tab.zoomed = [a], True
be._maybe_fit(peer, a); be.settle()
tab.sessions, tab.zoomed = [a, b], False
be._maybe_fit(peer, a); be.settle()
check("zoom while watching: the layout from just before is restored",
      api.layouts and api.layouts[-1][1] == {"a": (60, 20), "b": (120, 20)},
      f"({api.layouts})")

# A fit that can't be reached (a pane deep in a split can't grow the window
# past the screen) has still moved things part-way: put it back right away,
# not only when the client eventually leaves.
be, pane = rig(fits=False)
peer = Peer(80, 24)
be._maybe_fit(peer, pane); be.settle()
check("failed fit: layout restored immediately, client still attached",
      be.api.restored != [] and peer.fit_window is None,
      f"({be.api.restored}, fit_window={peer.fit_window})")


print("\n=== a failed fit while zoomed ===")

# The live bug: zoom in, the fit can't reach the client's size, the layout is
# "restored" -- onto the still-zoomed tab, shrinking its one visible pane to
# its split size. Only the window frame may be put back while zoomed.
a, b = Sess("a", 98, 64), Sess("b", 99, 64)
tab = Tab("t1", [a, b])
api = API([Window("w1", [tab])], fits=False)
be = Backend(api)
peer = Peer(400, 120); peer.window_mode = True
be._maybe_fit(peer, a); be.settle()            # watching the split: recorded
tab.sessions, tab.zoomed = [a], True           # Ctrl-B z
a.grid_size = Grid(200, 64)
be._maybe_fit(peer, a); be.settle()            # fit fails -> restore
check("failed fit while zoomed: no split layout forced onto the zoomed tab",
      api.layouts == [], f"({api.layouts})")
check("...the window frame is still put back", api.restored != [])


print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
