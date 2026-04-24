"""`saba-cli --tree` — dispatcher.

Picks between the interactive curses TUI and the static ANSI renderer:

  * interactive when stdout is a real TTY (or ``--interactive`` is forced)
  * static when piped / redirected / when ``--static`` is passed
  * static when ``--no-color`` is set (the TUI relies on color to
    differentiate ref kinds; without color it would be essentially
    unreadable, and the static flow already degrades cleanly)
"""

from __future__ import annotations

import sys


def run(args) -> int:
    wants_static = getattr(args, "static", False) or getattr(args, "no_color", False)
    force_tui = getattr(args, "interactive", False)

    use_tui = force_tui or (not wants_static and sys.stdout.isatty())

    if use_tui:
        from saba_cli.commands._tree_tui import run_tui
        return run_tui(args)

    from saba_cli.commands._tree_static import run_static
    return run_static(args)
