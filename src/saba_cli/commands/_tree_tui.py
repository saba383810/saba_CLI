"""Interactive curses TUI for `saba-cli --tree`.

Keys:
    ↑ / k           move selection up
    ↓ / j           move selection down
    PgUp / PgDn     scroll one screen
    Home / g        jump to newest
    End  / G        jump to oldest
    f or /          enter search (incremental filter by hash / author / refs / subject)
    n / N           next / previous match (when a filter is active)
    Enter           (in search) commit the filter and resume navigation
    ESC             in search → cancel; with filter active → clear filter; otherwise → quit
    q / Q           quit

The renderer re-defines terminal color slots to the exact Iceberg palette
when the terminal allows (``can_change_color()``). Falls back to the
closest xterm-256 color, or finally to 8-color ANSI.
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
# Iceberg palette + color pair registry
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


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex_to_rgb_1000(hex_color: str) -> tuple[int, int, int]:
    r, g, b = _hex_to_rgb(hex_color)
    return r * 1000 // 255, g * 1000 // 255, b * 1000 // 255


def _hex_to_xterm256(hex_color: str) -> int:
    r, g, b = _hex_to_rgb(hex_color)
    # Grayscale strip
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
    """Initialise color pairs. When ``use_color`` is False, all pairs map to
    the terminal default so ``A_REVERSE`` still gives us a visible cursor."""
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
    kind: str                     # "commit" | "graph"
    selectable: bool
    segments: list[Segment]
    commit: git.Commit | None = None
    haystack: str = ""            # lowercased search corpus


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


def _build_rows(
    entries: list,
    remotes: list[str],
) -> list[Row]:
    commits = [e for e in entries if isinstance(e, git.Commit)]
    graph_width = max((len(e.graph) for e in entries), default=0)
    hash_width = max((len(c.short_hash) for c in commits), default=7)
    date_width = max((len(c.rel_date) for c in commits), default=0)
    author_width = min(max((len(c.author) for c in commits), default=0), 20)
    refs_widths = [ref_group_width(parse_refs(c.refs, remotes)) for c in commits]
    refs_width = max(refs_widths, default=0)

    rows: list[Row] = []
    commit_iter = iter(zip(commits, refs_widths))
    for entry in entries:
        if isinstance(entry, git.GraphLine):
            graph_raw = pad_right(entry.graph, graph_width)
            rows.append(Row(
                kind="graph",
                selectable=False,
                segments=[(translate_graph_glyphs(graph_raw), CP.GRAPH, 0)],
            ))
            continue

        c = entry
        c_pair, raw_w = next(commit_iter)
        graph_raw = pad_right(c.graph, graph_width)
        hash_raw = pad_right(c.short_hash, hash_width)
        refs_pad = " " * (refs_width - raw_w)
        date_raw = pad_right(c.rel_date, date_width)
        author_raw = pad_right(truncate(c.author, author_width), author_width)

        segs: list[Segment] = [
            (translate_graph_glyphs(graph_raw), CP.GRAPH, 0),
            ("  ", CP.DEFAULT, 0),
            (hash_raw, CP.HASH, 0),
            ("  ", CP.DEFAULT, 0),
        ]
        segs.extend(_ref_segments(parse_refs(c.refs, remotes)))
        if refs_pad:
            segs.append((refs_pad, CP.DEFAULT, 0))
        segs.extend([
            ("  ", CP.DEFAULT, 0),
            (date_raw, CP.DATE, 0),
            ("  ", CP.DEFAULT, 0),
            (author_raw, CP.AUTHOR, 0),
            ("  ", CP.DEFAULT, 0),
            (c.subject, CP.MESSAGE, 0),
        ])

        haystack = " ".join([
            c.short_hash, c.full_hash, c.author, c.author_email, c.refs, c.subject,
        ]).lower()
        rows.append(Row(
            kind="commit",
            selectable=True,
            segments=segs,
            commit=c,
            haystack=haystack,
        ))
    return rows


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    all_rows: list[Row]
    rows: list[Row]
    repo_root: Path
    branch: str
    total_commits: int
    shown_commits: int

    selected: int = 0
    scroll_offset: int = 0

    search_mode: bool = False      # actively typing a query
    search_query: str = ""          # current query (live filter)
    use_color: bool = True

    status: str = ""
    status_kind: str = "muted"      # one of: muted | ok | warn | err


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
    stdscr: "curses._CursesWindow",
    y: int,
    x: int,
    max_w: int,
    segments: list[Segment],
    base_attr: int = 0,
) -> None:
    remaining = max_w
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


class TreeApp:
    HEADER_H = 4       # title + blank + info + divider
    FOOTER_H = 2       # divider + statusline

    def __init__(self, stdscr, state: AppState):
        self.stdscr = stdscr
        self.state = state

    # ---- input helpers ---------------------------------------------------
    def _body_height(self) -> int:
        h, _ = self.stdscr.getmaxyx()
        return max(1, h - self.HEADER_H - self.FOOTER_H)

    def _page_size(self) -> int:
        return max(1, self._body_height() - 1)

    def _clamp_selected(self) -> None:
        rows = self.state.rows
        if not rows:
            self.state.selected = 0
            self.state.scroll_offset = 0
            return
        self.state.selected = max(0, min(self.state.selected, len(rows) - 1))
        # If landed on a non-selectable (graph) line, hop to the nearest selectable.
        if not rows[self.state.selected].selectable:
            # search forward first, then backward
            fwd = next(
                (i for i in range(self.state.selected + 1, len(rows)) if rows[i].selectable),
                None,
            )
            bwd = next(
                (i for i in range(self.state.selected - 1, -1, -1) if rows[i].selectable),
                None,
            )
            if fwd is not None:
                self.state.selected = fwd
            elif bwd is not None:
                self.state.selected = bwd

    def _ensure_visible(self) -> None:
        body = self._body_height()
        if self.state.selected < self.state.scroll_offset:
            self.state.scroll_offset = self.state.selected
        elif self.state.selected >= self.state.scroll_offset + body:
            self.state.scroll_offset = self.state.selected - body + 1
        max_offset = max(0, len(self.state.rows) - body)
        self.state.scroll_offset = max(0, min(self.state.scroll_offset, max_offset))

    def _move(self, delta: int) -> None:
        rows = self.state.rows
        if not rows:
            return
        step = 1 if delta > 0 else -1
        target = self.state.selected + delta
        target = max(0, min(target, len(rows) - 1))
        # Snap to nearest selectable in the direction of motion.
        while 0 <= target < len(rows) and not rows[target].selectable:
            target += step
        if target < 0 or target >= len(rows):
            # fall back to any selectable
            target = _first_selectable(rows, max(0, self.state.selected))
        self.state.selected = target
        self._ensure_visible()

    def _jump(self, where: str) -> None:
        rows = self.state.rows
        if not rows:
            return
        if where == "top":
            self.state.selected = _first_selectable(rows, 0)
        else:  # "bottom"
            for i in range(len(rows) - 1, -1, -1):
                if rows[i].selectable:
                    self.state.selected = i
                    break
        self._ensure_visible()

    # ---- filtering -------------------------------------------------------
    def _apply_filter(self) -> None:
        q = self.state.search_query.strip().lower()
        if not q:
            self.state.rows = list(self.state.all_rows)
        else:
            self.state.rows = [
                r for r in self.state.all_rows
                if r.selectable and q in r.haystack
            ]
        self.state.selected = _first_selectable(self.state.rows, 0) if self.state.rows else 0
        self.state.scroll_offset = 0

    # ---- drawing ---------------------------------------------------------
    def _draw(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self._draw_header(w)
        self._draw_body(h, w)
        self._draw_footer(h, w)
        self.stdscr.refresh()

    def _draw_header(self, w: int) -> None:
        title_segs = [
            ("saba-cli", CP.TITLE, curses.A_BOLD),
            ("  ·  ", CP.MUTED, 0),
            ("git tree", CP.ACCENT, 0),
        ]
        _draw_segments(self.stdscr, 0, 2, w - 2, title_segs)

        st = self.state
        info_segs: list[Segment] = [
            ("Repo ",    CP.KEY,     0), (str(st.repo_root), CP.MESSAGE, 0),
            ("   ·   ",  CP.MUTED,   0),
            ("Branch ",  CP.KEY,     0), (st.branch,         CP.MESSAGE, 0),
            ("   ·   ",  CP.MUTED,   0),
            ("Commits ", CP.KEY,     0), (str(st.total_commits), CP.MESSAGE, 0),
        ]
        if st.shown_commits < st.total_commits:
            info_segs.append(
                (f"   (showing latest {st.shown_commits})", CP.MUTED, 0)
            )
        _draw_segments(self.stdscr, 2, 2, w - 2, info_segs)

        try:
            self.stdscr.addnstr(3, 0, "─" * w, w, curses.color_pair(CP.MUTED))
        except curses.error:
            pass

    def _draw_body(self, h: int, w: int) -> None:
        body_h = self._body_height()
        top = self.HEADER_H
        rows = self.state.rows

        if not rows:
            msg_segs: list[Segment] = [
                ("(no matches)", CP.WARN, 0),
            ]
            _draw_segments(self.stdscr, top + 1, 4, w - 4, msg_segs)
            return

        for i in range(body_h):
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
            _draw_segments(self.stdscr, y, 2, w - 3, row.segments, base_attr=base)

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
                ("▏", CP.ACCENT, curses.A_BOLD),   # fake block cursor
            ]
            right = "enter: apply   esc: cancel"
            _draw_segments(self.stdscr, bar_y, 2, w - 2, segs)
            right_x = max(w - len(right) - 2, len(prompt) + len(st.search_query) + 6)
            if right_x + len(right) < w:
                _draw_segments(self.stdscr, bar_y, right_x, w - right_x,
                               [(right, CP.MUTED, 0)])
            return

        # Nav footer
        if st.search_query:
            hits = sum(1 for r in st.rows if r.selectable)
            left_segs: list[Segment] = [
                ("filter ", CP.WARN, curses.A_BOLD),
                (f"\"{st.search_query}\"", CP.MESSAGE, 0),
                (f"  ·  {hits} hit" + ("" if hits == 1 else "s"), CP.MUTED, 0),
            ]
        else:
            left_segs = [
                ("↑↓ ", CP.ACCENT, curses.A_BOLD), ("move  ", CP.MUTED, 0),
                ("f ",  CP.ACCENT, curses.A_BOLD), ("search  ", CP.MUTED, 0),
                ("g/G ",CP.ACCENT, curses.A_BOLD), ("top/bot  ", CP.MUTED, 0),
                ("esc/q ", CP.ACCENT, curses.A_BOLD), ("quit", CP.MUTED, 0),
            ]
        _draw_segments(self.stdscr, bar_y, 2, w - 2, left_segs)

        # Position indicator on the right
        selectable_rows = [r for r in st.rows if r.selectable]
        if selectable_rows:
            # Which selectable index is currently highlighted?
            sel_idx = 0
            for i, r in enumerate(st.rows):
                if i == st.selected and r.selectable:
                    sel_idx = sum(1 for rr in st.rows[: i + 1] if rr.selectable)
                    break
            right = f"{sel_idx}/{len(selectable_rows)}"
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
            self._ensure_visible()
            return False
        if isinstance(key, str) and key == "\x1b":   # ESC
            if st.search_query:
                st.search_query = ""
                self._apply_filter()
                return False
            return True
        if isinstance(key, str) and key in ("q", "Q"):
            return True
        if key == curses.KEY_UP or key == "k":
            self._move(-1)
        elif key == curses.KEY_DOWN or key == "j":
            self._move(1)
        elif key == curses.KEY_PPAGE:
            self._move(-self._page_size())
        elif key == curses.KEY_NPAGE:
            self._move(self._page_size())
        elif key == curses.KEY_HOME or key == "g":
            self._jump("top")
        elif key == curses.KEY_END or key == "G":
            self._jump("bottom")
        elif isinstance(key, str) and key in ("f", "F", "/"):
            st.search_mode = True
            st.search_query = ""
            curses.curs_set(0)  # we draw our own cursor
            self._apply_filter()
        return False

    def _handle_search_key(self, key) -> bool:
        st = self.state
        if key == curses.KEY_RESIZE:
            return False
        if isinstance(key, str) and key == "\x1b":       # ESC → cancel
            st.search_mode = False
            st.search_query = ""
            self._apply_filter()
            return False
        if key in (curses.KEY_ENTER,) or (isinstance(key, str) and key in ("\n", "\r")):
            st.search_mode = False
            return False
        if key == curses.KEY_BACKSPACE or (isinstance(key, str) and key in ("\x7f", "\x08")):
            st.search_query = st.search_query[:-1]
            self._apply_filter()
            return False
        if isinstance(key, str) and key.isprintable():
            st.search_query += key
            self._apply_filter()
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_tui(args) -> int:
    # Make ESC feel snappy — default is 1s which is miserable for our UX.
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
    rows = _build_rows(entries, remotes)
    branch = git.get_current_branch(cwd=repo_root)
    shown = sum(1 for r in rows if r.selectable)

    state = AppState(
        all_rows=rows,
        rows=list(rows),
        repo_root=repo_root,
        branch=branch,
        total_commits=total,
        shown_commits=shown,
        use_color=not args.no_color,
    )
    state.selected = _first_selectable(state.rows, 0)

    def _main(stdscr):
        _setup_palette(state.use_color)
        app = TreeApp(stdscr, state)
        return app.run()

    return curses.wrapper(_main)
