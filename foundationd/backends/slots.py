"""Boot slots: discovery, boot state, selection and releasing.

A slot is a pair of GPT partitions on the disk that holds the running root: the root filesystem (type root, label
`ecp-root-a` or `ecp-root-b`) and its dm-verity hash tree (type root-verity, label `ecp-verity-a`/`-b`). Labels never change
and say nothing about the contents. What a slot holds is named by its root hash: the data partition's GPT UUID is the first
half of the hash and the hash partition's UUID the second half (verity.py), and the unified kernel image (UKI)
`ecp_<version>[+tries].efi` in <ESP>/EFI/Linux whose command line says `roothash=<that hash>` is the one that boots it. A slot
whose UUID matches no kernel image is empty. The installer medium's own root is labelled `ecp-live` (with `ecp-live-verity`).

Everything is derived from the partition table, the ESP's files and the loader's EFI variables, read directly. The only
commands run are `bootctl` (to write the default entry: there is no other interface) and `sfdisk` (to change a partition's
UUID safely).
"""

import hashlib
import os
import re
import struct
import uuid
from dataclasses import dataclass, field

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from . import efi, gpt, osinfo, pe, verity
from .runner import check, run

SLOT_PREFIX = "ecp-root-"         # a slot's root partition ("ecp-root-a"); the label never changes
VERITY_PREFIX = "ecp-verity-"     # its hash partition ("ecp-verity-a")
LIVE_LABEL = "ecp-live"           # the installer medium's root; never offered as a slot to install into
LIVE_VERITY_LABEL = "ecp-live-verity"
ENTRY = re.compile(r"^ecp_(?P<name>.+?)(?:\+(?P<left>\d+)(?:-(?P<done>\d+))?)?\.efi$")
ESP_CANDIDATES = ("/efi", "/boot", "/boot/efi")
CHUNK = 4 * 1024 * 1024


@dataclass
class Entry:
    file: str                 # file name on the ESP
    name: str                 # slot name
    left: int = -1            # tries left, -1 when blessed (no counter)
    done: int = 0

    @property
    def id(self):
        """The entry id without a boot counter: stable across systemd-bless-boot renames."""
        return f"ecp_{self.name}.efi"


@dataclass
class Slot:
    name: str
    label: str
    device: str
    number: int
    size: int
    offset: int
    state: str                # running | standby | empty
    running: bool = False
    default: bool = False
    entry: Entry = None
    live: bool = False        # the installer medium's own root: shown, but never a place to install
    verity_number: int = 0    # the slot's hash partition
    verity_device: str = ""
    verity_size: int = 0

    def as_dict(self):
        return {
            "name": self.name, "label": self.label, "device": self.device, "size": self.size,
            "state": self.state, "running": self.running, "default": self.default,
            "entry": self.entry.file if self.entry else "",
            "tries_left": self.entry.left if self.entry else -1,
            "bootable": bool(self.entry) and self.state != "empty",
            "live": self.live,
        }


@dataclass
class Layout:
    paths: dict
    disk: str = ""
    sector: int = 512
    esp: str = ""
    slots: list = field(default_factory=list)
    running: Slot = None
    mounts: dict = field(default_factory=dict)

    def find(self, name):
        for slot in self.slots:
            if slot.name == name or slot.label == name:
                return slot
        raise NotFound(f"no such slot: {name}")

    @property
    def uki_dir(self):
        return os.path.join(self.esp, "EFI", "Linux")


# -- discovery ---------------------------------------------------------------------------------

def parse_entry(filename):
    match = ENTRY.match(filename)
    if not match:
        return None
    left = match.group("left")
    return Entry(filename, match.group("name"), int(left) if left is not None else -1,
                 int(match.group("done") or 0))


