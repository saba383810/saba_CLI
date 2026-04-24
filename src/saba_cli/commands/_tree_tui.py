"""Interactive curses TUI for `saba-cli --tree`.

Two-pane layout:

    ┌─ header (title + repo/branch/commits) ───────────────────┐
    │  tree pane  — scrollable, column-aligned commit list     │
    ├─ details ────────────────────────────────────────────────┤
    │  detail pane — hash / author / date / refs / body / stat │
    └─ footer (key hints + position / filter indicator) ───────┘

Keys
----
    ↑ / k              move selection up
    ↓ / j              move selection down
    PgUp / PgDn        scroll one screen
    Home / g           jump to newest
    End  / G           jump to oldest
    [ / ]              scroll the detail pane up / down
    Tab / d            toggle detail pane
    m                  toggle "only my commits" filter
    f or /             enter search (incremental filter by hash/author/refs/subject)
    Enter              in search → apply filter and resume navigation
    ESC                in search → cancel; with filter → clear; otherwise → quit
    q / Q              quit

Design notes
------------
* The palette is registered through curses. Where the terminal supports
  ``can_change_color()`` we redefine 15 slots to the exact Iceberg RGB;
  otherwise we fall back to the closest xterm-256 color, or 8-color
  ANSI on very old terminals.
* Commit details (full message + file-level stats) are fetched lazily
  with ``git show`` and memoised. The shell-out cost only happens once
  per commit, so moving up/down is instant after the first visit.
* The tree row layout is adaptive: when the terminal is narrow we drop
  lower-priority columns (refs first, then author, then date) so the
  commit subject always has some room. The detail pane still shows
  everything for the currently-focused commit.
* When the terminal is short, the detail pane auto-hides so the tree
  stays usable — a Tab press re-opens it.
"""

from __future__ import annotations

import curses
import os
from dataclasses import dataclass, field
from pathlib import Path

from saba_cli.commands._tree_common import (
    Ref, parse_refs, translate_graph_glyphs, truncate, pad_right,
    ref_label_text, ref_group_width,
)
from saba_cli.utils import git


# ---------------------------------------------------------------------------
# Iceberg palette
# ---------------------------------------------------------------------------
PAL_HEX = {
    "fg":        "#c6c8d1",
    "fg_dim":    "#6b7089",
    "fg_bright": "#d2d4de",
    "red":       "#e27878",
    "green":     "#b4be82",
    "yellow":    "#e2a478",
    "blue":      "#84a0c6",
    "magenta":   "#a093c7",
    "cyan":      "#89b8c2",
    "red_b":     "#e98989",
    "green_b":   "#c0ca8e",
    "yellow_b":  "#e9b189",
    "blue_b":    "#91acd1",
    "magenta_b": "#ada0d3",
    "cyan_b":    "#95c4ce",
}


class CP:
    DEFAULT = 1
    GRAPH = 2
    HASH = 3
    HEAD = 4
    LOCAL = 5
    REMOTE = 6
    TAG = 7
    DATE = 8
    AUTHOR = 9
    MESSAGE = 10
    TITLE = 11
    MUTED = 12
    ACCENT = 13
    ERROR = 14
    WARN = 15
    OK = 16
    KEY = 17
    MINE = 18           # author name highlight for the current user's commits
    PLUS = 19           # "+" in file stat lines
    MINUS = 20          # "-" in file stat lines


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex_to_rgb_1000(hex_color: str) -> tuple[int, int, int]:
    r, g, b = _hex_to_rgb(hex_color)
    return r * 1000 // 255, g * 1000 // 255, b * 1000 // 255


