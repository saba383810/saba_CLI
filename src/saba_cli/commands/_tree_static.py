"""Static renderer for `saba-cli --tree`.

Prints the tree with ANSI color escapes. Used when stdout is not a TTY
(piped output, redirected to a file) or when the user passes ``--static``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from saba_cli import theme
from saba_cli.commands._tree_common import (
    Ref, parse_refs, translate_graph_glyphs, truncate, pad_right,
    ref_label_text,
)
from saba_cli.utils import git


def _render_refs(refs: list[Ref]) -> tuple[str, int]:
    """Return (ANSI-colored text, visible width)."""
    if not refs:
        return "", 0

    parts_colored: list[str] = []
    parts_raw: list[str] = []
    for ref in refs:
        raw = ref_label_text(ref)
        if ref.kind == "head":
            parts_colored.append(theme.c_head(raw))
        elif ref.kind == "local":
            parts_colored.append(theme.c_branch_local(raw))
        elif ref.kind == "remote":
            parts_colored.append(theme.c_branch_remote(raw))
        elif ref.kind == "tag":
            parts_colored.append(theme.c_tag(raw))
        else:
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
        len(raw_joined) + 4,
    )


def _render_header(repo_root: Path, branch: str, total_commits: int, shown: int) -> str:
    title = theme.c_title("saba-cli") + theme.c_muted("  ·  ") + theme.c_accent("git tree")

    def row(label: str, value: str) -> str:
        return "  " + theme.c_key(label.ljust(8)) + theme.c_message(value)

    lines = [
        "",
        "  " + title,
        "",
        row("Repo", str(repo_root)),
        row("Branch", branch),
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


def run_static(args) -> int:
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

    commits = [e for e in entries if isinstance(e, git.Commit)]
    graph_width = max((len(e.graph) for e in entries), default=0)
    hash_width = max((len(c.short_hash) for c in commits), default=7)

    remotes = git.list_remotes(cwd=repo_root)
    refs_list: list[tuple[str, int]] = []
    for c in commits:
        refs_list.append(_render_refs(parse_refs(c.refs, remotes)))
    refs_width = max((w for _, w in refs_list), default=0)

    date_width = max((len(c.rel_date) for c in commits), default=0)
    author_width = min(max((len(c.author) for c in commits), default=0), 20)

    term_cols = shutil.get_terminal_size((120, 24)).columns
    fixed = 2 + graph_width + 2 + hash_width + 2 + refs_width + 2 + date_width + 2 + author_width + 2
    subject_budget = max(20, term_cols - fixed - 1)

    refs_iter = iter(refs_list)
    for entry in entries:
        if isinstance(entry, git.GraphLine):
            raw = pad_right(entry.graph, graph_width)
            print("  " + theme.c_graph(translate_graph_glyphs(raw)))
            continue

        c = entry
        refs_colored, refs_raw_w = next(refs_iter)

        graph_raw = pad_right(c.graph, graph_width)
        hash_raw = pad_right(c.short_hash, hash_width)
        refs_pad = " " * (refs_width - refs_raw_w)
        date_raw = pad_right(c.rel_date, date_width)
        author_raw = pad_right(truncate(c.author, author_width), author_width)
        subject_trim = truncate(c.subject, subject_budget)

        line = (
            "  "
            + theme.c_graph(translate_graph_glyphs(graph_raw))
            + "  " + theme.c_hash(hash_raw)
            + "  " + refs_colored + refs_pad
            + "  " + theme.c_date(date_raw)
            + "  " + theme.c_author(author_raw)
            + "  " + theme.c_message(subject_trim)
        )
        print(line)

    print()
    print(
        "  "
        + theme.c_muted("tip: ")
        + theme.c_muted("run ")
        + theme.c_accent("saba-cli --tree")
        + theme.c_muted(" in a TTY for the interactive viewer (↑↓ nav, f search, esc quit).")
    )
    print()
    return 0
