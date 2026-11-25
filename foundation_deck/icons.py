"""Deck's own symbolic icons. The icon themes on people's computers differ, and a missing icon shows as a broken picture, so every
icon Deck draws comes from a file in this package (16x16, recoloured to the text colour). A name that is not bundled falls back to the
theme."""

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbolic")

# the theme's name -> the bundled file's name, so a caller can keep asking for the usual name
ALIASES = {
    "computer-symbolic": "overview", "dialog-information-symbolic": "about", "system-software-update-symbolic": "updates",
    "extension-symbolic": "bundles", "folder-remote-symbolic": "repositories", "package-symbolic": "packages",
    "drive-harddisk-symbolic": "slots", "applications-graphics-symbolic": "appearance", "network-wired-symbolic": "remote",
    "preferences-other-symbolic": "advanced", "document-open-recent-symbolic": "activity", "view-refresh-symbolic": "refresh",
    "user-trash-symbolic": "trash", "window-close-symbolic": "close", "emblem-ok-symbolic": "check",
    "dialog-warning-symbolic": "warning", "pan-up-symbolic": "chevron-up", "pan-down-symbolic": "chevron-down",
    "view-list-symbolic": "tasks", "system-software-install-symbolic": "appsources", "adw-external-link-symbolic": "external",
}


def gicon(name):
    path = os.path.join(DIRECTORY, f"ersa-deck-{ALIASES.get(name, name)}-symbolic.svg")
    if os.path.exists(path):
        return Gio.FileIcon.new(Gio.File.new_for_path(path))
    return Gio.ThemedIcon.new(name)


def image(name, **kwargs):
    picture = Gtk.Image(**kwargs)
    picture.set_from_gicon(gicon(name))
    return picture


def set_image(picture, name):
    picture.set_from_gicon(gicon(name))


def button(name, tip="", css=("flat",), **kwargs):
    """A button that shows only an icon."""
    result = Gtk.Button(child=image(name), tooltip_text=tip, css_classes=list(css), **kwargs)
    if tip:
        result.update_property([Gtk.AccessibleProperty.LABEL], [tip])
    return result
