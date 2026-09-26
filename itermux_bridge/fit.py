"""Size iTerm2 panes to the attached client, the way tmux sizes a window.

Without this, a client narrower than the pane on the Mac (a phone, a laptop
next to a big external display) sees every row cut off at its right edge: the
program in the pane is drawing for the Mac's width, and cropping is all the
renderer can do. tmux never shows that because it resizes the window to its
client and the program redraws. So do the same: while a client is attached,
iTerm2's panes take the client's size; when the last client leaves, the Mac
window gets its original frame back.

`target_sizes` is the pure part. `SizeFitter` is mixed into the backend and
uses `self.api`, `self._spawn` from there.
"""

import logging
import time
from typing import Dict, NamedTuple, Tuple

from . import layout

log = logging.getLogger(__name__)

#: How old a remembered layout may be and still be restored. The pump keeps it
#: current only while a client is looking at the tab unzoomed; a zoom then
#: triggers the fit within a poll or two. Anything older was taken during some
#: earlier visit, and the layout may have been changed since by someone at the
#: Mac: restoring it put a tab back to a layout the user had already replaced.
SNAPSHOT_MAX_AGE = 2.0


def target_sizes(window_mode: bool, root, pane_id: str, cols: int,
                 rows: int) -> Dict[str, Tuple[int, int]]:
    """{session_id: (width, height)} each pane needs to be shown whole.

    Pane mode draws one pane over the whole client. Window mode gives each pane
    its rectangle in the composite, minus the title bar drawn in its top row.
    """
    if not window_mode:
        return {pane_id: (cols, rows)}
    return {r.session_id: (max(2, r.width), max(1, r.height - 1))
            for r in layout.regions(root, cols, rows)}


class _Held(NamedTuple):
    """An iTerm2 window we resized, what to put back, and who is fitting it.

    The frame alone isn't enough: fitting one pane of a split moves its
    divider, and restoring the frame only rescales the split proportionally
    (measured: a 46|93 split came back 69|69). So each fitted tab's pane sizes
    are kept too.
    """
    frame: object
    peers: set
    grids: dict         # tab_id -> {session_id: (cols, rows)} before we fit


