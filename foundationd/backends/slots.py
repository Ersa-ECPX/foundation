"""Boot slots: discovery, boot state, selection, copying and releasing.

A slot is a root-type GPT partition on the disk that holds the running root. Its label is
`ecp-root_<name>` (or `_empty`), and the unified kernel image (UKI) `ecp_<name>[+tries].efi` in
<ESP>/EFI/Linux boots the partition with that label (its command line says
`root=PARTLABEL=ecp-root_<name>`).

Everything is derived from the partition table, the ESP's files and the loader's EFI variables,
read directly. The only commands run are `bootctl` (to write the default entry: there is no other
interface) and `sfdisk` (to change a partition label safely).

Copying makes the copy a slot of its own: a different label and a UKI whose command line names
that label. Two partitions with one label would make `root=PARTLABEL=` ambiguous and would leave
the update tool without a free slot.
"""

import hashlib
import os
import re
import struct
from dataclasses import dataclass, field

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from . import efi, gpt, osinfo, pe
from .runner import check, run

LABEL_PREFIX = "ecp-root_"
EMPTY_LABEL = "_empty"
ENTRY = re.compile(r"^ecp_(?P<name>.+?)(?:\+(?P<left>\d+)(?:-(?P<done>\d+))?)?\.efi$")
ESP_CANDIDATES = ("/efi", "/boot", "/boot/efi")
COPY_SUFFIX = "-copy"
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

    def as_dict(self):
        return {
            "name": self.name, "label": self.label, "device": self.device, "size": self.size,
            "state": self.state, "running": self.running, "default": self.default,
            "entry": self.entry.file if self.entry else "",
            "tries_left": self.entry.left if self.entry else -1,
            "bootable": bool(self.entry) and self.state != "empty",
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


def find_esp(paths, mounts):
    if paths.get("esp"):
        return paths["esp"]
    for target in ESP_CANDIDATES:
        mount = mounts.get(target)
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


def _partition_of(device, sysfs):
    """(disk name, partition number) for a partition device, from sysfs; (None, None) otherwise."""
    node = os.path.join(sysfs, "class/block", os.path.basename(os.path.realpath(device)))
    try:
        with open(os.path.join(node, "partition"), encoding="utf-8") as handle:
            number = int(handle.read().strip())
    except (OSError, ValueError):
        return None, None
    return os.path.basename(os.path.dirname(os.path.realpath(node))), number


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
    disk, running_number = _partition_of(root["source"], paths["sysfs"])
    if disk is None:
        raise Unavailable(f"the root filesystem ({root['source']}) is not on a partition")
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

    entries = list_entries(layout.esp) if layout.esp else {}
    default = efi.read_loader_variable("LoaderEntryDefault", paths["efivars"])
    default_entry = parse_entry(default) if default else None
    for part in table:
        if part.type != root_type:
            continue
        if part.name.startswith(LABEL_PREFIX):
            name = part.name[len(LABEL_PREFIX):]
        elif part.name == EMPTY_LABEL:
            name = f"empty{part.number}"
        else:
            continue
        is_empty = part.name == EMPTY_LABEL
        slot = Slot(
            name=name, label=part.name, device=partition_device(layout.disk, part.number),
            number=part.number, size=part.size, offset=part.offset,
            state="empty" if is_empty else ("running" if part.number == running_number else "standby"),
            running=part.number == running_number,
            entry=None if is_empty else entries.get(name))
        slot.default = bool(default_entry and not is_empty and default_entry.name == name)
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

def _label_on_disk(layout, number):
    for part in gpt.read_table(layout.disk, layout.sector):
        if part.number == number:
            return part.name
    return None


def _relabel(layout, slot, label, runner):
    """Set a partition label with sfdisk and confirm it on disk.

    sfdisk can report an error when the kernel cannot re-read the table of a disk that has mounted
    partitions, even though the label was written; the table on disk is the truth, so check that.
    """
    try:
        check(["sfdisk", "--part-label", layout.disk, str(slot.number), label], 30, runner=runner)
    except Failed:
        if _label_on_disk(layout, slot.number) != label:
            raise
    if _label_on_disk(layout, slot.number) != label:
        raise Failed(f"the label of partition {slot.number} did not change to {label}")


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
    layout = discover(paths)
    slot = layout.find(name)
    if slot.state == "empty":
        return {"slot": slot.name, "released": False}
    _check_free(layout, slot)
    others = [s for s in layout.slots if s is not slot and s.entry is not None and s.state != "empty"]
    if not others:
        raise InvalidArgument("refusing to release the only slot that can boot")
    ctx.step("release", f"Releasing slot {slot.name}")
    ctx.commit()
    removed = _remove_entries(layout, slot.name) if layout.esp else []
    _relabel(layout, slot, EMPTY_LABEL, runner)
    return {"slot": slot.name, "released": True, "removed_entries": removed}


# -- copying -----------------------------------------------------------------------------------

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


def new_slot_name(source):
    return source if source.endswith(COPY_SUFFIX) else source + COPY_SUFFIX


def copy(paths, source_name, target_name, force, ctx, runner=run, opener=open):
    """Copy a slot onto another and make the copy bootable as a slot of its own."""
    layout = discover(paths)
    if not layout.esp:
        raise Unavailable("cannot find the EFI system partition (is it mounted?)")
    source = layout.find(source_name) if source_name else layout.running
    if source is None:
        raise Unavailable("the running slot is unknown; name the source slot")
    target = layout.find(target_name)
    if source.state == "empty":
        raise InvalidArgument("the source slot is empty")
    if source is target:
        raise InvalidArgument("the source and the target are the same slot")
    _check_free(layout, target)
    if target.state != "empty" and not force:
        raise InvalidArgument(f"slot {target.name} is in use; pass force to overwrite it")
    if source.entry is None:
        raise InvalidArgument(f"slot {source.name} has no kernel image on the ESP to copy")

    new_name = new_slot_name(source.name)
    for other in layout.slots:
        if other.name == new_name and other is not target:
            raise InvalidArgument(f"a slot named {new_name} already exists; release it first")
    new_label = LABEL_PREFIX + new_name

    # Everything that can fail without damaging anything happens before the point of no return.
    ctx.step("prepare", "Checking the slots and preparing the kernel image")
    uki_path = os.path.join(layout.uki_dir, source.entry.file)
    with open(uki_path, "rb") as handle:
        uki = handle.read()
    patched = pe.replace_cmdline(uki, f"root=PARTLABEL={LABEL_PREFIX}{source.name}", f"root=PARTLABEL={new_label}")
    with opener(source.device, "rb") as probe:
        length = filesystem_length(probe, source.size)
    if length > target.size:
        raise InvalidArgument(f"the target ({target.size} bytes) is smaller than the source data ({length} bytes)")
    ctx.log(f"copying {length} bytes from {source.device} to {target.device}")

    ctx.commit()
    # From here the target's old contents are being destroyed. Leave it empty and unbootable
    # (label _empty, no kernel image) until the copy has been verified.
    ctx.step("invalidate", f"Emptying slot {target.name}")
    if target.state != "empty":
        _remove_entries(layout, target.name)
        _relabel(layout, target, EMPTY_LABEL, runner)

    ctx.step("copy", "Copying the root filesystem")
    with opener(source.device, "rb") as src, opener(target.device, "r+b") as dst:
        copy_range(src, dst, length, lambda f: ctx.progress(0.7 * f))

    ctx.step("verify", "Verifying the copy")
    with opener(source.device, "rb") as src, opener(target.device, "rb") as dst:
        expected = _digest(src, 0, length, lambda f: ctx.progress(0.7 + 0.15 * f))
        actual = _digest(dst, 0, length, lambda f: ctx.progress(0.85 + 0.1 * f))
    if expected != actual:
        raise Failed("the copy does not match the source; the target slot was left empty")

    ctx.step("label", "Labelling the new slot")
    _relabel(layout, target, new_label, runner)

    ctx.step("image", "Installing the kernel image")
    entry_file = f"ecp_{new_name}+3.efi"
    final = os.path.join(layout.uki_dir, entry_file)
    temporary = os.path.join(layout.uki_dir, f".{entry_file}.tmp")
    os.makedirs(layout.uki_dir, exist_ok=True)
    with open(temporary, "wb") as handle:
        handle.write(patched)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, final)
    ctx.progress(1.0)
    return {"slot": new_name, "label": new_label, "entry": entry_file, "bytes": length, "source": source.name}