def _hex_to_xterm256(hex_color: str) -> int:
    r, g, b = _hex_to_rgb(hex_color)
    if abs(r - g) < 16 and abs(g - b) < 16:
        v = (r + g + b) // 3
        if v < 8:
            return 16
        if v > 248:
            return 231
        return 232 + (v - 8) // 10
    levels = (0, 95, 135, 175, 215, 255)

    def q(c: int) -> int:
        best = 0
        best_d = 999
        for i, lv in enumerate(levels):
            d = abs(c - lv)
            if d < best_d:
                best_d, best = d, i
        return best
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def _setup_palette(use_color: bool) -> None:
    if not use_color:
        return
    try:
        curses.start_color()
    except curses.error:
        return
    try:
        curses.use_default_colors()
        default_bg = -1
    except curses.error:
        default_bg = curses.COLOR_BLACK

    color_map: dict[str, int] = {}
    if curses.can_change_color() and curses.COLORS >= 32:
        for i, (name, hex_code) in enumerate(PAL_HEX.items()):
            slot = 100 + i
            try:
                curses.init_color(slot, *_hex_to_rgb_1000(hex_code))
                color_map[name] = slot
            except curses.error:
                color_map[name] = (
                    _hex_to_xterm256(hex_code)
                    if curses.COLORS >= 256
                    else curses.COLOR_WHITE
                )
    elif curses.COLORS >= 256:
        for name, hex_code in PAL_HEX.items():
            color_map[name] = _hex_to_xterm256(hex_code)
    else:
        color_map = {
            "fg":        curses.COLOR_WHITE,
            "fg_dim":    curses.COLOR_WHITE,
            "fg_bright": curses.COLOR_WHITE,
            "red":       curses.COLOR_RED,
            "green":     curses.COLOR_GREEN,
            "yellow":    curses.COLOR_YELLOW,
            "blue":      curses.COLOR_BLUE,
            "magenta":   curses.COLOR_MAGENTA,
            "cyan":      curses.COLOR_CYAN,
            "red_b":     curses.COLOR_RED,
            "green_b":   curses.COLOR_GREEN,
            "yellow_b":  curses.COLOR_YELLOW,
            "blue_b":    curses.COLOR_BLUE,
            "magenta_b": curses.COLOR_MAGENTA,
            "cyan_b":    curses.COLOR_CYAN,
        }

    pairs = {
        CP.DEFAULT: color_map["fg"],
        CP.GRAPH:   color_map["blue"],
        CP.HASH:    color_map["yellow"],
        CP.HEAD:    color_map["magenta_b"],
        CP.LOCAL:   color_map["green"],
        CP.REMOTE:  color_map["red"],
        CP.TAG:     color_map["yellow_b"],
        CP.DATE:    color_map["fg_dim"],
        CP.AUTHOR:  color_map["cyan"],
        CP.MESSAGE: color_map["fg"],
        CP.TITLE:   color_map["blue_b"],
        CP.MUTED:   color_map["fg_dim"],
        CP.ACCENT:  color_map["magenta"],
        CP.ERROR:   color_map["red"],
        CP.WARN:    color_map["yellow_b"],
        CP.OK:      color_map["green_b"],
        CP.KEY:     color_map["cyan_b"],
        CP.MINE:    color_map["yellow_b"],
        CP.PLUS:    color_map["green_b"],
        CP.MINUS:   color_map["red"],
    }
    for pair_id, fg in pairs.items():
        try:
            curses.init_pair(pair_id, fg, default_bg)
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# Row model
# ---------------------------------------------------------------------------
Segment = tuple[str, int, int]   # (text, color_pair_id, extra_attrs)


@dataclass
class Row:
    kind: str                    # "commit" | "graph"
    selectable: bool
    segments: list[Segment]
    commit: git.Commit | None = None
    haystack: str = ""
    is_mine: bool = False


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------
HEADER_H = 3                     # title + info + divider
FOOTER_H = 2                     # divider + statusline
MIN_TREE_H = 3
MIN_DETAIL_H = 6
MIN_USEFUL_ROWS = 12             # below this, auto-hide detail pane
NARROW_THRESHOLD_REFS = 90       # drop the refs column below this width
NARROW_THRESHOLD_AUTHOR = 70     # drop the author column below this width
NARROW_THRESHOLD_DATE = 54       # drop the relative date below this width


# ---------------------------------------------------------------------------
# Row building (adaptive to terminal width + "mine" marker)
# ---------------------------------------------------------------------------
def _ref_segments(refs: list[Ref]) -> list[Segment]:
    segs: list[Segment] = []
    if not refs:
        return segs
    segs.append(("(", CP.MUTED, 0))
    segs.append((" ", CP.DEFAULT, 0))
    for i, ref in enumerate(refs):
        if i > 0:
            segs.append((" ", CP.DEFAULT, 0))
            segs.append(("·", CP.MUTED, 0))
            segs.append((" ", CP.DEFAULT, 0))
        text = ref_label_text(ref)
        if ref.kind == "head":
            segs.append((text, CP.HEAD, curses.A_BOLD))
        elif ref.kind == "local":
            segs.append((text, CP.LOCAL, curses.A_BOLD))
        elif ref.kind == "remote":
            segs.append((text, CP.REMOTE, 0))
        elif ref.kind == "tag":
            segs.append((text, CP.TAG, curses.A_BOLD))
        else:
            segs.append((text, CP.MUTED, 0))
    segs.append((" ", CP.DEFAULT, 0))
    segs.append((")", CP.MUTED, 0))
    return segs


