"""Machine state: what this machine has done, as opposed to what an administrator wants.

Stored as JSON in <state_dir>/state.json and written only by foundationd. A missing or unreadable
file means a never-provisioned machine. Provisioning is a one-way latch: once set it is never
cleared by this code (a factory reset wipes the state directory instead).

systemd units cannot read JSON, so latching also creates the marker file
<state_dir>/provisioned, which units test with ConditionPathExists=.
"""

import json
import os
import tempfile
import time

DEFAULTS = {"system.provisioned": False, "system.provisioned_at": ""}


class StateStore:
    def __init__(self, state_dir):
        self.directory = state_dir
        self.path = os.path.join(state_dir, "state.json")
        self.marker = os.path.join(state_dir, "provisioned")

    def read(self):
        data = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except FileNotFoundError:
            return data
        except (OSError, ValueError):
            # An unreadable state file must not be mistaken for "provisioned": keep the defaults.
            return data
        if isinstance(loaded, dict):
            for key, default in DEFAULTS.items():
                if isinstance(loaded.get(key), type(default)):
                    data[key] = loaded[key]
        return data

    @property
    def provisioned(self):
        return bool(self.read()["system.provisioned"])

    @property
    def provisioned_at(self):
        return self.read()["system.provisioned_at"]

    def latch_provisioned(self, now=None):
        """Record that first-boot setup is complete. Returns True if this call changed anything."""
        data = self.read()
        if data["system.provisioned"]:
            return False
        data["system.provisioned"] = True
        data["system.provisioned_at"] = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        os.makedirs(self.directory, mode=0o755, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", dir=self.directory, suffix=".tmp", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, self.path)
        with open(self.marker, "w", encoding="utf-8"):
            pass
        os.chmod(self.marker, 0o644)
        return True
