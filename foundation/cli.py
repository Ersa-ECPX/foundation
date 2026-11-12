"""The foundation command line: a thin client of the Ersa Compute Platform management service.

Every command talks to foundationd over D-Bus (see client.py); none changes the system itself.
Commands that change something show what will happen, ask first (or take --yes), then follow the
job the service starts and report how it ended. Everything that prints data also has --json.
"""

import argparse
import json
import os
import sys

from . import __version__, ui
from .client import Client, ClientError

PRODUCT = "Ersa Compute Platform"

HINTS = {
    "NotAuthorized": "Administrator rights are needed. Run this from a graphical session and approve the "
                     "prompt, or run it with sudo.",
    "Busy": "Another operation is using the same resource. See what is running with: foundation job list",
    "Unreachable": "Check that the service is installed: systemctl status foundationd",
}


class UserError(Exception):
    """A problem with how the command was used (not a failure of the system)."""


def emit_json(data):
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    print()


# ----------------------------------------------------------------------------- job reporting

class Reporter:
    """Shows a job's progress: a bar on a terminal, plain lines otherwise, JSON lines for scripts."""

    def __init__(self, json_mode=False):
        self.json_mode = json_mode
        self.live = ui.color_enabled() and not json_mode
        self._drawn = False

    def _clear(self):
        if self._drawn:
            sys.stdout.write("\r\x1b[K")
            self._drawn = False

    def __call__(self, kind, *args):
        if self.json_mode:
            keys = {"step": ("id", "title"), "progress": ("fraction",), "message": ("text",), "finished": ("ok", "message")}
            print(json.dumps({"event": kind, **dict(zip(keys[kind], args))}), flush=True)
            return
        if kind == "step":
            self._clear()
            print(f"{ui.style('›', 'cyan')} {args[1]}", flush=True)
        elif kind == "message":
            self._clear()
            print(ui.style(f"  {args[0]}", "gray"), flush=True)
        elif kind == "progress" and self.live:
            sys.stdout.write("\r\x1b[K  " + ui.progress_bar(args[0]))
            sys.stdout.flush()
            self._drawn = True

    def done(self):
        self._clear()


def run_job(client, args, interface, method, signature, *params, fds=None, title=None):
    """Start a job, follow it, print the outcome. Returns (exit code, JobResult or None)."""
    reporter = Reporter(args.json)
    if title and not args.json:
        ui.title(title)
    collector = client.collect()
    try:
        path = client.one(interface, method, signature, *params, fds=fds)
    except BaseException:
        collector.close()
        raise
    if args.no_wait:
        collector.close()
        if args.json:
            emit_json({"job": path})
        else:
            print(f"Started {path}. Follow it with: foundation job show {path.rsplit('/', 1)[1]}")
        return 0, None

    def interrupt(job_path):
        reporter.done()
        try:
            client.cancel(job_path)
            ui.note("Cancelling...")
        except ClientError as error:
            ui.note(f"This cannot be stopped now ({error.message}); it will finish in the background. "
                    f"Follow it with: foundation job show {job_path.rsplit('/', 1)[1]}")

    job = client.wait(path, reporter, interrupt, collector=collector)
    reporter.done()
    if args.json:
        emit_json({"job": job.path, "kind": job.kind, "state": job.state, "result": job.result,
                   "error": job.error_name or None, "message": job.error_message or None})
    elif job.ok:
        print(ui.badge("ok", "done"))
    elif job.state == "cancelled":
        ui.warn("the operation was cancelled")
    else:
        ui.error(job.error_message or "the operation failed")
        if job.error_name in HINTS:
            ui.note(HINTS[job.error_name])
    return (0 if job.ok else 1), job


def sure(args, question, expected=None):
    """Ask before a change. Typed confirmation for destructive operations."""
    if expected:
        ok = ui.confirm_typed(question, expected, args.yes)
    else:
        ok = ui.confirm(question, args.yes)
    if not ok:
        ui.note("Nothing was changed.")
    return ok


# ----------------------------------------------------------------------------- status

def slot_rows(slots):
    rows = []
    for slot in sorted(slots, key=lambda s: s["device"]):
        state = {"running": ui.badge("ok", "running"), "standby": ui.badge("unknown", "standby"),
                 "empty": ui.style("empty", "gray")}.get(slot["state"], slot["state"])
        marks = [m for m, on in (("default", slot.get("default")),) if on]
        tries = "blessed" if slot["tries_left"] < 0 and slot["entry"] else (
            f"{slot['tries_left']} left" if slot["entry"] else "-")
        rows.append([slot["name"], state, slot["entry"] or "-", tries, ", ".join(marks) or "-",
                     slot["device"], ui.human_size(slot["size"])])
    return rows


def print_slots(slots):
    if not slots:
        ui.note("No boot slots were found.")
        return
    ui.table(["Slot", "State", "Boot entry", "Tries", "Default", "Partition", "Size"], slot_rows(slots))


def print_boot(state):
    ui.kv([("Booted through UEFI", "yes" if state["efi"] else ui.style("no (boot selection is unavailable)", "gray")),
           ("Running slot", state["running_slot"] or ui.style("unknown", "gray")),
           ("Boot entry", state["selected_entry"] or None),
           ("Default entry", state["default_entry"] or None)])
    assessment = state["assessment"]
    if assessment == "good":
        ui.kv([("Boot assessment", ui.badge("good", "good: this boot was confirmed"))])
    elif assessment == "pending":
        ui.kv([("Boot assessment", ui.badge("pending", f"pending: {state['tries_left']} tries left"))])
    else:
        ui.kv([("Boot assessment", ui.badge("unknown", "unknown"))])


def optional(client, interface, method):
    """A call whose failure must not stop a status page: returns None and a note instead."""
    try:
        return client.one(interface, method)
    except ClientError as error:
        return error


def cmd_status(client, args):
    status = client.status()
    info = client.one("Manager", "GetSystemInfo")
    state = optional(client, "Boot", "GetBootState")
    slots = optional(client, "Boot", "GetSlots")
    if args.json:
        emit_json({"status": status, "system": info,
                   "boot": None if isinstance(state, ClientError) else state,
                   "slots": None if isinstance(slots, ClientError) else slots})
        return 0
    os_info = info["os"]
    ui.title(os_info.get("name") or PRODUCT)
    if not os_info.get("ecp"):
        ui.warn(f"this is not an {PRODUCT} image; the facts below describe this system anyway")
    image = " ".join(part for part in (os_info.get("image_id"), os_info.get("image_version")) if part)
    ui.kv([("Image", image or None),
           ("Base", os_info.get("base")), ("Architecture", info.get("architecture")),
           ("Provisioned", "yes" if status.get("provisioned") else "no"),
           ("Developer mode", "on" if status.get("developer") else None)])
    ui.section("Boot")
    if isinstance(state, ClientError):
        ui.note(f"  unavailable: {state.message}")
    else:
        print_boot(state)
    ui.section("Slots")
    if isinstance(slots, ClientError):
        ui.note(f"  unavailable: {slots.message}")
    else:
        print_slots(slots)
    ui.section("Storage")
    ui.table(["Mount", "Source", "Type", "Access"],
             [[m["target"], m["source"], m["fstype"], "read-only" if m["readonly"] else "read-write"]
              for m in info["mounts"]])
    ui.section("Services")
    ui.kv([(unit.removesuffix(".service"), ui.badge("active" if value == "active" else "unknown" if value == "unknown" else "inactive", value))
           for unit, value in sorted(info["services"].items())])
    print()
    return 0