def find_esp(paths, mounts, trigger=os.listdir):
    """The mount point of the EFI system partition, or "".

    systemd's gpt-auto-generator mounts it lazily: until something looks at /efi (or /boot) only an autofs is
    there, which is not the vfat filesystem. Looking at the directory makes systemd mount it, so do that and read the
    mount table again before deciding there is none.
    """
    if paths.get("esp"):
        return paths["esp"]
    for target in ESP_CANDIDATES:
        mount = mounts.get(target)
        if mount and mount["fstype"] == "autofs":
            try:
                trigger(target)
            except OSError:
                continue
            mount = osinfo.mounts(paths["mountinfo"]).get(target)
        if mount and mount["fstype"] == "vfat":
            return target
    return ""


def list_entries(esp):
    """The UKIs on the ESP, by slot name. A slot with several files keeps the lexically first."""
    found = {}
    try:
        names = sorted(os.listdir(os.path.join(esp, "EFI", "Linux")))
    except OSError:
        return found
    for filename in names:
        entry = parse_entry(filename)
        if entry and entry.name not in found:
            found[entry.name] = entry
    return found


def hash_entries(esp):
    """{data partition UUID: Entry} for the UKIs on the ESP that carry a root hash (dm-verity images).

    The UUID is what the partition holding that image is called, so this is how a slot with a fixed label is matched with
    the kernel image that boots it. A file that cannot be read is left out; a slot with several files keeps the first."""
    found = {}
    base = os.path.join(esp, "EFI", "Linux")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return found
    for filename in names:
        entry = parse_entry(filename)
        if not entry:
            continue
        try:
            roothash = verity.roothash_of(verity.cmdline_of_file(os.path.join(base, filename)))
        except (OSError, Failed):
            continue
        if roothash:
            found.setdefault(verity.uuids(roothash)[0], entry)
    return found


def _partition_of(device, sysfs):
    """(disk name, partition number) for a partition device, from sysfs; (None, None) otherwise."""
    node = os.path.join(sysfs, "class/block", os.path.basename(os.path.realpath(device)))
    try:
        with open(os.path.join(node, "partition"), encoding="utf-8") as handle:
            number = int(handle.read().strip())
    except (OSError, ValueError):
        return None, None
    return os.path.basename(os.path.dirname(os.path.realpath(node))), number


def _backing_partitions(device, sysfs):
    """[(disk name, partition number)] for the devices under `device`.

    A dm-verity root is /dev/mapper/root, a device-mapper device, not a partition: its backing devices (the data partition and
    the hash partition) are listed in sysfs under slaves/. A plain partition is its own backing device."""
    node = os.path.join(sysfs, "class/block", os.path.basename(os.path.realpath(device)))
    try:
        under = sorted(os.listdir(os.path.join(node, "slaves")))
    except OSError:
        under = []
    if not under:
        disk, number = _partition_of(device, sysfs)
        return [(disk, number)] if disk else []
    found = []
    for name in under:
        disk, number = _partition_of(os.path.join("/dev", name), sysfs)
        if disk:
            found.append((disk, number))
    return found


def partition_device(disk_path, number):
    return f"{disk_path}{'p' if disk_path[-1].isdigit() else ''}{number}"


