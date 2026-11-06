"""Fixtures shared by the backend tests: fake system trees, a real GPT image, a UKI-like PE file."""

import os
import shutil
import struct
import subprocess
import tempfile

ROOT_TYPE = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
ESP_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LOADER_GUID = "4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
MIB = 1024 * 1024


def build_pe(cmdline, raw_size=512, signed=False, with_cmdline=True):
    """A minimal PE32+ file with a .linux and (optionally) a .cmdline section."""
    sections = [(b".linux", 0x400, 0x200, 0x200)]
    if with_cmdline:
        sections.append((b".cmdline", 0x600, raw_size, len(cmdline) + 1))
    out = bytearray(0x600 + raw_size + 0x200)
    out[0:2] = b"MZ"
    struct.pack_into("<I", out, 0x3C, 0x40)
    out[0x40:0x44] = b"PE\0\0"
    optional_size = 112 + 16 * 8
    struct.pack_into("<HHIIIHH", out, 0x44, 0x8664, len(sections), 0, 0, 0, optional_size, 0x22)
    optional = 0x44 + 20
    struct.pack_into("<H", out, optional, 0x20B)
    if signed:
        struct.pack_into("<II", out, optional + 112 + 4 * 8, 0x900, 0x100)
    table = optional + optional_size
    for index, (name, pointer, size, virtual) in enumerate(sections):
        struct.pack_into("<8sIIII", out, table + index * 40, name, virtual, 0x1000 * (index + 1), size, pointer)
        struct.pack_into("<I", out, table + index * 40 + 36, 0x40000040)   # initialized data, readable
    if with_cmdline:
        out[0x600:0x600 + len(cmdline)] = cmdline
    return bytes(out)


def sfdisk(image, script):
    subprocess.run(["sfdisk", "-q", image], input=script.encode(), check=True, capture_output=True)


class SliceFile:
    """A file object over a byte range of a disk image: stands in for a partition device."""

    def __init__(self, path, offset, length, mode):
        self._f = open(path, "r+b" if "+" in mode or "w" in mode else "rb")
        self._offset, self._length, self._pos = offset, length, 0

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
            raise OSError("write past the end of the partition")
        self._f.seek(self._offset + self._pos)
        self._f.write(data)
        self._pos += len(data)
        return len(data)

    def flush(self):
        self._f.flush()

    def fileno(self):
        return self._f.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


