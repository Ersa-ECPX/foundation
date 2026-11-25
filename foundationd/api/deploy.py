"""org.ersa.Foundation1.Deploy: write the operating system to a new disk (the installer's back end).

Listing and planning are read-only workers. Installing is a job holding the `disk` lock; it wipes the
target disk, so it has its own polkit action that always asks, and the caller must repeat the disk
path as the confirmation.
"""

from gi.repository import GLib

from ..backends import preseed as preseed_backend
from ..service import Method
from ..variants import reply_dict, reply_list
from . import validate

DEPLOY = "org.ersa.Foundation1.Deploy"
ACTION = "org.ersa.foundation.deploy"
LOCKS = ("disk",)


def register(service):
    jobs = service.jobs

    def disks(call):
        return service.query(call, "deploy.disks", {}, reply_list)

    def plan(call):
        (disk,) = call.args
        return service.query(call, "deploy.plan", {"disk": disk}, reply_dict)

    def check_plan(call):
        validate.device_path(call.args[0])

    def check_install(call):
        validate.device_path(call.args[0])
        validate.device_path(call.args[1])
        validate.locale_name(call.args[2])
        validate.timezone_name(call.args[3])
        jobs.check_locks(LOCKS)

    def install(call):
        disk, confirm, language, timezone = call.args
        return service.start_job("deploy.run", {"disk": disk, "confirm": confirm, "language": language,
                                                "timezone": timezone}, LOCKS)

    def checked_preseed(text):
        return preseed_backend.parse(text, service.config.schema)

    def check_install_preseeded(call):
        check_install(call)
        checked_preseed(call.args[4])                 # a bad file is refused before anything is erased

    def install_preseeded(call):
        disk, confirm, language, timezone, text = call.args
        return service.start_job("deploy.run", {"disk": disk, "confirm": confirm, "language": language, "timezone": timezone,
                                                "preseed": checked_preseed(text)}, LOCKS)

    def check_preseed(call):
        """What a preseed file would set, or InvalidArgument listing every problem. Reads nothing from the disk; no authorization."""
        result = checked_preseed(call.args[0])
        return GLib.Variant("(a{sv})", ({"config": GLib.Variant("a{ss}", result["config"]), "language": GLib.Variant("s", result["language"]),
                                         "timezone": GLib.Variant("s", result["timezone"]), "hostname": GLib.Variant("s", result["hostname"])},))

    return {
        (DEPLOY, "ListDisks"): Method(disks),
        (DEPLOY, "PlanDeploy"): Method(plan, None, check_plan),
        (DEPLOY, "Deploy"): Method(install, ACTION, check_install),
        (DEPLOY, "DeployPreseeded"): Method(install_preseeded, ACTION, check_install_preseeded),
        (DEPLOY, "CheckPreseed"): Method(check_preseed),
    }

