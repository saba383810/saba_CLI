"""Main CLI entry point.

The dispatcher is intentionally thin so that adding a new capability later
(e.g. ``saba-cli --status``, ``saba-cli diff …``) is a drop-in addition:
register a handler in ``_DISPATCH`` and an argparse flag/subcommand, and
the rest of the framework (colors, error formatting, exit codes) is reused.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from saba_cli import __version__, theme


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saba-cli",
        description="saba-cli — a personal toolbelt of handy CLI utilities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  saba-cli --tree              show the git tree of the current repo\n"
            "  saba-cli --tree --limit 20   show only the most recent 20 commits\n"
            "  saba-cli --tree --no-all     restrict the graph to the current branch\n"
        ),
    )

    # --- commands (currently one, add more here) ---
    parser.add_argument(
        "--tree",
        action="store_true",
        help="Render the git commit tree for the current repository.",
    )

    # --- shared options ---
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=100,
        metavar="N",
        help="Limit the number of commits shown (default: 100).",
    )
    parser.add_argument(
        "--all",
        dest="all_branches",
        action="store_true",
        default=True,
        help="Include all branches in the tree (default).",
    )
    parser.add_argument(
        "--no-all",
        dest="all_branches",
        action="store_false",
        help="Restrict the tree to the current branch only.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output (NO_COLOR env var is also honored).",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"saba-cli {__version__}",
    )
    return parser


# Handler type: takes parsed args, returns exit code.
_Handler = Callable[[argparse.Namespace], int]


def _dispatch(args: argparse.Namespace) -> int:
    # Import lazily so a broken command module can't stop the help from rendering.
    if args.tree:
        from saba_cli.commands.tree import run as run_tree
        return run_tree(args)

    # No command specified — show a short welcome and usage.
    print()
    print("  " + theme.c_title("saba-cli") + theme.c_muted(f"  ·  v{__version__}"))
    print()
    print("  " + theme.c_message("No command given. Try one of:"))
    print("    " + theme.c_accent("saba-cli --tree") + theme.c_muted("    # pretty git tree"))
    print("    " + theme.c_accent("saba-cli --help") + theme.c_muted("    # full usage"))
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.no_color:
        theme.set_color_enabled(False)

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print()
        print(theme.c_muted("interrupted."))
        return 130


if __name__ == "__main__":
    sys.exit(main())
