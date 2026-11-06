"""Layered configuration.

Layers, later wins:
  1. image defaults:  <defaults_dir>/*.conf      (/usr/lib/foundation/defaults.d)
  2. admin overrides: <conf_dir>/*.conf          (/etc/foundation/foundation.conf.d)

Files are systemd-style `key=value` lines; `#` starts a comment. The API writes exactly one file,
`90-api.conf`, in the override directory. Administrators keep their own files, which the API never
touches (a file that sorts after `90-api.conf` therefore wins over the API, by design).

Files are read on every call: they are tiny, and this means an administrator's edit is visible at
once. Reading is tolerant (a typo in an admin file must not take the daemon down: the bad line is
skipped and reported through `warnings`); writing through the API is strict.
"""

import os
import tempfile

from .errors import InvalidArgument, ReadOnly

API_FILE = "90-api.conf"


def parse_conf(text):
    """Yield (key, value) pairs; lines without `=` and comments are skipped."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        yield key.strip(), value.strip()


class ConfigStore:
    def __init__(self, schema, defaults_dir, conf_dir):
        self.schema = schema
        self.defaults_dir = defaults_dir
        self.conf_dir = conf_dir
        self.warnings = []

    # -- reading ---------------------------------------------------------------------------------

    def _layer_files(self):
        for directory in (self.defaults_dir, self.conf_dir):
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if name.endswith(".conf"):
                    yield os.path.join(directory, name)

    def _effective(self):
        values = {key.name: key.default for key in self.schema}
        warnings = []
        for path in self._layer_files():
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as error:
                warnings.append(f"{path}: {error.strerror}")
                continue
            for name, raw in parse_conf(text):
                if name not in self.schema:
                    warnings.append(f"{path}: unknown key {name!r} ignored")
                    continue
                try:
                    values[name] = self.schema.get(name).parse(raw)
                except InvalidArgument as error:
                    warnings.append(f"{path}: {error}; line ignored")
        self.warnings = warnings
        return values

    def get(self, name):
        self.schema.get(name)  # raises UnknownKey
        return self._effective()[name]

    def all(self):
        return self._effective()

    # -- writing ---------------------------------------------------------------------------------

    @property
    def _api_path(self):
        return os.path.join(self.conf_dir, API_FILE)

    def _read_api(self):
        try:
            with open(self._api_path, encoding="utf-8") as handle:
                return {k: v for k, v in parse_conf(handle.read()) if k in self.schema}
        except FileNotFoundError:
            return {}

    def _write_api(self, entries):
        if not entries:
            try:
                os.unlink(self._api_path)
            except FileNotFoundError:
                pass
            return
        os.makedirs(self.conf_dir, mode=0o755, exist_ok=True)
        lines = ["# Written by foundationd (Config.Set); changes here may be overwritten.",
                 "# Put your own overrides in a separate file in this directory.", ""]
        lines += [f"{name}={value}" for name, value in sorted(entries.items())]
        # Write a temporary file in the same directory, flush it to disk, then rename over the
        # target: a power cut leaves either the old file or the new one, never half of one.
        handle = tempfile.NamedTemporaryFile(
            "w", dir=self.conf_dir, prefix=".90-api.", suffix=".tmp", encoding="utf-8", delete=False)
        try:
            with handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, 0o644)
            os.replace(handle.name, self._api_path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except FileNotFoundError:
                pass
            raise
        directory_fd = os.open(self.conf_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def set(self, name, value):
        """Store an override. Returns True if the effective value changed."""
        key = self.schema.get(name)
        if not key.writable:
            raise ReadOnly(f"{name} cannot be changed through the API")
        value = key.validate(value)
        before = self.get(name)
        entries = self._read_api()
        entries[name] = key.format(value)
        self._write_api(entries)
        return self.get(name) != before

    def reset(self, name):
        """Remove the API override. Returns True if the effective value changed."""
        key = self.schema.get(name)
        if not key.writable:
            raise ReadOnly(f"{name} cannot be changed through the API")
        before = self.get(name)
        entries = self._read_api()
        if name not in entries:
            return False
        del entries[name]
        self._write_api(entries)
        return self.get(name) != before
