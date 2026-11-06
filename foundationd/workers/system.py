"""system.info and system.base_packages (queries)."""

import platform

from ..backends import osinfo
from ..backends.runner import check
from ..workerlib import worker


@worker("system.info")
def info(ctx):
    release = osinfo.os_release(ctx.paths["os_release"])
    return {
        "os": {
            "name": release.get("NAME"),
            "base": release.get("ECP_BASE") or release.get("PRETTY_NAME"),
            "image_id": release.get("IMAGE_ID"),
            "image_version": release.get("IMAGE_VERSION"),
            "ecp": release.get("IMAGE_ID") == "ecp",
        },
        "architecture": platform.machine(),
        "mounts": [m for target in ("/", "/etc", "/var", "/home")
                   if (m := osinfo.mounts(ctx.paths["mountinfo"]).get(target))],
        "services": osinfo.unit_states(osinfo.SERVICES, ctx.paths["bus"]),
    }


@worker("system.base_packages")
def base_packages(ctx):
    out = check(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\n"], timeout=60)
    rows = sorted(tuple(line.split("\t")) for line in out.splitlines() if line.count("\t") == 2)
    return [{"name": n, "version": v, "vendor": vendor} for n, v, vendor in rows]
