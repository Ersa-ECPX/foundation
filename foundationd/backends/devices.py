"""Linking this computer to a person's Ersa ID account, and reporting its health (docs/devices-and-telemetry.md).

How it fits together:

  * The computer makes an Ed25519 key of its own. With a TPM the private key is sealed to it (`<state>/device/key.cred`, made by
    systemd-creds, no PCR policy so an update never loses it): the file is useless on any other computer, or copied out of a backup.
    Without a TPM, or if sealing fails, it is a file only root can read (`key.pem`); `Status` says which. Only the public half leaves.
  * The signed-in person's own session registers that public key with the account service, so the person's token never reaches this
    service. The account service first hands out a challenge, and the key is linked only if it is signed (`sign_link`: this service
    signs one fixed form, never arbitrary data), which shows the registering party holds the private half. Afterwards `link` stores
    the id the service gave back.
  * A check-in is signed with the device key (the device id, the time and the exact body), so reporting needs no account token.
    The service refuses a body that changed, an old time, a request that is not newer than the last one, or a device that was
    unlinked. The request is sent by a child process that has dropped root (the key is used first, then the network is touched
    without any privilege), does not follow redirects, and reads only a few kilobytes of any answer.
  * A report holds only the fields in REPORT_FIELDS. `report()` is what `foundation devices show` prints: exactly what would be sent.
"""

import base64
import json
import os
import pwd
import re
import signal
import socket
import time
import unicodedata
import urllib.error
import urllib.request

from ..errors import Failed, InvalidArgument, Unavailable
from . import encryption, osinfo, slots, updates
from .runner import run

SCHEMA = 1
# What a check-in may carry. Nothing else is read, kept or sent: no file names, application lists, host names, serial numbers or keys.
REPORT_FIELDS = ("schema", "version", "arch", "slot", "update_state", "boot_ok", "encrypted", "tpm")
UPDATE_STATES = ("idle", "available", "staged", "pending-reboot", "failed")
TIMEOUT = 20
TOTAL_TIMEOUT = 60                 # the whole request, however slowly the server answers
ANSWER_MAX = 4096                 # how much of an answer is read
DAY = 24 * 3600
BODY_MAX = 4096
CRED_NAME = "ecp-device-key"
USER_AGENT = "foundation-device-checkin"
# What the account service hands out to be signed: expires.nonce.mac, all url-safe. Nothing else is ever signed for it.
CHALLENGE = re.compile(r"[0-9]{1,12}\.[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9_-]{1,64}")
LINK_PREFIX = "prism-device-link"


def _dir(paths):
    return os.path.join(paths["state"], "device")


def key_path(paths):
    return os.path.join(_dir(paths), "key.pem")


def sealed_path(paths):
    return os.path.join(_dir(paths), "key.cred")


def link_path(paths):
    return os.path.join(_dir(paths), "link.json")


def last_path(paths):
    return os.path.join(_dir(paths), "last.json")


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as error:
        raise Unavailable("python3-cryptography is not installed") from error
    return serialization, Ed25519PrivateKey


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _seal(pem, runner):
    """The key sealed to this computer's TPM (text), or None if that could not be done. No PCR policy: an update, a firmware change
    or a Secure Boot change must not lose the identity; the seal ties the key to this TPM, which is what keeps a copy useless."""
    code, out, _err = runner(["systemd-creds", "encrypt", "--with-key=tpm2", "--tpm2-pcrs=", f"--name={CRED_NAME}", "-", "-"], 30,
                             input_bytes=pem)
    return out if code == 0 and out.strip() else None


def _unseal(text, runner):
    code, out, _err = runner(["systemd-creds", "decrypt", f"--name={CRED_NAME}", "-", "-"], 30, input_bytes=text.encode())
    if code != 0 or not out.strip():
        raise Failed("the device key cannot be unsealed: the TPM is not available, or it is not the one the key was sealed to")
    return out.encode()


def _load(serialization, pem):
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as error:
        raise Failed("the device key cannot be read") from error


