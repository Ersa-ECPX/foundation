"""Safely unpack an RPM payload (a decompressed "newc" cpio stream) into a directory.

`rpm2cpio` decompresses the payload; this module reads the stream itself instead of handing it to
the `cpio` tool, because a crafted archive can contain a symlink followed by files written
*through* it, which would land outside the target directory. Here every entry is checked:

  - the path is relative after removing leading "./" and "/", and contains no ".." component;
  - no parent component that already exists is a symlink (so nothing is written through one);
  - only directories, regular files, symlinks and hard links are created; devices, FIFOs and
    sockets are skipped.

Modes (including setuid bits) and modification times are kept; ownership is the extracting user's.
"""

import os
import stat

from ..errors import Failed

NEWC_MAGIC = b"070701"
HEADER = 110


class UnsafeArchive(Failed):
    pass


def _field(header, index):
    return int(header[6 + index * 8:14 + index * 8], 16)


def _read_exact(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise Failed("the RPM payload is truncated")
        data += chunk
    return data


def _skip_padding(stream, length):
    pad = (4 - length % 4) % 4
    if pad:
        _read_exact(stream, pad)


def _safe_target(root, name):
    relative = name.lstrip("/")
    while relative.startswith("./"):
        relative = relative[2:]
    parts = [p for p in relative.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise UnsafeArchive(f"unsafe path in the RPM payload: {name!r}")
    current = root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise UnsafeArchive(f"the RPM payload writes through a symlink: {name!r}")
    return os.path.join(root, *parts), parts


def unpack(stream, root):
    """Unpack the cpio stream into `root`. Returns the number of regular files and symlinks."""
    count = 0
    deferred = {}  # (dev, ino) -> [paths of hard links whose data comes with the last one]
    while True:
        header = _read_exact(stream, HEADER)
        if header[:6] != NEWC_MAGIC:
            raise Failed("the RPM payload is not a newc cpio archive")
        mode, nlink, mtime, size = _field(header, 1), _field(header, 4), _field(header, 5), _field(header, 6)
        major, minor, namesize = _field(header, 7), _field(header, 8), _field(header, 11)
        raw_name = _read_exact(stream, namesize)
        _skip_padding(stream, HEADER + namesize)
        name = raw_name.rstrip(b"\0").decode("utf-8", "surrogateescape")
        if name == "TRAILER!!!":
            break
        data = _read_exact(stream, size)
        _skip_padding(stream, size)
        kind = stat.S_IFMT(mode)
        if kind not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK):
            continue
        path, _parts = _safe_target(root, name)
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        if kind == stat.S_IFDIR:
            os.makedirs(path, exist_ok=True)
            os.chmod(path, stat.S_IMODE(mode) | 0o700)
            continue
        if os.path.lexists(path):
            os.unlink(path)
        if kind == stat.S_IFLNK:
            os.symlink(data.decode("utf-8", "surrogateescape"), path)
            count += 1
            continue
        key = (major, minor, _field(header, 2))
        if nlink > 1 and size == 0:        # a hard link whose data arrives with the last one
            deferred.setdefault(key, []).append(path)
            continue
        with open(path, "wb") as out:
            out.write(data)
        os.chmod(path, stat.S_IMODE(mode))
        os.utime(path, (mtime, mtime))
        count += 1
        for other in deferred.pop(key, []):
            if os.path.lexists(other):
                os.unlink(other)
            os.link(path, other)
            count += 1
    if deferred:
        raise Failed("the RPM payload has hard links without data")
    return count