class SizeFitter:
    """Keeps the panes a client is looking at sized to that client."""

    def _held_windows(self) -> Dict[str, _Held]:
        if not hasattr(self, "_held"):
            self._held = {}
        return self._held

    def _layouts(self) -> Dict[str, tuple]:
        """tab_id -> (taken_at, {session_id: (cols, rows)}): last seen layout."""
        if not hasattr(self, "_layout_snap"):
            self._layout_snap = {}
        return self._layout_snap

    def _maybe_fit(self, peer, pane) -> None:
        """Refit when what the client shows changes. Called every poll.

        Only a view of ONE pane is fitted: pane mode, or a zoomed pane (or a
        tab with a single pane) in window mode. Fitting a whole split tab meant
        resizing every pane one at a time, which with nested splits never
        converged — each attempt left the dividers part-moved, and every
        zoom/unzoom moved them further (measured: one cell per cycle, some
        panes shrinking to 4 rows). A multi-pane view is left alone and drawn
        scaled, as before; switching to one gives the window back.

        No RPC here: the key is built from the tab object iTerm2 keeps current
        and the client's size, so a steady view costs nothing.
        """
        tab = self.api.tab_of(pane.session_id)
        if tab is None or peer.tty is None:
            return
        window = self.api.window_of(tab)
        # Remember the layout while it is still iTerm2's own: once a pane is
        # zoomed, the hidden panes report no size at all, so this is the only
        # way to put the split back exactly afterwards.
        if (window is not None and not self.api.is_zoomed(tab)
                and window.window_id not in self._held_windows()
                and window.window_id not in self._restoring()):
            self._layouts()[tab.tab_id] = (time.monotonic(), {
                s.session_id: (s.grid_size.width, s.grid_size.height)
                for s in tab.sessions})

        cols, rows = peer.tty.size()
        # In window mode moving between panes of one tab (Ctrl-B o) changes the
        # pane but not the layout, so key on the tab there.
        target = tab.tab_id if peer.window_mode else pane.session_id
        key = (target, peer.window_mode,
               tuple(s.session_id for s in tab.sessions), cols, rows)
        if key == peer.fit_key:
            return
        peer.fit_key = key
        # Only the latest fit matters — a client being dragged to a new size
        # produces a burst of them.
        if peer.fit_task is not None:
            peer.fit_task.cancel()
            peer.fit_task = None
        if peer.window_mode and len(tab.sessions) > 1:
            self._release_window(peer)      # whole split in view: hands off
            return
        peer.fit_task = self._spawn(
            self._fit(peer, pane, tab, cols, rows), "fit")

    async def _fit(self, peer, pane, tab, cols: int, rows: int) -> None:
        window = self.api.window_of(tab)
        if window is None:
            return
        # A client running in a pane of this same window would be resized by
        # this too, report its new size, and trigger another fit — forever.
        if await self._client_inside(peer, window):
            return

        # iTerm2 won't resize a fullscreen window; don't hold (and later
        # "restore") one we can't have changed.
        if await self.api.is_fullscreen(window):
            return

        held = self._held_windows().get(window.window_id)
        if held is None:
            frame = await self.api.frame(window)
            if frame is None:
                return          # without the original frame we can't undo it
            held = _Held(frame, set(), {})
            self._held_windows()[window.window_id] = held
        if tab.tab_id not in held.grids:
            layout_now = self._layout_to_restore(tab)
            if layout_now is not None:
                held.grids[tab.tab_id] = layout_now
        if peer.fit_window != window.window_id:
            # Moved here from another window. Not _release_fit(): that cancels
            # peer.fit_task, which is this very coroutine.
            self._release_window(peer)
        held.peers.add(peer)
        peer.fit_window = window.window_id

        sizes = target_sizes(peer.window_mode, tab.root, pane.session_id,
                             cols, rows)
        if await self.api.set_grid_sizes(sizes):
            log.info("fit %d pane(s) to client %dx%d", len(sizes), cols, rows)
            return
        # Not reached: the window can't grow past the screen. What that leaves
        # behind depends on the view.
        if len(tab.sessions) > 1:
            # A pane inside a split: set_grid_size has moved the dividers
            # part-way. Don't leave the layout half-moved until this client
            # leaves: give it back now and fall back to cropping.
            log.info("could not fit pane to %dx%d; restoring the layout, rows "
                     "will be cropped", cols, rows)
            self._release_window(peer)
        else:
            # A lone (or zoomed) pane: only the window grew, as far as the
            # screen allows -- closer to the client than before, nothing
            # damaged. Keep it; undoing it now would resize the program a
            # second time for nothing.
            log.info("fit pane as close to %dx%d as the screen allows",
                     cols, rows)

    def _layout_to_restore(self, tab):
        """The split to put back when this fit ends, or None if unknown.

        Unzoomed, the tab's current layout is right here. Zoomed, the hidden
        panes report no size, so only a snapshot taken moments ago (the zoom
        that triggered this fit) will do. Without one, restore just the
        window frame: iTerm2 keeps its own record of the split across a zoom
        and puts it back itself.
        """
        if not self.api.is_zoomed(tab):
            return {s.session_id: (s.grid_size.width, s.grid_size.height)
                    for s in tab.sessions}
        snap = self._layouts().get(tab.tab_id)
        if snap is None or time.monotonic() - snap[0] > SNAPSHOT_MAX_AGE:
            return None
        return dict(snap[1])

    async def _client_inside(self, peer, window) -> bool:
        if not peer.ttyname:
            return False
        for t in window.tabs:
            for s in t.all_sessions:
                if await self.api.variable(s, "tty") == peer.ttyname:
                    return True
        return False

    def _release_fit(self, peer) -> None:
        """The client is gone: stop fitting for it and give its window back."""
        if peer.fit_task is not None:
            peer.fit_task.cancel()
            peer.fit_task = None
        self._release_window(peer)

    def _release_window(self, peer) -> None:
        """This client no longer fits its window; restore it if nobody does."""
        wid, peer.fit_window = peer.fit_window, None
        held = self._held_windows().get(wid)
        if held is None:
            return
        held.peers.discard(peer)
        if held.peers:
            # Another client still looks at this window: let it refit to its
            # own size on its next poll, as tmux follows the latest client.
            for other in held.peers:
                other.fit_key = None
            return
        del self._held_windows()[wid]
        self._spawn(self._restore(wid, held), "restore window size")

    def _restoring(self) -> set:
        if not hasattr(self, "_restoring_ids"):
            self._restoring_ids = set()
        return self._restoring_ids

    async def _restore(self, wid: str, held: _Held) -> None:
        # Frame, then layout, then frame again. Measured on iTerm2: ending on
        # the layout call leaves the window a row short; ending on the frame
        # comes back exact, dividers and position included. The layout call
        # sets every pane at once (preferred_size + update_layout) — per-pane
        # set_grid_size can't converge on a nested split.
        self._restoring().add(wid)
        try:
            await self.api.set_frame(wid, held.frame)
            for tab_id, sizes in held.grids.items():
                # Never lay out a tab that is still zoomed: its one visible pane
                # is the maximized one, and giving it its split size shrinks
                # the program in it -- which iTerm2 then re-maximizes. Seen
                # live after a failed fit: 200 -> 219 -> 98 -> 200 columns in
                # one second, and Claude's redraw came out as a staircase of
                # words. The split is iTerm2's to put back when it unzooms.
                if self.api.is_zoomed(self.api.tab_by_id(tab_id)):
                    continue
                await self.api.set_layout(tab_id, sizes)
            await self.api.set_frame(wid, held.frame)
        finally:
            self._restoring().discard(wid)
