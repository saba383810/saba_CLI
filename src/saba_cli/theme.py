"""Iceberg color theme.

Palette reference: https://cocopon.github.io/iceberg.vim/
24-bit (truecolor) ANSI escapes are emitted so the output matches
the original Iceberg palette on any terminal that supports it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"


@dataclass(frozen=True)
class IcebergPalette:
    # Base
    bg: str = "#161821"
    bg_alt: str = "#1e2132"
    fg: str = "#c6c8d1"
    fg_dim: str = "#6b7089"
    fg_bright: str = "#d2d4de"

    # Accent (normal)
    red: str = "#e27878"
    green: str = "#b4be82"
    yellow: str = "#e2a478"
    blue: str = "#84a0c6"
    magenta: str = "#a093c7"
    cyan: str = "#89b8c2"

    # Accent (bright)
    red_b: str = "#e98989"
    green_b: str = "#c0ca8e"
    yellow_b: str = "#e9b189"
    blue_b: str = "#91acd1"
    magenta_b: str = "#ada0d3"
    cyan_b: str = "#95c4ce"


PALETTE = IcebergPalette()


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("SABA_CLI_NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        # Still allow color if FORCE_COLOR is set (useful for piping to less -R)
        return bool(os.environ.get("FORCE_COLOR"))
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return False
    return True


_COLOR_ENABLED = _supports_color()


def set_color_enabled(enabled: bool) -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = enabled


def color_enabled() -> bool:
    return _COLOR_ENABLED


def fg(hex_color: str) -> str:
    if not _COLOR_ENABLED:
        return ""
    r, g, b = _hex_to_rgb(hex_color)
    return f"\x1b[38;2;{r};{g};{b}m"


def bg(hex_color: str) -> str:
    if not _COLOR_ENABLED:
        return ""
    r, g, b = _hex_to_rgb(hex_color)
    return f"\x1b[48;2;{r};{g};{b}m"


def style(text: str, *codes: str) -> str:
    if not _COLOR_ENABLED:
        return text
    prefix = "".join(codes)
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


# Semantic shorthands used by commands
def c_commit(text: str) -> str:
    return style(text, fg(PALETTE.yellow), BOLD)


def c_hash(text: str) -> str:
    return style(text, fg(PALETTE.yellow))


def c_author(text: str) -> str:
    return style(text, fg(PALETTE.cyan))


def c_date(text: str) -> str:
    return style(text, fg(PALETTE.fg_dim))


def c_message(text: str) -> str:
    return style(text, fg(PALETTE.fg))


def c_head(text: str) -> str:
    return style(text, fg(PALETTE.magenta_b), BOLD)


def c_branch_local(text: str) -> str:
    return style(text, fg(PALETTE.green), BOLD)


def c_branch_remote(text: str) -> str:
    return style(text, fg(PALETTE.red))


def c_tag(text: str) -> str:
    return style(text, fg(PALETTE.yellow_b), BOLD)


def c_graph(text: str) -> str:
    return style(text, fg(PALETTE.blue))


def c_muted(text: str) -> str:
    return style(text, fg(PALETTE.fg_dim))


def c_title(text: str) -> str:
    return style(text, fg(PALETTE.blue_b), BOLD)


def c_accent(text: str) -> str:
    return style(text, fg(PALETTE.magenta))


def c_error(text: str) -> str:
    return style(text, fg(PALETTE.red), BOLD)


def c_warn(text: str) -> str:
    return style(text, fg(PALETTE.yellow_b))


def c_ok(text: str) -> str:
    return style(text, fg(PALETTE.green_b))


def c_key(text: str) -> str:
    """Label / key-name color used inside the header banner."""
    return style(text, fg(PALETTE.cyan_b))
