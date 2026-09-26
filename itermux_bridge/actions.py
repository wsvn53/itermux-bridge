"""Prefix actions: what Ctrl-B <key> actually does to iTerm2.

zoom, splits, pane navigation, window switching, scrollback paging, copy-mode
entry and the mouse toggle. Kept apart from input *routing* (input.py) and from
rendering (view.py) so each stays readable on its own.

`PrefixActions` is mixed into the backend.
"""

import logging

from . import ansi

log = logging.getLogger(__name__)

class PrefixActions:
    """Execution of the tmux prefix bindings against iTerm2."""

    def on_prefix_command(self, peer, action: str) -> None:
        """A tmux prefix binding (Ctrl-B z, etc.) -> the equivalent iTerm2 op."""
        sid = getattr(peer, "iterm_session_id", None)
        if sid is None:
            return
        self._spawn(self._prefix(peer, sid, action), f"prefix {action}")

    async def _prefix(self, peer, sid: str, action: str) -> None:
        session = self.api.pane(sid)
        if session is None:
            return
        # Prefix actions can print on the client's screen (rename-window's
        # hint, break-pane's refusal) and change what's shown wholesale, so the
        # next paint must not assume the screen still matches the last frame.
        peer.last_frame = None

        try:
            if action == "copy-mode":
                cols, rows = peer.tty.size()
                peer.copy.enter(
                    rows, peer.scroll_offset,
                    bounds=self._pane_bounds(peer, session, cols, rows))
                await self._paint(peer, session)
                return

            if action in ("page-up", "page-down"):
                cols, rows = peer.tty.size()
                if not peer.copy.active:
                    peer.copy.enter(
                        rows, peer.scroll_offset,
                        bounds=self._pane_bounds(peer, session, cols, rows))
                self._page(peer, rows, -1 if action == "page-up" else 1)
                # Scrolled all the way back to the live screen: nothing left to
                # be in copy-mode for.
                if peer.scroll_offset == 0:
                    peer.copy.leave()
                await self._paint(peer, session)
                return

            if action == "toggle-mouse":
                # tmux's `set -g mouse on/off`. OFF (the default) leaves the
                # mouse with the client's terminal, so selection is native. ON
                # hands it to us: the wheel pages through scrollback and a TUI
                # gets its clicks — at the cost of the terminal's own selection.
                # Toggle what the user actually has: during a zoom the mouse is
                # already ours, and flipping only `mouse_on` would switch it
                # "on" with no visible change. Turning it off also drops the
                # zoom's claim for the rest of that zoom.
                owned = not peer.mouse_owned
                peer.mouse_on = owned
                peer.zoom_mouse = False
                # Leave copy-mode whenever mouse ownership changes. Mouse-driven
                # selection put us there; once the mouse is no longer ours (or
                # its ownership just flipped) there's no way to drive or exit the
                # selection with it, so a leftover copy-mode would trap the
                # keyboard with no way out. Reset fully.
                peer.copy.leave()
                peer.scroll_offset = 0
                peer.write_out(ansi.ENABLE_MOUSE if peer.mouse_on
                               else ansi.DISABLE_MOUSE)
                log.info("mouse reporting %s",
                         "on" if peer.mouse_on else "off")
                await self._paint(peer, session)
                return

            if action == "paste":
                return          # the client's own terminal handles paste

            if action == "zoom":
                await self._before_unzoom(peer, session)
                await self.api.zoom(session)

            elif action == "new-window":
                # A tmux window is an iTerm2 tab; follow it, as tmux does.
                new = await self.api.new_window(session)
                if new is not None:
                    peer.iterm_session_id = new.session_id
                    peer.copy.leave()
                    peer.window_mode = False

            elif action.startswith("select-window-"):
                index = int(action.rsplit("-", 1)[1])
                target = self._window_by_index(index)
                if target is not None:
                    await self.api.activate(target)
                    peer.iterm_session_id = target.session_id
                    peer.copy.leave()

            elif action == "last-window":
                target = self.api.pane(peer.last_window_pane)
                if target is not None:
                    await self.api.activate(target)
                    peer.iterm_session_id = target.session_id
                    peer.copy.leave()

            elif action == "last-pane":
                target = self.api.pane(peer.last_pane)
                if target is not None:
                    await self.api.activate(target)
                    peer.iterm_session_id = target.session_id
                    peer.copy.leave()

            elif action.startswith("resize-"):
                direction = {
                    "resize-left": "left", "resize-right": "right",
                    "resize-up": "above", "resize-down": "below",
                }[action]
                tab = self.api.tab_of(session.session_id)
                await self.api.resize(tab, session, direction, amount=2)

            elif action == "rename-window":
                # Without a command prompt there's nothing to type a name into;
                # say so rather than doing nothing.
                peer.write_out(
                    b"\r\n\033[33mitermux-bridge:\033[m rename from outside: "
                    b"tmux -S <sock> rename-window <name>\r\n")

            elif action == "break-pane":
                peer.write_out(
                    b"\r\n\033[33mitermux-bridge:\033[m break-pane isn't "
                    b"supported (iTerm2 has no move-session-to-tab API).\r\n")

            elif action in ("split-horizontal", "split-vertical"):
                new = await self.api.split(
                    session, vertical=(action == "split-vertical"))
                # Follow the new pane, like tmux does.
                if new is not None:
                    peer.iterm_session_id = new.session_id

            elif action == "kill-pane":
                await self.api.close_pane(session)
                peer.detach(status=0)
                return

            elif action in ("next-window", "previous-window"):
                # Windows are iTerm2 tabs — switch to the neighbouring tab's
                # active pane, the way Ctrl-B n/p works in tmux.
                from .commands import _window_target
                cmd = ("next-window" if action == "next-window"
                       else "previous-window")
                target = _window_target(self, self.mapper, self.app, cmd, None)
                if target is not None:
                    await self.api.activate(target)
                    peer.iterm_session_id = target.session_id
                    peer.copy.leave()

            elif action in ("next-pane", "select-left", "select-right",
                            "select-up", "select-down"):
                target = await self._pane_in_direction(session, action)
                if target is not None:
                    await self.api.activate(target)
                    peer.iterm_session_id = target.session_id

            # Record where we came from so Ctrl-B ; (last-pane) and Ctrl-B l
            # (last-window) have somewhere to go back to. Only when the action
            # actually moved us, and never onto itself.
            if peer.iterm_session_id != sid:
                peer.last_pane = sid
                old_tab = self.api.tab_of(sid)
                new_tab = self.api.tab_of(peer.iterm_session_id)
                if old_tab is not None and new_tab is not None and \
                        old_tab.tab_id != new_tab.tab_id:
                    peer.last_window_pane = sid

            await self.api.refresh()
            peer.scroll_offset = 0
            await self._paint(peer,
                              self.api.pane(peer.iterm_session_id) or session)

        except Exception as e:
            log.warning("prefix %s failed: %s", action, e)


    def _window_by_index(self, index: int):
        """The active pane of window #index (tmux's Ctrl-B 0..9)."""
        for _s, w, pane in self.mapper.flat_panes(self.app):
            if w["index"] == index and pane["active"]:
                return self.api.pane(pane["iterm_session_id"])
        # No active pane recorded for it — fall back to its first pane.
        for _s, w, pane in self.mapper.flat_panes(self.app):
            if w["index"] == index:
                return self.api.pane(pane["iterm_session_id"])
        return None

    async def _pane_in_direction(self, session, action: str):
        """Resolve a select-* / next-pane action to the pane it means."""
        tab = self.api.tab_of(session.session_id)
        if tab is None or len(tab.sessions) < 2:
            return None

        if action == "next-pane":
            ids = [s.session_id for s in tab.sessions]
            i = ids.index(session.session_id)
            return tab.sessions[(i + 1) % len(ids)]

        direction = {
            "select-left": "left", "select-right": "right",
            "select-up": "above", "select-down": "below",
        }.get(action)
        return await self.api.neighbour(tab, session, direction)
