"""Link this computer to an Ersa ID account: the person's side (docs/devices-and-telemetry.md).

Only a client of foundationd. The key lives in foundationd (sealed to the TPM when there is one): this code asks it for the public key
and for a signature over the account service's challenge, registers the key with the account service using the person's stored
Ersa ID sign-in (the one `foundation identity login` and Deck make; it asks for `devices:write` along with the rest), and tells
foundationd the id it got back. There is one sign-in for everything: nothing here asks for another. foundationd never sees the token.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import identity
from .fetch import url_allowed

SCOPE = "devices:write"
READ_SCOPE = "devices:read"
MESSAGES = {
    "insufficient_scope": "the account service did not accept this sign-in for linking computers. The app this computer signs in with "
                          "must be allowed to use devices:write, and be owned by a site administrator.",
    "device_proof_invalid": "the account service could not verify this computer's proof of its key; try again.",
    "device_already_linked": "this computer's key is already linked to an account. Run `foundation devices unlink` here (that makes a "
                             "new key), remove the old entry on the website, and link again.",
    "devices_limit": "this account already has the most computers it can link; remove one on the website first.",
    "rate_limited": "the account service is limiting linking right now; try again in a few minutes.",
    "unauthorized": "the account service did not accept the sign-in.",
    "device_not_found": "the account service does not know this computer.",
}


class DevicesError(Exception):
    """Something a person can act on."""


def _post(url, body, token, opener=urllib.request.urlopen, method="POST"):
    """Send JSON with the person's token; returns the answer as a dict. Errors become DevicesError with a plain message."""
    if not url_allowed(url):
        raise DevicesError(f"not an https address: {url}")
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "foundation-devices"})
    try:
        with opener(request, timeout=identity.TIMEOUT) as response:
            return json.loads(response.read(identity.MAX_BODY).decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            code = str(json.loads(error.read(identity.MAX_BODY).decode("utf-8")).get("error", ""))
        except (ValueError, OSError):
            code = ""
        raise DevicesError(MESSAGES.get(code) or f"the account service refused the request (HTTP {error.code}{', ' + code if code else ''})") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise DevicesError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from error


def link_with_token(client, url, name, token, opener=urllib.request.urlopen):
    """Register this computer's key with the account service using `token` and record the id. Returns the device id."""
    url = url.rstrip("/")
    challenge = _post(f"{url}/api/oauth/me/devices/challenge", {}, token, opener).get("challenge")
    if not challenge:
        raise DevicesError("the account service did not hand out a challenge")
    proof = client.one("Devices", "SignLink", "(s)", challenge)
    made = _post(f"{url}/api/oauth/me/devices", {"name": name, "public_key": proof["public_key"], "challenge": challenge,
                                                   "signature": proof["signature"]}, token, opener)
    if not made.get("id"):
        raise DevicesError("the account service did not return a device id")
    try:
        client.call("Devices", "CompleteLink", "(s)", made["id"])
    except Exception as error:      # noqa: BLE001 - the account side has the computer now; say how to clean up
        raise DevicesError(f"the computer was registered with the account service (id {made['id']}) but could not be recorded here: "
                           f"{error}. Remove it on the website and try again.") from error
    try:
        client.one("Devices", "CheckinNow")      # shows it on the account with its report at once, and brings back its name
    except Exception:               # noqa: BLE001 - linked is linked; the daily check-in will report
        pass
    return made["id"]


def link_with_session(client, provider, url, name, opener=urllib.request.urlopen, state_home=None):
    """Link using the sign-in that is already stored on this computer, when it was granted devices:write. Nothing to approve again.
    Raises DevicesError with what to do when nobody is signed in or the permission was not granted."""
    session = identity.load_session(provider["issuer"], state_home)
    if session and not identity.session_has_scope(session, SCOPE):
        raise DevicesError("This sign-in was not allowed to link computers. Sign in again to allow it; if that does not change, the "
                           "account service has not allowed this app to link computers (the app must be owned by a site administrator "
                           "and permitted devices:write).")
    return link_with_token(client, url, name, _session_token(provider, opener, state_home), opener)


def _session_token(provider, opener, state_home, scope=SCOPE):
    """The stored sign-in's token, when there is one that was allowed to use `scope`."""
    session = identity.load_session(provider["issuer"], state_home)
    if not session:
        raise DevicesError("Sign in to Ersa ID first.")
    if not identity.session_has_scope(session, scope):
        raise DevicesError("This sign-in was not allowed to " + ("see your computers" if scope == READ_SCOPE else "manage computers")
                           + ". Sign in again to allow it.")
    try:
        token = identity.access_token(provider, state_home, opener=opener)
    except identity.IdentityError as error:
        raise DevicesError(str(error)) from error
    if not token:
        raise DevicesError("The sign-in has expired. Sign in again.")
    return token


def list_devices(provider, url, opener=urllib.request.urlopen, state_home=None):
    """The computers linked to the account, from the account service, with the stored sign-in: a list of dicts with id, name, created_at,
    last_seen_at and report (what it last reported, or None)."""
    token = _session_token(provider, opener, state_home, READ_SCOPE)
    answer = _post(f"{url.rstrip('/')}/api/oauth/me/devices", None, token, opener, method="GET")
    found = answer.get("devices")
    if not isinstance(found, list):
        raise DevicesError("the account service sent a list of computers this version cannot read")
    return [d for d in found if isinstance(d, dict) and d.get("id") and isinstance(d.get("name"), str)]


def unlink(client, provider, opener=urllib.request.urlopen, state_home=None):
    """Remove this computer from the account on the website, then forget the link and key here. A computer the account no longer knows
    (it was removed on the website already) counts as removed. Raises DevicesError, leaving everything as it was, when the account
    could not be told: the person can then unlink here only (`unlink_here`). Returns whether the computer was linked."""
    status = client.one("Devices", "Status")
    if status.get("linked"):
        token = _session_token(provider, opener, state_home)
        try:
            _post(f"{status['url'].rstrip('/')}/api/oauth/me/devices/{urllib.parse.quote(status['id'], safe='')}", None, token, opener,
                  method="DELETE")
        except DevicesError as error:
            if "does not know this computer" not in str(error):
                raise
    return unlink_here(client)


def unlink_here(client):
    """Forget the link and key on this computer only; the account keeps listing it until it is removed on the website."""
    return bool(client.one("Devices", "Unlink")["was_linked"])


def link(client, provider, url, name, opener=urllib.request.urlopen, state_home=None):
    """Link this computer with the sign-in stored on it, the same one Deck and `foundation identity` use. Returns the device id."""
    return link_with_session(client, provider, url, name, opener, state_home)