def _private_key(paths, create, runner=run):
    """The private key: unsealed from the TPM when it was sealed, else read from its file. A file key is sealed on the first use after
    a TPM turns up. With `create`, a missing key is made (sealed when there is a TPM, else a file)."""
    serialization, Ed25519PrivateKey = _crypto()
    try:
        with open(sealed_path(paths), encoding="utf-8") as handle:
            return _load(serialization, _unseal(handle.read(), runner))
    except FileNotFoundError:
        pass
    except OSError as error:
        raise Failed("the sealed device key cannot be read") from error
    tpm = encryption.tpm_present(paths)
    try:
        with open(key_path(paths), "rb") as handle:
            pem = handle.read()
    except FileNotFoundError:
        pem = None
    except OSError as error:
        raise Failed("the device key cannot be read") from error
    if pem is not None:
        key = _load(serialization, pem)
        if tpm:                      # seal a key that was made before a TPM was usable; keep the file if that fails
            sealed = _seal(pem, runner)
            if sealed:
                _write_private(sealed_path(paths), sealed.encode())
                _remove(key_path(paths))
        return key
    if not create:
        raise Unavailable("this computer has no device key yet")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    sealed = _seal(pem, runner) if tpm else None
    if sealed:
        _write_private(sealed_path(paths), sealed.encode())
    else:
        _write_private(key_path(paths), pem)
    return key


def key_protection(paths):
    """How the private key is kept: "tpm" (sealed to this computer's TPM), "file" (readable by root only) or "" (no key yet)."""
    if os.path.exists(sealed_path(paths)):
        return "tpm"
    return "file" if os.path.exists(key_path(paths)) else ""


def public_key(paths, runner=run):
    """This computer's public key, made on first use: 32 bytes as base64url, the form the account service expects."""
    serialization, _ = _crypto()
    raw = _private_key(paths, True, runner).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64url(raw)


def sign_link(paths, challenge, runner=run):
    """Prove this computer holds its key, for the account service that hands out `challenge`: the signature over one fixed form, the
    prefix, the challenge and the public key. Nothing else is ever signed here, so a caller cannot use this as a signing oracle: a
    challenge that is not in the account service's exact shape is refused."""
    if not isinstance(challenge, str) or not CHALLENGE.fullmatch(challenge):
        raise InvalidArgument("the challenge is not in the form the account service hands out")
    key = _private_key(paths, True, runner)
    serialization, _ = _crypto()
    public = _b64url(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return {"public_key": public, "signature": _b64url(key.sign(f"{LINK_PREFIX}.{challenge}.{public}".encode()))}


def link_state(paths):
    """{} when not linked, else {"id", "url"}."""
    try:
        with open(link_path(paths), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("id") and data.get("url") else {}


def link(paths, device_id, url, runner=run):
    """Remember the id the account service gave this computer's key."""
    if not isinstance(device_id, str) or not device_id.strip() or "/" in device_id or len(device_id) > 128:
        raise InvalidArgument("the device id is not valid")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise InvalidArgument("the account service address must start with https://")
    public_key(paths, runner)
    os.makedirs(_dir(paths), mode=0o700, exist_ok=True)
    temporary = link_path(paths) + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"id": device_id, "url": url.rstrip("/")}, handle)
    os.replace(temporary, link_path(paths))


def unlink(paths):
    """Forget the link (and the key, so a later link is a new device). True if there was one."""
    was = bool(link_state(paths))
    for path in (link_path(paths), key_path(paths), sealed_path(paths)):
        _remove(path)
    return was


def report(paths, runner=run):
    """What a check-in would carry, as read from this system now. A fact that cannot be read is left out, not guessed."""
    out = {"schema": SCHEMA}
    release = osinfo.os_release(paths["os_release"])
    if release.get("IMAGE_VERSION"):
        out["version"] = release["IMAGE_VERSION"]
    out["arch"] = os.uname().machine
    try:
        boot = slots.boot_state(paths)
    except (Unavailable, Failed):
        boot = {}
    if boot.get("running_slot") in ("a", "b"):
        out["slot"] = boot["running_slot"]
    if boot.get("assessment") in ("good", "pending"):
        out["boot_ok"] = boot["assessment"] == "good"
    out["update_state"] = "pending-reboot" if updates.pending(paths) else "idle"
    try:
        facts = encryption.status(paths, runner)
        out["encrypted"] = bool(facts.get("encrypted"))
        out["tpm"] = bool(facts.get("tpm_present"))
    except (Unavailable, Failed):
        pass
    return {key: out[key] for key in REPORT_FIELDS if key in out}