@dataclass(frozen=True)
class ColumnSpec:
    show_refs: bool
    show_date: bool
    show_author: bool

    @classmethod
    def for_width(cls, w: int) -> "ColumnSpec":
        return cls(
            show_refs=w >= NARROW_THRESHOLD_REFS,
            show_date=w >= NARROW_THRESHOLD_DATE,
            show_author=w >= NARROW_THRESHOLD_AUTHOR,
        )


def _build_rows(
    entries: list,
    remotes: list[str],
    my_email: str,
    cols: ColumnSpec,
) -> list[Row]:
    commits = [e for e in entries if isinstance(e, git.Commit)]
    graph_width = max((len(e.graph) for e in entries), default=0)
    hash_width = max((len(c.short_hash) for c in commits), default=7)
    date_width = max((len(c.rel_date) for c in commits), default=0) if cols.show_date else 0
    author_width = 0
    if cols.show_author:
        author_width = min(max((len(c.author) for c in commits), default=0), 20)
    refs_widths = [ref_group_width(parse_refs(c.refs, remotes)) for c in commits] if cols.show_refs else []
    refs_width = max(refs_widths, default=0)

    rows: list[Row] = []
    commit_iter = iter(zip(commits, refs_widths or [0] * len(commits)))
    for entry in entries:
        if isinstance(entry, git.GraphLine):
            graph_raw = pad_right(entry.graph, graph_width)
            rows.append(Row(
                kind="graph",
                selectable=False,
                segments=[
                    ("  ", CP.DEFAULT, 0),
                    (translate_graph_glyphs(graph_raw), CP.GRAPH, 0),
                ],
            ))
            continue

        c = entry
        c_pair, raw_refs_w = next(commit_iter)
        is_mine = bool(my_email) and c.author_email.lower() == my_email

        graph_raw = pad_right(c.graph, graph_width)
        hash_raw = pad_right(c.short_hash, hash_width)

        # Leading "mine" indicator occupies a fixed 2-char column so rows
        # stay aligned whether the commit is mine or not.
        segs: list[Segment] = []
        if is_mine:
            segs.append(("▎ ", CP.MINE, curses.A_BOLD))
        else:
            segs.append(("  ", CP.DEFAULT, 0))

        segs.append((translate_graph_glyphs(graph_raw), CP.GRAPH, 0))
        segs.append(("  ", CP.DEFAULT, 0))
        segs.append((hash_raw, CP.HASH, 0))

        if cols.show_refs:
            segs.append(("  ", CP.DEFAULT, 0))
            ref_segs = _ref_segments(parse_refs(c.refs, remotes))
            segs.extend(ref_segs)
            pad = refs_width - raw_refs_w
            if pad > 0:
                segs.append((" " * pad, CP.DEFAULT, 0))

        if cols.show_date:
            segs.append(("  ", CP.DEFAULT, 0))
            segs.append((pad_right(c.rel_date, date_width), CP.DATE, 0))

        if cols.show_author:
            segs.append(("  ", CP.DEFAULT, 0))
            author_txt = pad_right(truncate(c.author, author_width), author_width)
            segs.append((
                author_txt,
                CP.MINE if is_mine else CP.AUTHOR,
                curses.A_BOLD if is_mine else 0,
            ))

        segs.append(("  ", CP.DEFAULT, 0))
        segs.append((c.subject, CP.MESSAGE, 0))

        haystack = " ".join([
            c.short_hash, c.full_hash, c.author, c.author_email, c.refs, c.subject,
        ]).lower()
        rows.append(Row(
            kind="commit",
            selectable=True,
            segments=segs,
            commit=c,
            haystack=haystack,
            is_mine=is_mine,
        ))
    return rows


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    repo_root: Path
    branch: str
    my_email: str
    total_commits: int
    shown_commits: int

    entries: list                         # raw git.Commit | git.GraphLine
    remotes: list[str]
    all_rows: list[Row]                   # before filters, respects current width
    rows: list[Row]                       # after search + mine filter
    col_spec: ColumnSpec

    selected: int = 0
    scroll_offset: int = 0

    search_mode: bool = False
    search_query: str = ""
    only_mine: bool = False

    show_details: bool = True
    detail_scroll: int = 0
    detail_cache: dict[str, git.CommitDetail] = field(default_factory=dict)

    use_color: bool = True


