"""Split-tree -> screen regions.

Models iTerm2's Splitter/Session tree: a Splitter has `.vertical` and
`.children`; a leaf Session has `.session_id` and `.grid_size`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import layout


class Grid:
    def __init__(self, w, h):
        self.width, self.height = w, h


class Sess:
    def __init__(self, sid, w, h):
        self.session_id = sid
        self.grid_size = Grid(w, h)


class Split:
    def __init__(self, vertical, children):
        self.vertical = vertical
        self.children = children


ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


def overlaps(regions):
    cells = set()
    for r in regions:
        for y in range(r.y, r.y + r.height):
            for x in range(r.x, r.x + r.width):
                if (x, y) in cells:
                    return True
                cells.add((x, y))
    return False


print("\n=== split layout ===")

# Single pane fills everything.
rs = layout.regions(Sess("a", 80, 24), 80, 24)
check("single pane fills the screen",
      len(rs) == 1 and (rs[0].width, rs[0].height) == (80, 24))

# Two side by side: 80 cols - 1 divider = 79, split ~40/39.
rs = layout.regions(Split(True, [Sess("a", 40, 24), Sess("b", 40, 24)]), 80, 24)
check("vertical split -> two columns", len(rs) == 2)
check("a divider column is reserved",
      rs[0].width + rs[1].width == 79, f"({rs[0].width}+{rs[1].width})")
check("second column starts after the divider",
      rs[1].x == rs[0].x + rs[0].width + 1)
check("both are full height", all(r.height == 24 for r in rs))

# Two stacked.
rs = layout.regions(Split(False, [Sess("a", 80, 12), Sess("b", 80, 12)]), 80, 24)
check("horizontal split -> two rows",
      len(rs) == 2 and rs[0].height + rs[1].height == 23)
check("second row starts after the divider",
      rs[1].y == rs[0].y + rs[0].height + 1)

# The real mini2 shape: 2 columns, each split into 3 rows.
tree = Split(True, [
    Split(False, [Sess("a", 99, 16), Sess("b", 99, 17), Sess("c", 99, 15)]),
    Split(False, [Sess("d", 101, 15), Sess("e", 101, 17), Sess("f", 101, 16)]),
])
rs = layout.regions(tree, 120, 40)
check("6-pane tree -> 6 regions", len(rs) == 6)
check("no two panes overlap", not overlaps(rs))
check("everything stays on screen",
      all(r.x >= 0 and r.y >= 0 and r.x + r.width <= 120
          and r.y + r.height <= 40 for r in rs))

left = [r for r in rs if r.session_id in "abc"]
right = [r for r in rs if r.session_id in "def"]
check("left column panes share an x", len({r.x for r in left}) == 1)
check("right column is to the right of the left",
      min(r.x for r in right) > max(r.x + r.width for r in left) - 1)
check("each column's 3 panes stack vertically",
      len({r.y for r in left}) == 3 and len({r.y for r in right}) == 3)

# Proportions: iTerm2 had rows 16/17/15, so the middle pane should be tallest.
heights = {r.session_id: r.height for r in rs}
check("pane heights follow iTerm2's real proportions",
      heights["b"] >= heights["a"] >= heights["c"], f"({heights})")

# Degenerate: a tiny client must not produce negative or zero-sized regions.
rs = layout.regions(tree, 10, 6)
check("tiny client still yields usable regions",
      all(r.width >= 1 and r.height >= 1 for r in rs))

print("\n=== active pane border ===")

from itermux_bridge import ansi


class Grid2:
    def __init__(self, w, h): self.width, self.height = w, h


class FakeLine:
    string = "x"
    def style_at(self, i): return None


class FakeContents:
    number_of_lines = 1
    def __init__(self):
        class P: pass
        self.cursor_coord = P()
        self.cursor_coord.x = self.cursor_coord.y = 0
    def line(self, i): return FakeLine()


# Two side-by-side panes: the divider between them must be highlighted when one
# of them is active, so you can see where your keystrokes land.
rs = layout.regions(Split(True, [Sess("a", 40, 24), Sess("b", 40, 24)]), 80, 24)
panes = [(r, FakeContents()) for r in rs]

out_a = ansi.render_panes(panes, 80, 24, active_id="a")
check("active pane's border uses the highlight colour",
      ansi.ACTIVE_DIVIDER_SGR in out_a)

# With no active pane, every divider is the dim colour.
out_none = ansi.render_panes(panes, 80, 24, active_id="")
check("no highlight when nothing is active",
      ansi.ACTIVE_DIVIDER_SGR not in out_none
      and ansi.DIVIDER_SGR in out_none)

# Switching the active pane must move the highlight, not just add one.
out_b = ansi.render_panes(panes, 80, 24, active_id="b")
check("the highlight follows the active pane", out_a != out_b)

print("\n=== copy-mode in WINDOW mode ===")

from itermux_bridge.copymode import CopyMode

# Regression: copy-mode was only wired into the single-pane renderer. In window
# mode (`-t @N`) the state changed server-side but NOTHING was drawn — no status
# bar, no selection — which is indistinguishable from "copy-mode doesn't work".
cm = CopyMode()
cm.enter(rows=24)
out = ansi.render_panes(panes, 80, 24, active_id="a", copy=cm)
check("copy-mode status bar is drawn in window mode", b"COPY" in out)

cm.cy, cm.cx = 5, 2
cm.start_selection()
cm.cy, cm.cx = 5, 20
out = ansi.render_panes(panes, 80, 24, active_id="a", copy=cm)
check("the selection is highlighted in window mode",
      ansi.SELECTION_SGR in out)

# And with copy-mode off, neither appears.
out = ansi.render_panes(panes, 80, 24, active_id="a", copy=CopyMode())
check("no copy-mode chrome when the mode is off",
      b"COPY" not in out and ansi.SELECTION_SGR not in out)

print("\n=== rounding and on-screen sizes ===")


class Pt:
    def __init__(self, w, h):
        class S: pass
        self.size = S(); self.size.width, self.size.height = w, h


class FSess(Sess):
    """A pane with an on-screen frame, like iTerm2's (points)."""
    def __init__(self, sid, w, h, pw, ph):
        super().__init__(sid, w, h)
        self.frame = Pt(pw, ph)


