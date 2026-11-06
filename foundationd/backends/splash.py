"""The boot splash theme: light (the default `ersa` theme) or dark (`ersa-dark`).

Plymouth reads `plymouth.splash=<theme>` from the kernel command line at boot, so the choice lives in the `.cmdline`
of every unified kernel image (UKI) on the ESP: dark adds `plymouth.splash=ersa-dark`, light removes the token. Every
slot is changed, so the choice survives switching slots. Nothing happens until the next boot, because the running
kernel's command line is fixed. A signed UKI cannot be edited without breaking its signature; it is reported, not
changed. Files are rewritten atomically under their own names (the boot counter in a name is kept).
"""

import os

from ..errors import Failed, InvalidArgument, Unavailable
from . import pe, slots
from .runner import run

KEY = "plymouth.splash="
THEMES = {"light": "", "dark": "ersa-dark"}


def token(theme):
    """The command line token for a theme ("" for light)."""
    if theme not in THEMES:
        raise InvalidArgument(f"unknown splash theme {theme!r}; use light or dark")
    return KEY + THEMES[theme] if THEMES[theme] else ""


def apply_theme(text, theme):
    """The command line `text` with its plymouth.splash= token set for `theme`."""
    wanted = token(theme)
    tokens = text.split()
    existing = [t for t in tokens if t.startswith(KEY)]
    if existing == ([wanted] if wanted else []):
        return text
    tokens = [t for t in tokens if not t.startswith(KEY)]
    if wanted:
        tokens.append(wanted)
    return " ".join(tokens)


def theme_of(text):
    """"dark" when the command line selects the dark theme, else "light"."""
    return "dark" if (KEY + THEMES["dark"]) in text.split() else "light"


def _write_atomically(path, data):
    temporary = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _esp(paths):
    layout = slots.discover(paths)
    if not layout.esp:
        raise Unavailable("cannot find the EFI system partition (is it mounted?)")
    return layout


def _ukis(layout):
    try:
        names = sorted(os.listdir(layout.uki_dir))
    except OSError as error:
        raise Unavailable(f"cannot read the kernel images on the EFI system partition: {error.strerror}")
    return [name for name in names if slots.parse_entry(name)]


def set_theme(paths, theme, ctx, runner=run):
    """Set the splash theme in every UKI on the ESP. Returns {"theme", "changed": [files], "skipped": [{file, reason}]}."""
    token(theme)                       # validates
    layout = _esp(paths)
    names = _ukis(layout)
    if not names:
        raise Unavailable("there is no kernel image on the EFI system partition")
    ctx.step("splash", f"Setting the {theme} boot splash")
    changed, skipped, failed = [], [], []
    for index, name in enumerate(names):
        path = os.path.join(layout.uki_dir, name)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            new = pe.rewrite_cmdline(data, lambda text: apply_theme(text, theme))
            if new != data:
                _write_atomically(path, new)
                changed.append(name)
        except Failed as error:
            if "is signed" in str(error):
                skipped.append({"file": name, "reason": str(error)})
            else:
                failed.append(f"{name}: {error}")
        except OSError as error:
            failed.append(f"{name}: {error.strerror}")
        ctx.progress((index + 1) / len(names))
    if failed:
        done = f" ({len(changed)} other kernel image(s) were changed)" if changed else ""
        raise Failed("cannot change the boot splash in " + "; ".join(failed) + done)
    return {"theme": theme, "changed": changed, "skipped": skipped}


def current(paths):
    """The theme the running slot's kernel image selects: {"theme": "light"|"dark"|"unknown", "file"}."""
    layout = slots.discover(paths)
    entry = layout.running.entry if layout.running else None
    if not layout.esp or entry is None:
        return {"theme": "unknown", "file": ""}
    with open(os.path.join(layout.uki_dir, entry.file), "rb") as handle:
        return {"theme": theme_of(pe.read_cmdline(handle.read())), "file": entry.file}
