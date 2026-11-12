"""dm-verity facts the slot code needs: how a root hash names its partitions, and where a kernel image states it.

A kernel image built with dm-verity carries `roothash=<hex>` on its command line. systemd finds the root partition from
it without any label: following the Discoverable Partitions Specification, the data partition's UUID is the first 128 bits
of the root hash and its hash partition's UUID is the last 128 bits (a SHA-256 root hash is 256 bits, so the two halves are
distinct). So a slot is identified by the hash of what it holds, and the same kernel image boots on every machine.
"""

import re
import struct
import uuid

from ..errors import Failed

# partition type GUIDs, from the Discoverable Partitions Specification
ROOT_VERITY_TYPES = {
    "x86_64": uuid.UUID("2c7357ed-ebd2-46d9-aec1-23d437ec2bf5"),
    "aarch64": uuid.UUID("df3300ce-d69f-4c92-978c-9bfb0f38d820"),
}
ROOTHASH = re.compile(r"(?:^|\s)roothash=([0-9a-fA-F]{64})(?=\s|$)")
SECTION_SIZE = 40


def uuids(roothash):
    """(data partition UUID, hash partition UUID) for a SHA-256 root hash given as 64 hex digits."""
    text = str(roothash).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise Failed("a root hash is 64 hexadecimal digits (SHA-256)")
    return uuid.UUID(text[:32]), uuid.UUID(text[32:])


def roothash_of(cmdline):
    """The root hash on a kernel command line, as lowercase hex, or "" if it has none."""
    match = ROOTHASH.search(cmdline or "")
    return match.group(1).lower() if match else ""


def cmdline_of_file(path):
    """The `.cmdline` of a UKI file, reading only its headers and that one section (a UKI is over 100 MB)."""
    with open(path, "rb") as handle:
        head = handle.read(4096)
        if head[:2] != b"MZ":
            raise Failed(f"{path}: not a PE image")
        (pe,) = struct.unpack_from("<I", head, 0x3C)
        handle.seek(pe)
        header = handle.read(24)
        if header[:4] != b"PE\0\0":
            raise Failed(f"{path}: not a PE image")
        count, = struct.unpack_from("<H", header, 6)
        optional_size, = struct.unpack_from("<H", header, 20)
        handle.seek(pe + 24 + optional_size)
        table = handle.read(count * SECTION_SIZE)
        for index in range(count):
            base = index * SECTION_SIZE
            if table[base:base + 8].rstrip(b"\0") != b".cmdline":
                continue
            virtual_size, _address, raw_size, raw_pointer = struct.unpack_from("<IIII", table, base + 8)
            handle.seek(raw_pointer)
            return handle.read(min(virtual_size, raw_size)).rstrip(b"\0").decode(errors="replace")
    raise Failed(f"{path}: the kernel image has no .cmdline section")