def last(paths):
    """{"at": epoch seconds of the last attempt, "error": "" when it worked} or {} before the first."""
    try:
        with open(last_path(paths), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("at"), int) else {}


def _record(paths, at, error):
    os.makedirs(_dir(paths), mode=0o700, exist_ok=True)
    temporary = last_path(paths) + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"at": int(at), "error": error}, handle)
    os.replace(temporary, last_path(paths))


def status(paths, enabled, url):
    """What a client shows about this computer's link. `enabled` and `url` are the settings (devices.enabled, devices.url)."""
    state, seen = link_state(paths), last(paths)
    return {"linked": bool(state), "id": state.get("id", ""), "url": state.get("url") or url, "enabled": bool(enabled),
            "key_protection": key_protection(paths), "last_attempt": seen.get("at", 0), "last_error": seen.get("error", "")}


def signed_request(paths, body, now=time.time, runner=run):
    """(url, headers, body bytes) for one check-in: the report, signed with the device key."""
    state = link_state(paths)
    if not state:
        raise Unavailable("this computer is not linked to an account")
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    if len(raw) > BODY_MAX:
        raise Failed("the report is too large")
    timestamp = str(int(now()))
    message = f"{state['id']}.{timestamp}.{raw}".encode()
    signature = _b64url(_private_key(paths, False, runner).sign(message))
    url = f"{state['url']}/api/oauth/me/devices/{state['id']}/checkin"
    # A User-Agent is required in practice: a CDN in front of the account service refuses the bare "Python-urllib" one (HTTP 403).
    return url, {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT, "X-Device-Timestamp": timestamp,
                 "X-Device-Signature": signature}, raw.encode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None             # a redirect is an error: the report goes to the address that was linked, nowhere else


def _error_code(error):
    """The `error` field of the account service's answer, reading at most ANSWER_MAX bytes of it."""
    try:
        return str(json.loads(error.read(ANSWER_MAX).decode(errors="replace")).get("error", ""))[:64]
    except (ValueError, OSError, AttributeError):
        return ""


def _request(url, headers, data, opener=None):
    """(HTTP status, error code, name) of one POST, status 0 when the service cannot be reached. `name` is what the account calls this
    computer, from a successful answer (at most ANSWER_MAX bytes of it are read). Redirects are errors; certificates are checked."""
    opener = opener or urllib.request.build_opener(_NoRedirect).open
    try:
        with opener(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=TIMEOUT) as answer:
            try:
                name = json.loads(answer.read(ANSWER_MAX).decode("utf-8", "replace")).get("name", "")
            except (ValueError, OSError, AttributeError):
                name = ""
            return 200, "", name if isinstance(name, str) else ""
    except urllib.error.HTTPError as error:
        return error.code, _error_code(error), ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, "", ""


def drop_privileges(setgroups=os.setgroups, setgid=os.setgid, setuid=os.setuid, getuid=os.getuid, lookup=pwd.getpwnam):
    """Give up root for good, before touching the network. Does nothing when not root (tests, development). Refuses to go on if root
    could be taken back."""
    if getuid() != 0:
        return False
    try:
        entry = lookup("nobody")
        uid, gid = entry.pw_uid, entry.pw_gid
    except KeyError:
        uid = gid = 65534
    setgroups([])
    setgid(gid)
    setuid(uid)
    try:
        setuid(0)
    except PermissionError:
        return True
    raise Failed("could not give up root before contacting the account service")


