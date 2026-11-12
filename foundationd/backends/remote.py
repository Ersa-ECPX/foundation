"""Remote access: the SSH server and the keys that may log in.

What this does for a person: turns the computer's SSH server on or off, safely. "Safely" means it will not switch SSH on
with password login off and nobody able to log in with a key, and that it opens the firewall only when the firewall
would block it. It also lists, adds and removes the public keys of a user (`~/.ssh/authorized_keys`).

How: the server's settings live in one drop-in, `/etc/ssh/sshd_config.d/50-foundation.conf` (sshd uses the first value
it finds, so a file that sorts before it, such as `10-local.conf`, overrides it). The service is controlled through
systemd's D-Bus API and the firewall through firewalld's D-Bus API; `sshd -t` checks the configuration before the
service is touched. Keys are changed through directory file descriptors without following links, because this runs as
root inside somebody else's home directory.
"""

import base64
import binascii
import hashlib
import os
import re
import tempfile
import time

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from .runner import run

UNIT = "sshd.service"
UNIT_FILE = "usr/lib/systemd/system/sshd.service"
DROPIN = "etc/ssh/sshd_config.d/50-foundation.conf"
FIREWALL_SERVICE = "ssh"
KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
MIN_UID = 1000


# -- keys ----------------------------------------------------------------------------------------

