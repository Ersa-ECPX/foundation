"""Convert plain Python data (what workers produce as JSON) into D-Bus variants.

The mapping is deliberately small and fixed, so clients can rely on it:

  dict   -> a{sv}   (keys must be strings; values converted recursively; None values are dropped)
  list   -> as      when every item is a string (also for an empty list)
            aa{sv}  when every item is a dict
  str    -> s       bool -> b       int -> x (int64)       float -> d

A list of mixed types is a programming error and raises TypeError: a worker must not produce it.
"""

from gi.repository import GLib


def to_variant(value):
    """A GLib.Variant for `value`; see the module docstring for the mapping."""
    if isinstance(value, bool):  # before int: bool is an int subclass
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("x", value)
    if isinstance(value, float):
        return GLib.Variant("d", value)
    if isinstance(value, str):
        return GLib.Variant("s", value)
    if isinstance(value, dict):
        return dict_variant(value)
    if isinstance(value, (list, tuple)):
        return list_variant(value)
    raise TypeError(f"cannot convert {type(value).__name__} to a D-Bus variant")


def dict_variant(mapping):
    """An a{sv} variant. Keys with a None value are left out (D-Bus has no null)."""
    return GLib.Variant("a{sv}", entries(mapping))


def list_variant(items):
    items = list(items)
    if all(isinstance(item, str) for item in items):
        return GLib.Variant("as", items)
    if all(isinstance(item, dict) for item in items):
        return GLib.Variant("aa{sv}", [entries(item) for item in items])
    raise TypeError("a list must hold only strings or only dictionaries")


def reply_dict(data):
    """The reply body for a method returning a single a{sv}."""
    return GLib.Variant("(a{sv})", (entries(data),))


def reply_list(data):
    """The reply body for a method returning a single aa{sv} (empty lists included)."""
    return GLib.Variant("(aa{sv})", ([entries(item) for item in data],))


def entries(mapping):
    entries = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"dictionary keys must be strings, got {key!r}")
        if item is not None:
            entries[key] = to_variant(item)
    return entries