# ----------------------------------------------------------------------------- slots and boot

def cmd_slot_status(client, args):
    state, slots = client.one("Boot", "GetBootState"), client.one("Boot", "GetSlots")
    if args.json:
        emit_json({"boot": state, "slots": slots})
        return 0
    ui.title("Boot slots")
    print_boot(state)
    ui.section("Slots")
    print_slots(slots)
    print()
    return 0


def find_slot(slots, name):
    for slot in slots:
        if name in (slot["name"], slot["label"] if "label" in slot else None):
            return slot
    raise UserError(f"no slot named '{name}' (see: foundation slot status)")


def cmd_slot_boot(client, args):
    slots = client.one("Boot", "GetSlots")
    slot = find_slot(slots, args.slot)
    if slot["state"] == "empty" or not slot["bootable"]:
        raise UserError(f"slot {slot['name']} has no kernel image to boot")
    when = "at the next boot only" if args.once else "from now on"
    ui.title(f"Boot slot {slot['name']} {when}")
    ui.kv([("Kernel image", slot["entry"]), ("Partition", slot["device"])])
    print()
    if not sure(args, f"Boot slot {slot['name']} {when}?"):
        return 1
    code, _job = run_job(client, args, "Boot", "SetNextBoot" if args.once else "SetDefaultSlot", "(s)", slot["name"])
    if code == 0 and not args.json and not args.once:
        ui.note("Takes effect at the next reboot. A new slot is tried a few times and the system returns "
                "to the previous one if it does not start.")
    return code


def cmd_slot_release(client, args):
    slots = client.one("Boot", "GetSlots")
    slot = find_slot(slots, args.slot)
    if slot["running"]:
        raise UserError("the running slot cannot be released")
    if slot["state"] == "empty":
        ui.note(f"Slot {slot['name']} is already empty.")
        return 0
    ui.title(f"Release slot {slot['name']}")
    ui.note("The slot is marked empty and its kernel image is removed, so updates can use it again. "
            "Its contents are not erased, but it can no longer boot.")
    print()
    if not sure(args, f"Release slot {slot['name']}?", expected=slot["name"]):
        return 1
    return run_job(client, args, "Boot", "ReleaseSlot", "(s)", slot["name"])[0]


def cmd_splash(client, args):
    code, job = run_job(client, args, "Boot", "SetSplash", "(s)", args.theme)
    if job is not None and job.ok and not args.json:
        changed, skipped = job.result.get("changed", []), job.result.get("skipped", [])
        ui.kv([("Boot splash", args.theme),
               ("Changed", ", ".join(changed) or "nothing (already set)")])
        for item in skipped:
            ui.warn(f"{item['file']} was not changed: {item['reason']}")
        ui.note("The boot splash changes at the next restart.")
    return code


def cmd_reboot(client, args):
    what = "into the firmware setup" if args.firmware else "now"
    if not sure(args, f"Restart the computer {what}?"):
        return 1
    client.call("Boot", "RebootToFirmware" if args.firmware else "Reboot", "(b)", bool(args.force))
    if args.json:
        emit_json({"rebooting": True})
    else:
        print(ui.badge("ok", "restarting"))
    return 0


# ----------------------------------------------------------------------------- remote access

def cmd_remote_status(client, args):
    status = client.one("Remote", "GetStatus")
    if args.json:
        emit_json(status)
        return 0
    ui.title("Remote access")
    if not status.get("installed"):
        ui.warn("The SSH server is not part of this system image.")
        return 0
    if status.get("active"):
        state = ui.badge("ok", "on")
    else:
        state = "off"
    password = status.get("password_auth")
    login = "not set up by foundation" if password is None else ("keys or password" if password else "keys only")
    firewall = {"open": ui.badge("ok", "open"), "closed": ui.badge("warn", "closed"), "unknown": "unknown"}.get(status.get("firewall"), "unknown")
    ui.kv([("SSH server", state), ("Starts at boot", "yes" if status.get("enabled") else "no"), ("Login", login),
           ("Firewall", firewall), ("Keys", f"{status.get('keys', 0)}"
                                   + (f" ({', '.join(status.get('users_with_keys', []))})" if status.get("users_with_keys") else ""))])
    print()
    return 0


def cmd_remote_ssh_on(client, args):
    if args.allow_password and not args.json:
        ui.warn("Anyone who can reach this computer on the network will be able to try passwords. Keys are safer.")
    if not sure(args, "Turn the SSH server on?" + (" (passwords allowed)" if args.allow_password else " (keys only)")):
        return 1
    code, job = run_job(client, args, "Remote", "SetSsh", "(bb)", True, bool(args.allow_password))
    if job is not None and job.ok and not args.json:
        firewall = job.result.get("firewall")
        if firewall == "closed":
            ui.warn("The firewall still blocks SSH, so other computers cannot connect. Open it with: "
                    "firewall-cmd --permanent --add-service=ssh && firewall-cmd --reload")
        elif firewall == "opened":
            ui.note("The firewall was opened for SSH.")
        ui.note("Connect with: ssh <user>@<this computer>")
    return code


def cmd_remote_ssh_off(client, args):
    if not sure(args, "Turn the SSH server off?"):
        return 1
    return run_job(client, args, "Remote", "SetSsh", "(bb)", False, False)[0]


def _key_owner(args):
    if args.user:
        return args.user
    import getpass
    name = getpass.getuser()
    if os.geteuid() == 0 and not os.environ.get("SUDO_USER"):
        raise UserError("say whose key this is: --user NAME")
    return os.environ.get("SUDO_USER") or name


def _read_key(argument):
    if argument == "-":
        text = sys.stdin.read(8193)
    elif os.path.isfile(argument):
        with open(argument, encoding="utf-8") as handle:
            text = handle.read(8193)
    else:
        text = argument
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise UserError("give exactly one public key (a .pub file, the key itself, or - for standard input)")
    return lines[0].strip()


def cmd_remote_key_list(client, args):
    keys = client.one("Remote", "ListKeys", "(s)", args.user or "")
    if args.json:
        emit_json(keys)
        return 0
    ui.title("SSH keys that can log in")
    if not keys:
        ui.note("No keys yet. Add one with: foundation remote key add KEYFILE")
        return 0
    ui.table(["User", "Type", "Fingerprint", "Comment"],
             [[k["user"], k["type"], k["fingerprint"], k.get("comment", "") + (" (has options)" if k.get("options") else "")] for k in keys])
    print()
    return 0


