"""Edit the kernel command line of an unsigned unified kernel image (UKI), in memory.

A UKI is a PE/COFF file whose `.cmdline` section holds the kernel command line. Replacing the text
in place is safe when the new text fits in the section's file space and the image carries no
Authenticode signature (changing a signed image would invalidate it). Anything else is refused:
the caller must never have to guess.
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


def rewrite_cmdline(data, transform):
    """Return a copy of the UKI `data` whose `.cmdline` text is `transform(text)`.

    `transform` gets the current command line as a str and returns the new one. Raises Failed if the
    image is signed, has no `.cmdline`, or the result does not fit in the section's file space.
    """
    table, count, cert_size = _headers(data)
    if cert_size:
        raise Failed("the kernel image is signed; its command line cannot be changed")
    for index in range(count):
        base = table + index * SECTION_SIZE
        name = data[base:base + 8].rstrip(b"\0")
        if name != b".cmdline":
            continue
        virtual_size, _address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, base + 8)
        text = bytes(data[raw_pointer:raw_pointer + min(virtual_size, raw_size)])
        had_nul = text.endswith(b"\0")
        replaced = transform(text.rstrip(b"\0").decode()).encode() + (b"\0" if had_nul else b"")
        if len(replaced) > raw_size:
            raise Failed("the new kernel command line does not fit in the image's .cmdline section")
        out = bytearray(data)
        out[raw_pointer:raw_pointer + raw_size] = replaced + b"\0" * (raw_size - len(replaced))
        struct.pack_into("<I", out, base + 8, len(replaced))
        return bytes(out)
    raise Failed("the kernel image has no .cmdline section")


def replace_cmdline(data, old, new):
    """Return a copy of the UKI `data` with the token `old` replaced by `new` in `.cmdline`.

    `old` must occur exactly once as a whole whitespace-separated token. Raises Failed if the image
    is signed, has no `.cmdline`, the token is not found exactly once, or the result does not fit.
    """
    def transform(text):
        tokens = text.split(" ")
        if tokens.count(old) != 1:
            raise Failed(f"the kernel command line does not contain '{old}' exactly once")
        tokens[tokens.index(old)] = new
        return " ".join(tokens)
    return rewrite_cmdline(data, transform)


def read_cmdline(data):
    """The `.cmdline` text of a UKI (for checks and tests)."""
    table, count, _cert = _headers(data)
    for index in range(count):
        base = table + index * SECTION_SIZE
        if data[base:base + 8].rstrip(b"\0") == b".cmdline":
            virtual_size, _address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, base + 8)
            return data[raw_pointer:raw_pointer + min(virtual_size, raw_size)].rstrip(b"\0").decode()
    raise Failed("the kernel image has no .cmdline section")
