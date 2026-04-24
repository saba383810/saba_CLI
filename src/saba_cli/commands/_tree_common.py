"""Helpers shared between the static (`_tree_static.py`) and the
interactive (`_tree_tui.py`) renderers.

These are pure data transforms: no printing, no ANSI, no curses —
each renderer decides how to color / lay out the output.
"""

from __future__ import annotations

from dataclasses import dataclass

# Graph glyph rewrites: git's raw graph uses ASCII; we swap the commit
# marker for a filled bullet so commits pop while connectors stay
# understated.
GRAPH_GLYPH_MAP = {
    "*": "●",
}

ELLIPSIS = "…"


@dataclass
class Ref:
    kind: str    # "head" | "local" | "remote" | "tag" | "other"
    label: str   # display text


def parse_refs(raw: str, remotes: list[str]) -> list[Ref]:
    """Parse git's %D output into typed refs.

    Example inputs:
        ""
        "HEAD -> main, origin/main, tag: v1.0"
        "HEAD, tag: v0.2"
        "origin/feature/foo"

    ``remotes`` is used to tell remote-tracking refs apart from local
    branches that simply contain a ``/`` (e.g. ``feature/x``).
    """
    refs: list[Ref] = []
    if not raw.strip():
        return refs

    remote_prefixes = tuple(f"{r}/" for r in remotes)

    for piece in (p.strip() for p in raw.split(",")):
        if not piece:
            continue
        if piece.startswith("HEAD -> "):
            branch = piece[len("HEAD -> "):].strip()
            refs.append(Ref(kind="head", label=branch))
        elif piece == "HEAD":
            refs.append(Ref(kind="head", label="HEAD"))
        elif piece.startswith("tag: "):
            refs.append(Ref(kind="tag", label=piece[len("tag: "):].strip()))
        elif remote_prefixes and piece.startswith(remote_prefixes):
            refs.append(Ref(kind="remote", label=piece))
        else:
            refs.append(Ref(kind="local", label=piece))
    return refs


def translate_graph_glyphs(graph: str) -> str:
    """Replace the commit marker (*) with a filled bullet (●)."""
    if not graph:
        return graph
    return "".join(GRAPH_GLYPH_MAP.get(ch, ch) for ch in graph)


def truncate(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width == 1:
        return ELLIPSIS
    return text[: max_width - 1] + ELLIPSIS


def pad_right(text: str, width: int) -> str:
    pad = width - len(text)
    if pad <= 0:
        return text
    return text + (" " * pad)


def ref_label_text(ref: Ref) -> str:
    """The exact text to display for a given Ref (no color)."""
    if ref.kind == "head":
        return f"HEAD → {ref.label}" if ref.label != "HEAD" else "HEAD"
    if ref.kind == "tag":
        return f"◆ {ref.label}"
    return ref.label


def ref_group_width(refs: list[Ref]) -> int:
    """Total visible width for a rendered (labels + '(' ')' + ' · ' separators) ref group."""
    if not refs:
        return 0
    labels = [ref_label_text(r) for r in refs]
    # "( " + labels joined by " · " + " )"
    return 4 + sum(len(l) for l in labels) + max(0, len(labels) - 1) * 3
