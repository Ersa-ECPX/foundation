"""Link this computer to an Ersa ID account: the person's side (docs/devices-and-telemetry.md).

Only a client of foundationd. The key lives in foundationd (sealed to the TPM when there is one): this code asks it for the public key
and for a signature over the account service's challenge, registers the key with the account service using a sign-in of the person's
own, and tells foundationd the id it got back. That sign-in asks for one permission (`devices:write`), is used for these few requests
and is not stored: nothing here, and nothing in foundationd, keeps the person's token.
"""

import json
import time
import urllib.error
import urllib.request

from . import identity
from .fetch import url_allowed

SCOPE = "devices:write"
MESSAGES = {
    "insufficient_scope": "the account service did not accept this sign-in for linking computers. The app this computer signs in with "
                          "must be allowed to use devices:write, and be owned by a site administrator.",
    "device_proof_invalid": "the account service could not verify this computer's proof of its key; try again.",
    "device_already_linked": "this computer's key is already linked to an account. Run `foundation devices unlink` here (that makes a "
                             "new key), remove the old entry on the website, and link again.",
    "devices_limit": "this account already has the most computers it can link; remove one on the website first.",
    "rate_limited": "the account service is limiting linking right now; try again in a few minutes.",
    "unauthorized": "the account service did not accept the sign-in.",
}


class DevicesError(Exception):
    """Something a person can act on."""


def _post(url, body, token, opener=urllib.request.urlopen):
    """POST JSON with the person's token; returns the answer as a dict. Errors become DevicesError with a plain message."""
    if not url_allowed(url):
        raise DevicesError(f"not an https address: {url}")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
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


def link(client, provider, url, name, opener=urllib.request.urlopen, announce=print, sleep=time.sleep, now=time.time):
    """Sign in for this one purpose, register this computer's key and record the id. Returns the device id."""
    url = url.rstrip("/")
    narrow = dict(provider, scopes=SCOPE)
    try:
        started = identity.start_login(narrow, opener)
        announce(f"Open {started.get('verification_uri_complete') or started['verification_uri']} on any device")
        if not started.get("verification_uri_complete"):
            announce(f"and enter the code {started['user_code']}")
        announce("Waiting for you to approve it…")
        token = identity.finish_login(narrow, started, sleep, now, opener)["access_token"]
    except identity.IdentityError as error:
        raise DevicesError(str(error)) from error
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
    return made["id"]
