"""Deploy: write the operating system to a disk (the installer's back end).

What it does, in this order, and why the order matters:

  1. Validate. Refuse the disk that holds the running system, any disk with a mounted partition, one
     that is read-only or too small, and anything that is not a whole disk. The caller must repeat the
     disk path exactly (`confirm`), so a stray argument cannot pick a disk.
  2. Prepare, changing nothing: stage the ESP files and read the root image's real length.
  3. Commit: the point of no return (the disk is about to be wiped).
  4. Partition with systemd-repart: ESP (with the boot loader but NO usable kernel image yet), a
     reserved recovery partition, root slot A (labelled for this version), an empty root slot B, and a
     btrfs data volume (`var` and `home` subvolumes) seeded from a pre-labelled tree.
  5. Copy the root image into slot A with progress, then read both back and compare SHA-256.
  6. Only now make the disk bootable: rename the staged kernel image to `ecp_<version>+3.efi` on the
     new ESP. Until this last step systemd-boot has nothing to boot, so an interrupted or failed
     install never leaves a half-written system that looks bootable.

The root image is copied from the running root partition, never rebuilt, so the installed system is
byte-for-byte what was tested. The whole disk node is written (not partition nodes), so no waiting for
udev. `mtools` renames the file on the new ESP without mounting it.
"""

import os
import shutil
import stat
import tempfile

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from . import gpt, layout, osinfo, slots
from .runner import check, run

SKIP_PREFIXES = ("loop", "ram", "zram", "dm-", "md", "sr", "nbd", "fd", "sg")
PARTIAL = ".partial"


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return default


def _disk_names(paths):
    base = os.path.join(paths["sysfs"], "class/block")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [n for n in names if not n.startswith(SKIP_PREFIXES) and not os.path.exists(os.path.join(base, n, "partition"))]


def _partitions_of(paths, name):
    node = os.path.join(paths["sysfs"], "class/block", name)
    try:
        entries = sorted(os.listdir(node))
    except OSError:
        return []
    return [e for e in entries if e.startswith(name) and os.path.exists(os.path.join(node, e, "partition"))]


