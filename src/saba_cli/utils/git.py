"""Thin wrapper around the git CLI.

We shell out to git rather than depend on a library so there are no
external Python dependencies. Output is produced with NUL / SOH / STX
delimiters so we can reliably separate fields even when commit messages
contain tabs, pipes, or other graph-looking characters.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FIELD_BEGIN = "\x01"   # Start Of Heading — marks the start of structured commit data
FIELD_SEP = "\x1f"     # Unit Separator — between fields
FIELD_END = "\x02"     # Start Of Text — marks the end of structured commit data


class GitError(RuntimeError):
    pass


@dataclass
class Commit:
    graph: str
    short_hash: str
    full_hash: str
    refs: str          # raw %D string, e.g. "HEAD -> main, origin/main, tag: v1.0"
    author: str
    author_email: str
    rel_date: str      # e.g. "2 hours ago"
    iso_date: str      # e.g. "2026-04-24 09:13:45 +0900"
    subject: str


@dataclass
class GraphLine:
    """A graph-only connector line (no commit payload)."""
    graph: str


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise GitError("`git` was not found on PATH. Install git and try again.")


def run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise GitError(str(e)) from e
    if out.returncode != 0:
        stderr = out.stderr.strip() or out.stdout.strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return out.stdout


def get_repo_root(cwd: Path | None = None) -> Path:
    out = run_git(["rev-parse", "--show-toplevel"], cwd=cwd).strip()
    return Path(out)


def get_current_branch(cwd: Path | None = None) -> str:
    try:
        out = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd).strip()
        return out or "(detached HEAD)"
    except GitError:
        # Detached HEAD — fall back to short hash
        try:
            return "(detached @ " + run_git(["rev-parse", "--short", "HEAD"], cwd=cwd).strip() + ")"
        except GitError:
            return "(no commits yet)"


def log_graph(
    limit: int | None = 100,
    all_branches: bool = True,
    cwd: Path | None = None,
) -> list[Commit | GraphLine]:
    """Parse `git log --graph` output into structured entries.

    Each commit line is produced with a known delimiter envelope so we can
    split the leading graph art from the structured data reliably:

        <graph chars>FIELD_BEGIN<hash>FIELD_SEP<full>FIELD_SEP<refs>FIELD_SEP<author>FIELD_SEP<email>FIELD_SEP<rel>FIELD_SEP<iso>FIELD_SEP<subject>FIELD_END

    Lines without FIELD_BEGIN are pure graph connectors (``|/``, ``|\\``,
    ``| |``, etc.) and are preserved as-is so the tree stays aligned.
    """
    fmt = FIELD_BEGIN + FIELD_SEP.join([
        "%h", "%H", "%D", "%an", "%ae", "%ar", "%ai", "%s",
    ]) + FIELD_END

    args = ["log", "--graph", f"--pretty=format:{fmt}"]
    if all_branches:
        args.append("--all")
    if limit is not None and limit > 0:
        args.append(f"-n{limit}")

    raw = run_git(args, cwd=cwd)
    entries: list[Commit | GraphLine] = []

    for line in raw.splitlines():
        if FIELD_BEGIN not in line:
            # pure connector line — keep verbatim for alignment
            if line.strip() == "":
                continue
            entries.append(GraphLine(graph=line.rstrip()))
            continue

        graph_part, _, payload = line.partition(FIELD_BEGIN)
        payload = payload.rstrip()
        if payload.endswith(FIELD_END):
            payload = payload[: -len(FIELD_END)]

        fields = payload.split(FIELD_SEP)
        # If subject contains FIELD_SEP somehow (shouldn't, but be safe),
        # re-join the tail into subject.
        if len(fields) < 8:
            fields += [""] * (8 - len(fields))
        short_hash, full_hash, refs, author, email, rel_date, iso_date, *rest = fields
        subject = FIELD_SEP.join(rest)

        entries.append(Commit(
            graph=graph_part.rstrip(),
            short_hash=short_hash,
            full_hash=full_hash,
            refs=refs,
            author=author,
            author_email=email,
            rel_date=rel_date,
            iso_date=iso_date,
            subject=subject,
        ))

    return entries


def count_commits(cwd: Path | None = None) -> int:
    try:
        out = run_git(["rev-list", "--all", "--count"], cwd=cwd).strip()
        return int(out or "0")
    except GitError:
        return 0