def discover(paths, machine=None):
    """Find the slots on the disk that holds the running root. Raises Unavailable if it cannot."""
    machine = machine or os.uname().machine
    root_type = gpt.ROOT_TYPES.get(machine)
    if root_type is None:
        raise Unavailable(f"unsupported architecture: {machine}")
    layout = Layout(paths=paths, mounts=osinfo.mounts(paths["mountinfo"]))
    layout.esp = find_esp(paths, layout.mounts)
    root = layout.mounts.get("/")
    if not root:
        raise Unavailable("cannot find the root filesystem")
    backing = _backing_partitions(root["source"], paths["sysfs"])
    if not backing:
        raise Unavailable(f"the root filesystem ({root['source']}) is not on a partition")
    disk = backing[0][0]
    layout.disk = os.path.join(paths["dev"], disk)
    try:
        with open(os.path.join(paths["sysfs"], "class/block", disk, "queue/logical_block_size"), encoding="utf-8") as handle:
            layout.sector = int(handle.read().strip())
    except (OSError, ValueError):
        layout.sector = 512
    try:
        table = gpt.read_table(layout.disk, layout.sector)
    except (OSError, gpt.GptError) as error:
        raise Unavailable(f"cannot read the partition table of {layout.disk}: {error}") from error

    # of the partitions under the root device, the one of root type is the filesystem (the other is its hash tree)
    root_numbers = {p.number for p in table if p.type == root_type}
    running_number = next((number for _disk, number in backing if number in root_numbers), backing[0][1])
    default = efi.read_loader_variable("LoaderEntryDefault", paths["efivars"])
    default_entry = parse_entry(default) if default else None
    verity_type = verity.ROOT_VERITY_TYPES.get(machine)
    hash_parts = {p.name[len(VERITY_PREFIX):]: p for p in table if p.type == verity_type and p.name.startswith(VERITY_PREFIX)}
    by_hash = hash_entries(layout.esp) if layout.esp else {}
    for part in table:
        if part.type != root_type:
            continue
        live = part.name == LIVE_LABEL
        if not (live or part.name.startswith(SLOT_PREFIX)):
            continue
        suffix = "live" if live else part.name[len(SLOT_PREFIX):]
        entry = by_hash.get(part.uuid)
        companion = next((p for p in table if p.type == verity_type and p.name == (LIVE_VERITY_LABEL if live else VERITY_PREFIX + suffix)), None)
        slot = Slot(
            name=entry.name if entry else suffix, label=part.name, device=partition_device(layout.disk, part.number),
            number=part.number, size=part.size, offset=part.offset,
            state="empty" if entry is None else ("running" if part.number == running_number else "standby"),
            running=part.number == running_number, entry=entry, live=live,
            verity_number=companion.number if companion else 0,
            verity_device=partition_device(layout.disk, companion.number) if companion else "",
            verity_size=companion.size if companion else 0)
        slot.default = bool(default_entry and entry and default_entry.name == entry.name)
        layout.slots.append(slot)
        if slot.running:
            layout.running = slot
    return layout


# -- state -------------------------------------------------------------------------------------

def boot_state(paths):
    efivars = paths["efivars"]
    state = {"efi": efi.booted_with_efi(efivars), "running_slot": "", "running_device": "",
             "selected_entry": "", "default_entry": "", "tries_left": -1, "tries_done": 0,
             "assessment": "unknown"}
    state["selected_entry"] = efi.read_loader_variable("LoaderEntrySelected", efivars) or ""
    state["default_entry"] = efi.read_loader_variable("LoaderEntryDefault", efivars) or ""
    try:
        layout = discover(paths)
    except Unavailable:
        return state
    if layout.running is not None:
        state["running_slot"] = layout.running.name
        state["running_device"] = layout.running.device
        entry = layout.running.entry
        if entry is not None:
            state["tries_left"], state["tries_done"] = entry.left, entry.done
            if state["efi"]:
                # A counter on the file name means systemd-bless-boot has not blessed this boot.
                state["assessment"] = "pending" if entry.left >= 0 else "good"
    return state


def slots(paths):
    return [slot.as_dict() for slot in discover(paths).slots]


# -- selection ---------------------------------------------------------------------------------

def _bootable(layout, name):
    slot = layout.find(name)
    if slot.state == "empty":
        raise InvalidArgument(f"slot {slot.name} is empty")
    if slot.entry is None:
        raise InvalidArgument(f"slot {slot.name} has no kernel image on the ESP, so it cannot boot")
    return slot


def select(paths, name, once, runner=run):
    """Make a slot the default (or the next boot only) through bootctl."""
    layout = discover(paths)
    if not efi.booted_with_efi(paths["efivars"]):
        raise Unavailable("this system was not booted through UEFI; boot selection is unavailable")
    slot = _bootable(layout, name)
    command = "set-oneshot" if once else "set-default"
    check(["bootctl", command, slot.entry.id], 30, runner=runner)
    variable = "LoaderEntryOneShot" if once else "LoaderEntryDefault"
    return {"slot": slot.name, "entry": slot.entry.id, "mode": "next" if once else "default",
            "stored": efi.read_loader_variable(variable, paths["efivars"]) or ""}


