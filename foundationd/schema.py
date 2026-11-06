"""The registry of configuration keys: type, default, choices and mutability.

Schema files are JSON objects keyed by the setting name, in a directory read in lexical order (later
files override earlier ones, so a variant of the image can adjust a default). Unknown keys and values
of the wrong type are rejected everywhere else in the daemon by consulting this registry.
"""

import json
import os
from dataclasses import dataclass

from .errors import InvalidArgument, UnknownKey

TYPES = {"boolean": "b", "string": "s", "integer": "i"}


@dataclass(frozen=True)
class Key:
    name: str
    type: str
    default: object
    description: str = ""
    choices: tuple = ()
    writable: bool = True

    @property
    def signature(self):
        return TYPES[self.type]

    def parse(self, text):
        """Convert a config-file string to this key's type, or raise InvalidArgument."""
        text = text.strip()
        if self.type == "boolean":
            lowered = text.lower()
            if lowered in ("true", "yes", "1", "on"):
                return True
            if lowered in ("false", "no", "0", "off"):
                return False
            raise InvalidArgument(f"{self.name}: not a boolean: {text!r}")
        if self.type == "integer":
            try:
                return self.validate(int(text))
            except ValueError:
                raise InvalidArgument(f"{self.name}: not an integer: {text!r}") from None
        return self.validate(text)

    def validate(self, value):
        """Check an already-typed value (from D-Bus) and return it, or raise InvalidArgument."""
        expected = {"boolean": bool, "string": str, "integer": int}[self.type]
        # bool is a subclass of int; an integer key must not accept True.
        if not isinstance(value, expected) or (self.type == "integer" and isinstance(value, bool)):
            raise InvalidArgument(f"{self.name} takes a {self.type}")
        if self.type == "string":
            if "\n" in value or "\r" in value or "\0" in value:
                raise InvalidArgument(f"{self.name}: value must be a single line")
            if self.choices and value not in self.choices:
                raise InvalidArgument(f"{self.name}: must be one of {', '.join(self.choices)}")
        return value

    def format(self, value):
        """Render a value for a config file."""
        if self.type == "boolean":
            return "true" if value else "false"
        return str(value)


class Schema:
    def __init__(self, keys):
        self._keys = dict(keys)

    @classmethod
    def load(cls, directory):
        keys = {}
        if os.path.isdir(directory):
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(directory, filename)
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                for name, spec in data.items():
                    keys[name] = cls._build(name, spec, path)
        return cls(keys)

    @staticmethod
    def _build(name, spec, path):
        kind = spec.get("type")
        if kind not in TYPES:
            raise ValueError(f"{path}: {name}: unknown type {kind!r}")
        key = Key(
            name=name,
            type=kind,
            default=spec["default"],
            description=spec.get("description", ""),
            choices=tuple(spec.get("choices", ())),
            writable=spec.get("writable", True),
        )
        key.validate(key.default)  # a bad default in the image is a packaging bug: fail loudly
        return key

    def get(self, name):
        try:
            return self._keys[name]
        except KeyError:
            raise UnknownKey(f"unknown key: {name}") from None

    def __contains__(self, name):
        return name in self._keys

    def __iter__(self):
        return iter(sorted(self._keys.values(), key=lambda key: key.name))
