"""Composite a whole iTerm2 tab (all its split panes) into one screen.

Attaching to a *window* means seeing every pane at once, with dividers between
them — what tmux does. iTerm2 hands us the split tree (`tab.root`): nested
Splitters, each either vertical (children side by side) or horizontal (children
stacked), with Sessions at the leaves.

We walk that tree, give every pane a rectangle in the client's grid in proportion
to its real size, and draw a divider line between siblings.

    Splitter(vertical=True)         ->  columns
      Splitter(vertical=False)      ->  rows within the left column
        Session 99x16
        ...
"""

from typing import List, NamedTuple


class Region(NamedTuple):
    """Where one pane lands in the client's grid (0-based, cells)."""
    session_id: str
    x: int
    y: int
    width: int
    height: int


def _is_leaf(node) -> bool:
    return not hasattr(node, "children")


def _leaves(node):
    if _is_leaf(node):
        yield node
        return
    for c in node.children:
        yield from _leaves(c)


def _has_frame(leaf) -> bool:
    f = getattr(leaf, "frame", None)
    size = getattr(f, "size", None)
    return size is not None and size.width > 0 and size.height > 0


def _weight(node, vertical: bool, use_frames: bool = False) -> float:
    """A node's size along the axis its parent divides.

    Weighs by the pane's size ON SCREEN (its frame, in points) when every pane
    has one, else by its cell count. Cells are the wrong measure when panes use
    different fonts: an 11pt pane shows 14 rows in the height a 12pt pane shows
    12, so weighing by rows drew four equally tall panes as 14/17/14/14.

    A splitter's extent is the max of its children across the divider, and
    their sum along it.
    """
    if _is_leaf(node):
        if use_frames:
            size = node.frame.size
            return float(size.width if vertical else size.height)
        g = node.grid_size
        return float(g.width if vertical else g.height)

    same_axis = (node.vertical == vertical)
    parts = [_weight(c, vertical, use_frames) for c in node.children]
    if not parts:
        return 1
    # Children laid out ALONG this axis add up; across it they overlap.
    return sum(parts) if same_axis else max(parts)


def _split(total: int, weights: List[float], gaps: int) -> List[int]:
    """Divide `total` cells among weights, reserving `gaps` cells for dividers.

    Rounds the BOUNDARIES (cumulative positions), not each size on its own.
    Rounding sizes independently misplaces dividers in two ways: leftover cells
    all went to the largest pane (four equal panes drew 17/14/14/14), and in a
    two-column layout each column's leftovers landed on different panes, so
    dividers that are level on the Mac came out a row apart. Boundaries that
    are level on screen round to the same row. Every pane still gets at least
    1 cell and the regions fill the space exactly.
    """
    avail = total - gaps
    n = len(weights)
    if avail < n:
        # Not enough room for everyone; give what we can.
        return [1] * n

    tw = sum(weights)
    if tw <= 0:
        weights, tw = [1.0] * n, float(n)
    cuts, acc = [], 0.0
    for w in weights:
        acc += w
        cuts.append(int(avail * acc / tw + 0.5))
    cuts[-1] = avail
    sizes = [b - a for a, b in zip([0] + cuts[:-1], cuts)]

    # A sliver of a pane can round to 0; lend it a cell from the largest.
    for i in range(n):
        while sizes[i] < 1:
            j = sizes.index(max(sizes))
            if sizes[j] <= 1:
                break
            sizes[j] -= 1
            sizes[i] += 1
    return sizes


#: Dividers closer than this (in client cells) are drawn on the same line.
#: Level dividers on the Mac can still come out a fraction of a row apart —
#: each column quantises to its own panes' font row heights (seen live: 461pt
#: vs 459pt, about 0.13 of a client row) — and rounding each on its own can put
#: them on neighbouring rows when they straddle a .5.
SNAP = 0.5


def _snapper(values):
    """Map each divider position to a row shared with its near neighbours."""
    target = {}
    group = []
    for v in sorted(values) + [None]:
        if group and (v is None or v - group[-1] > SNAP):
            row = int(sum(group) / len(group) + 0.5)
            for g in group:
                target[g] = row
            group = []
        if v is not None:
            group.append(v)
    return lambda v: target.get(v, int(v + 0.5))


def regions(root, cols: int, rows: int) -> List[Region]:
    """Lay out every pane in a tab's split tree onto a cols x rows grid.

    Two passes. First every divider's exact (fractional) position is worked
    out across the whole tree; then dividers that are nearly level — in any
    split, not just the same one — are snapped to one shared row or column,
    and panes are cut along those. Each pane keeps at least one cell and the
    regions fill the grid exactly.
    """
    # Points only if EVERY pane has a frame: mixing points and cells in one
    # split would compare numbers in different units.
    use_frames = all(_has_frame(leaf) for leaf in _leaves(root))
    cuts = {}       # id(splitter) -> (vertical, [exact divider positions])

    def plan(node, x: float, y: float, w: float, h: float) -> None:
        if _is_leaf(node):
            return
        kids = list(node.children)
        if not kids:
            return
        if len(kids) == 1:
            plan(kids[0], x, y, w, h)
            return
        vertical = bool(node.vertical)
        weights = [_weight(k, vertical, use_frames) for k in kids]
        tw = sum(weights) or float(len(kids))
        span = w if vertical else h
        avail = span - (len(kids) - 1)        # one divider between each pair
        pos = x if vertical else y
        divs = []
        for i, (kid, wt) in enumerate(zip(kids, weights)):
            size = avail * wt / tw
            if vertical:
                plan(kid, pos, y, size, h)
            else:
                plan(kid, x, pos, w, size)
            pos += size
            if i < len(kids) - 1:
                divs.append(pos)              # the divider occupies [pos, pos+1)
                pos += 1
        cuts[id(node)] = (vertical, divs)

    plan(root, 0.0, 0.0, float(cols), float(rows))
    snap = {v: _snapper([d for vv, ds in cuts.values() if vv == v for d in ds])
            for v in (True, False)}

    out: List[Region] = []

    def place(node, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        if _is_leaf(node):
            out.append(Region(node.session_id, x, y, w, h))
            return
        kids = list(node.children)
        if not kids:
            return
        if len(kids) == 1:
            place(kids[0], x, y, w, h)
            return

        vertical, divs = cuts[id(node)]
        start, end = (x, x + w) if vertical else (y, y + h)
        n = len(kids)
        if end - start < 2 * n - 1:
            # Not even one cell per pane plus dividers: share what there is.
            sizes = _split(end - start, [1.0] * n, n - 1)
            edges, at = [start - 1], start
            for sz in sizes[:-1]:
                at += sz
                edges.append(at)
                at += 1
            edges.append(end)
        else:
            edges, prev = [start - 1], start - 1
            for i, d in enumerate(divs):
                lo = prev + 2                     # >= 1 cell after the last one
                hi = end - 2 * (n - 1 - i) - 1    # room for the panes after it
                prev = min(max(snap[vertical](d), lo), hi)
                edges.append(prev)
            edges.append(end)

        for i, kid in enumerate(kids):
            a, b = edges[i] + 1, edges[i + 1]
            if vertical:
                place(kid, a, y, b - a, h)
            else:
                place(kid, x, a, w, b - a)

    place(root, 0, 0, cols, rows)
    return out
