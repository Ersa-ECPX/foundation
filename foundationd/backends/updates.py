"""Installing a system image update into the standby slot.

An update is one archive (`.tar.zst`, or a plain `.tar`) holding exactly:

    manifest.json        first in the archive: format, name, version, architecture, and the size and SHA-256
                         of the two files below
    manifest.json.sig    a detached GPG signature of manifest.json by an image signing key
    root.erofs           the root filesystem image for one slot
    verity.img           its dm-verity hash tree
    uki.efi              the unified kernel image that boots it

The manifest carries `roothash`, and the kernel image states the same hash on its command line. Nothing about the kernel
image is changed on install: it is signed for Secure Boot, and its command line is the same on every machine. The slot's two
partitions are named by the hash (their GPT UUIDs), so the kernel image finds them without a label.

The manifest is signed and it names the digests of the big files, so one signature covers everything. Nothing else
is read from the archive: it is streamed, every entry is checked by name, type and size before a byte is written,
and the files land under names this code chooses (an archive cannot place a file anywhere).

Installing writes only the standby slot. The running slot is never touched, so a power cut at any point leaves the
system exactly as it was: the target is emptied before it is overwritten (so a half-written slot cannot be booted),
verified by reading it back from the disk, and only then labelled and given its kernel image, which carries
systemd-boot's three boot tries. Bundles are rebuilt for the new image before the reboot (bundles.prebuild) and the
new image becomes the default boot entry. `update-pending.json` records that a restart is waiting, for whoever
wants to tell the user.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from . import bundles, compat, osinfo, pe, repos, slots, verity
from .runner import run

FORMAT = 1
MANIFEST, SIGNATURE, ROOT, UKI, VERITY = "manifest.json", "manifest.json.sig", "root.erofs", "uki.efi", "verity.img"
ALLOWED = (MANIFEST, SIGNATURE, ROOT, UKI, VERITY)
FILE_KEYS = {ROOT: "root", UKI: "uki", VERITY: "verity"}
MAX_VERITY = 4 * 1024 ** 3
MAX_SMALL = 1024 * 1024
MAX_UKI = 512 * 1024 * 1024
MAX_ROOT = 64 * 1024 ** 3
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
KEY_GLOBS = ("usr/share/foundation/keys", "etc/foundation/keys")
KEY_PREFIX = "ersa-image"
CHUNK = 4 * 1024 * 1024


def pending_path(paths):
    return os.path.join(paths["state"], "update-pending.json")


def pending(paths):
    """The update that is installed and waiting for a restart ({} if none)."""
    try:
        with open(pending_path(paths), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def clear_pending(paths):
    """Forget a waiting update once the system runs it (or something newer). Returns True if it was cleared."""
    waiting = pending(paths)
    if not waiting:
        return False
    running = osinfo.os_release(paths["os_release"]).get("IMAGE_VERSION")
    if running and running == waiting.get("version"):
        try:
            os.unlink(pending_path(paths))
        except OSError:
            pass
        return True
    return False


def signing_keys(paths):
    root = paths["root"]
    found = []
    for directory in KEY_GLOBS:
        full = os.path.join(root, directory)
        try:
            found += [os.path.join(full, n) for n in sorted(os.listdir(full)) if n.startswith(KEY_PREFIX) and n.endswith(".asc")]
        except OSError:
            pass
    return found


# -- reading the archive -----------------------------------------------------------------------

@contextlib.contextmanager
def _open(path):
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic == b"\x28\xb5\x2f\xfd":
        try:
            process = subprocess.Popen(["zstd", "-dc", "--", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError as error:
            raise Unavailable("zstd is not installed, so a .tar.zst update cannot be read") from error
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
                yield tar
        finally:
            process.stdout.close()
            process.kill()
            process.wait()
    else:
        mode = "r|gz" if magic[:2] == b"\x1f\x8b" else "r|"
        with tarfile.open(path, mode) as tar:
            yield tar


def parse_manifest(data, paths):
    try:
        manifest = json.loads(data.decode())
    except (ValueError, UnicodeDecodeError) as error:
        raise Failed(f"manifest.json is not valid: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise Failed("this update has a format this system does not understand")
    host = osinfo.os_release(paths["os_release"])
    if host.get("IMAGE_ID") and manifest.get("name") != host["IMAGE_ID"]:
        raise InvalidArgument(f"this update is for {manifest.get('name')!r}, not for this system ({host['IMAGE_ID']})")
    if manifest.get("arch") != os.uname().machine:
        raise InvalidArgument(f"this update is for {manifest.get('arch')!r} machines, not {os.uname().machine}")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION.match(version):
        raise Failed("the update has no usable version")
    try:
        verity.uuids(manifest.get("roothash", ""))
    except Failed as error:
        raise Failed("the update's manifest has no usable roothash") from error
    for key, limit in (("root", MAX_ROOT), ("uki", MAX_UKI), ("verity", MAX_VERITY)):
        entry = manifest.get(key)
        if not isinstance(entry, dict) or not SHA256.match(str(entry.get("sha256", ""))) \
                or not isinstance(entry.get("size"), int) or not 0 < entry["size"] <= limit:
            raise Failed(f"the update's manifest has no usable {key} entry")
    return manifest


def read_archive(path, stage, paths, unsigned=False, runner=run, progress=lambda fraction: None):
    """Check and unpack an update into `stage`. Returns (manifest, {"root": file, "uki": file}, signed)."""
    keys = signing_keys(paths)
    manifest, signature, seen, files, verified = None, None, set(), {}, False
    with _open(path) as tar:
        for member in tar:
            name = member.name
            if name not in ALLOWED or not member.isreg():
                raise Failed(f"unexpected entry in the update: {name!r}")
            if name in seen:
                raise Failed(f"{name} appears twice in the update")
            seen.add(name)
            if name in (MANIFEST, SIGNATURE):
                if member.size > MAX_SMALL:
                    raise Failed(f"{name} is too large")
                data = tar.extractfile(member).read()
                if name == MANIFEST:
                    if seen != {MANIFEST}:
                        raise Failed("manifest.json must be the first entry of the update")
                    manifest = parse_manifest(data, paths)
                    manifest_bytes = data
                else:
                    signature = data
                continue
            if manifest is None:
                raise Failed("manifest.json must be the first entry of the update")
            if not verified:
                if unsigned:
                    verified = True
                else:
                    if not keys:
                        raise Failed("no image signing key is installed, so the update cannot be verified; "
                                     "pass unsigned only for an image you built yourself")
                    if signature is None or not repos.verify_detached(manifest_bytes, signature, keys, runner):
                        raise Failed("the update is not signed by a trusted image signing key")
                    verified = True
            key = FILE_KEYS[name]
            if key not in manifest:
                raise Failed(f"unexpected entry in the update: {name!r}")
            if member.size != manifest[key]["size"]:
                raise Failed(f"{name} is {member.size} bytes; the manifest says {manifest[key]['size']}")
            target = os.path.join(stage, name)
            digest = hashlib.sha256()
            source, done = tar.extractfile(member), 0
            with open(target, "wb") as out:
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    out.write(chunk)
                    done += len(chunk)
                    progress(done / max(1, member.size))
                out.flush()
                os.fsync(out.fileno())
            if digest.hexdigest() != manifest[key]["sha256"]:
                raise Failed(f"{name} does not match the checksum in the signed manifest")
            files[key] = target
    if manifest is None or set(files) != {"root", "uki", "verity"}:
        raise Failed("the update is incomplete: it needs manifest.json, root.erofs, verity.img and uki.efi")
    return manifest, files, bool(keys) and not unsigned


# -- the kernel command line -------------------------------------------------------------------

# -- installing --------------------------------------------------------------------------------

def _choose_target(layout, wanted, version, force):
    for slot in layout.slots:
        if slot.name == version and slot.state != "empty":
            if slot.running:
                raise InvalidArgument(f"this system already runs version {version}")
            if not force:
                raise InvalidArgument(f"version {version} is already installed in slot {slot.name}; restart to use it "
                                      "(or install again with force)")
    if wanted:
        target = layout.find(wanted)
    else:
        candidates = [s for s in layout.slots if not s.running and not s.live]
        empty = [s for s in candidates if s.state == "empty"]
        pool = empty or candidates
        if not pool:
            raise Unavailable("there is no standby slot to install into")
        target = pool[0]
    if target.running:
        raise InvalidArgument("the running slot cannot be updated")
    return target


@contextlib.contextmanager
def mount_erofs(device, runner=run):
    directory = tempfile.mkdtemp(prefix="newroot.")
    try:
        code, _out, error = runner(["mount", "-t", "erofs", "-o", "ro,nosuid,nodev", device, directory], 60)
        if code != 0:
            raise Failed(f"cannot mount the new image to prepare it: {error.strip()[:200]}")
        try:
            yield directory
        finally:
            runner(["umount", directory], 60)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _write_slot(layout, manifest, files, target, force, ctx, runner, opener):
    """The image and its hash tree go into the standby slot's two partitions, which are then named by the root hash (their
    GPT UUIDs), and the signed kernel image is copied exactly as it is. The slot's label never changes.
    Returns (slot, label, entry_file, length)."""
    version = manifest["version"]
    roothash = manifest["roothash"].lower()
    data_uuid, hash_uuid = verity.uuids(roothash)
    slot = _choose_target(layout, target, version, force)
    slots._check_free(layout, slot)
    if not slot.verity_number:
        raise InvalidArgument(f"slot {slot.name} has no hash partition, so it cannot take a dm-verity update")
    length, hash_length = manifest["root"]["size"], manifest["verity"]["size"]
    if length > slot.size:
        raise InvalidArgument(f"the image ({length} bytes) does not fit slot {slot.name} ({slot.size} bytes)")
    if hash_length > slot.verity_size:
        raise InvalidArgument(f"the hash tree ({hash_length} bytes) does not fit slot {slot.name}'s hash partition ({slot.verity_size} bytes)")
    if slot.state != "empty" and slot.name != version and not force and slot.default:
        raise InvalidArgument(f"slot {slot.name} is the default boot entry; select the running slot first or pass force")
    for other in layout.slots:
        if other.name == version and other is not slot and other.state != "empty":
            raise InvalidArgument(f"a slot named {version} already exists")

    ctx.step("prepare", "Checking the kernel image")
    if verity.roothash_of(verity.cmdline_of_file(files["uki"])) != roothash:
        raise Failed("the kernel image does not carry the root hash the update's manifest names; nothing was changed")
    ctx.log(f"installing version {version} ({length} bytes, root hash {roothash[:16]}...) into slot {slot.name} ({slot.device})")

    ctx.commit()
    ctx.step("invalidate", f"Emptying slot {slot.name}")
    if slot.state != "empty":
        slots._remove_entries(layout, slot.name)
    # No kernel image names a random UUID, so from here until the end the slot cannot be booted half-written.
    slots._set_uuid(layout, slot.number, uuid.uuid4(), runner)
    slots._set_uuid(layout, slot.verity_number, uuid.uuid4(), runner)

    ctx.step("write", "Writing the system image")
    with open(files["root"], "rb") as source, opener(slot.device, "r+b") as device:
        slots.copy_range(source, device, length, lambda f: ctx.progress(0.3 + 0.3 * f))
    with open(files["verity"], "rb") as source, opener(slot.verity_device, "r+b") as device:
        slots.copy_range(source, device, hash_length, lambda f: ctx.progress(0.6 + 0.05 * f))
    ctx.step("verify-write", "Verifying what was written")
    with opener(slot.device, "rb") as device:
        actual = slots._digest(device, 0, length, lambda f: ctx.progress(0.65 + 0.15 * f))
    with opener(slot.verity_device, "rb") as device:
        actual_hash = slots._digest(device, 0, hash_length, lambda f: ctx.progress(0.8 + 0.03 * f))
    if actual != manifest["root"]["sha256"] or actual_hash != manifest["verity"]["sha256"]:
        raise Failed("what was written does not match the update; the slot was left unbootable")

    ctx.step("label", "Naming the new slot by its root hash")
    slots._set_uuid(layout, slot.number, data_uuid, runner)
    slots._set_uuid(layout, slot.verity_number, hash_uuid, runner)
    ctx.step("image", "Installing the kernel image")
    entry_file = f"ecp_{version}+3.efi"
    os.makedirs(layout.uki_dir, exist_ok=True)
    temporary = os.path.join(layout.uki_dir, f".{entry_file}.tmp")
    with open(files["uki"], "rb") as source, open(temporary, "wb") as handle:
        shutil.copyfileobj(source, handle, CHUNK)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, os.path.join(layout.uki_dir, entry_file))
    ctx.progress(0.86)
    return slot, slot.label, entry_file, length

def install(paths, archive, ctx, target="", unsigned=False, force=False, runner=run, opener=open, mount=None):
    """Install the update in `archive` into the standby slot and prepare the next boot. Returns a summary."""
    layout = slots.discover(paths)
    if not layout.esp:
        raise Unavailable("cannot find the EFI system partition (is it mounted?)")
    if layout.running is None or layout.running.live:
        raise Unavailable("this is not an installed system (the running root is unknown or is the installer medium's)")
    staging = os.path.join(paths["state"], "staging")
    os.makedirs(staging, mode=0o700, exist_ok=True)
    stage = tempfile.mkdtemp(dir=staging, prefix="update.")
    try:
        ctx.step("verify", "Checking the update")
        manifest, files, signed = read_archive(archive, stage, paths, unsigned, runner, lambda f: ctx.progress(0.3 * f))
        version = manifest["version"]
        slot, label, entry_file, length = _write_slot(layout, manifest, files, target, force, ctx, runner, opener)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # The new image is installed and bootable from here on; what follows prepares it and must not undo it.
    result = {"version": version, "slot": version, "label": label, "entry": entry_file, "bytes": length, "signed": signed,
              "bundles": {"ready": [], "failed": []}, "default_set": False, "notes": []}
    ctx.step("bundles", "Rebuilding your bundles for the new system")
    try:
        with (mount or mount_erofs)(slot.device) as new_root:
            outcome = bundles.prebuild(paths, ctx, new_root, runner)
        result["bundles"] = {"ready": outcome["ready"], "failed": outcome["failed"]}
        if outcome["failed"]:
            result["notes"].append("some bundles will be rebuilt after the restart: " + ", ".join(f["name"] for f in outcome["failed"]))
    except (Failed, Unavailable, InvalidArgument, NotFound) as error:
        result["notes"].append(f"bundles were not prepared ({error}); they are rebuilt after the restart")
        result["bundles"]["failed"] = [{"name": b["name"], "error": str(error)} for b in bundles.list_bundles(paths)]
    ctx.progress(0.97)

    ctx.step("boot", "Choosing the new system for the next start")
    try:
        slots.select(paths, version, False, runner)
        result["default_set"] = True
    except (Unavailable, InvalidArgument, Failed) as error:
        result["notes"].append(f"the new system is not the default yet ({error}); choose it in the boot menu")
    compat._write_json(pending_path(paths), {"version": version, "slot": version, "default_set": result["default_set"],
                                              "bundles": result["bundles"]})
    os.chmod(pending_path(paths), 0o644)      # a user session reads it to tell the user
    ctx.progress(1.0)
    result["reboot_required"] = True
    return result
