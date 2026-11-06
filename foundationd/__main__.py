"""foundationd entry point.

Production: `foundationd` on the system bus, started by D-Bus activation (see data/dbus and
data/systemd). Development and tests: `--bus session`, with directory overrides and the stub
authorizer, none of which are usable on the system bus.
"""

import argparse
import dataclasses
import logging
import os
import shlex
import sys

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio  # noqa: E402

from .auth import PolkitAuthorizer, StubAuthorizer  # noqa: E402
from .config import ConfigStore  # noqa: E402
from .jobs import DirectLauncher  # noqa: E402
from .paths import Paths  # noqa: E402
from .schema import Schema  # noqa: E402
from .service import Service  # noqa: E402
from .state import StateStore  # noqa: E402

PATH_FIELDS = [f.name for f in dataclasses.fields(Paths) if f.name not in ("bus", "allow_file_targets")]
WORKER_COMMAND = "/usr/libexec/foundationd-worker"


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="foundationd", description="Ersa Compute Platform management daemon")
    parser.add_argument("--bus", choices=("system", "session"), default="system")
    parser.add_argument("--idle-timeout", type=int, default=60, metavar="SECONDS",
                        help="exit after this long with no activity; 0 disables (default 60)")
    dev = parser.add_argument_group("development (session bus only)")
    dev.add_argument("--authorizer", choices=("polkit", "allow", "deny"), default="polkit")
    dev.add_argument("--worker-command", default=WORKER_COMMAND,
                     help="command that runs a worker; the job kind is appended")
    defaults = Paths()
    for name in PATH_FIELDS:
        dev.add_argument(f"--{name.replace('_', '-')}-path", dest=name, default=getattr(defaults, name))
    dev.add_argument("--allow-file-targets", action="store_true", help="let Deploy write plain files (tests)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.bus == "system":
        overridden = [n for n in PATH_FIELDS if getattr(args, n) != getattr(defaults, n)]
        if args.authorizer != "polkit" or args.allow_file_targets or overridden or args.worker_command != WORKER_COMMAND:
            parser.error("path, worker and authorizer overrides are for --bus session only")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    if args.bus == "system" and os.geteuid() != 0:
        print("foundationd on the system bus must run as root", file=sys.stderr)
        return 1

    paths = Paths(bus=args.bus, allow_file_targets=args.allow_file_targets, **{name: getattr(args, name) for name in PATH_FIELDS})
    schema = Schema.load(paths.schema)
    with open(paths.xml, encoding="utf-8") as handle:
        xml = handle.read()
    config = ConfigStore(schema, paths.defaults, paths.conf)
    state = StateStore(paths.state)

    bus_type = Gio.BusType.SYSTEM if args.bus == "system" else Gio.BusType.SESSION
    connection = Gio.bus_get_sync(bus_type, None)
    if args.authorizer == "polkit":
        authorizer = PolkitAuthorizer(connection)
    else:
        authorizer = StubAuthorizer(allow=args.authorizer == "allow")

    launcher = DirectLauncher(shlex.split(args.worker_command))
    service = Service(xml, config, state, authorizer, paths=paths, launcher=launcher,
                      idle_timeout=args.idle_timeout)
    return service.run(connection)


if __name__ == "__main__":
    sys.exit(main())
