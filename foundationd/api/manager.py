"""org.ersa.Foundation1.Manager and the Job interface."""

import logging

from gi.repository import GLib

from ..backends import provisioning
from ..errors import AlreadyProvisioned, NotFound
from ..service import Method
from ..variants import reply_dict, reply_list

log = logging.getLogger("foundationd")

MANAGER = "org.ersa.Foundation1.Manager"
JOB = "org.ersa.Foundation1.Job"
ACTION_JOBS = "org.ersa.foundation.jobs"
ACTION_PROVISION = "org.ersa.foundation.provision"


def register(service):
    def get_status(call):
        capabilities = [name for name in service.capabilities if name != "manager"] + ["jobs"]
        status = {
            "provisioned": GLib.Variant("b", service.state.provisioned),
            "developer": GLib.Variant("b", bool(service.config.get("system.developer_mode"))),
            "capabilities": GLib.Variant("as", sorted(capabilities)),
        }
        if service.state.provisioned:
            status["provisioned_at"] = GLib.Variant("s", service.state.provisioned_at)
        return GLib.Variant("(a{sv})", (status,))

    def check_provisioning(call):
        if not service.state.provisioned:
            user = provisioning.evidence(service.paths.passwd, service.paths.group)
            if user and service.state.latch_provisioned():
                log.info("first-boot setup complete (evidence: %s's setup-done file)", user)
        return GLib.Variant("(b)", (service.state.provisioned,))

    def precheck_complete(call):
        if service.state.provisioned:
            raise AlreadyProvisioned("first-boot setup has already been completed")

    def complete_provisioning(call):
        precheck_complete(call)            # time passes while a person types a password
        service.state.latch_provisioned()
        log.info("first-boot setup completed by request")

    def system_info(call):
        return service.query(call, "system.info", {}, reply_dict)

    def base_packages(call):
        return service.query(call, "system.base_packages", {}, reply_list)

    def list_jobs(call):
        return GLib.Variant("(ao)", ([job.path for job in sorted(service.jobs.jobs.values(), key=lambda j: j.id)],))

    def job_for(call):
        job = service.jobs.jobs.get(service._job_id(call.path))
        if job is None:
            raise NotFound("no such job")
        return job

    def cancel(call):
        service.jobs.cancel(job_for(call).id)
        return None

    def logs(call):
        (count,) = call.args
        lines = list(job_for(call).log)
        return GLib.Variant("(as)", (lines[-count:] if count > 0 else lines,))

    return {
        (MANAGER, "GetStatus"): Method(get_status),
        (MANAGER, "GetSystemInfo"): Method(system_info),
        (MANAGER, "ListBasePackages"): Method(base_packages),
        (MANAGER, "ListJobs"): Method(list_jobs),
        (MANAGER, "CheckProvisioning"): Method(check_provisioning),
        (MANAGER, "CompleteProvisioning"): Method(complete_provisioning, ACTION_PROVISION, precheck_complete),
        # Cancelling someone else's job is a denial of service, so it needs administrator rights.
        (JOB, "Cancel"): Method(cancel, ACTION_JOBS),
        (JOB, "Logs"): Method(logs),
    }
