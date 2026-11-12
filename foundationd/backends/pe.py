"""Read the kernel command line of a unified kernel image (UKI), in memory (verity.py reads it from a file).

A UKI is a PE/COFF file whose `.cmdline` section holds the kernel command line. Images are signed for Secure Boot, so
nothing here (or anywhere) changes one: the command line is the same on every machine.
"""

import struct

from ..errors import Failed

SECTION_SIZE = 40
CERTIFICATE_DIRECTORY = 4


def _headers(data):
    if data[:2] != b"MZ":
        raise Failed("not a PE image")
    (pe,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise Failed("not a PE image")
    coff = pe + 4
    sections, = struct.unpack_from("<H", data, coff + 2)
    optional_size, = struct.unpack_from("<H", data, coff + 16)
    optional = coff + 20
    magic, = struct.unpack_from("<H", data, optional)
    if magic != 0x20B:
        raise Failed("only PE32+ images are supported")
    directory = optional + 112 + CERTIFICATE_DIRECTORY * 8
    cert_offset, cert_size = struct.unpack_from("<II", data, directory)
    return optional + optional_size, sections, cert_size


def read_cmdline(data):
    """The `.cmdline` text of a UKI (for checks and tests)."""
    table, count, _cert = _headers(data)
    for index in range(count):
        base = table + index * SECTION_SIZE
        if data[base:base + 8].rstrip(b"\0") == b".cmdline":
            virtual_size, _address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, base + 8)
            return data[raw_pointer:raw_pointer + min(virtual_size, raw_size)].rstrip(b"\0").decode()
    raise Failed("the kernel image has no .cmdline section")