# ---------------------------------------------------------------------------
# Misc drawing helpers
# ---------------------------------------------------------------------------
def _first_selectable(rows: list[Row], start: int = 0) -> int:
    for i in range(start, len(rows)):
        if rows[i].selectable:
            return i
    for i in range(min(start, len(rows)) - 1, -1, -1):
        if rows[i].selectable:
            return i
    return 0


def _segments_width(segs: list[Segment]) -> int:
    return sum(len(s) for s, _, _ in segs)


def _draw_segments(
    stdscr,
    y: int,
    x: int,
    max_w: int,
    segments: list[Segment],
    base_attr: int = 0,
) -> int:
    """Draw segments starting at (y, x), respecting max_w. Returns the
    visible width actually written."""
    remaining = max_w
    written = 0
    for text, pair, attr in segments:
        if remaining <= 0:
            break
        shown = text if len(text) <= remaining else text[:remaining]
        try:
            stdscr.addnstr(y, x, shown, remaining,
                           curses.color_pair(pair) | attr | base_attr)
        except curses.error:
            pass
        x += len(shown)
        remaining -= len(shown)
        written += len(shown)
    return written


def _stat_segments(line: str) -> list[Segment]:
    """Color a `git show --stat` line: filename | NNN +++---."""
    # Summary line: "3 files changed, 20 insertions(+), 4 deletions(-)"
    if "|" not in line:
        return [(line, CP.MUTED, 0)]
    left, _, right = line.partition("|")
    segs: list[Segment] = [(left, CP.AUTHOR, 0), ("|", CP.MUTED, 0)]
    for ch in right:
        if ch == "+":
            segs.append((ch, CP.PLUS, 0))
        elif ch == "-":
            segs.append((ch, CP.MINUS, 0))
        else:
            segs.append((ch, CP.MESSAGE, 0))
    return segs


