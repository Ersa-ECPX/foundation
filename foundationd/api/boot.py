"""org.ersa.Foundation1.Boot: boot state, slots, boot selection, slot copy and reboot.

Reading state and changing slots happen in workers (`boot.*`, see workers/boot.py). Rebooting is a
call to systemd-logind, which has a D-Bus API, so no worker and no command line is involved.
"""

import re

from gi.repository import Gio, GLib

from ..errors import Busy, InvalidArgument, Unavailable
from ..service import DEFERRED, Method
from ..variants import reply_dict, reply_list
from . import validate

BOOT = "org.ersa.Foundation1.Boot"
ACTION_BOOT = "org.ersa.foundation.boot"
ACTION_SLOTS = "org.ersa.foundation.slots"
ACTION_SPLASH = "org.ersa.foundation.splash"
LOGIN = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
LOCKS = ("slots",)


def register(service):
    jobs = service.jobs

    def state(call):
        return service.query(call, "boot.state", {}, reply_dict)

    def slots(call):
        return service.query(call, "boot.slots", {}, reply_list)

    def job_method(kind, argnames, validators):
        """Handler and precheck for a job whose arguments are the call's arguments, in order."""
        def check(call):
            for value, validator in zip(call.args, validators):
                validator(value)
            jobs.check_locks(LOCKS)

        def handler(call):
            args = dict(zip(argnames, call.args))
            return service.start_job(kind, args, LOCKS)

        return handler, check

    set_default, check_default = job_method("boot.set_default", ("slot",), (validate.slot,))
    set_next, check_next = job_method("boot.set_next", ("slot",), (validate.slot,))
    release, check_release = job_method("boot.release", ("slot",), (validate.slot,))

    def splash_theme(value):
        if value not in ("light", "dark"):
            raise InvalidArgument(f"not a splash theme: {value!r} (use light or dark)")
        return value

    set_splash, check_splash = job_method("boot.splash", ("theme",), (splash_theme,))

    def logind(method, parameters, then):
        """Call a logind method; then(error_message_or_None) runs when it returns."""
        def done(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                then(error.message)
                return
            then(None)

        service.connection.call(*LOGIN[:2], LOGIN[2], method, parameters, None,
                                Gio.DBusCallFlags.NONE, 30_000, None, done)

    def reboot_handler(firmware):
        def handler(call):
            (force,) = call.args
            running = jobs.running_jobs()
            if running and not force:
                raise Busy(f"{len(running)} job(s) are still running; wait for them or force the reboot")

            def failed(message):
                reason = re.sub(r"^GDBus\.Error:\S+?:\s*", "", message)
                call.fail(Unavailable(f"cannot ask systemd-logind to restart the computer: {reason}"))

            def do_reboot():
                logind("Reboot", GLib.Variant("(b)", (False,)),
                       lambda error: call.reply() if error is None else failed(error))

            if firmware:
                logind("SetRebootToFirmwareSetup", GLib.Variant("(b)", (True,)),
                       lambda error: do_reboot() if error is None else failed(error))
            else:
                do_reboot()
            return DEFERRED
        return handler

    return {
        (BOOT, "GetBootState"): Method(state),
        (BOOT, "GetSlots"): Method(slots),
        (BOOT, "SetDefaultSlot"): Method(set_default, ACTION_BOOT, check_default),
        (BOOT, "SetNextBoot"): Method(set_next, ACTION_BOOT, check_next),
        (BOOT, "ReleaseSlot"): Method(release, ACTION_SLOTS, check_release),
        (BOOT, "SetSplash"): Method(set_splash, ACTION_SPLASH, check_splash),
        (BOOT, "Reboot"): Method(reboot_handler(False), ACTION_BOOT),
        (BOOT, "RebootToFirmware"): Method(reboot_handler(True), ACTION_BOOT),
    }