# -- releasing ---------------------------------------------------------------------------------

def _uuid_on_disk(layout, number):
    for part in gpt.read_table(layout.disk, layout.sector):
        if part.number == number:
            return part.uuid
    return None


def _set_uuid(layout, number, value, runner):
    """Set a partition's GPT UUID with sfdisk and confirm it on disk (sfdisk can report an error when the kernel cannot re-read the table of a disk with mounted partitions even though the UUID was written, so the table on disk is the truth)."""
    value = uuid.UUID(str(value))
    try:
        check(["sfdisk", "--part-uuid", layout.disk, str(number), str(value)], 30, runner=runner)
    except Failed:
        if _uuid_on_disk(layout, number) != value:
            raise
    if _uuid_on_disk(layout, number) != value:
        raise Failed(f"the UUID of partition {number} did not change to {value}")


def _remove_entries(layout, name):
    removed = []
    try:
        names = os.listdir(layout.uki_dir)
    except OSError:
        return removed
    for filename in names:
        entry = parse_entry(filename)
        if entry and entry.name == name:
            os.unlink(os.path.join(layout.uki_dir, filename))
            removed.append(filename)
    return removed


def _check_free(layout, slot):
    if slot.running:
        raise InvalidArgument("the running slot cannot be changed")
    for mount in layout.mounts.values():
        if os.path.realpath(mount["source"]) == os.path.realpath(slot.device):
            raise InvalidArgument(f"{slot.device} is mounted at {mount['target']}")


def release(paths, name, ctx, runner=run):
    """Make a slot empty: remove its kernel image and give its partitions new random UUIDs, so that nothing names them any more.
    The data stays on the partition until an update overwrites it."""
    layout = discover(paths)
    slot = layout.find(name)
    if slot.live:
        raise InvalidArgument("the installer medium's own root cannot be released")
    if slot.state == "empty":
        return {"slot": slot.name, "released": False}
    _check_free(layout, slot)
    others = [s for s in layout.slots if s is not slot and s.entry is not None and s.state != "empty" and not s.live]
    if not others:
        raise InvalidArgument("refusing to release the only slot that can boot")
    ctx.step("release", f"Releasing slot {slot.name}")
    ctx.commit()
    removed = _remove_entries(layout, slot.name) if layout.esp else []
    _set_uuid(layout, slot.number, uuid.uuid4(), runner)
    if slot.verity_number:
        _set_uuid(layout, slot.verity_number, uuid.uuid4(), runner)
    return {"slot": slot.label, "released": True, "removed_entries": removed}


# -- writing a slot ----------------------------------------------------------------------------

def filesystem_length(handle, partition_size):
    """Bytes of an EROFS image (from its superblock), or the whole partition if it is not one."""
    handle.seek(1024)
    superblock = handle.read(40)
    if len(superblock) == 40 and struct.unpack_from("<I", superblock, 0)[0] == 0xE0F5E1E2:
        block_bits = superblock[12]
        blocks = struct.unpack_from("<I", superblock, 36)[0]
        length = blocks << block_bits
        if 0 < length <= partition_size:
            return length
    return partition_size


def copy_range(source, target, length, progress, chunk=CHUNK):
    """Copy `length` bytes from the current position of `source` to that of `target`."""
    done = 0
    while done < length:
        data = source.read(min(chunk, length - done))
        if not data:
            raise Failed("unexpected end of the source while copying")
        target.write(data)
        done += len(data)
        progress(done / length)
    target.flush()
    os.fsync(target.fileno())


def _digest(handle, offset, length, progress, chunk=CHUNK):
    handle.seek(offset)
    try:  # make the verification read from the disk, not from the page cache
        os.posix_fadvise(handle.fileno(), offset, length, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError):
        pass
    digest, done = hashlib.sha256(), 0
    while done < length:
        data = handle.read(min(chunk, length - done))
        if not data:
            raise Failed("unexpected end of data while verifying")
        digest.update(data)
        done += len(data)
        progress(done / length)
    return digest.hexdigest()
