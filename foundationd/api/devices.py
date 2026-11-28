"""org.ersa.Foundation1.Devices: this computer's link to an Ersa ID account and its daily health check-in (workers: devices.*).

The key is made here (sealed to the TPM when there is one) and only its public half leaves. SignLink proves, to the account service
that hands out a challenge, that this computer holds the key: it signs one fixed form and refuses any other input. The signed-in person's own client registers that key with the account service
(with the person's token, which never reaches this service) and then calls CompleteLink with the id it got. The address of the account
service is the setting devices.url and is not an argument of any method: changing it needs the config authorization, so a call here
cannot send reports somewhere else.
"""

from ..service import Method
from ..variants import reply_dict

DEVICES = "org.ersa.Foundation1.Devices"
ACTION_DEVICES = "org.ersa.foundation.devices"


def register(service):
    def settings():
        return {"enabled": bool(service.config.get("devices.enabled")), "url": service.config.get("devices.url")}

    def status(call):
        return service.query(call, "devices.status", settings(), reply_dict)

    def report(call):
        return service.query(call, "devices.report", {}, reply_dict)

    def prepare_link(call):
        return service.query(call, "devices.prepare_link", {}, reply_dict)

    def sign_link(call):
        (challenge,) = call.args
        return service.query(call, "devices.sign_link", {"challenge": challenge}, reply_dict)

    def complete_link(call):
        (device_id,) = call.args
        return service.query(call, "devices.complete_link", {"id": device_id, "url": service.config.get("devices.url")}, reply_dict)

    def unlink(call):
        return service.query(call, "devices.unlink", {}, reply_dict)

    def checkin_if_due(call):
        return service.query(call, "devices.checkin", dict(settings(), due=True), reply_dict)

    def checkin_now(call):
        return service.query(call, "devices.checkin", dict(settings(), due=False), reply_dict)

    return {
        (DEVICES, "Status"): Method(status),
        (DEVICES, "Report"): Method(report),
        (DEVICES, "PrepareLink"): Method(prepare_link, ACTION_DEVICES),
        (DEVICES, "SignLink"): Method(sign_link, ACTION_DEVICES),
        (DEVICES, "CompleteLink"): Method(complete_link, ACTION_DEVICES),
        (DEVICES, "Unlink"): Method(unlink, ACTION_DEVICES),
        (DEVICES, "CheckinIfDue"): Method(checkin_if_due),
        (DEVICES, "CheckinNow"): Method(checkin_now, ACTION_DEVICES),
    }