def parse_key(line):
    """(type, base64 blob, comment, fingerprint, options) of one authorized_keys line, or None for a blank, comment
    or unreadable line. Options (`command=`, `from=` ...) before the key type are returned, never interpreted."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    tokens = text.split()
    for index, token in enumerate(tokens[:-1] if len(tokens) > 1 else []):
        if token in KEY_TYPES:
            blob = tokens[index + 1]
            try:
                raw = base64.b64decode(blob, validate=True)
            except (binascii.Error, ValueError):
                return None
            digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
            return token, blob, " ".join(tokens[index + 2:]), "SHA256:" + digest, " ".join(tokens[:index])
    return None


def check_new_key(text):
    """The cleaned one-line public key, or InvalidArgument. Options are refused: a key added through foundation is
    a plain key, so nothing can sneak in a forced command or a source restriction the caller did not mean to set."""
    if not isinstance(text, str) or "\n" in text.strip() or "\0" in text or len(text) > 8192:
        raise InvalidArgument("give one public key on a single line")
    parsed = parse_key(text)
    if parsed is None:
        raise InvalidArgument("that is not an SSH public key (it should start with ssh-ed25519, ecdsa-sha2-..., sk-... or ssh-rsa)")
    kind, blob, comment, _fingerprint, options = parsed
    if options:
        raise InvalidArgument("keys with options (command=, from=, ...) cannot be added here; edit authorized_keys yourself")
    if any(ord(c) < 32 for c in comment):
        raise InvalidArgument("the key comment has control characters")
    return " ".join(part for part in (kind, blob, comment) if part)


def login_users(paths):
    """{name: (uid, gid, home)} for the people who can log in: real accounts with a shell."""
    users = {}
    try:
        with open(paths["passwd"], encoding="utf-8") as handle:
            for line in handle:
                fields = line.rstrip("\n").split(":")
                if len(fields) < 7:
                    continue
                name, _x, uid, gid, _gecos, home, shell = fields[:7]
                if not uid.isdigit() or int(uid) < MIN_UID or int(uid) >= 60000:
                    continue
                if shell.endswith(("nologin", "false")) or not home.startswith("/"):
                    continue
                users[name] = (int(uid), int(gid), home)
    except OSError:
        pass
    return users


def _user(paths, name):
    users = login_users(paths)
    if name not in users:
        raise NotFound(f"{name} is not a user who can log in")
    return users[name]


def _rooted(paths, home):
    """`home` inside the filesystem root the paths describe (a temp directory in tests)."""
    return os.path.join(paths["root"], home.lstrip("/"))


def _open_ssh_dir(paths, home, uid, gid, create):
    """A file descriptor for ~/.ssh, never through a link; None when it does not exist and `create` is false."""
    top = os.open(_rooted(paths, home), os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            return os.open(".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top)
        except FileNotFoundError:
            if not create:
                return None
        except OSError as error:
            raise Failed(f"{home}/.ssh is not a plain directory ({error.strerror}); not touching it") from error
        os.mkdir(".ssh", 0o700, dir_fd=top)
        fd = os.open(".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top)
        _chown(fd, uid, gid)
        return fd
    finally:
        os.close(top)


def _chown(fd, uid, gid):
    try:
        os.fchown(fd, uid, gid)
    except PermissionError:
        if os.geteuid() == 0:
            raise


def _read_lines(directory_fd):
    if directory_fd is None:
        return []
    try:
        fd = os.open("authorized_keys", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise Failed(f"authorized_keys is not a plain file ({error.strerror}); not touching it") from error
    with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _write_lines(directory_fd, lines, uid, gid):
    temporary = ".authorized_keys.foundation"
    try:
        os.unlink(temporary, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))
            handle.flush()
            _chown(handle.fileno(), uid, gid)
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.rename(temporary, "authorized_keys", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def list_keys(paths, user=""):
    """The keys of `user` (or of everyone who can log in): [{user, type, fingerprint, comment, options}]."""
    names = [user] if user else sorted(login_users(paths))
    result = []
    for name in names:
        uid, gid, home = _user(paths, name)
        fd = _open_ssh_dir(paths, home, uid, gid, create=False)
        try:
            lines = _read_lines(fd)
        finally:
            if fd is not None:
                os.close(fd)
        for line in lines:
            parsed = parse_key(line)
            if parsed:
                kind, _blob, comment, fingerprint, options = parsed
                result.append({"user": name, "type": kind, "fingerprint": fingerprint, "comment": comment,
                               "options": options})
    return result


def add_key(paths, user, text):
    cleaned = check_new_key(text)
    parsed = parse_key(cleaned)
    uid, gid, home = _user(paths, user)
    fd = _open_ssh_dir(paths, home, uid, gid, create=True)
    try:
        lines = _read_lines(fd)
        if any((p := parse_key(line)) and p[1] == parsed[1] for line in lines):
            return {"user": user, "fingerprint": parsed[3], "added": False}
        _write_lines(fd, lines + [cleaned], uid, gid)
    finally:
        os.close(fd)
    return {"user": user, "fingerprint": parsed[3], "added": True}


def remove_key(paths, user, fingerprint):
    if not FINGERPRINT.match(fingerprint or ""):
        raise InvalidArgument("give the key's fingerprint, as shown by `foundation remote ssh key list` (SHA256:...)")
    uid, gid, home = _user(paths, user)
    fd = _open_ssh_dir(paths, home, uid, gid, create=False)
    try:
        lines = _read_lines(fd)
        kept = [line for line in lines if not ((p := parse_key(line)) and p[3] == fingerprint)]
        if len(kept) == len(lines):
            raise NotFound(f"{user} has no key with that fingerprint")
        _write_lines(fd, kept, uid, gid)
    finally:
        if fd is not None:
            os.close(fd)
    return {"user": user, "fingerprint": fingerprint, "removed": len(lines) - len(kept)}


# -- the server's settings -----------------------------------------------------------------------

def render_dropin(password_auth):
    return ("# Written by foundation (foundation remote ssh). sshd uses the first value it finds, so a file that sorts\n"
            "# before this one (for example 10-local.conf) overrides it.\n"
            f"PasswordAuthentication {'yes' if password_auth else 'no'}\n"
            f"KbdInteractiveAuthentication {'yes' if password_auth else 'no'}\n"
            "PermitRootLogin no\n")


def read_password_auth(paths):
    """True or False as foundation set it, None when foundation has not configured the server."""
    try:
        with open(os.path.join(paths["root"], DROPIN), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PasswordAuthentication "):
                    return line.split()[1] == "yes"
    except OSError:
        pass
    return None


def _write_dropin(paths, text):
    target = os.path.join(paths["root"], DROPIN)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    previous = None
    try:
        with open(target, encoding="utf-8") as handle:
            previous = handle.read()
    except OSError:
        pass
    temporary = target + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return previous


def _restore_dropin(paths, previous):
    target = os.path.join(paths["root"], DROPIN)
    if previous is None:
        try:
            os.unlink(target)
        except OSError:
            pass
    else:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(previous)


# -- systemd and the firewall ---------------------------------------------------------------------

class System:
    """systemd and firewalld over D-Bus. Tests replace this with a fake of the same methods."""

    SYSTEMD = ("org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager")
    FIREWALLD = "org.fedoraproject.FirewallD1"

    def __init__(self, bus="system"):
        self.bus = bus

    def _connection(self):
        from gi.repository import Gio
        return Gio.bus_get_sync(Gio.BusType.SYSTEM if self.bus == "system" else Gio.BusType.SESSION, None)

    def _call(self, destination, path, interface, method, parameters=None, reply=None, timeout=15000):
        from gi.repository import Gio, GLib
        try:
            result = self._connection().call_sync(
                destination, path, interface, method, parameters, GLib.VariantType(reply) if reply else None,
                Gio.DBusCallFlags.NONE, timeout, None)
        except GLib.Error as error:
            if Gio.DBusError.is_remote_error(error):
                Gio.DBusError.strip_remote_error(error)
            raise Failed(f"{destination}: {error.message}") from error
        return result.unpack() if result is not None else ()

    def _systemd(self, method, parameters=None, reply=None):
        from gi.repository import GLib  # noqa: F401
        return self._call(*self.SYSTEMD[:2], self.SYSTEMD[2], method, parameters, reply)

    def unit_file_state(self, unit):
        try:
            from gi.repository import GLib
            return self._systemd("GetUnitFileState", GLib.Variant("(s)", (unit,)), "(s)")[0]
        except Failed:
            return "not-found"

    def active_state(self, unit):
        from . import osinfo
        return osinfo.unit_states([unit], self.bus)[unit]

    def enable(self, unit):
        from gi.repository import GLib
        self._systemd("EnableUnitFiles", GLib.Variant("(asbb)", ([unit], False, True)), "(ba(sss))")
        self._systemd("Reload")

    def disable(self, unit):
        from gi.repository import GLib
        self._systemd("DisableUnitFiles", GLib.Variant("(asb)", ([unit], False)), "(a(sss))")
        self._systemd("Reload")

    def start(self, unit, restart=False):
        from gi.repository import GLib
        self._systemd("RestartUnit" if restart else "StartUnit", GLib.Variant("(ss)", (unit, "replace")), "(o)")

    def stop(self, unit):
        from gi.repository import GLib
        self._systemd("StopUnit", GLib.Variant("(ss)", (unit, "replace")), "(o)")

    def preset(self, unit):
        """What `systemctl preset UNIT` does: enable or disable it as the preset files say."""
        from gi.repository import GLib
        self._systemd("PresetUnitFiles", GLib.Variant("(asbb)", ([unit], False, False)), "(ba(sss))")

    def disable_units(self, units):
        from gi.repository import GLib
        for unit in units:
            try:
                self._systemd("DisableUnitFiles", GLib.Variant("(asb)", ([unit], False)), "(a(sss))")
            except Failed:
                pass
        self.reload()

    def reload(self):
        self._systemd("Reload")

    # firewalld: None means "not running or not answering"
    def firewall_allows(self, service):
        from gi.repository import GLib
        try:
            zone = self._call(self.FIREWALLD, "/org/fedoraproject/FirewallD1", self.FIREWALLD, "getDefaultZone", None, "(s)")[0]
            return bool(self._call(self.FIREWALLD, "/org/fedoraproject/FirewallD1", self.FIREWALLD + ".zone", "queryService",
                                   GLib.Variant("(ss)", (zone, service)), "(b)")[0])
        except Failed:
            return None

    def firewall_allow(self, service):
        """Allow `service` in the default zone now and permanently."""
        from gi.repository import GLib
        zone = self._call(self.FIREWALLD, "/org/fedoraproject/FirewallD1", self.FIREWALLD, "getDefaultZone", None, "(s)")[0]
        self._call(self.FIREWALLD, "/org/fedoraproject/FirewallD1", self.FIREWALLD + ".zone", "addService",
                   GLib.Variant("(ssi)", (zone, service, 0)), "(s)")
        config = self._call(self.FIREWALLD, "/org/fedoraproject/FirewallD1/config", self.FIREWALLD + ".config",
                            "getZoneByName", GLib.Variant("(s)", (zone,)), "(o)")[0]
        self._call(self.FIREWALLD, config, self.FIREWALLD + ".config.zone", "addService", GLib.Variant("(s)", (service,)))


def check_config(runner=run):
    """(code, output) of `sshd -t` on the settings. A computer that has never run the SSH server has no host keys yet, and
    sshd refuses to check anything without one ("no hostkeys available"), so the check is given a throwaway key of its own.
    The real keys are made by the service itself when it starts (sshd-gen-keys-start), with the right security labels."""
    with tempfile.TemporaryDirectory() as directory:
        key = os.path.join(directory, "check_key")
        made, _out, _err = runner(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], 15)
        command = ["sshd", "-t"] + (["-h", key] if made == 0 else [])
        code, out, err = runner(command, 15)
    if code != 0 and made != 0 and "no hostkeys available" in (out + err):
        return 0, ""             # nothing wrong with the settings; there was only no key to check them with
    return code, (err or out)


def installed(paths):
    return os.path.exists(os.path.join(paths["root"], UNIT_FILE))


def status(paths, system=None):
    """What is true now: installed, enabled at boot, running, how people may log in, the firewall, the keys."""
    system = system or System(paths.get("bus", "system"))
    have = installed(paths)
    keys = list_keys(paths)
    result = {"installed": have, "enabled": False, "active": False, "keys": len(keys),
              "users_with_keys": sorted({k["user"] for k in keys})}
    password = read_password_auth(paths)
    if password is not None:
        result["password_auth"] = password
    if have:
        result["enabled"] = system.unit_file_state(UNIT) in ("enabled", "enabled-runtime")
        result["active"] = system.active_state(UNIT) == "active"
        allowed = system.firewall_allows(FIREWALL_SERVICE)
        result["firewall"] = "unknown" if allowed is None else ("open" if allowed else "closed")
    return result


def set_ssh(paths, ctx, enable, allow_password, system=None, runner=run):
    """Turn the SSH server on (key login only unless `allow_password`) or off."""
    system = system or System(paths.get("bus", "system"))
    if not installed(paths):
        raise Unavailable("the SSH server is not part of this system image")
    if not enable:
        ctx.step("stop", "Stopping the SSH server")
        ctx.commit()
        system.stop(UNIT)
        system.disable(UNIT)
        return {"enabled": False, "active": False}

    if not allow_password and not list_keys(paths):
        raise InvalidArgument("nobody could log in: no user has an SSH key and password login is off. Add a key first "
                              "(foundation remote ssh key add) or allow password login")
    ctx.step("configure", "Writing the SSH server settings")
    previous = _write_dropin(paths, render_dropin(allow_password))
    code, text = check_config(runner)
    if code not in (0, 127):
        _restore_dropin(paths, previous)
        detail = text.strip().splitlines()
        raise Failed("the SSH server rejected the settings, so they were not applied: " + (detail[-1] if detail else "no detail"))
    ctx.commit()
    ctx.step("start", "Starting the SSH server")
    was_active = system.active_state(UNIT) == "active"
    system.enable(UNIT)
    system.start(UNIT, restart=was_active)
    state = "unknown"
    for _ in range(20):
        state = system.active_state(UNIT)
        if state in ("active", "failed"):
            break
        time.sleep(0.25)
    if state != "active":
        raise Failed(f"the SSH server did not start (it is {state}); see `journalctl -u sshd`")

    firewall = "unknown"
    allowed = system.firewall_allows(FIREWALL_SERVICE)
    if allowed:
        firewall = "open"
    elif allowed is False:
        ctx.step("firewall", "Opening the firewall for SSH")
        try:
            system.firewall_allow(FIREWALL_SERVICE)
            firewall = "opened"
        except Failed as error:
            ctx.log(f"the firewall was not changed: {error}")
            firewall = "closed"
    return {"enabled": True, "active": True, "password_auth": allow_password, "firewall": firewall}
