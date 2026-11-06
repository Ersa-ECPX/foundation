"""Where the daemon and its workers find things on the system.

Production uses the defaults. Tests and development override them (session bus only). Workers
receive `worker_view()` in their request, so they never guess a location on their own.
"""

from dataclasses import dataclass, asdict

PRODUCTION = {
    "xml": "/usr/share/dbus-1/interfaces/org.ersa.Foundation1.xml",
    "schema": "/usr/lib/foundation/schema.d",
    "defaults": "/usr/lib/foundation/defaults.d",
    "conf": "/etc/foundation/foundation.conf.d",
    "state": "/var/lib/foundation",
}


@dataclass
class Paths:
    xml: str = PRODUCTION["xml"]
    schema: str = PRODUCTION["schema"]
    defaults: str = PRODUCTION["defaults"]
    conf: str = PRODUCTION["conf"]
    state: str = PRODUCTION["state"]
    efivars: str = "/sys/firmware/efi/efivars"
    os_release: str = "/usr/lib/os-release"
    mountinfo: str = "/proc/self/mountinfo"
    sysfs: str = "/sys"
    dev: str = "/dev"
    esp: str = ""                # empty: find the mounted EFI system partition
    passwd: str = "/etc/passwd"
    group: str = "/etc/group"
    root: str = "/"              # the filesystem root that extensions are installed into (tests: a temp dir)
    allow_file_targets: bool = False  # tests only: Deploy may write a plain file standing in for a disk
    bus: str = "system"          # which bus workers use for systemd and logind ("session" in tests)

    def worker_view(self):
        """The subset workers need, as plain strings."""
        view = asdict(self)
        for name in ("xml", "schema", "defaults", "conf"):
            del view[name]
        return view
