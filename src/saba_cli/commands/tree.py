"""`saba-cli --tree` — pretty git tree rendered in the Iceberg palette.

Layout (left to right):
    <graph>  <hash>  <refs>  <rel-date>  <author>  <subject>

Column widths are computed from the data so the tree always stays aligned,
and the subject column is truncated to the remaining terminal width with
a single-character ellipsis so one commit never spills onto two rows.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from saba_cli import theme
from saba_cli.utils import git

# Graph glyph rewrites: git's raw graph uses ASCII; we swap the commit marker
# for a filled bullet so commits pop while connectors stay understated.
_GRAPH_GLYPH_MAP = {
    "*": "●",
}

ELLIPSIS = "…"


@dataclass
class Ref:
    kind: str    # "head", "local", "remote", "tag", "other"
    label: str   # display text


def _parse_refs(raw: str) -> list[Ref]:
    """Parse git's %D output into typed refs.

    Example inputs:
        ""
        "HEAD -> main, origin/main, tag: v1.0"
        "HEAD, tag: v0.2"
        "origin/feature/foo"
    """
    refs: list[Ref] = []
    if not raw.strip():
        return refs

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
        elif "/" in piece and not piece.startswith("refs/"):
            # assume "remote/branch" shape
            refs.append(Ref(kind="remote", label=piece))
        else:
            refs.append(Ref(kind="local", label=piece))
    return refs


def _render_refs(refs: list[Ref]) -> tuple[str, int]:
    """Return (colored_text, visible_width). Visible width excludes ANSI."""
    if not refs:
        return "", 0

    parts_colored: list[str] = []
    parts_raw: list[str] = []
    for ref in refs:
        if ref.kind == "head":
            raw = f"HEAD → {ref.label}" if ref.label != "HEAD" else "HEAD"
            parts_colored.append(theme.c_head(raw))
        elif ref.kind == "local":
            raw = ref.label
            parts_colored.append(theme.c_branch_local(raw))
        elif ref.kind == "remote":
            raw = ref.label
            parts_colored.append(theme.c_branch_remote(raw))
        elif ref.kind == "tag":
            raw = f"◆ {ref.label}"
            parts_colored.append(theme.c_tag(raw))
        else:
            raw = ref.label
            parts_colored.append(theme.c_muted(raw))
        parts_raw.append(raw)

    sep_colored = theme.c_muted("·")
    sep_raw = "·"
    colored = (" " + sep_colored + " ").join(parts_colored)
    raw_joined = (" " + sep_raw + " ").join(parts_raw)

    opener_colored = theme.c_muted("(")
    closer_colored = theme.c_muted(")")
    return (
        opener_colored + " " + colored + " " + closer_colored,
        len(raw_joined) + 4,  # "( " + raw + " )"
    )


def _recolor_graph(graph: str) -> str:
    """Replace the commit marker with a bullet and color all graph chars."""
    if not graph:
        return ""
    swapped = "".join(_GRAPH_GLYPH_MAP.get(ch, ch) for ch in graph)
    return theme.c_graph(swapped)


def _visual_width(s: str) -> int:
    # Graph characters we emit are all single-width, and we keep the raw
    # version around explicitly, so a naive len() on the raw string is fine.
    return len(s)


def _truncate(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width == 1:
        return ELLIPSIS
    return text[: max_width - 1] + ELLIPSIS


def _pad_right(text: str, width: int) -> str:
    pad = width - len(text)
    if pad <= 0:
        return text
    return text + (" " * pad)


def _render_header(repo_root: Path, branch: str, total_commits: int, shown: int) -> str:
    title = theme.c_title("saba-cli") + theme.c_muted("  ·  ") + theme.c_accent("git tree")

    def row(label: str, value: str) -> str:
        return "  " + theme.c_key(label.ljust(8)) + theme.c_message(value)

    lines = [
        "",
        "  " + title,
        "",
        row("Repo",    str(repo_root)),
        row("Branch",  branch),
        row("Commits", f"{total_commits}" + (
            theme.c_muted(f"   (showing latest {shown})") if shown < total_commits else ""
        )),
        "",
    ]
    return "\n".join(lines)


def _render_empty_repo(repo_root: Path) -> str:
    return (
        "\n  " + theme.c_title("saba-cli") + theme.c_muted("  ·  ") + theme.c_accent("git tree")
        + "\n\n  " + theme.c_warn("This repository has no commits yet.")
        + "\n  " + theme.c_muted(f"Repo: {repo_root}")
        + "\n"
    )


def run(args) -> int:
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
        print(_render_empty_repo(repo_root))
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

    branch = git.get_current_branch(cwd=repo_root)
    shown = sum(1 for e in entries if isinstance(e, git.Commit))
    print(_render_header(repo_root, branch, total, shown))

    # First pass: compute raw (uncolored) column widths.
    commits = [e for e in entries if isinstance(e, git.Commit)]
    graph_width = max((len(e.graph) for e in entries), default=0)

    hash_width = max((len(c.short_hash) for c in commits), default=7)

    refs_list: list[tuple[str, int]] = []  # (colored, raw_width) per commit
    for c in commits:
        refs_list.append(_render_refs(_parse_refs(c.refs)))
    refs_width = max((w for _, w in refs_list), default=0)

    date_width = max((len(c.rel_date) for c in commits), default=0)
    author_width = max((len(c.author) for c in commits), default=0)
    # Cap author width so one unusually long name doesn't eat the subject column.
    author_width = min(author_width, 20)

    term_cols = shutil.get_terminal_size((120, 24)).columns
    # Layout: "  " + graph + "  " + hash + "  " + refs + "  " + date + "  " + author + "  " + subject
    fixed = 2 + graph_width + 2 + hash_width + 2 + refs_width + 2 + date_width + 2 + author_width + 2
    subject_budget = max(20, term_cols - fixed - 1)

    refs_iter = iter(refs_list)
    for entry in entries:
        if isinstance(entry, git.GraphLine):
            # Connector line: graph only, padded so the rest of the tree stays aligned.
            raw = _pad_right(entry.graph, graph_width)
            print("  " + _recolor_graph(raw))
            continue

        c = entry  # git.Commit
        refs_colored, refs_raw_w = next(refs_iter)

        # Pad raw (uncolored) forms first, then colorize each cell.
        graph_raw = _pad_right(c.graph, graph_width)
        hash_raw = _pad_right(c.short_hash, hash_width)
        refs_pad = " " * (refs_width - refs_raw_w)
        date_raw = _pad_right(c.rel_date, date_width)
        author_trim = _truncate(c.author, author_width)
        author_raw = _pad_right(author_trim, author_width)
        subject_trim = _truncate(c.subject, subject_budget)

        line = (
            "  "
            + _recolor_graph(graph_raw)
            + "  " + theme.c_hash(hash_raw)
            + "  " + refs_colored + refs_pad
            + "  " + theme.c_date(date_raw)
            + "  " + theme.c_author(author_raw)
            + "  " + theme.c_message(subject_trim)
        )
        print(line)

    # Footer hint
    print()
    print(
        "  "
        + theme.c_muted("tip: ")
        + theme.c_muted("use ")
        + theme.c_accent("--limit N")
        + theme.c_muted(" to change history depth, ")
        + theme.c_accent("--no-all")
        + theme.c_muted(" to restrict to the current branch.")
    )
    print()
    return 0