def send(url, headers, data, opener=None):
    """POST in a child process that has dropped root, with a time limit, and read back only (status, error code, name). The key was used
    before this (the request is already signed), so nothing the server sends can reach it, or anything else root can."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:                       # the child: never returns
        code = 1
        try:
            os.close(read_fd)
            signal.alarm(TOTAL_TIMEOUT)
            drop_privileges()
            status_, error, name = _request(url, headers, data, opener)
            os.write(write_fd, json.dumps({"status": status_, "error": error, "name": name}).encode())
            code = 0
        except BaseException:          # noqa: BLE001 - nothing may escape a forked child
            pass
        os._exit(code)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as handle:
        answer = handle.read(1024)
    os.waitpid(pid, 0)
    try:
        result = json.loads(answer.decode())
        return int(result["status"]), str(result["error"]), str(result.get("name", ""))
    except (ValueError, KeyError, TypeError):
        return 0, "", ""


def hostname_from_name(name):
    """The host name that goes with a computer's name: spaces become hyphens, the ends are trimmed, and only letters, digits and
    hyphens stay (what a host name may hold), at most 63 characters. None when nothing usable is left. "Ersa Workstation" is
    "Ersa-Workstation"."""
    if not isinstance(name, str):
        return None
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^A-Za-z0-9-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")[:63].strip("-")
    return text or None


def set_hostname(paths, hostname):
    """Change the host name through systemd-hostnamed (the static one, kept across restarts, and the running one)."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM if paths.get("bus", "system") == "system" else Gio.BusType.SESSION, None)
    for method in ("SetStaticHostname", "SetHostname"):
        try:
            bus.call_sync("org.freedesktop.hostname1", "/org/freedesktop/hostname1", "org.freedesktop.hostname1", method,
                          GLib.Variant("(sb)", (hostname, False)), None, Gio.DBusCallFlags.NONE, 15000, None)
        except GLib.Error as error:
            raise Failed(f"the host name could not be changed: {error.message}") from error


def _remember_name(paths, name):
    state = link_state(paths)
    if state:
        state["name"] = name
        temporary = link_path(paths) + ".new"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, link_path(paths))


def sync_hostname(paths, name, enabled=True, setter=set_hostname):
    """Make the host name follow the account's name for this computer, once per change: a name that is the same as the last one seen
    leaves the host name alone, so a name the person sets by hand afterwards stays. Returns the new host name, or None."""
    state = link_state(paths)
    if not enabled or not state or not name or name == state.get("name"):
        return None
    wanted = hostname_from_name(name)
    _remember_name(paths, name)
    if not wanted or wanted == socket.gethostname():
        return None
    setter(paths, wanted)
    return wanted


def checkin_if_due(paths, enabled, due=True, interval=DAY, now=time.time, sender=send, runner=run, follow_name=True, setter=set_hostname):
    """Check in when this computer is linked, reporting is on and (if `due`) `interval` seconds have passed since the last attempt.
    Returns "sent", "off", "not linked" or "not due". A failed attempt is recorded (and raised), and is not tried again before the
    interval has passed, so a service that is down is asked once a day and not at every run."""
    if not enabled:
        return "off"
    if not link_state(paths):
        return "not linked"
    seen = last(paths)
    if due and seen and now() - seen["at"] < interval:
        return "not due"
    try:
        sent = checkin(paths, sender, now, runner)
    except (Failed, Unavailable) as error:
        _record(paths, now(), str(error))
        raise
    _record(paths, now(), "")
    try:
        sync_hostname(paths, sent.get("_name", ""), follow_name, setter)
    except Failed as error:                      # the report went through; only the host name could not be changed
        _record(paths, now(), str(error))
    return "sent"


REFUSALS = {
    ("device_clock_skew", 401): "the account service says this computer's clock is too far off",
    ("device_replayed", 409): "the account service says this report is not newer than the last one (is the clock set back?)",
    ("rate_limited", 429): "the account service is limiting how often this computer may report",
    ("payload_too_large", 413): "the account service says the report is too large",
    ("device_report_invalid", 400): "the account service does not accept the report",
}


def checkin(paths, sender=send, now=time.time, runner=run):
    """Send one check-in. Returns the report that was sent. The link is never dropped because of a refusal: the account service answers
    the same way for a computer unlinked on the website and for a signature that is wrong for another reason, and a wrong clock is its
    own answer. The person unlinks (or links again) deliberately."""
    body = report(paths, runner)
    url, headers, data = signed_request(paths, body, now, runner)
    answer = sender(url, headers, data)
    status_, error, name = (tuple(answer) + ("",))[:3]
    if status_ == 200:
        return dict(body, _name=name) if name else body
    if status_ == 0:
        raise Unavailable("the account service cannot be reached")
    if status_ == 401 and error != "device_clock_skew":
        raise Failed("the account service does not accept this device; it may have been unlinked on the website")
    raise Failed(REFUSALS.get((error, status_)) or f"the account service refused the report (HTTP {status_})")
