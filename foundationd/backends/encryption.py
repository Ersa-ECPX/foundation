"""Disk encryption: the data volume (`/var` and `/home`) as a LUKS2 container that the computer's TPM unlocks.

How it fits together (docs/features/encryption.md):

  * `systemd-repart` creates the volume encrypted (`Encrypt=key-file`) with a random one-time key, and fills it (deploy.py).
  * The key slots that stay are made here with `systemd-cryptenroll`: a recovery key, always (made by the client, so the service never
    sends a secret back); the TPM, when the computer has one; a passphrase, when the person gave one (the way in without a TPM).
    The one-time key's slot is then wiped.
  * The TPM slot is not bound to the measurements of one boot image but to a *signed policy*: the kernel image carries a signature
    (`.pcrsig`) over what its measurement will be, and the slot trusts the signer's public key (`.pcrpkey`, also in the kernel image).
    So every update, which brings a new signed kernel image, still unlocks the disk, and an image signed by nobody does not.
  * The initrd unlocks the volume (`mkosi.initrd.extra/usr/libexec/ecp/data-link`).

Secrets reach the worker as a private file staged by the service (never as arguments of a job), and tools get them through their
environment, never on a command line.
"""

import json
import os
import re

from ..errors import Failed, InvalidArgument
from . import osinfo, verity
from .runner import check, run

MIN_PASSPHRASE = 8
# The client makes the key (foundation/recovery.py); this is how it must look: eight blocks of eight letters of systemd's alphabet.
RECOVERY_KEY = re.compile("^[cbdefghijklnrtuv]{8}(-[cbdefghijklnrtuv]{8}){7}$")
RECOVERY_SLOT_LABEL = "ecp-recovery"


def parse_secrets(text):
    """The checked secrets: {"recovery_key": str, "passphrase": str ("" when none)}. InvalidArgument names the problem."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as error:
        raise InvalidArgument("the encryption secrets are not valid") from error
    if not isinstance(data, dict):
        raise InvalidArgument("the encryption secrets are not valid")
    key, passphrase = data.get("recovery_key", ""), data.get("passphrase", "") or ""
    if not (isinstance(key, str) and RECOVERY_KEY.match(key)):
        raise InvalidArgument("the recovery key is not in the expected form")
    if not isinstance(passphrase, str) or (passphrase and len(passphrase) < MIN_PASSPHRASE):
        raise InvalidArgument(f"the passphrase must be at least {MIN_PASSPHRASE} characters")
    if "\n" in passphrase or "\0" in passphrase:
        raise InvalidArgument("the passphrase cannot contain a line break")
    return {"recovery_key": key, "passphrase": passphrase}


def tpm_present(paths):
    """Whether this computer has a TPM 2.0 that systemd can use."""
    return os.path.exists(os.path.join(paths.get("tpm_class", "/sys/class/tpm"), "tpm0")) and os.path.exists(paths.get("tpmrm", "/dev/tpmrm0"))


def partition_node(disk, number):
    """The device node of a partition: /dev/vda -> /dev/vda7, but /dev/nvme0n1 -> /dev/nvme0n1p7."""
    return f"{disk}{'p' if disk[-1].isdigit() else ''}{number}"


def pcr_public_key(uki_path):
    """The public key the kernel image's PCR signature is made with (its `.pcrpkey` section, a PEM), or None when the image has none."""
    data = verity.section_of_file(uki_path, b".pcrpkey")
    return data.rstrip(b"\0") if data else None


def plan(paths, uki_path, passphrase):
    """What this computer can do: {"tpm": bool, "pubkey": bytes|None}; raises InvalidArgument when no way to unlock would remain."""
    pubkey = pcr_public_key(uki_path)
    tpm = tpm_present(paths) and pubkey is not None
    if not tpm and not passphrase:
        why = "this computer has no usable TPM" if not tpm_present(paths) else "the system image carries no signed boot policy"
        raise InvalidArgument(f"{why}, so encryption needs a passphrase to unlock the disk")
    return {"tpm": tpm, "pubkey": pubkey}


