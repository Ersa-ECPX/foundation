"""Terminal output: colour, panels and tables, with plain fallbacks.

Colour is used only on a terminal and never when NO_COLOR is set. Box drawing falls back to
ASCII when the locale is not UTF-8. Nothing here is required for correctness: --json output and
exit codes carry the same information for scripts and graphical clients.
"""

import os
import re
import shutil
import sys
import unicodedata

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CODES = {
    "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "gray": "90",
}


def color_enabled(stream=None):
    stream = stream or sys.stdout
    return (
        hasattr(stream, "isatty") and stream.isatty()
        and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    )


def unicode_enabled(stream=None):
    stream = stream or sys.stdout
    return (getattr(stream, "encoding", None) or "").lower().replace("-", "") == "utf8"


def style(text, *names, stream=None):
    if not color_enabled(stream):
        return str(text)
    codes = ";".join(_CODES[n] for n in names if n in _CODES)
    return f"\x1b[{codes}m{text}\x1b[0m" if codes else str(text)


def visible_len(text):
    """Display width of text, ignoring colour codes and counting wide characters as two."""
    plain = _ANSI.sub("", str(text))
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)


def pad(text, width):
    return str(text) + " " * max(0, width - visible_len(text))


def terminal_width(default=80):
    return max(40, shutil.get_terminal_size((default, 24)).columns)


_BOXES = {
    True: dict(h="─", v="│", tl="╭", tr="╮", bl="╰", br="╯", ml="├", mr="┤", mm="┼", tm="┬", bm="┴", dot="●"),
    False: dict(h="-", v="|", tl="+", tr="+", bl="+", br="+", ml="+", mr="+", mm="+", tm="+", bm="+", dot="*"),
}


def box():
    return _BOXES[unicode_enabled()]


BADGES = {
    "ok": ("green", "OK"), "good": ("green", "good"), "active": ("green", "active"),
    "warn": ("yellow", "warn"), "pending": ("yellow", "pending"),
    "fail": ("red", "FAIL"), "bad": ("red", "bad"), "inactive": ("red", "inactive"),
    "unknown": ("gray", "unknown"),
}


def badge(state, label=None):
    color, default = BADGES.get(state, ("gray", state))
    return style(f"{box()['dot']} {label or default}", color)


def title(text):
    print()
    print(style(text, "bold", "cyan"))
    print(style(box()["h"] * min(terminal_width(), max(visible_len(text), 24)), "gray"))


def section(text):
    print()
    print(style(text, "bold"))


def kv(rows, indent=2):
    """Aligned key/value lines. Values may be pre-styled strings."""
    rows = [(k, v) for k, v in rows if v not in (None, "")]
    if not rows:
        return
    width = max(visible_len(k) for k, _ in rows)
    for k, v in rows:
        print(" " * indent + style(pad(k, width), "gray") + "  " + str(v))


def table(headers, rows, indent=2):
    """A simple table sized to the terminal; long cells are truncated with an ellipsis."""
    if not rows:
        return
    b = box()
    cols = len(headers)
    widths = [max(visible_len(h), *(visible_len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    budget = terminal_width() - indent - 3 * (cols - 1)
    while sum(widths) > budget and max(widths) > 12:
        widths[widths.index(max(widths))] -= 1

    def cell(text, width):
        text = str(text)
        if visible_len(text) <= width:
            return pad(text, width)
        plain = _ANSI.sub("", text)
        return pad(plain[: max(1, width - 1)] + "…", width)

    gap = f" {b['v']} " if unicode_enabled() else "  "
    print(" " * indent + style(gap.join(cell(h, widths[i]) for i, h in enumerate(headers)), "bold"))
    print(" " * indent + style((b["h"] * (sum(widths) + len(gap) * (cols - 1)))[: terminal_width() - indent], "gray"))
    for r in rows:
        print(" " * indent + gap.join(cell(r[i], widths[i]) for i in range(cols)))


def error(message):
    print(style("error:", "bold", "red"), message, file=sys.stderr)


def warn(message):
    print(style("warning:", "bold", "yellow"), message, file=sys.stderr)


def note(message):
    print(style(message, "gray"))


def confirm(question, assume_yes=False):
    """Ask before a change. Without a terminal and without --yes the answer is no."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        error("confirmation required: run on a terminal or pass --yes")
        return False
    try:
        answer = input(f"{style(question, 'bold')} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def confirm_typed(question, expected, assume_yes=False):
    """For destructive operations: the person must type `expected` (for example the slot name)."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        error("confirmation required: run on a terminal or pass --yes")
        return False
    try:
        answer = input(f"{style(question, 'bold')} Type '{expected}' to continue: ").strip()
    except EOFError:
        return False
    return answer == expected


def human_size(count):
    """1536 -> '1.5 KiB'. Binary units, one decimal where it helps."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" or value == int(value) else f"{value:.1f} {unit}"
        value /= 1024


def progress_bar(fraction, width=28):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    block, empty = ("█", "░") if unicode_enabled() else ("#", "-")
    return f"{block * filled}{empty * (width - filled)} {int(fraction * 100):3d}%"