# Four EQUAL panes stacked in 62 rows: 59 after dividers, 14.75 each. The old
# rounding gave all 3 spare rows to one pane: 17/14/14/14.
col = Split(False, [Sess(c, 100, 12) for c in "abcd"])
hs = [r.height for r in layout.regions(col, 100, 62)]
check("equal panes stay within one row of each other", max(hs) - min(hs) <= 1,
      f"({hs})")
check("...and still fill the height exactly", sum(hs) + 3 == 62, f"({hs})")

# The live case: one pane uses an 11pt font, so it shows 14 rows in the same
# height the 12pt panes show 12. Equal on screen must draw equal.
col = Split(False, [FSess("a", 85, 12, 600, 229), FSess("b", 85, 14, 600, 233),
                    FSess("c", 85, 12, 600, 228), FSess("d", 85, 12, 600, 229)])
hs = [r.height for r in layout.regions(col, 100, 62)]
check("weighted by on-screen height, not row count",
      max(hs) - min(hs) <= 1, f"({hs})")

# Two columns of equal panes, one with the 11pt pane: dividers line up.
left = Split(False, [FSess("a", 85, 12, 600, 229), FSess("b", 85, 14, 600, 233),
                     FSess("c", 85, 12, 600, 228), FSess("d", 85, 12, 600, 229)])
right = Split(False, [FSess(x, 112, 12, 790, 230) for x in "efgh"])
regs = {r.session_id: r for r in layout.regions(Split(True, [left, right]), 200, 62)}
ys_left = [regs[x].y for x in "abcd"]
ys_right = [regs[x].y for x in "efgh"]
check("the two columns' dividers line up", ys_left == ys_right,
      f"(left {ys_left}, right {ys_right})")

# A pane without a frame means cells for everyone: never mix units.
col = Split(False, [FSess("a", 85, 12, 600, 229), Sess("b", 85, 36)])
hs = [r.height for r in layout.regions(col, 100, 49)]
check("any pane lacking a frame: fall back to row counts", hs[1] > 2 * hs[0],
      f"({hs})")


print("\n=== near-level dividers snap to one line ===")

# Measured live on @0 at 200x62: the middle dividers sit at 461pt (left) and
# 459pt (right) -- level to the eye, but rounding each column on its own drew
# them on rows 32 and 31.
left = Split(False, [FSess("a", 98, 12, 701, 228), FSess("b", 98, 14, 701, 233),
                     FSess("c", 98, 12, 701, 229), FSess("d", 98, 12, 701, 229)])
right = Split(False, [FSess("e", 99, 12, 708, 229), FSess("f", 99, 12, 708, 230),
                      FSess("g", 99, 12, 708, 230), FSess("h", 99, 12, 708, 230)])
regs = {r.session_id: r for r in layout.regions(Split(True, [left, right]), 200, 62)}
check("the live @0 case: every divider on the same row in both columns",
      [regs[x].y for x in "abcd"] == [regs[x].y for x in "efgh"],
      f"(left {[regs[x].y for x in 'abcd']}, right {[regs[x].y for x in 'efgh']})")
check("...columns split the width evenly",
      abs(regs["a"].width - regs["e"].width) <= 1,
      f"({regs['a'].width} vs {regs['e'].width})")
check("...still no overlaps", not overlaps(list(regs.values())))

# Dividers that really are rows apart must NOT be pulled together.
left = Split(False, [FSess("a", 98, 10, 700, 200), FSess("b", 98, 30, 700, 700)])
right = Split(False, [FSess("c", 98, 20, 700, 450), FSess("d", 98, 20, 700, 450)])
regs = {r.session_id: r for r in layout.regions(Split(True, [left, right]), 200, 62)}
check("dividers genuinely apart stay apart", abs(regs["b"].y - regs["d"].y) >= 5,
      f"(left divider row {regs['b'].y}, right {regs['d'].y})")


print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