def _wrap_plain(text: str, width: int) -> list[str]:
    """Soft-wrap a plain string to ``width``. Preserves empty lines."""
    if width <= 0:
        return [text]
    out: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw:
            out.append("")
            continue
        line = raw
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------
class TreeApp:
    def __init__(self, stdscr, state: AppState):
        self.stdscr = stdscr
        self.state = state

    # ---- sizing ----------------------------------------------------------
    def _screen_size(self) -> tuple[int, int]:
        h, w = self.stdscr.getmaxyx()
        return h, w

    def _body_region(self) -> tuple[int, int]:
        """Return (top, height) of the main body area (between header and footer)."""
        h, _ = self._screen_size()
        return HEADER_H, max(1, h - HEADER_H - FOOTER_H)

    def _should_show_details(self) -> bool:
        if not self.state.show_details:
            return False
        h, _ = self._screen_size()
        body = h - HEADER_H - FOOTER_H
        return body >= MIN_USEFUL_ROWS

    def _pane_split(self) -> tuple[int, int]:
        """Return (tree_height, detail_height). Detail may be 0 if hidden."""
        _, body_h = self._body_region()
        if not self._should_show_details():
            return body_h, 0
        # Aim for ~55% tree / ~45% detail, but never less than MIN_DETAIL_H
        # and never steal the tree's MIN_TREE_H.
        detail_h = max(MIN_DETAIL_H, (body_h - 1) * 9 // 20)
        tree_h = body_h - detail_h - 1
        if tree_h < MIN_TREE_H:
            tree_h = MIN_TREE_H
            detail_h = max(1, body_h - tree_h - 1)
        return tree_h, detail_h

    # ---- selection / scroll ---------------------------------------------
    def _current_commit(self) -> git.Commit | None:
        rows = self.state.rows
        if not rows:
            return None
        row = rows[min(self.state.selected, len(rows) - 1)]
        return row.commit if row.selectable else None

    def _ensure_visible(self, tree_h: int) -> None:
        if tree_h <= 0 or not self.state.rows:
            return
        if self.state.selected < self.state.scroll_offset:
            self.state.scroll_offset = self.state.selected
        elif self.state.selected >= self.state.scroll_offset + tree_h:
            self.state.scroll_offset = self.state.selected - tree_h + 1
        max_offset = max(0, len(self.state.rows) - tree_h)
        self.state.scroll_offset = max(0, min(self.state.scroll_offset, max_offset))

    def _move(self, delta: int) -> None:
        rows = self.state.rows
        if not rows:
            return
        step = 1 if delta > 0 else -1
        target = self.state.selected + delta
        target = max(0, min(target, len(rows) - 1))
        while 0 <= target < len(rows) and not rows[target].selectable:
            target += step
        if target < 0 or target >= len(rows):
            target = _first_selectable(rows, max(0, self.state.selected))
        self.state.selected = target
        self.state.detail_scroll = 0

    def _jump(self, where: str) -> None:
        rows = self.state.rows
        if not rows:
            return
        if where == "top":
            self.state.selected = _first_selectable(rows, 0)
        else:
            for i in range(len(rows) - 1, -1, -1):
                if rows[i].selectable:
                    self.state.selected = i
                    break
        self.state.detail_scroll = 0

    # ---- filtering -------------------------------------------------------
    def _apply_filters(self) -> None:
        query = self.state.search_query.strip().lower()
        only_mine = self.state.only_mine
        filtered: list[Row] = []
        for r in self.state.all_rows:
            if not r.selectable and (query or only_mine):
                # Skip graph connectors when any filter is active — the
                # tree structure would be misleading anyway.
                continue
            if only_mine and not r.is_mine:
                continue
            if query and query not in r.haystack:
                continue
            filtered.append(r)
        self.state.rows = filtered
        self.state.selected = _first_selectable(self.state.rows, 0) if self.state.rows else 0
        self.state.scroll_offset = 0
        self.state.detail_scroll = 0

    # ---- width changes ---------------------------------------------------
    def _rebuild_rows_for_width(self) -> None:
        _, w = self._screen_size()
        spec = ColumnSpec.for_width(w)
        if spec == self.state.col_spec:
            return
        self.state.col_spec = spec
        # Preserve currently-selected commit across the rebuild.
        selected_hash = None
        if 0 <= self.state.selected < len(self.state.rows):
            r = self.state.rows[self.state.selected]
            if r.commit is not None:
                selected_hash = r.commit.full_hash
        self.state.all_rows = _build_rows(
            self.state.entries, self.state.remotes, self.state.my_email, spec,
        )
        self._apply_filters()
        if selected_hash:
            for i, r in enumerate(self.state.rows):
                if r.commit and r.commit.full_hash == selected_hash:
                    self.state.selected = i
                    break

    # ---- detail pane -----------------------------------------------------
    def _get_detail(self, full_hash: str) -> git.CommitDetail:
        cache = self.state.detail_cache
        if full_hash not in cache:
            cache[full_hash] = git.get_commit_detail(full_hash, cwd=self.state.repo_root)
        return cache[full_hash]

    def _detail_lines(self, w: int) -> list[list[Segment]]:
        c = self._current_commit()
        if c is None:
            return [[("(no commit selected)", CP.MUTED, 0)]]
        detail = self._get_detail(c.full_hash)
        inner_w = max(20, w - 4)
        lines: list[list[Segment]] = []

        lines.append([
            ("commit ", CP.KEY, 0),
            (c.full_hash, CP.HASH, 0),
        ])
        mine_suffix: list[Segment] = []
        if self.state.my_email and c.author_email.lower() == self.state.my_email:
            mine_suffix = [("  ", CP.DEFAULT, 0), ("(you)", CP.MINE, curses.A_BOLD)]
        lines.append([
            ("author ", CP.KEY, 0),
            (c.author, CP.AUTHOR, 0),
            (f"  <{c.author_email}>", CP.MUTED, 0),
            *mine_suffix,
        ])
        lines.append([
            ("date   ", CP.KEY, 0),
            (c.iso_date, CP.MESSAGE, 0),
            (f"  ({c.rel_date})", CP.DATE, 0),
        ])
        refs = parse_refs(c.refs, self.state.remotes)
        if refs:
            segs: list[Segment] = [("refs   ", CP.KEY, 0)]
            segs.extend(_ref_segments(refs))
            lines.append(segs)

        lines.append([])  # blank separator

        body = detail.body or c.subject
        for raw in _wrap_plain(body, inner_w - 2):
            lines.append([("  " + raw, CP.MESSAGE, 0)])

        if detail.stat_lines:
            lines.append([])
            # Keep the summary line if present (usually last)
            shown = detail.stat_lines[:20]
            for stat in shown:
                lines.append(_stat_segments(stat))
            if len(detail.stat_lines) > len(shown):
                lines.append([
                    (f"  … {len(detail.stat_lines) - len(shown)} more files omitted",
                     CP.MUTED, 0),
                ])
        return lines

    # ---- drawing ---------------------------------------------------------
    def _draw(self) -> None:
        self._rebuild_rows_for_width()
        self.stdscr.erase()
        h, w = self._screen_size()

        if w < 30 or h < 8:
            try:
                self.stdscr.addnstr(0, 0, "terminal too small", w,
                                    curses.color_pair(CP.WARN))
            except curses.error:
                pass
            self.stdscr.refresh()
            return

        self._draw_header(w)
        tree_h, detail_h = self._pane_split()
        tree_top = HEADER_H
        self._ensure_visible(tree_h)
        self._draw_tree(tree_top, tree_h, w)

        if detail_h > 0:
            self._draw_detail_divider(tree_top + tree_h, w)
            self._draw_detail(tree_top + tree_h + 1, detail_h, w)

        self._draw_footer(h, w)
        self.stdscr.refresh()

    def _draw_header(self, w: int) -> None:
        title_segs = [
            ("saba-cli", CP.TITLE, curses.A_BOLD),
            ("  ·  ", CP.MUTED, 0),
            ("git tree", CP.ACCENT, 0),
        ]
        if self.state.only_mine:
            title_segs.extend([
                ("  ·  ", CP.MUTED, 0),
                ("mine only", CP.MINE, curses.A_BOLD),
            ])
        _draw_segments(self.stdscr, 0, 2, w - 2, title_segs)

        st = self.state
        info_segs: list[Segment] = [
            ("repo ",    CP.KEY,     0), (str(st.repo_root), CP.MESSAGE, 0),
            ("   ·   ",  CP.MUTED,   0),
            ("branch ",  CP.KEY,     0), (st.branch,         CP.MESSAGE, 0),
            ("   ·   ",  CP.MUTED,   0),
            ("commits ", CP.KEY,     0), (str(st.total_commits), CP.MESSAGE, 0),
        ]
        if st.shown_commits < st.total_commits:
            info_segs.append(
                (f"  (latest {st.shown_commits})", CP.MUTED, 0)
            )
        _draw_segments(self.stdscr, 1, 2, w - 2, info_segs)

        try:
            self.stdscr.addnstr(2, 0, "─" * w, w, curses.color_pair(CP.MUTED))
        except curses.error:
            pass

    def _draw_tree(self, top: int, height: int, w: int) -> None:
        rows = self.state.rows
        if not rows:
            msg = ("(no matches — press esc to clear filter)" if
                   (self.state.search_query or self.state.only_mine)
                   else "(no commits)")
            try:
                self.stdscr.addnstr(top + 1, 2, msg, w - 2,
                                    curses.color_pair(CP.WARN))
            except curses.error:
                pass
            return

        for i in range(height):
            idx = self.state.scroll_offset + i
            if idx >= len(rows):
                break
            row = rows[idx]
            selected = (idx == self.state.selected and row.selectable)
            y = top + i
            if selected:
                try:
                    self.stdscr.addnstr(y, 0, " " * w, w, curses.A_REVERSE)
                except curses.error:
                    pass
            base = curses.A_REVERSE if selected else 0
            _draw_segments(self.stdscr, y, 0, w, row.segments, base_attr=base)

    def _draw_detail_divider(self, y: int, w: int) -> None:
        # Divider with a small label in the middle.
        label = " details "
        bar = "─"
        try:
            self.stdscr.addnstr(y, 0, bar * w, w, curses.color_pair(CP.MUTED))
        except curses.error:
            pass
        label_x = 2
        try:
            self.stdscr.addnstr(y, label_x, label, w - label_x,
                                curses.color_pair(CP.ACCENT) | curses.A_BOLD)
        except curses.error:
            pass

    def _draw_detail(self, top: int, height: int, w: int) -> None:
        lines = self._detail_lines(w)
        self.state.detail_scroll = max(
            0, min(self.state.detail_scroll, max(0, len(lines) - height))
        )
        for i in range(height):
            idx = self.state.detail_scroll + i
            if idx >= len(lines):
                break
            segs = lines[idx]
            if not segs:
                continue
            _draw_segments(self.stdscr, top + i, 2, w - 3, segs)

        # Scroll hint if content was clipped
        if len(lines) > height:
            hint = f" {self.state.detail_scroll + 1}–{min(len(lines), self.state.detail_scroll + height)}/{len(lines)} "
            try:
                self.stdscr.addnstr(top, max(0, w - len(hint) - 1), hint,
                                    len(hint), curses.color_pair(CP.MUTED))
            except curses.error:
                pass

    def _draw_footer(self, h: int, w: int) -> None:
        divider_y = h - 2
        bar_y = h - 1
        try:
            self.stdscr.addnstr(divider_y, 0, "─" * w, w, curses.color_pair(CP.MUTED))
        except curses.error:
            pass

        st = self.state
        if st.search_mode:
            prompt = "search: "
            segs: list[Segment] = [
                (prompt, CP.ACCENT, curses.A_BOLD),
                (st.search_query, CP.MESSAGE, 0),
                ("▏", CP.ACCENT, curses.A_BOLD),
            ]
            right = "enter: apply   esc: cancel"
            _draw_segments(self.stdscr, bar_y, 2, w - 2, segs)
            right_x = max(w - len(right) - 2, len(prompt) + len(st.search_query) + 6)
            if right_x + len(right) < w:
                _draw_segments(self.stdscr, bar_y, right_x, w - right_x,
                               [(right, CP.MUTED, 0)])
            return

        # Compact / wide footer depending on available columns.
        compact = w < 80
        left_segs: list[Segment] = []
        if st.search_query or st.only_mine:
            bits: list[str] = []
            if st.only_mine:
                bits.append("mine")
            if st.search_query:
                bits.append(f'"{st.search_query}"')
            hits = sum(1 for r in st.rows if r.selectable)
            left_segs = [
                ("filter ", CP.WARN, curses.A_BOLD),
                (" · ".join(bits), CP.MESSAGE, 0),
                (f"  ·  {hits} hit" + ("" if hits == 1 else "s"), CP.MUTED, 0),
            ]
        else:
            if compact:
                left_segs = [
                    ("↑↓ ", CP.ACCENT, curses.A_BOLD), ("nav  ", CP.MUTED, 0),
                    ("f ",  CP.ACCENT, curses.A_BOLD), ("find  ", CP.MUTED, 0),
                    ("m ",  CP.ACCENT, curses.A_BOLD), ("mine  ", CP.MUTED, 0),
                    ("tab ",CP.ACCENT, curses.A_BOLD), ("detail  ", CP.MUTED, 0),
                    ("q ",  CP.ACCENT, curses.A_BOLD), ("quit", CP.MUTED, 0),
                ]
            else:
                left_segs = [
                    ("↑↓ ",  CP.ACCENT, curses.A_BOLD), ("move  ", CP.MUTED, 0),
                    ("f ",   CP.ACCENT, curses.A_BOLD), ("search  ", CP.MUTED, 0),
                    ("m ",   CP.ACCENT, curses.A_BOLD), ("mine  ", CP.MUTED, 0),
                    ("tab ", CP.ACCENT, curses.A_BOLD), ("detail  ", CP.MUTED, 0),
                    ("[ ] ", CP.ACCENT, curses.A_BOLD), ("scroll  ", CP.MUTED, 0),
                    ("g/G ", CP.ACCENT, curses.A_BOLD), ("top/bot  ", CP.MUTED, 0),
                    ("esc/q ", CP.ACCENT, curses.A_BOLD), ("quit", CP.MUTED, 0),
                ]
        _draw_segments(self.stdscr, bar_y, 2, w - 2, left_segs)

        selectable_rows = [r for r in st.rows if r.selectable]
        if selectable_rows:
            sel_pos = 0
            for i in range(min(st.selected, len(st.rows) - 1) + 1):
                if st.rows[i].selectable:
                    sel_pos += 1
            right = f"{sel_pos}/{len(selectable_rows)}"
            right_x = w - len(right) - 2
            if right_x > _segments_width(left_segs) + 4:
                _draw_segments(self.stdscr, bar_y, right_x, len(right) + 1,
                               [(right, CP.MUTED, 0)])

    # ---- main loop -------------------------------------------------------
    def run(self) -> int:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        while True:
            self._draw()
            try:
                key = self.stdscr.get_wch()
            except KeyboardInterrupt:
                return 130
            except curses.error:
                continue
            if self._handle_key(key):
                return 0

    def _handle_key(self, key) -> bool:
        if self.state.search_mode:
            return self._handle_search_key(key)
        return self._handle_nav_key(key)

    def _handle_nav_key(self, key) -> bool:
        st = self.state
        if key == curses.KEY_RESIZE:
            return False
        if isinstance(key, str) and key == "\x1b":   # ESC
            if st.search_query or st.only_mine:
                st.search_query = ""
                st.only_mine = False
                self._apply_filters()
                return False
            return True
        if isinstance(key, str) and key in ("q", "Q"):
            return True
        if key == curses.KEY_UP or key == "k":
            self._move(-1)
        elif key == curses.KEY_DOWN or key == "j":
            self._move(1)
        elif key == curses.KEY_PPAGE:
            tree_h, _ = self._pane_split()
            self._move(-max(1, tree_h - 1))
        elif key == curses.KEY_NPAGE:
            tree_h, _ = self._pane_split()
            self._move(max(1, tree_h - 1))
        elif key == curses.KEY_HOME or key == "g":
            self._jump("top")
        elif key == curses.KEY_END or key == "G":
            self._jump("bottom")
        elif isinstance(key, str) and key in ("f", "F", "/"):
            st.search_mode = True
            st.search_query = ""
            self._apply_filters()
        elif isinstance(key, str) and key in ("m", "M"):
            st.only_mine = not st.only_mine
            self._apply_filters()
        elif key == "\t" or (isinstance(key, str) and key in ("d", "D")):
            st.show_details = not st.show_details
            st.detail_scroll = 0
        elif isinstance(key, str) and key == "[":
            st.detail_scroll = max(0, st.detail_scroll - 1)
        elif isinstance(key, str) and key == "]":
            st.detail_scroll += 1   # clamped during draw
        return False

    def _handle_search_key(self, key) -> bool:
        st = self.state
        if key == curses.KEY_RESIZE:
            return False
        if isinstance(key, str) and key == "\x1b":
            st.search_mode = False
            st.search_query = ""
            self._apply_filters()
            return False
        if key in (curses.KEY_ENTER,) or (isinstance(key, str) and key in ("\n", "\r")):
            st.search_mode = False
            return False
        if key == curses.KEY_BACKSPACE or (isinstance(key, str) and key in ("\x7f", "\x08")):
            st.search_query = st.search_query[:-1]
            self._apply_filters()
            return False
        if isinstance(key, str) and key.isprintable():
            st.search_query += key
            self._apply_filters()
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_tui(args) -> int:
    os.environ.setdefault("ESCDELAY", "25")

    from saba_cli import theme

    try:
        git.ensure_git_available()
    except git.GitError as e:
        print(theme.c_error("error: ") + str(e))
        return 127

    cwd = Path.cwd()
    try:
        repo_root = git.get_repo_root(cwd)
    except git.GitError:
        print(theme.c_error("error: ") + f"not a git repository: {cwd}")
        print(theme.c_muted("hint: run `git init` first, or cd into a repo."))
        return 128

    total = git.count_commits(cwd=repo_root)
    if total == 0:
        print(
            "\n  " + theme.c_title("saba-cli") + theme.c_muted("  ·  ")
            + theme.c_accent("git tree")
            + "\n\n  " + theme.c_warn("This repository has no commits yet.")
            + "\n  " + theme.c_muted(f"Repo: {repo_root}") + "\n"
        )
        return 0

    try:
        entries = git.log_graph(
            limit=args.limit,
            all_branches=args.all_branches,
            cwd=repo_root,
        )
    except git.GitError as e:
        print(theme.c_error("error: ") + str(e))
        return 1

    remotes = git.list_remotes(cwd=repo_root)
    my_email = git.get_user_email(cwd=repo_root)
    branch = git.get_current_branch(cwd=repo_root)

    # Start with a wide assumption — the real ColumnSpec is recomputed
    # from the actual curses window size on the first draw.
    initial_spec = ColumnSpec.for_width(120)
    rows = _build_rows(entries, remotes, my_email, initial_spec)
    shown = sum(1 for r in rows if r.selectable)

    state = AppState(
        repo_root=repo_root,
        branch=branch,
        my_email=my_email,
        total_commits=total,
        shown_commits=shown,
        entries=entries,
        remotes=remotes,
        all_rows=rows,
        rows=list(rows),
        col_spec=initial_spec,
        use_color=not args.no_color,
    )
    state.selected = _first_selectable(state.rows, 0)

    # Start with detail pane off if the user explicitly asked or the
    # terminal looks shorter than ``MIN_USEFUL_ROWS`` — the TreeApp will
    # also auto-hide if space is tight at draw time.
    if getattr(args, "no_details", False):
        state.show_details = False
    if getattr(args, "mine", False):
        state.only_mine = True
        # re-filter once the state is complete
        q = ""
        state.search_query = q
        state.rows = [r for r in state.all_rows if r.selectable and r.is_mine]
        state.selected = _first_selectable(state.rows, 0) if state.rows else 0

    def _main(stdscr):
        _setup_palette(state.use_color)
        app = TreeApp(stdscr, state)
        return app.run()

    return curses.wrapper(_main)