def cmd_remote_key_add(client, args):
    user = _key_owner(args)
    key = _read_key(args.key)
    return run_job(client, args, "Remote", "AddKey", "(ss)", user, key)[0]


def cmd_remote_key_remove(client, args):
    if not sure(args, f"Remove that key from {args.user}?"):
        return 1
    return run_job(client, args, "Remote", "RemoveKey", "(ss)", args.user, args.fingerprint)[0]


# ----------------------------------------------------------------------------- updates

def update_url_allowed(url):
    """https from anywhere; plain http only from this machine or a private network (a test server). The update is
    verified by its signature either way, but an unsigned one must not travel over the open internet unprotected."""
    import ipaddress
    import urllib.parse
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    if parts.scheme != "http" or not parts.hostname:
        return False
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts.hostname == "localhost"
    return address.is_private or address.is_loopback


def fetch_update(url, directory, limit=64 * 1024 ** 3, progress=None):
    """Download an update archive to a new file in `directory`. Returns its path."""
    import tempfile
    import urllib.error
    import urllib.request
    try:
        response = urllib.request.urlopen(url, timeout=60)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UserError(f"cannot download {url}: {getattr(error, 'reason', error)}") from error
    total = int(response.headers.get("Content-Length") or 0)
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix="update-", suffix=".tar.zst", delete=False)
    done = 0
    try:
        with handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise UserError("the update is larger than the allowed size")
                handle.write(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        os.unlink(handle.name)
        raise
    return handle.name


def cmd_update_install(client, args):
    path = args.archive
    downloaded = None
    if path.startswith(("http://", "https://")):
        if not update_url_allowed(path):
            raise UserError("an update is downloaded over https (plain http only from this computer or a private network)")
        if not args.json:
            ui.note(f"Downloading {path} ...")
        downloaded = path = fetch_update(path, "/var/tmp")
    try:
        return _install_update_file(client, args, path)
    finally:
        if downloaded:
            os.unlink(downloaded)


def _install_update_file(client, args, path):
    if not args.json:
        ui.title("Install a system update")
        ui.kv([("Update file", path), ("Signature", "not checked (you said this image is your own)" if args.unsigned
                                                     else "must be signed by a trusted image signing key")])
        print()
        ui.note("The new system is written to the spare slot; the running system is not changed and keeps working. Your "
                "bundles are rebuilt for the new system, and it starts on the next restart. If it does not start properly, "
                "the computer returns to the system that worked.")
        print()
    if not sure(args, "Install this update?"):
        return 1
    fds, index = open_rpm(path)
    code, job = run_job(client, args, "Updates", "Install", "(hsbb)", index, args.slot or "", bool(args.unsigned), bool(args.force), fds=fds)
    if job is not None and job.ok and not args.json:
        result = job.result
        ui.kv([("Version", result["version"]), ("Installed in", f"slot {result['slot']}"),
               ("Bundles", ", ".join(result["bundles"]["ready"]) or "none to rebuild")])
        for note in result.get("notes", []):
            ui.warn(note)
        print()
        print(ui.badge("ok", f"update {result['version']} is ready"))
        ui.note("Restart to start using it: foundation reboot")
    return code


def cmd_update_status(client, args):
    waiting = client.one("Updates", "Pending")
    if args.json:
        emit_json(waiting)
        return 0
    if not waiting:
        ui.note("No update is waiting for a restart.")
        return 0
    print(ui.badge("ok", f"update {waiting['version']} is installed"))
    ui.note("Restart to start using it: foundation reboot")
    return 0


# ----------------------------------------------------------------------------- deploy

def cmd_deploy_list(client, args):
    disks = client.one("Deploy", "ListDisks")
    if args.json:
        emit_json(disks)
        return 0
    if not disks:
        ui.note("No disks were found.")
        return 0
    ui.title("Disks")
    rows = []
    for disk in disks:
        status = ui.style("available", "green") if disk["usable"] else ui.style(disk.get("reason", "unusable"), "gray")
        rows.append([disk["path"], ui.human_size(disk["size"]), disk.get("model") or "-",
                     "removable" if disk["removable"] else "fixed", status])
    ui.table(["Disk", "Size", "Model", "Type", "Status"], rows)
    return 0


def cmd_deploy_install(client, args):
    plan = client.one("Deploy", "PlanDeploy", "(s)", args.disk)
    if args.json and args.dry_run:
        emit_json(plan)
        return 0 if plan["possible"] else 1
    ui.title(f"Install {PRODUCT} {plan.get('version', '')} to {plan['disk']}".replace("  ", " "))
    ui.kv([("Disk", f"{plan['disk']}  ({ui.human_size(plan['size'])}{', ' + plan['model'] if plan.get('model') else ''})")])
    print()
    ui.note("Partitions that will be created:")
    ui.table(["Partition", "Size", ""], [[p["label"], ui.human_size(p["size"]) if p["size"] else "the rest", p["note"]]
                                          for p in plan["partitions"]])
    print()
    for problem in plan["problems"]:
        ui.warn(problem)
    if not plan["possible"]:
        raise UserError(f"cannot install to {plan['disk']}")
    erased = plan.get("will_erase", [])
    ui.warn(f"everything on {plan['disk']} will be erased and cannot be recovered"
            + (f" ({len(erased)} existing partition(s))" if erased else ""))
    print()
    if args.dry_run:
        ui.note("Dry run: nothing was changed.")
        return 0
    if not sure(args, f"Erase {plan['disk']} and install {PRODUCT}?", expected=plan["disk"]):
        return 1
    code, job = run_job(client, args, "Deploy", "Deploy", "(ssss)", plan["disk"], plan["disk"],
                        args.language or "", args.timezone or "")
    if job is not None and job.ok and not args.json:
        ui.kv([("Installed", f"version {job.result.get('version')} ({ui.human_size(job.result.get('bytes', 0))})")])
        ui.note("Remove the installation medium and restart the computer to start the new system.")
    return code


# ----------------------------------------------------------------------------- applications

def need_remote_hint(error):
    if error.name == "Unavailable" and "remote" in error.message:
        error.message += "; set it up with: foundation apps setup"
    return error


def cmd_apps_list(client, args):
    apps = client.one("Apps", "ListInstalled")
    if args.json:
        emit_json(apps)
        return 0
    if not apps:
        ui.note("No applications installed.")
        return 0
    ui.title("Installed applications")
    ui.table(["ID", "Name", "Version", "Origin"], [[a["id"], a["name"], a["version"], a["origin"]] for a in apps])
    print()
    return 0


def cmd_apps_search(client, args):
    try:
        results = client.one("Apps", "Search", "(s)", args.term, timeout=120_000)
    except ClientError as error:
        raise need_remote_hint(error)
    if args.json:
        emit_json(results)
        return 0
    if not results:
        ui.note(f"No applications match '{args.term}'.")
        return 0
    ui.title(f"Applications matching '{args.term}'")
    ui.table(["ID", "Name", "Description"], [[r["id"], r["name"], r["description"]] for r in results])
    print()
    return 0


def cmd_apps_info(client, args):
    try:
        info = client.one("Apps", "GetInfo", "(s)", args.app, timeout=120_000)
    except ClientError as error:
        raise need_remote_hint(error)
    if args.json:
        emit_json(info)
        return 0
    ui.title(args.app)
    ui.kv(list(info.items()))
    print()
    return 0


def cmd_apps_install(client, args):
    info = {}
    try:
        info = client.one("Apps", "GetInfo", "(s)", args.app, timeout=120_000)
    except ClientError as error:
        if error.name == "NotFound":
            raise
    ui.title(f"Install {args.app}")
    ui.kv([(k, v) for k, v in info.items() if k in ("Ref", "Version", "License", "Download", "Installed")]
          + [("Scope", "system-wide, stored in /var/lib/flatpak")])
    print()
    if not sure(args, "Install this application?"):
        return 1
    return run_job(client, args, "Apps", "Install", "(s)", args.app)[0]


def cmd_apps_remove(client, args):
    installed = {a["id"]: a for a in client.one("Apps", "ListInstalled")}
    if args.app not in installed:
        raise UserError(f"{args.app} is not installed")
    ui.title(f"Remove {args.app}")
    ui.kv([("Name", installed[args.app]["name"]), ("Version", installed[args.app]["version"])])
    ui.note("Application data under ~/.var/app is kept.")
    print()
    if not sure(args, "Remove this application?"):
        return 1
    return run_job(client, args, "Apps", "Remove", "(s)", args.app)[0]


def cmd_apps_update(client, args):
    if not sure(args, "Update all system-wide applications?"):
        return 1
    code, job = run_job(client, args, "Apps", "Update", None)
    if job is not None and job.ok and not args.json:
        updated = job.result.get("updated", [])
        ui.note("Updated: " + ", ".join(updated) if updated else "Everything is up to date.")
    return code


def cmd_apps_setup(client, args):
    ui.title("Set up the application source")
    ui.kv([("Remote", "flathub"), ("Scope", "system-wide")])
    print()
    if not sure(args, "Add the Flathub remote?"):
        return 1
    return run_job(client, args, "Apps", "SetupRemote", None)[0]


# ----------------------------------------------------------------------------- compat RPMs

def open_rpm(path):
    """Open an RPM file as the *caller* and return a descriptor list for D-Bus: the service never
    opens a path chosen by an unprivileged user."""
    from gi.repository import Gio
    try:
        if not os.path.isfile(path):
            raise UserError(f"{path} is not a file")
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as error:
        raise UserError(f"cannot open {path}: {error.strerror}") from error
    fds = Gio.UnixFDList.new()
    index = fds.append(fd)
    os.close(fd)
    return fds, index


def print_report(report):
    notes = report.get("notes") or []
    if not report["compatible"]:
        verdict = ui.badge("fail", "not compatible")
    elif notes:
        verdict = ui.badge("ok", "compatible, with notes below")
    else:
        verdict = ui.badge("ok", "compatible with this system")
    ui.kv([("Package", f"{report['name']} {report['version']}-{report['release']}  ({report['arch']})"),
           ("Files to install", str(report["files"])), ("Verdict", verdict)])
    # What stops an install, then what is only worth knowing.
    for title, key in (("Requirements the base does not provide", "missing_requires"),
                       ("Shared libraries that are not available", "missing_libraries"),
                       ("Files that would replace files of the base", "conflicts")):
        items = report.get(key) or []
        if items:
            ui.section(title)
            for item in items[:15]:
                print(f"  {item}")
            if len(items) > 15:
                ui.note(f"  ... and {len(items) - 15} more")
    if notes:
        ui.section("Notes")
        for note in notes:
            print(f"  {note}")
        for title, key in (("Libraries used only by plugins, not available", "optional_libraries"),
                           ("Files that will not be installed", "outside_usr")):
            items = report.get(key) or []
            if items:
                ui.note(f"  {title}:")
                for item in items[:8]:
                    ui.note(f"    {item}")
                if len(items) > 8:
                    ui.note(f"    ... and {len(items) - 8} more")


def inspect(client, path):
    fds, index = open_rpm(path)
    return client.one("Compat", "Inspect", "(h)", index, fds=fds)


def cmd_compat_inspect(client, args):
    report = inspect(client, args.rpm)
    if args.json:
        emit_json(report)
    else:
        ui.title(f"Check {os.path.basename(args.rpm)}")
        print_report(report)
        print()
    return 0 if report["compatible"] else 1


def _install_rpm_file(client, args, path):
    report = inspect(client, path)
    if not args.json:
        ui.title(f"Install {report['name']} as an extension")
        print_report(report)
        print()
        ui.note("The package is unpacked without running its scriptlets and overlaid on /usr. "
                "Remove it at any time with: foundation compat remove " + report["name"])
        print()
    if not report["compatible"] and not args.force:
        raise UserError("this package does not fit the system as it is; add --force to install it anyway")
    if not sure(args, f"Install {report['name']}?"):
        return 1
    fds, index = open_rpm(path)
    return run_job(client, args, "Compat", "Install", "(hb)", index, bool(args.force), fds=fds)[0]


def _describe_result(job, args):
    if job is None or not job.ok or args.json:
        return
    packages = job.result.get("packages", [])
    ui.kv([("Bundle", job.result.get("name", "")), ("Packages", f"{len(packages)} ({job.result.get('files', 0)} files)")])
    if job.result.get("skipped"):
        ui.note("already in the base image, left out: " + ", ".join(job.result["skipped"]))
    for note in job.result.get("notes", []):
        ui.warn("Not done: " + note)
    ui.note("Everything in a bundle is one system extension: remove it, or part of it, with `foundation compat remove`.")


def _install_catalog_entry(client, args, name):
    entries = {e["name"]: e for e in client.one("Compat", "ListCatalog")}
    if name not in entries:
        raise UserError(f"no catalog bundle named {name}; see: foundation compat catalog")
    entry = entries[name]
    if not args.json:
        ui.title(f"Install {entry['title']}")
        print(entry["description"])
        print()
        if entry["notice"]:
            ui.warn(entry["notice"])
            print()
        ui.note(f"{len(entry['packages'])} packages from: {', '.join(entry['repos']) or 'the enabled repositories'}")
        if entry["needs_enabling"]:
            ui.warn("This turns on the repository " + ", ".join(entry["needs_enabling"]) + ".")
        ui.note("Signed packages are downloaded and checked, unpacked without running their scriptlets, and "
                "overlaid on /usr as one extension.")
        print()
    if not sure(args, f"Install {entry['title']}?"):
        return 1
    code, job = run_job(client, args, "Compat", "InstallCatalog", "(ssb)", name, args.into or "", True)
    _describe_result(job, args)
    return code


def _install_packages(client, args, names):
    bundle = args.into or "extras"
    if not args.json:
        ui.title(f"Install {', '.join(names)}")
        ui.note(f"Into the bundle {bundle}: one system extension for many packages, built from the signed repositories.")
        print()
    if not sure(args, f"Add {', '.join(names)} to the bundle {bundle}?"):
        return 1
    code, job = run_job(client, args, "Compat", "InstallPackages", "(sasas)", bundle, names, [])
    _describe_result(job, args)
    return code


def cmd_compat_install(client, args):
    targets = args.targets
    files = [t for t in targets if t.endswith(".rpm") or os.path.isfile(t)]
    if files:
        if len(targets) != 1:
            raise UserError("install an RPM file on its own, or give package names; not both")
        return _install_rpm_file(client, args, targets[0])
    catalog = [t for t in targets if t.startswith("@")]
    if catalog:
        if len(targets) != 1:
            raise UserError("install one catalog bundle at a time, for example: foundation compat install @codecs")
        return _install_catalog_entry(client, args, targets[0][1:])
    return _install_packages(client, args, targets)


def cmd_compat_catalog(client, args):
    entries = client.one("Compat", "ListCatalog")
    if args.json:
        emit_json(entries)
        return 0
    if not entries:
        ui.note("The catalog is empty.")
        return 0
    ui.title("Ready-made bundles")
    ui.table(["Install with", "What it is", "State"],
             [[f"@{e['name']}", e["title"], ui.badge("ok", "installed") if e["installed"] else "-"] for e in entries])
    print()
    ui.note("foundation compat install @NAME shows what it contains and asks before installing.")
    return 0


def cmd_compat_bundles(client, args):
    items = client.one("Compat", "ListBundles")
    if args.json:
        emit_json(items)
        return 0
    if not items:
        ui.note("No bundles. Create one with: foundation compat install PACKAGE... or foundation compat install @codecs")
        return 0
    for item in items:
        ui.title(f"{item['name']}" + (f"  (from the catalog: {item['catalog']})" if item["catalog"] else ""))
        ui.kv([("Asked for", ", ".join(item["requested"])), ("Repositories", ", ".join(item["repos"])),
               ("Packages", f"{len(item['packages'])}, {item['files']} files"),
               ("State", ui.badge("ok", "enabled") if item["enabled"] else ui.badge("warn", "disabled"))])
        print()
    return 0


def cmd_compat_update(client, args):
    if not sure(args, "Update " + (f"the bundle {args.bundle}" if args.bundle else "all bundles") + " to the newest packages?"):
        return 1
    code, job = run_job(client, args, "Compat", "UpdateBundles", "(s)", args.bundle or "")
    if job is not None and job.ok and not args.json:
        for result in job.result.get("bundles", []):
            if result.get("changed"):
                ui.note(f"{result['name']}: updated " + ", ".join(result["changed"]))
            else:
                ui.note(f"{result['name']}: already up to date")
        if not job.result.get("bundles"):
            ui.note("There are no bundles to update.")
    return code


def cmd_compat_refresh(client, args):
    code, job = run_job(client, args, "Compat", "RefreshBundles", None)
    if job is not None and job.ok and not args.json:
        for result in job.result.get("bundles", []):
            ui.note(f"{result['name']}: rebuilt for this system")
        for failure in job.result.get("failed", []):
            ui.warn(f"{failure['name']}: {failure['error']}")
        if not job.result.get("bundles") and not job.result.get("failed"):
            ui.note("Every bundle already matches this system.")
        if job.result.get("failed"):
            return 1
    return code


def cmd_compat_remove(client, args):
    bundles = {b["name"] for b in client.one("Compat", "ListBundles")}
    if args.name in bundles:
        what = f"{', '.join(args.packages)} from the bundle {args.name}" if args.packages else f"the whole bundle {args.name}"
        if not sure(args, f"Remove {what}?"):
            return 1
        return run_job(client, args, "Compat", "RemovePackages", "(sas)", args.name, args.packages)[0]
    if args.packages:
        raise UserError(f"{args.name} is not a bundle; packages can only be removed from a bundle")
    if not sure(args, f"Remove the extension {args.name}?"):
        return 1
    return run_job(client, args, "Compat", "Remove", "(s)", args.name)[0]


def cmd_compat_list(client, args):
    items = client.one("Compat", "List")
    if args.json:
        emit_json(items)
        return 0
    if not items:
        ui.note("No compat extensions installed.")
        return 0
    ui.title("Compat extensions")
    ui.table(["Name", "Version", "Files", "State", "Merged"],
             [[i["name"], f"{i['version']}-{i['release']}", i["files"],
               ui.badge("ok", "enabled") if i["enabled"] else ui.badge("warn", "disabled"),
               "yes" if i["merged"] else "no"] for i in items])
    for item in items:
        for problem in item["problems"]:
            ui.warn(f"{item['name']}: {problem}")
    print()
    return 0


# ----------------------------------------------------------------------------- repositories

def _age(seconds):
    if seconds < 0:
        return "never"
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def cmd_repo_list(client, args):
    items = client.one("Repos", "List")
    if args.json:
        emit_json(items)
        return 0
    ui.title("Package repositories")
    ui.table(["Name", "Enabled", "Priority", "Signed", "Updated", "URL"],
             [[r["alias"], ui.badge("ok", "yes") if r["enabled"] else "no", r["priority"], "yes" if r["signed"] else ui.badge("warn", "no"),
               _age(r["refreshed_seconds_ago"]) if r["enabled"] else "-", r["url"]] for r in items])
    print()
    return 0


def cmd_repo_refresh(client, args):
    code, job = run_job(client, args, "Repos", "Refresh", "(asb)", args.aliases, bool(args.force))
    if job is not None and job.ok and not args.json:
        for result in job.result.get("repositories", []):
            ui.note(f"{result['alias']}: " + ("updated" if result["changed"] else "already current"))
    return code


def _set_repo(client, args, enabled):
    if enabled and not args.json:
        ui.warn("Packages from a repository are only as trustworthy as that repository; they are checked against its own "
                "signing key, but they are not part of the Ersa Compute Platform.")
    if not sure(args, f"{'Enable' if enabled else 'Disable'} the repository {args.alias}?"):
        return 1
    return run_job(client, args, "Repos", "SetEnabled", "(sb)", args.alias, enabled)[0]


def cmd_repo_add(client, args):
    try:
        with open(args.key, encoding="utf-8") as handle:
            key = handle.read(256 * 1024 + 1)
    except (OSError, UnicodeDecodeError) as error:
        ui.error(f"Cannot read the key file {args.key}: {error}")
        return 1
    keys = client.one("Repos", "InspectKey", "(s)", key)
    if not args.json:
        ui.title(f"Add the repository {args.alias}")
        ui.kv([("Address", args.url), ("Signing key", ", ".join(k["fingerprint"] for k in keys)),
               ("Key owner", ", ".join(k["owner"] or "-" for k in keys))])
        print()
        ui.warn("Packages from this repository will be trusted if they are signed by that key. Check the fingerprint with the "
                "repository's publisher. They are not part of the Ersa Compute Platform.")
    if not sure(args, f"Trust this key and add {args.alias}?"):
        return 1
    code, job = run_job(client, args, "Repos", "Add", "(sssis)", args.alias, args.name or args.alias, args.url, args.priority, key)
    if job is not None and job.ok and not args.json:
        ui.note(f"Added. Fetch its package list with: foundation repo refresh {args.alias}")
    return code


def cmd_repo_remove(client, args):
    if not sure(args, f"Remove the repository {args.alias} and its key?"):
        return 1
    return run_job(client, args, "Repos", "Remove", "(s)", args.alias)[0]


def cmd_repo_enable(client, args):
    return _set_repo(client, args, True)


def cmd_repo_disable(client, args):
    return _set_repo(client, args, False)


def cmd_repo_search(client, args):
    try:
        results = client.one("Repos", "Search", "(s)", args.term, timeout=180_000)
    except ClientError as error:
        raise UserError(error.message) from error
    if args.json:
        emit_json(results)
        return 0
    if not results:
        ui.note(f"No package matches '{args.term}'. Refresh the lists with: foundation repo refresh")
        return 0
    ui.title(f"Packages matching '{args.term}'")
    ui.table(["Package", "Version", "Repository", "Summary"], [[r["name"], r["evr"], r["repo"], r["summary"]] for r in results])
    print()
    return 0


def cmd_repo_info(client, args):
    try:
        results = client.one("Repos", "Info", "(s)", args.name, timeout=180_000)
    except ClientError as error:
        raise UserError(error.message) from error
    if args.json:
        emit_json(results)
        return 0
    ui.title(args.name)
    ui.table(["Repository", "Version", "Size", "In the base image"],
             [[r["repo"], r["evr"], ui.human_size(r["size"]), "yes" if r["installed"] else "no"] for r in results])
    print()
    return 0


def cmd_compat_revalidate(client, args):
    code, job = run_job(client, args, "Compat", "Revalidate", None)
    if job is not None and job.ok and not args.json:
        disabled, enabled = job.result.get("disabled", []), job.result.get("enabled", [])
        ui.note(f"Checked {job.result.get('checked', 0)} extension(s).")
        if disabled:
            ui.warn("disabled because they no longer fit this system: " + ", ".join(disabled))
        if enabled:
            ui.note("enabled again: " + ", ".join(enabled))
    return code


# ----------------------------------------------------------------------------- extensions

def cmd_ext_list(client, args):
    items = client.one("Extensions", "List", "(s)", args.kind)
    if args.json:
        emit_json(items)
        return 0
    if not items:
        ui.note(f"No {args.kind} extensions found.")
        return 0
    ui.title(f"{args.kind} extensions")
    ui.table(["Name", "Type", "Merged", "Managed", "Path"],
             [[i["name"], i["type"], ui.badge("ok", "merged") if i["merged"] else ui.badge("unknown", "not merged"),
               "compat" if i["managed"] else "-", i["path"]] for i in items])
    print()
    return 0


def ext_operation(method, question):
    def command(client, args):
        if not sure(args, question.format(kind=args.kind)):
            return 1
        return run_job(client, args, "Extensions", method, "(s)", args.kind)[0]
    return command


# ----------------------------------------------------------------------------- settings

def cmd_config_list(client, args):
    values = client.one("Config", "List")
    if args.json:
        emit_json(values)
        return 0
    ui.title("Settings")
    ui.table(["Key", "Value"], [[k, str(v).lower() if isinstance(v, bool) else v] for k, v in sorted(values.items())])
    print()
    return 0


def cmd_config_get(client, args):
    value = client.one("Config", "Get", "(s)", args.key)
    if args.json:
        emit_json({args.key: value})
    else:
        print(str(value).lower() if isinstance(value, bool) else value)
    return 0


def cmd_config_describe(client, args):
    info = client.one("Config", "Describe", "(s)", args.key)
    if args.json:
        emit_json(info)
        return 0
    ui.title(args.key)
    ui.kv([("Type", {"b": "boolean", "s": "text", "i": "integer"}.get(info["type"], info["type"])),
           ("Default", str(info["default"]).lower() if isinstance(info["default"], bool) else info["default"]),
           ("Changeable", "yes" if info["writable"] else "no"),
           ("Choices", ", ".join(info.get("choices", [])) or None), ("About", info["description"])])
    print()
    return 0


def typed_value(signature, text):
    from gi.repository import GLib
    if signature == "b":
        lowered = text.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return GLib.Variant("b", True)
        if lowered in ("false", "no", "off", "0"):
            return GLib.Variant("b", False)
        raise UserError(f"'{text}' is not true or false")
    if signature == "i":
        try:
            return GLib.Variant("i", int(text))
        except ValueError:
            raise UserError(f"'{text}' is not a number") from None
    return GLib.Variant("s", text)


def cmd_config_set(client, args):
    info = client.one("Config", "Describe", "(s)", args.key)
    client.call("Config", "Set", "(sv)", args.key, typed_value(info["type"], args.value))
    if args.json:
        emit_json({args.key: client.one("Config", "Get", "(s)", args.key)})
    else:
        print(ui.badge("ok", f"{args.key} updated"))
    return 0


def cmd_config_reset(client, args):
    client.call("Config", "Reset", "(s)", args.key)
    print(ui.badge("ok", f"{args.key} reset to its default") if not args.json else json.dumps({"reset": args.key}))
    return 0


# ----------------------------------------------------------------------------- jobs

def job_summary(client, path):
    props = client.properties(path)
    return {"id": path.rsplit("/", 1)[1], "path": path, "kind": props["Kind"], "state": props["State"],
            "step": props["StepTitle"], "progress": props["Progress"], "cancellable": props["Cancellable"],
            "error": props["ErrorName"].removeprefix("org.ersa.Foundation1.Error.") or None,
            "message": props["ErrorMessage"] or None, "result": props["Result"]}


def job_path(raw):
    if not raw.isdigit():
        raise UserError("a job is named by its number (see: foundation job list)")
    return f"/org/ersa/Foundation1/Job/{raw}"


def cmd_job_list(client, args):
    jobs = [job_summary(client, p) for p in client.one("Manager", "ListJobs")]
    if args.json:
        emit_json(jobs)
        return 0
    if not jobs:
        ui.note("No jobs.")
        return 0
    ui.title("Jobs")
    ui.table(["Job", "Kind", "State", "Step"],
             [[j["id"], j["kind"], {"done": ui.badge("ok", "done"), "running": ui.badge("pending", "running"),
                                    "failed": ui.badge("fail", "failed"), "cancelled": ui.badge("warn", "cancelled")}.get(j["state"], j["state"]),
               j["step"] or (j["message"] or "")] for j in jobs])
    print()
    return 0


def cmd_job_show(client, args):
    path = job_path(args.job)
    summary = job_summary(client, path)
    summary["log"] = client.logs(path)
    if args.json:
        emit_json(summary)
        return 0
    ui.title(f"Job {summary['id']}: {summary['kind']}")
    ui.kv([("State", summary["state"]), ("Step", summary["step"] or None),
           ("Progress", ui.progress_bar(summary["progress"]) if summary["progress"] >= 0 else None),
           ("Can be cancelled", "yes" if summary["cancellable"] and summary["state"] == "running" else None),
           ("Error", f"{summary['error']}: {summary['message']}" if summary["error"] else None)])
    if summary["result"]:
        ui.section("Result")
        ui.kv([(k, v) for k, v in summary["result"].items()])
    if summary["log"]:
        ui.section("Log")
        for line in summary["log"]:
            print(f"  {line}")
    print()
    return 0


def cmd_job_cancel(client, args):
    client.cancel(job_path(args.job))
    print(ui.badge("ok", "cancellation requested") if not args.json else json.dumps({"cancelled": args.job}))
    return 0


def cmd_job_logs(client, args):
    for line in client.logs(job_path(args.job)):
        print(line)
    return 0


# ----------------------------------------------------------------------------- base packages

def cmd_base_list(client, args):
    rows = client.one("Manager", "ListBasePackages")
    if args.filter:
        rows = [r for r in rows if args.filter.lower() in r["name"].lower()]
    if args.json:
        emit_json(rows)
        return 0
    ui.title("Base image packages")
    ui.table(["Package", "Version", "Vendor"], [[r["name"], r["version"], r["vendor"]] for r in rows])
    ui.note(f"\n{len(rows)} packages. The base image is read-only; it changes through system updates.")
    print()
    return 0


# ----------------------------------------------------------------------------- parser

def build_parser():
    parser = argparse.ArgumentParser(
        prog="foundation", description=f"{PRODUCT} management",
        epilog="Use 'foundation COMMAND --help' for details. Most commands accept --json.")
    parser.add_argument("--version", action="version", version=f"foundation {__version__}")
    parser.add_argument("--no-color", action="store_true", help="disable colour output")
    parser.add_argument("--bus", choices=("system", "session"), default="system", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(parent, name, func, help, json_flag=True, yes=False, wait=False, aliases=()):
        sp = parent.add_parser(name, help=help, description=help, aliases=list(aliases))
        sp.set_defaults(func=func)
        if json_flag:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        if yes:
            sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
        if wait:
            sp.add_argument("--no-wait", action="store_true", help="start the operation and return at once")
        return sp

    def group(name, help):
        g = sub.add_parser(name, help=help, description=help)
        g.set_defaults(func=None, group=g)
        return g, g.add_subparsers(dest=f"{name}_command", metavar="SUBCOMMAND")

    add(sub, "status", cmd_status, "show the system, boot slot, storage and services")

    _, slot = group("slot", "inspect and manage the boot slots (A/B)")
    add(slot, "status", cmd_slot_status, "show the running slot, the boot assessment and every slot", aliases=["list"])
    boot = add(slot, "boot", cmd_slot_boot, "choose the slot to boot", yes=True, wait=True)
    boot.add_argument("slot")
    boot.add_argument("--once", action="store_true", help="only for the next boot")
    add(slot, "release", cmd_slot_release, "empty a standby slot (its kernel image and its names) so updates can use it", yes=True, wait=True).add_argument("slot")

    add(sub, "splash", cmd_splash, "choose the light or dark boot splash (takes effect at the next restart)",
        wait=True).add_argument("theme", choices=("light", "dark"))

    reboot = add(sub, "reboot", cmd_reboot, "restart the computer", yes=True)
    reboot.add_argument("--firmware", action="store_true", help="restart into the firmware setup")
    reboot.add_argument("--force", action="store_true", help="restart even if operations are running")

    _, remote = group("remote", "remote access: the SSH server and the keys that may log in")
    add(remote, "status", cmd_remote_status, "show whether SSH is on, how people log in and the firewall")
    ssh = remote.add_parser("ssh", help="turn the SSH server on or off", description="turn the SSH server on or off")
    ssh.set_defaults(func=None, group=ssh)
    ssh_sub = ssh.add_subparsers(dest="ssh_command", metavar="SUBCOMMAND")
    ssh_on = add(ssh_sub, "on", cmd_remote_ssh_on, "turn the SSH server on (keys only unless you allow passwords)", yes=True, wait=True)
    ssh_on.add_argument("--allow-password", action="store_true", help="also allow password login")
    add(ssh_sub, "off", cmd_remote_ssh_off, "turn the SSH server off", yes=True, wait=True)
    key = remote.add_parser("key", help="the public keys that may log in", description="the public keys that may log in")
    key.set_defaults(func=None, group=key)
    key_sub = key.add_subparsers(dest="key_command", metavar="SUBCOMMAND")
    key_list = add(key_sub, "list", cmd_remote_key_list, "list the keys")
    key_list.add_argument("--user", help="only this user's keys")
    key_add = add(key_sub, "add", cmd_remote_key_add, "add a public key (a .pub file, the key itself, or - for stdin)", wait=True)
    key_add.add_argument("key", metavar="KEY")
    key_add.add_argument("--user", help="whose key it is (default: you)")
    key_remove = add(key_sub, "remove", cmd_remote_key_remove, "remove a key by its fingerprint", yes=True, wait=True)
    key_remove.add_argument("user")
    key_remove.add_argument("fingerprint", help="as shown by `key list` (SHA256:...)")

    _, deploy = group("deploy", "install the operating system on a disk")
    add(deploy, "list", cmd_deploy_list, "list the disks and whether each can be installed to")
    inst = add(deploy, "install", cmd_deploy_install, "erase a disk and install this system on it", yes=True, wait=True)
    inst.add_argument("disk", help="the whole disk, such as /dev/sdb")
    inst.add_argument("--dry-run", action="store_true", help="show what would happen and change nothing")
    inst.add_argument("--language", metavar="LOCALE", help="the installed system's language, such as de_DE.UTF-8")
    inst.add_argument("--timezone", metavar="ZONE", help="the installed system's time zone, such as Europe/Berlin")

    _, apps = group("apps", "manage applications (Flatpak, system-wide)")
    add(apps, "list", cmd_apps_list, "list installed applications")
    add(apps, "search", cmd_apps_search, "search for applications").add_argument("term")
    add(apps, "info", cmd_apps_info, "show details of an application").add_argument("app")
    add(apps, "install", cmd_apps_install, "install an application", yes=True, wait=True).add_argument("app")
    add(apps, "remove", cmd_apps_remove, "remove an application", yes=True, wait=True).add_argument("app")
    add(apps, "update", cmd_apps_update, "update installed applications", yes=True, wait=True)
    add(apps, "setup", cmd_apps_setup, "add the Flathub application source", yes=True, wait=True)

    _, compat = group("compat", "run legacy RPMs as system extensions")
    add(compat, "inspect", cmd_compat_inspect, "check an RPM against this system").add_argument("rpm")
    install = add(compat, "install", cmd_compat_install,
                  "install packages from the repositories into a bundle, a catalog bundle (@codecs) or an RPM file", yes=True, wait=True)
    install.add_argument("targets", nargs="+", metavar="PACKAGE", help="package names, @CATALOG-NAME, or one .rpm file")
    install.add_argument("--into", metavar="BUNDLE", help="the bundle (one extension) to put the packages in (default: extras)")
    install.add_argument("--force", action="store_true", help="for an RPM file: install even if the check finds problems")
    remove = add(compat, "remove", cmd_compat_remove, "remove an extension, a bundle, or packages from a bundle", yes=True, wait=True)
    remove.add_argument("name")
    remove.add_argument("packages", nargs="*", metavar="PACKAGE", help="only these packages (of a bundle)")
    add(compat, "list", cmd_compat_list, "list installed extensions")
    add(compat, "bundles", cmd_compat_bundles, "list the bundles and what is in them")
    add(compat, "catalog", cmd_compat_catalog, "list ready-made bundles (@codecs, ...)")
    update = add(compat, "update", cmd_compat_update, "update bundles to the newest packages", yes=True, wait=True)
    update.add_argument("bundle", nargs="?", help="one bundle (default: all)")
    add(compat, "refresh", cmd_compat_refresh, "rebuild bundles that were built for a different system version", wait=True)
    add(compat, "revalidate", cmd_compat_revalidate, "recheck extensions after a system update", wait=True)

    _, update = group("update", "install system updates")
    install_update = add(update, "install", cmd_update_install, "install an update file (.tar.zst) into the spare slot", yes=True, wait=True)
    install_update.add_argument("archive", metavar="FILE")
    install_update.add_argument("--slot", help="install into this slot (default: the spare one)")
    install_update.add_argument("--unsigned", action="store_true", help="skip the signature check, for an image you built yourself")
    install_update.add_argument("--force", action="store_true", help="install again even if this version is already installed")
    add(update, "status", cmd_update_status, "show whether an update is waiting for a restart")

    _, repo = group("repo", "package repositories (signed openSUSE and Packman metadata)")
    add(repo, "list", cmd_repo_list, "list the repositories")
    refresh = add(repo, "refresh", cmd_repo_refresh, "download the newest package lists", wait=True)
    refresh.add_argument("aliases", nargs="*", metavar="REPOSITORY", help="default: all enabled repositories")
    refresh.add_argument("--force", action="store_true", help="download again even if nothing changed")
    add_repo = add(repo, "add", cmd_repo_add, "add a repository of your own (https, signed)", yes=True, wait=True)
    add_repo.add_argument("alias", help="a short name, for example myrepo")
    add_repo.add_argument("url", help="the repository's address (https://...); its repodata/ must be signed")
    add_repo.add_argument("--key", required=True, metavar="FILE", help="the publisher's armoured public key (.asc)")
    add_repo.add_argument("--name", help="a description to show in lists")
    add_repo.add_argument("--priority", type=int, default=99, help="1-999; lower wins (default 99)")
    add(repo, "remove", cmd_repo_remove, "remove a repository you added", yes=True, wait=True).add_argument("alias")
    add(repo, "enable", cmd_repo_enable, "turn a repository on", yes=True, wait=True).add_argument("alias")
    add(repo, "disable", cmd_repo_disable, "turn a repository off", yes=True, wait=True).add_argument("alias")
    add(repo, "search", cmd_repo_search, "find packages by name").add_argument("term")
    add(repo, "info", cmd_repo_info, "show the newest version of a package in each repository").add_argument("name")

    _, ext = group("ext", "manage system extension images (sysext and confext)")
    for name, help, method, question in (
            ("list", "list extension images", None, None),
            ("merge", "overlay the extensions onto the system", "Merge", "Merge the {kind} extensions?"),
            ("unmerge", "remove the overlay", "Unmerge", "Unmerge the {kind} extensions?"),
            ("refresh", "merge again, picking up changes", "Refresh", "Refresh the {kind} extensions?")):
        sp = add(ext, name, cmd_ext_list if method is None else ext_operation(method, question), help,
                 yes=method is not None, wait=method is not None)
        sp.add_argument("--kind", choices=("sysext", "confext"), default="sysext")

    _, config = group("config", "view and change settings")
    add(config, "list", cmd_config_list, "show every setting")
    add(config, "get", cmd_config_get, "show one setting").add_argument("key")
    add(config, "describe", cmd_config_describe, "explain a setting").add_argument("key")
    setter = add(config, "set", cmd_config_set, "change a setting")
    setter.add_argument("key")
    setter.add_argument("value")
    add(config, "reset", cmd_config_reset, "return a setting to its default").add_argument("key")

    _, job = group("job", "follow operations started earlier")
    add(job, "list", cmd_job_list, "list running and recent jobs")
    add(job, "show", cmd_job_show, "show one job").add_argument("job")
    add(job, "cancel", cmd_job_cancel, "stop a running job").add_argument("job")
    add(job, "logs", cmd_job_logs, "print a job's log", json_flag=False).add_argument("job")

    base = add(sub, "base", cmd_base_list, "list packages in the base image")
    base.add_argument("filter", nargs="?")
    return parser


def _discard_output():
    """The reader of our output went away (`foundation ... | head`). Point stdout and stderr at
    /dev/null so Python does not report an error when it flushes them at exit."""
    for stream in (sys.stdout, sys.stderr):
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())
        except (OSError, ValueError):
            pass


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    if not getattr(args, "func", None):
        (getattr(args, "group", None) or parser).print_help()
        return 2
    args.json = getattr(args, "json", False)
    args.no_wait = getattr(args, "no_wait", False)
    args.yes = getattr(args, "yes", False)
    try:
        return args.func(Client(args.bus), args)
    except UserError as error:
        ui.error(str(error))
        return 2
    except ClientError as error:
        if args.json:
            emit_json({"error": error.name, "message": error.message})
        else:
            ui.error(error.message)
            if error.name in HINTS:
                ui.note(HINTS[error.name])
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        _discard_output()
        return 0
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except BrokenPipeError:
                _discard_output()
