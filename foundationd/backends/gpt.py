"""Read a GUID Partition Table directly from a disk (or a disk image file). Read-only.

Writing partition labels is done with sfdisk (see slots.py): the table has CRCs, a backup copy and
a kernel notification, which are not worth reimplementing.
"""

import struct
import uuid
import zlib
from dataclasses import dataclass

ROOT_TYPES = {
    "x86_64": uuid.UUID("4f68bce3-e8cd-4db1-96e7-fbcaf984b709"),
    "aarch64": uuid.UUID("b921b045-1df0-41c3-af44-4c6f280d3fae"),
}
ESP_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
SIGNATURE = b"EFI PART"


class GptError(Exception):
    pass


@dataclass(frozen=True)
class Partition:
    number: int            # 1-based index in the table
    type: uuid.UUID
    uuid: uuid.UUID
    first_lba: int
    last_lba: int
    name: str
    sector: int

    @property
    def size(self):
        return (self.last_lba - self.first_lba + 1) * self.sector

    @property
    def offset(self):
        return self.first_lba * self.sector


def read_table(path, sector=512):
    """The used partitions of the GPT on `path`. Raises GptError if there is no valid table."""
    with open(path, "rb") as disk:
        disk.seek(sector)
        header = disk.read(92)
        if len(header) < 92 or header[:8] != SIGNATURE:
            raise GptError(f"{path}: no GPT header")
        header_size, stored_crc = struct.unpack_from("<II", header, 12)
        if not 92 <= header_size <= sector:
            raise GptError(f"{path}: implausible GPT header size")
        disk.seek(sector)
        whole = bytearray(disk.read(header_size))
        whole[16:20] = b"\0\0\0\0"
        if zlib.crc32(bytes(whole)) & 0xFFFFFFFF != stored_crc:
            raise GptError(f"{path}: GPT header checksum mismatch")
        entries_lba, count, entry_size, entries_crc = struct.unpack_from("<QIII", header, 72)
        if not 128 <= entry_size <= 4096 or count > 4096:
            raise GptError(f"{path}: implausible GPT entry layout")
        disk.seek(entries_lba * sector)
        raw = disk.read(count * entry_size)
    if len(raw) != count * entry_size or zlib.crc32(raw) & 0xFFFFFFFF != entries_crc:
        raise GptError(f"{path}: GPT entry array checksum mismatch")
    partitions = []
    for index in range(count):
        entry = raw[index * entry_size:(index + 1) * entry_size]
        type_guid = uuid.UUID(bytes_le=entry[0:16])
        if type_guid.int == 0:
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        name = entry[56:128].decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        partitions.append(Partition(index + 1, type_guid, uuid.UUID(bytes_le=entry[16:32]),
                                    first, last, name, sector))
    return partitions