class FakeSystem:
    """A temporary directory laid out like the parts of a running system the slot code reads."""

    def __init__(self, slot_names=("0.2.0", "empty"), running=1, root_mib=4):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        self.image = os.path.join(t, "dev", "vda")
        os.makedirs(os.path.dirname(self.image))
        with open(self.image, "wb") as handle:
            handle.truncate(64 * MIB)
        script = "label: gpt\nfirst-lba: 2048\n\n"
        for name in slot_names:
            label = "_empty" if name == "empty" else (f"ecp-live_{name[5:]}" if name.startswith("live:") else f"ecp-root_{name}")
            script += f'size={root_mib}M, type={ROOT_TYPE}, name="{label}"\n'
        script += f'size=8M, type={ESP_TYPE}, name="ECP-ESP"\n'
        sfdisk(self.image, script)
        self.running = running
        # sysfs: class/block/vda -> devices/vda, class/block/vdaN -> devices/vda/vdaN
        sysfs = os.path.join(t, "sys")
        os.makedirs(os.path.join(sysfs, "class/block"))
        os.makedirs(os.path.join(sysfs, "devices/vda/queue"))
        with open(os.path.join(sysfs, "devices/vda/queue/logical_block_size"), "w") as handle:
            handle.write("512\n")
        os.symlink("../../devices/vda", os.path.join(sysfs, "class/block/vda"))
        for number in range(1, len(slot_names) + 2):
            node = os.path.join(sysfs, f"devices/vda/vda{number}")
            os.makedirs(node)
            with open(os.path.join(node, "partition"), "w") as handle:
                handle.write(f"{number}\n")
            os.symlink(f"../../devices/vda/vda{number}", os.path.join(sysfs, f"class/block/vda{number}"))
        self.esp = os.path.join(t, "esp")
        os.makedirs(os.path.join(self.esp, "EFI/Linux"))
        self.efivars = os.path.join(t, "efivars")
        os.makedirs(self.efivars)
        self.mountinfo = os.path.join(t, "mountinfo")
        self.write_mountinfo([])
        self.paths = {
            "state": os.path.join(t, "state"), "efivars": self.efivars, "os_release": os.path.join(t, "os-release"),
            "mountinfo": self.mountinfo, "sysfs": sysfs, "dev": os.path.join(t, "dev"), "esp": self.esp,
            "root": os.path.join(t, "root"), "bus": "session"}
        with open(self.paths["os_release"], "w") as handle:
            handle.write('ID="opensuse-tumbleweed"\nIMAGE_ID=ecp\n')

    def write_mountinfo(self, extra):
        lines = [f"36 35 0:1 / / ro - erofs /dev/vda{self.running} ro"] + extra
        with open(self.mountinfo, "w") as handle:
            handle.write("\n".join(lines) + "\n")

    def uki(self, name, tries=None, root=None, **kw):
        """Create a UKI file for slot `name`; returns its file name."""
        suffix = "" if tries is None else f"+{tries}"
        filename = f"ecp_{name}{suffix}.efi"
        cmdline = f"quiet root=PARTLABEL=ecp-root_{root or name} rw".encode()
        with open(os.path.join(self.esp, "EFI/Linux", filename), "wb") as handle:
            handle.write(build_pe(cmdline, **kw))
        return filename

    def efivar(self, name, value):
        with open(os.path.join(self.efivars, f"{name}-{LOADER_GUID}"), "wb") as handle:
            handle.write(b"\x07\x00\x00\x00" + value.encode("utf-16-le") + b"\x00\x00")

    def fill_slot(self, number, data, erofs_bytes=None):
        """Write `data` into partition `number`, with an EROFS superblock saying `erofs_bytes`."""
        from foundationd.backends import gpt
        part = [p for p in gpt.read_table(self.image) if p.number == number][0]
        payload = bytearray(data)
        if erofs_bytes:
            payload[1024:1028] = struct.pack("<I", 0xE0F5E1E2)
            payload[1024 + 12] = 12
            struct.pack_into("<I", payload, 1024 + 36, erofs_bytes >> 12)
        with open(self.image, "r+b") as handle:
            handle.seek(part.offset)
            handle.write(bytes(payload))
        return part

    def make_device_files(self):
        """Create dev/vdaN as plain files holding each partition's bytes, for tests that run the real
        workers (which open partition device paths)."""
        from foundationd.backends import gpt
        with open(self.image, "rb") as disk:
            for part in gpt.read_table(self.image):
                disk.seek(part.offset)
                with open(os.path.join(self.tmp, "dev", f"vda{part.number}"), "wb") as out:
                    out.write(disk.read(part.size))

    def opener(self):
        from foundationd.backends import gpt
        parts = {p.number: p for p in gpt.read_table(self.image)}

        def open_partition(path, mode="rb"):
            number = int(os.path.basename(path).removeprefix("vda").removeprefix("p"))
            return SliceFile(self.image, parts[number].offset, parts[number].size, mode)
        return open_partition

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Recorder:
    """A worker context that records what a backend reports."""

    def __init__(self):
        self.steps, self.logs, self.progress_values, self.committed = [], [], [], False

    def step(self, step_id, title):
        self.steps.append(step_id)

    def progress(self, fraction):
        self.progress_values.append(fraction)

    def log(self, text):
        self.logs.append(text)

    def commit(self):
        self.committed = True


def tool_available(name):
    return shutil.which(name) is not None