def enroll(device, key_file, secrets, plan_, stage, runner=run):
    """Turn the one-time key of a fresh encrypted volume into the keys that stay. Returns what was set up."""
    base = ["systemd-cryptenroll", f"--unlock-key-file={key_file}"]
    check(base + ["--password", device], 120, runner=runner, env={"NEWPASSWORD": secrets["recovery_key"]})
    done = {"recovery_key": True, "tpm": False, "passphrase": False}
    if secrets["passphrase"]:
        check(base + ["--password", device], 120, runner=runner, env={"NEWPASSWORD": secrets["passphrase"]})
        done["passphrase"] = True
    if plan_["tpm"]:
        pubkey = os.path.join(stage, "pcr-public-key.pem")
        with open(pubkey, "wb") as handle:
            handle.write(plan_["pubkey"])
        # Only the signed policy (PCR 11, the kernel image's own measurement) is bound: not the Secure Boot state, which changes
        # with the firmware's key database. Binding PCR 7 as well is a later, stronger option (docs/features/encryption.md).
        check(base + ["--tpm2-device=auto", "--tpm2-pcrs=", f"--tpm2-public-key={pubkey}", "--tpm2-public-key-pcrs=11", device],
              120, runner=runner)
        done["tpm"] = True
    # the first slot is the one-time key; the recovery key, passphrase and TPM were added after it
    check(["systemd-cryptenroll", f"--unlock-key-file={key_file}", "--wipe-slot=0", device], 120, runner=runner)
    return done


def new_key_file(stage):
    """A random one-time key in a private file; the caller deletes the stage directory."""
    path = os.path.join(stage, "one-time.key")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(os.urandom(64))
    return path


def read_secrets_file(path):
    """Read and delete the staged secrets file."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(8192)
    except OSError as error:
        raise Failed(f"cannot read the encryption secrets: {error}") from error
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return parse_secrets(text)


def status(paths, runner=run):
    """Facts about the encryption of the data volume, read only (for Deck). Keys: tpm_present (b), encrypted (b); when encrypted also
    cipher (s), tpm_slots, recovery_slots, fido2_slots and password_slots (counts of the key slots of each kind).

    A key slot is told by the token that names it: `systemd-tpm2`, `systemd-recovery` and `systemd-fido2` (what systemd-cryptenroll
    writes). A slot no token names is a password slot: it holds the recovery key the installer made (a recovery key enrolled by this
    platform is a password slot, so it is among these), or a passphrase. The key material itself is never read."""
    result = {"tpm_present": tpm_present(paths), "encrypted": False}
    source = (osinfo.mounts(paths["mountinfo"]).get("/var") or {}).get("source", "")
    if not source.startswith("/dev/mapper/"):
        return result
    code, out, _err = runner(["cryptsetup", "status", os.path.basename(source)], 30)
    fields = dict(line.strip().split(":", 1) for line in out.splitlines() if ":" in line) if code == 0 else {}
    fields = {key.strip(): value.strip() for key, value in fields.items()}
    device = fields.get("device")
    if not device or not fields.get("type", "").upper().startswith("LUKS"):
        return result
    code, out, _err = runner(["cryptsetup", "luksDump", "--dump-json-metadata", device], 30)
    if code != 0:
        raise Failed("cannot read the encryption keys of the data volume")
    try:
        metadata = json.loads(out)
        keyslots, tokens = metadata.get("keyslots", {}), metadata.get("tokens", {})
    except (ValueError, AttributeError) as error:
        raise Failed("cannot read the encryption keys of the data volume") from error
    named = {str(slot) for token in tokens.values() for slot in token.get("keyslots", [])}
    count = lambda kind: sum(1 for token in tokens.values() if token.get("type") == kind)
    result.update({"encrypted": True, "cipher": fields.get("cipher", ""), "tpm_slots": count("systemd-tpm2"),
                   "recovery_slots": count("systemd-recovery"), "fido2_slots": count("systemd-fido2"),
                   "password_slots": len([slot for slot in keyslots if slot not in named])})
    return result