def _same(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


def _running_disk(paths, mounts):
    root = mounts.get("/")
    if not root:
        return None
    disk, _number = slots._partition_of(root["source"], paths["sysfs"])
    return disk


def _mounted_on(paths, mounts, name):
    """Mount targets whose source is this disk or one of its partitions."""
    devices = [os.path.join(paths["dev"], n) for n in [name] + _partitions_of(paths, name)]
    return sorted(m["target"] for m in mounts.values() if any(_same(m["source"], d) for d in devices))


def _describe_block(paths, name, mounts, running):
    node = os.path.join(paths["sysfs"], "class/block", name)
    sector = 512
    size = int(_read(os.path.join(node, "size"), "0") or 0) * sector
    problems = []
    mounted = _mounted_on(paths, mounts, name)
    boot_medium = name == running
    if boot_medium:
        problems.append("it holds the running system")
    elif mounted:
        problems.append("it has mounted partitions: " + ", ".join(mounted))
    if _read(os.path.join(node, "ro")) == "1":
        problems.append("it is read-only")
    if size < layout.MIN_DISK:
        problems.append(f"it is too small: {size / layout.GIB:.1f} GiB, at least {layout.MIN_DISK / layout.GIB:.1f} GiB is needed")
    return {
        "path": os.path.join(paths["dev"], name), "name": name, "size": size,
        "model": " ".join(_read(os.path.join(node, "device/model")).split()),
        "removable": _read(os.path.join(node, "removable")) == "1",
        "boot_medium": boot_medium, "mounted": mounted, "problems": problems,
    }


def list_disks(paths):
    """Every whole disk, with whether it can be installed to and, if not, why not."""
    mounts = osinfo.mounts(paths["mountinfo"])
    running = _running_disk(paths, mounts)
    disks = []
    for name in _disk_names(paths):
        info = _describe_block(paths, name, mounts, running)
        info["usable"] = not info["problems"]
        info["reason"] = "; ".join(info["problems"])
        del info["problems"]
        disks.append(info)
    return disks


def describe_target(paths, target, allow_file=False):
    """Check that `target` is a disk we may write to. Returns its description (with `problems`)."""
    if not isinstance(target, str) or not target.startswith("/") or "\0" in target or ".." in target.split("/"):
        raise InvalidArgument("the disk must be an absolute path such as /dev/sdb")
    try:
        mode = os.stat(target).st_mode
    except OSError:
        raise NotFound(f"no such disk: {target}") from None
    mounts = osinfo.mounts(paths["mountinfo"])
    running = _running_disk(paths, mounts)
    if stat.S_ISBLK(mode):
        name = os.path.basename(os.path.realpath(target))
        if not os.path.isdir(os.path.join(paths["sysfs"], "class/block", name)):
            raise Unavailable(f"{target} is not known to the kernel (no /sys/class/block/{name})")
        if os.path.exists(os.path.join(paths["sysfs"], "class/block", name, "partition")):
            raise InvalidArgument(f"{target} is a partition; give the whole disk")
        info = _describe_block(paths, name, mounts, running)
        info["file"] = False
        info["sector"] = int(_read(os.path.join(paths["sysfs"], "class/block", name, "queue/logical_block_size"), "512") or 512)
        return info
    if not (stat.S_ISREG(mode) and allow_file):
        raise InvalidArgument(f"{target} is not a block device")
    # A plain file stands in for a disk in tests only (the daemon refuses this outside --bus session).
    problems = []
    if running and _same(target, os.path.join(paths["dev"], running)):
        problems.append("it holds the running system")
    elif any(_same(target, m["source"]) for m in mounts.values()):
        problems.append("it is mounted")
    size = os.path.getsize(target)
    if size < layout.MIN_DISK:
        problems.append(f"it is too small: {size / layout.GIB:.1f} GiB, at least {layout.MIN_DISK / layout.GIB:.1f} GiB is needed")
    return {"path": target, "name": os.path.basename(target), "size": size, "model": "", "removable": False,
            "boot_medium": "it holds the running system" in problems, "mounted": [], "problems": problems,
            "file": True, "sector": 512}


def _existing_partitions(info):
    try:
        return [{"number": p.number, "name": p.name, "size": p.size} for p in gpt.read_table(info["path"], info["sector"])]
    except (OSError, gpt.GptError):
        return []


def plan(paths, target, allow_file=False):
    """What deploying to `target` would do. Changes nothing."""
    info = describe_target(paths, target, allow_file)
    running = slots.discover(paths).running
    version = running.name if running else ""
    problems = list(info["problems"])
    if running is None:
        problems.append("the running slot is unknown")
    elif running.entry is None:
        problems.append("the running slot has no kernel image on the ESP to install")
    elif len(layout.LABEL_PREFIX + version) > layout.MAX_LABEL:
        problems.append(f"the version {version!r} is too long for a partition name")
    return {
        "disk": info["path"], "size": info["size"], "model": info["model"], "removable": info["removable"],
        "version": version,
        "partitions": [{"label": label, "size": size or 0, "note": note} for label, size, note in layout.rows(version or "?")],
        "will_erase": _existing_partitions(info),
        "problems": problems, "possible": not problems,
    }


class Window:
    """A byte range of a file or device, as a file object (the new root partition inside a disk)."""

    def __init__(self, handle, offset, length):
        self._f, self._offset, self._length, self._pos = handle, offset, length, 0

    def seek(self, pos, whence=0):
        self._pos = pos if whence == 0 else self._length + pos
        return self._pos

    def read(self, count=-1):
        count = self._length - self._pos if count < 0 else min(count, self._length - self._pos)
        self._f.seek(self._offset + self._pos)
        data = self._f.read(max(0, count))
        self._pos += len(data)
        return data

    def write(self, data):
        if self._pos + len(data) > self._length:
            raise Failed("write past the end of the partition")
        self._f.seek(self._offset + self._pos)
        self._f.write(data)
        self._pos += len(data)
        return len(data)

    def flush(self):
        self._f.flush()

    def fileno(self):
        return self._f.fileno()


def _stage_esp(layout_, version, stage):
    """Copy the running ESP into a staging tree, with the kernel image under a name nothing boots."""
    esp = os.path.join(stage, "esp")
    source_linux = os.path.join(layout_.esp, "EFI", "Linux")

    def ignore(directory, names):
        return names if os.path.samefile(directory, source_linux) else []

    shutil.copytree(layout_.esp, esp, ignore=ignore, copy_function=shutil.copyfile)
    target_dir = os.path.join(esp, "EFI", "Linux")
    os.makedirs(target_dir, exist_ok=True)
    running = layout_.running
    shutil.copyfile(os.path.join(source_linux, running.entry.file), os.path.join(target_dir, f"ecp_{version}+3.efi{PARTIAL}"))
    return esp


def _write_definitions(directory, version, esp, seed):
    os.makedirs(directory)
    for name, text in layout.definitions(version, esp, seed).items():
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(text)


ZONEINFO = "/usr/share/zoneinfo"


def _label(path, context):
    try:
        os.setxattr(path, "security.selinux", context.encode() + b"\0", follow_symlinks=False)
    except OSError:
        pass  # no SELinux on this host (tests)


def _make_seed(stage, language="", timezone="", zoneinfo=ZONEINFO):
    """The initial contents of the data volume: the /etc overlay directories (labelled etc_t, as the
    image build does, so SELinux accepts the overlay), an empty /home, and the chosen language and time
    zone as files in the overlay's upper layer, which is what /etc is: /etc/locale.conf and /etc/localtime."""
    seed = os.path.join(stage, "seed")
    upper = os.path.join(seed, "var/lib/ecp/etc/upper")
    work = os.path.join(seed, "var/lib/ecp/etc/work")
    os.makedirs(upper, mode=0o755)
    os.makedirs(work, mode=0o700)
    os.makedirs(os.path.join(seed, "home"), mode=0o755)
    for path in (upper, work):
        _label(path, "system_u:object_r:etc_t:s0")
    if language:
        conf = os.path.join(upper, "locale.conf")
        with open(conf, "w") as handle:
            handle.write(f"LANG={language}\n")
        _label(conf, "system_u:object_r:locale_t:s0")
    if timezone:
        link = os.path.join(upper, "localtime")
        os.symlink(f"../usr/share/zoneinfo/{timezone}", link)
        _label(link, "system_u:object_r:locale_t:s0")
    return seed


def deploy(paths, target, confirm, ctx, runner=run, opener=open, allow_file=False, language="", timezone="",
           zoneinfo=ZONEINFO):
    if confirm != target:
        raise InvalidArgument("the confirmation must repeat the disk path exactly")
    if timezone and not os.path.isfile(os.path.join(zoneinfo, timezone)):
        raise InvalidArgument(f"unknown time zone: {timezone}")
    info = describe_target(paths, target, allow_file)
    if info["problems"]:
        raise InvalidArgument(f"cannot install to {target}: " + "; ".join(info["problems"]))
    layout_ = slots.discover(paths)
    running = layout_.running
    if running is None or running.entry is None:
        raise Unavailable("the running system has no kernel image on the ESP to install")
    version = running.name
    if len(layout.LABEL_PREFIX + version) > layout.MAX_LABEL:
        raise InvalidArgument(f"the version {version!r} is too long for a partition name")

    ctx.step("prepare", "Preparing the installation")
    os.makedirs(os.path.join(paths["state"], "staging"), mode=0o700, exist_ok=True)
    stage = tempfile.mkdtemp(dir=os.path.join(paths["state"], "staging"), prefix="deploy.")
    try:
        esp = _stage_esp(layout_, version, stage)
        with opener(running.device, "rb") as probe:
            length = slots.filesystem_length(probe, running.size)
        if length > layout.ROOT_SLOT:
            raise InvalidArgument(f"the root image ({length} bytes) does not fit a {layout.ROOT_SLOT}-byte slot")
        definitions = os.path.join(stage, "repart.d")
        _write_definitions(definitions, version, esp, _make_seed(stage, language, timezone, zoneinfo))
        ctx.log(f"installing version {version} ({length} bytes of root image) to {target}")

        ctx.commit()
        ctx.step("partition", "Partitioning the disk")
        _repart(target, definitions, runner)
        ctx.progress(0.1)

        try:
            table = gpt.read_table(target, info["sector"])
        except (OSError, gpt.GptError) as error:
            raise Failed(f"the new partition table cannot be read back: {error}") from error
        by_label = {p.name: p for p in table}
        for label, _size, _note in layout.rows(version):
            if label not in by_label:
                raise Failed(f"the partition {label} was not created")
        root_a, esp_part = by_label[layout.LABEL_PREFIX + version], by_label["ECP-ESP"]

        ctx.step("copy", "Copying the operating system")
        with opener(running.device, "rb") as src, open(target, "r+b") as disk:
            window = Window(disk, root_a.offset, root_a.size)
            slots.copy_range(src, window, length, lambda f: ctx.progress(0.1 + 0.55 * f))
            ctx.step("verify", "Verifying the copy")
            try:  # read the copy back from the disk, not the page cache
                os.posix_fadvise(disk.fileno(), root_a.offset, root_a.size, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError):
                pass
            expected = slots._digest(src, 0, length, lambda f: ctx.progress(0.65 + 0.15 * f))
            actual = slots._digest(window, 0, length, lambda f: ctx.progress(0.8 + 0.15 * f))
        if expected != actual:
            raise Failed("the copy does not match the source; the disk was left without a bootable kernel image")

        ctx.step("finish", "Making the disk bootable")
        spec = f"{target}@@{esp_part.offset}"
        rename = ["mren", "-i", spec, f"::EFI/Linux/ecp_{version}+3.efi{PARTIAL}", f"::EFI/Linux/ecp_{version}+3.efi"]
        check(rename, 120, runner=runner)
        listing = check(["mdir", "-b", "-i", spec, "::EFI/Linux"], 60, runner=runner)
        if f"ecp_{version}+3.efi" not in listing or PARTIAL in listing:
            raise Failed("the kernel image is not in place on the new ESP")
        with open(target, "rb") as disk:
            os.fsync(disk.fileno())
        ctx.progress(1.0)
        return {"disk": target, "version": version, "root_partition": root_a.number, "bytes": length,
                "partitions": [{"label": p.name, "size": p.size} for p in table]}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _repart(target, definitions, runner):
    check(["systemd-repart", "--dry-run=no", "--empty=force", "--offline=yes", "--seed=random",
           f"--definitions={definitions}", target], 3600, runner=runner)
