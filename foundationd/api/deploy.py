"""org.ersa.Foundation1.Deploy: write the operating system to a new disk (the installer's back end).

Listing and planning are read-only workers. Installing is a job holding the `disk` lock; it wipes the
target disk, so it has its own polkit action that always asks, and the caller must repeat the disk
path as the confirmation.
"""

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
        jobs.check_locks(LOCKS)

    def install(call):
        disk, confirm = call.args
        return service.start_job("deploy.run", {"disk": disk, "confirm": confirm}, LOCKS)

    return {
        (DEPLOY, "ListDisks"): Method(disks),
        (DEPLOY, "PlanDeploy"): Method(plan, None, check_plan),
        (DEPLOY, "Deploy"): Method(install, ACTION, check_install),
    }

