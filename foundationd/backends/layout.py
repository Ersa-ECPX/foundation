"""The disk layout of an installed system, in one place.

`systemd-repart` definitions are generated from these constants when a system is deployed to a disk
(deploy.py). They must match `mkosi.repart.system/` (the layout of the normal image); a test compares
the two so they cannot drift apart silently.
"""

MIB = 1024 * 1024
GIB = 1024 * MIB

ESP = 1 * GIB
RECOVERY = 1 * GIB
ROOT_SLOT = 8 * GIB
DATA_MIN = 10 * GIB              # the data volume has no maximum: it takes the rest of the disk
OVERHEAD = 16 * MIB              # partition tables and alignment
MIN_DISK = ESP + RECOVERY + 2 * ROOT_SLOT + DATA_MIN + OVERHEAD

LABEL_PREFIX = "ecp-root_"
EMPTY_LABEL = "_empty"
MAX_LABEL = 36                   # a GPT partition name holds 36 UTF-16 characters


def rows(version):
    """The planned partitions, in order: (label, size in bytes or None for 'the rest', note)."""
    return [
        ("ECP-ESP", ESP, "boot loader and the kernel image"),
        ("ECP-RECOVERY", RECOVERY, "reserved for a recovery system"),
        (LABEL_PREFIX + version, ROOT_SLOT, "root slot A: the operating system"),
        (EMPTY_LABEL, ROOT_SLOT, "root slot B: empty, for the next update"),
        ("ECP-DATA", None, "/var and /home (btrfs), the rest of the disk"),
    ]


def definitions(version, esp_source, seed_source):
    """The repart.d files (name -> text). `esp_source` and `seed_source` are directories holding what
    the ESP and the data volume start with."""
    label = LABEL_PREFIX + version
    return {
        "00-esp.conf": f"""[Partition]
Type=esp
Label=ECP-ESP
Format=vfat
CopyFiles={esp_source}:/
SizeMinBytes={ESP}
SizeMaxBytes={ESP}
""",
        "05-recovery.conf": f"""[Partition]
Type=linux-generic
Label=ECP-RECOVERY
SizeMinBytes={RECOVERY}
SizeMaxBytes={RECOVERY}
""",
        "10-root-a.conf": f"""[Partition]
Type=root
Label={label}
ReadOnly=yes
SizeMinBytes={ROOT_SLOT}
SizeMaxBytes={ROOT_SLOT}
""",
        "15-root-b.conf": f"""[Partition]
Type=root
Label={EMPTY_LABEL}
ReadOnly=yes
SizeMinBytes={ROOT_SLOT}
SizeMaxBytes={ROOT_SLOT}
""",
        "30-data.conf": f"""[Partition]
Type=linux-generic
Label=ECP-DATA
Format=btrfs
Subvolumes=/var /home
CopyFiles={seed_source}/var:/var
CopyFiles={seed_source}/home:/home
SizeMinBytes={DATA_MIN}
""",
    }
