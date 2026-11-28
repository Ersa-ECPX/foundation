"""An OpenID Connect client for the person's identity provider (Ersa ID by default), with no dependency beyond the standard library.

Which provider is a setting, not code: `identity.issuer` and `identity.client_id` name it (the defaults are Ersa ID), and
`identity.enabled=false` switches the whole thing off. Both can be set from a preseed file, so a company points a fleet at its own
provider or turns the feature off. Any provider that publishes OIDC discovery and the device authorization grant (RFC 8628) works.

What is here: discovery, the device flow, refresh, revocation, and a small per-user session file. What is not here, on purpose:
verifying the ID token's signature. The token comes straight from the provider's token endpoint over TLS, which OpenID Connect Core
(3.1.3.7) accepts without a signature check; the issuer, audience and expiry are checked. A service that receives a token from someone
else (a backend that is an OIDC client of the same provider) must verify the signature against the provider's keys itself.

The session file holds the refresh token in the person's state directory, mode 0600. A keyring is the better place; that is not built.
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .fetch import url_allowed

DISCOVERY = "/.well-known/openid-configuration"
# devices:write lets this computer link itself to the account (docs/devices-and-telemetry.md). If the provider does not allow the app that
# permission, the sign-in still works and simply grants less; linking then says what is missing.
DEFAULT_SCOPES = "openid profile email offline_access devices:read devices:write"
TIMEOUT = 15
MAX_BODY = 256 * 1024
MAX_PICTURE = 2 * 1024 * 1024
PROFILE_CLAIMS = ("sub", "name", "preferred_username", "email", "email_verified", "picture")


class IdentityError(Exception):
    """Something a person can act on: the message says what and, where possible, why."""


def settings_from(values):
    """The provider description from configuration values (`identity.*`); `enabled` is False when switched off or not set up."""
    issuer = str(values.get("identity.issuer", "") or "").strip().rstrip("/")
    client = str(values.get("identity.client_id", "") or "").strip()
    scopes = str(values.get("identity.scopes", "") or "").strip() or DEFAULT_SCOPES
    enabled = bool(values.get("identity.enabled", True))
    return {"enabled": enabled, "issuer": issuer, "client_id": client, "scopes": scopes,
            "configured": bool(issuer and client)}


def _request(url, data=None, opener=urllib.request.urlopen):
    if not url_allowed(url):
        raise IdentityError(f"not an https address: {url}")
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers={"Accept": "application/json", "User-Agent": "foundation-identity"})
    try:
        with opener(request, timeout=TIMEOUT) as response:
            text = response.read(MAX_BODY).decode("utf-8")
            return json.loads(text) if text.strip() else {}          # revocation answers 200 with nothing
    except urllib.error.HTTPError as error:        # OAuth errors come as 400 with a JSON body that the caller needs
        try:
            return json.loads(error.read(MAX_BODY).decode("utf-8"))
        except (ValueError, OSError):
            raise IdentityError(f"{url}: HTTP {error.code}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise IdentityError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from error


def discover(issuer, opener=urllib.request.urlopen):
    """The provider's metadata, checked: the issuer must be the one asked for and the endpoints must be web addresses."""
    if not issuer:
        raise IdentityError("no identity provider is set (identity.issuer)")
    meta = _request(issuer.rstrip("/") + DISCOVERY, opener=opener)
    if not isinstance(meta, dict) or str(meta.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        raise IdentityError(f"the provider at {issuer} names a different issuer; refusing it")
    for key in ("token_endpoint", "device_authorization_endpoint", "revocation_endpoint", "userinfo_endpoint", "jwks_uri"):
        if meta.get(key) and not url_allowed(str(meta[key])):
            raise IdentityError(f"the provider's {key} is not an https address")
    return meta


def check(provider, opener=urllib.request.urlopen):
    """What `foundation identity check` reports: each thing this client needs from the provider, as (name, ok, detail)."""
    rows = []
    try:
        meta = discover(provider["issuer"], opener=opener)
    except IdentityError as error:
        return [("Discovery", False, str(error))]
    rows.append(("Discovery", True, provider["issuer"]))
    rows.append(("Device sign-in", bool(meta.get("device_authorization_endpoint")),
                 "supported" if meta.get("device_authorization_endpoint") else "the provider has no device authorization endpoint"))
    grants = meta.get("grant_types_supported")
    rows.append(("Refresh tokens", grants is None or "refresh_token" in grants,
                 "supported" if grants is None or "refresh_token" in grants else "refresh_token grant not offered"))
    scopes = meta.get("scopes_supported")
    missing = [s for s in provider["scopes"].split() if scopes and s not in scopes]
    rows.append(("Scopes", not missing, "all offered" if not missing else "not offered: " + ", ".join(missing)))
    rows.append(("Revocation", bool(meta.get("revocation_endpoint")),
                 "supported" if meta.get("revocation_endpoint") else "not offered (sign-out only forgets the tokens here)"))
    return rows


# -- the device flow ---------------------------------------------------------------------------

def start_login(provider, opener=urllib.request.urlopen):
    """Begin the device flow. Returns the provider's answer (`user_code`, `verification_uri`, `device_code`, `interval`, `expires_in`)."""
    meta = discover(provider["issuer"], opener=opener)
    endpoint = meta.get("device_authorization_endpoint")
    if not endpoint:
        raise IdentityError("this provider does not offer device sign-in")
    answer = _request(endpoint, {"client_id": provider["client_id"], "scope": provider["scopes"]}, opener=opener)
    if "error" in answer or not answer.get("device_code") or not answer.get("user_code"):
        raise IdentityError(_describe(answer) or "the provider refused to start a sign-in")
    answer["_token_endpoint"] = meta["token_endpoint"]
    answer["_meta"] = {k: meta.get(k) for k in ("revocation_endpoint", "userinfo_endpoint")}
    return answer


def finish_login(provider, started, sleep=time.sleep, now=time.time, opener=urllib.request.urlopen, on_wait=None):
    """Poll until the person approves (or refuses, or the code expires). Returns the session and does not store it."""
    interval = max(int(started.get("interval") or 5), 1)
    deadline = now() + int(started.get("expires_in") or 600)
    while now() < deadline:
        sleep(interval)
        answer = _request(started["_token_endpoint"], {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": started["device_code"],
            "client_id": provider["client_id"]}, opener=opener)
        error = answer.get("error")
        if not error:
            return _session(provider, started["_token_endpoint"], answer, started.get("_meta", {}), now())
        if error == "authorization_pending":
            if on_wait:
                on_wait()
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise IdentityError("the sign-in was refused")
        if error == "expired_token":
            break
        raise IdentityError(_describe(answer))
    raise IdentityError("the sign-in code expired before it was approved")


def _describe(answer):
    return (answer.get("error_description") or answer.get("error") or "") if isinstance(answer, dict) else ""


def id_claims(provider, token, now=time.time):
    """The claims of an ID token received from the token endpoint, with issuer, audience and expiry checked (not the signature)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as error:
        raise IdentityError("the provider sent an ID token that cannot be read") from error
    if str(claims.get("iss", "")).rstrip("/") != provider["issuer"]:
        raise IdentityError("the ID token was issued by a different provider")
    audience = claims.get("aud")
    if provider["client_id"] not in (audience if isinstance(audience, list) else [audience]):
        raise IdentityError("the ID token was issued for a different application")
    if float(claims.get("exp", 0)) < now() - 60:
        raise IdentityError("the ID token has already expired")
    return claims


def _session(provider, token_endpoint, answer, meta, now):
    if not answer.get("access_token"):
        raise IdentityError("the provider sent no access token")
    claims = id_claims(provider, answer["id_token"], now=lambda: now) if answer.get("id_token") else {}
    return {"issuer": provider["issuer"], "client_id": provider["client_id"], "token_endpoint": token_endpoint,
            "revocation_endpoint": meta.get("revocation_endpoint"), "userinfo_endpoint": meta.get("userinfo_endpoint"), "access_token": answer["access_token"],
            "refresh_token": answer.get("refresh_token"), "scope": answer.get("scope", ""), "expires_at": now + int(answer.get("expires_in") or 300),
            "claims": {k: claims[k] for k in PROFILE_CLAIMS if k in claims}}


# -- the session of this person on this computer ------------------------------------------------

def session_path(issuer, state_home=None):
    base = state_home or os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ersa-id", hashlib.sha256(issuer.encode()).hexdigest()[:16] + ".json")


def load_session(issuer, state_home=None):
    try:
        with open(session_path(issuer, state_home), encoding="utf-8") as handle:
            session = json.load(handle)
    except (OSError, ValueError):
        return None
    return session if isinstance(session, dict) and session.get("issuer") == issuer else None


def save_session(session, state_home=None):
    path = session_path(session["issuer"], state_home)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(session, handle)
    os.replace(temporary, path)
    return path


def access_token(provider, state_home=None, now=time.time, opener=urllib.request.urlopen):
    """A usable access token for this person, refreshed when it is about to expire; None when nobody is signed in."""
    session = load_session(provider["issuer"], state_home)
    if not session or session.get("client_id") != provider["client_id"]:
        return None
    if session["expires_at"] - 30 > now():
        return session["access_token"]
    if not session.get("refresh_token"):
        return None
    answer = _request(session["token_endpoint"], {"grant_type": "refresh_token", "refresh_token": session["refresh_token"],
                                                  "client_id": provider["client_id"]}, opener=opener)
    if answer.get("error") or not answer.get("access_token"):
        os.unlink(session_path(provider["issuer"], state_home))      # the grant is gone (revoked, expired): sign in again
        return None
    if load_session(provider["issuer"], state_home) is None:       # signed out while the provider was answering: sign-out wins
        return None
    session["access_token"] = answer["access_token"]
    session["expires_at"] = now() + int(answer.get("expires_in") or 300)
    session["refresh_token"] = answer.get("refresh_token") or session["refresh_token"]      # providers that rotate send a new one
    save_session(session, state_home)
    return session["access_token"]


def logout(provider, state_home=None, opener=urllib.request.urlopen):
    """Forget the session here and, where the provider offers it, revoke the refresh token there. Returns what happened."""
    session = load_session(provider["issuer"], state_home)
    if not session:
        return "not signed in"
    revoked = False
    # the refresh token is the long-lived one; a provider that issued none leaves only the access token to revoke
    token, hint = ((session["refresh_token"], "refresh_token") if session.get("refresh_token")
                   else (session.get("access_token"), "access_token"))
    if session.get("revocation_endpoint") and token:
        try:
            _request(session["revocation_endpoint"], {"token": token, "token_type_hint": hint, "client_id": provider["client_id"]},
                     opener=opener)
            revoked = True
        except IdentityError:
            pass
    os.unlink(session_path(provider["issuer"], state_home))
    return "signed out" if revoked else "signed out here (the provider could not be told to revoke it)"


def session_has_scope(session, scope):
    """Whether the provider granted `scope` at sign-in (the scope field of the token answer, space or comma separated)."""
    granted = str((session or {}).get("scope", "")).replace(",", " ").split()
    return scope in granted


# -- the profile ----------------------------------------------------------------------------------

def refresh_profile(provider, state_home=None, now=time.time, opener=urllib.request.urlopen):
    """Fetch the person's current profile from the provider's userinfo endpoint and keep it in the session. Returns the claims,
    or None when nobody is signed in or the provider has no userinfo endpoint (the claims from sign-in stay)."""
    token = access_token(provider, state_home, now=now, opener=opener)
    session = load_session(provider["issuer"], state_home) if token else None
    endpoint = (session or {}).get("userinfo_endpoint")
    if not endpoint:
        return None
    request = urllib.request.Request(endpoint, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                                        "User-Agent": "foundation-identity"})
    if not url_allowed(endpoint):
        return None
    try:
        with opener(request, timeout=TIMEOUT) as response:
            info = json.loads(response.read(MAX_BODY).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("sub") != session["claims"].get("sub"):
        return None                                  # the answer must be about the same person
    # The answer can arrive after the person signed out here (the request takes a moment). Writing the old session back would sign
    # them in again, so write only if the same person is still signed in.
    current = load_session(provider["issuer"], state_home)
    if current is None or current.get("claims", {}).get("sub") != session["claims"].get("sub"):
        return None
    session["claims"].update({k: info[k] for k in PROFILE_CLAIMS if k in info and isinstance(info[k], (str, bool))})
    save_session(session, state_home)
    return session["claims"]


def fetch_picture(url, opener=urllib.request.urlopen):
    """The profile picture's bytes (an https address, at most 2 MiB), or None. Not decoded here: the caller's image loader decides
    whether it is a picture."""
    if not url or not url_allowed(url):
        return None
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": "foundation-identity"}), timeout=TIMEOUT) as response:
            data = response.read(MAX_PICTURE + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if 0 < len(data) <= MAX_PICTURE else None


# -- offering the sign-in once ----------------------------------------------------------------------

def offer_marker(state_home=None):
    """The file that records that this person has been asked (and either signed in or said not now)."""
    base = state_home or os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ersa-id", "offered")


def offer_needed(provider, state_home=None):
    """True when the sign-in should be offered now: identity is on, a provider is set, nobody is signed in, and the person has not
    been asked before. Asking is per person, because the session is: it is not the machine's `provisioned` flag, which latches when
    Initial Setup ends, the very moment the offer is due."""
    if not (provider["enabled"] and provider["configured"]):
        return False
    if os.path.exists(offer_marker(state_home)) or load_session(provider["issuer"], state_home):
        return False
    return True


def mark_offered(state_home=None):
    path = offer_marker(state_home)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("")
    return path
