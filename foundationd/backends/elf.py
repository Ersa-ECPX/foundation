"""Read the shared-library dependencies of an ELF file (64-bit, little-endian), without tools.

`needed` are the sonames the file asks the dynamic linker for (DT_NEEDED); `soname` is the name the
file itself provides (DT_SONAME). Anything that is not a readable 64-bit little-endian ELF returns
None, so callers can scan a whole directory tree and ignore data files.
"""

import struct

PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
DT_NULL, DT_NEEDED, DT_STRTAB, DT_SONAME = 0, 1, 5, 14


def read_dynamic(path):
    """{'needed': [...], 'soname': str or None, 'interp': bool}, or None if `path` is not a suitable ELF.

    `interp` is true for a program that the kernel starts through a dynamic loader (it has a
    PT_INTERP header), false for a library or plugin."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
            if len(head) < 64 or head[:4] != b"\x7fELF" or head[4] != 2 or head[5] != 1:
                return None
            phoff, = struct.unpack_from("<Q", head, 32)
            phentsize, phnum = struct.unpack_from("<HH", head, 54)
            if phentsize < 56 or phnum == 0 or phnum > 512:
                return None
            handle.seek(phoff)
            table = handle.read(phentsize * phnum)
            segments = []
            for index in range(phnum):
                p_type, _flags, offset, vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from(
                    "<IIQQQQQQ", table, index * phentsize)
                segments.append((p_type, offset, vaddr, filesz))
            interp = any(t == PT_INTERP for t, _o, _v, _s in segments)
            dynamic = next(((o, s) for t, o, _v, s in segments if t == PT_DYNAMIC), None)
            if dynamic is None:
                return {"needed": [], "soname": None, "interp": interp}
            handle.seek(dynamic[0])
            raw = handle.read(min(dynamic[1], 1 << 20))
            entries = []
            for index in range(0, len(raw) - 15, 16):
                tag, value = struct.unpack_from("<qQ", raw, index)
                if tag == DT_NULL:
                    break
                entries.append((tag, value))
            strtab = next((v for t, v in entries if t == DT_STRTAB), None)
            if strtab is None:
                return {"needed": [], "soname": None, "interp": interp}
            # DT_STRTAB is a virtual address; find the file offset through the loadable segments.
            file_offset = next((o + (strtab - v) for t, o, v, s in segments
                                if t == PT_LOAD and v <= strtab < v + s), None)
            if file_offset is None:
                return None

            def string(offset):
                handle.seek(file_offset + offset)
                chunk = handle.read(512)
                return chunk.split(b"\0", 1)[0].decode(errors="replace")

            return {"needed": [string(v) for t, v in entries if t == DT_NEEDED],
                    "soname": next((string(v) for t, v in entries if t == DT_SONAME), None),
                    "interp": interp}
    except (OSError, struct.error):
        return None
