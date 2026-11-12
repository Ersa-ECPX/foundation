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
VERITY_SLOT = 96 * MIB           # the dm-verity hash tree of one root slot (about 65 MiB for 8 GiB)
DATA_MIN = 10 * GIB              # the data volume has no maximum: it takes the rest of the disk
OVERHEAD = 16 * MIB              # partition tables and alignment
MIN_DISK = ESP + RECOVERY + 2 * (ROOT_SLOT + VERITY_SLOT) + DATA_MIN + OVERHEAD

# Slot labels never change (docs/features/dm-verity.md): what a slot holds is named by the root hash, which sets the
# partition UUIDs. The installer medium's own root is labelled differently, so it is never taken for a slot.
ROOT_LABELS = ("ecp-root-a", "ecp-root-b")
VERITY_LABELS = ("ecp-verity-a", "ecp-verity-b")


def rows():
    """The planned partitions, in order: (label, size in bytes or None for 'the rest', note)."""
    return [
        ("ECP-ESP", ESP, "boot loader and the kernel image"),
        ("ECP-RECOVERY", RECOVERY, "reserved for a recovery system"),
        (ROOT_LABELS[0], ROOT_SLOT, "root slot A: the operating system"),
        (VERITY_LABELS[0], VERITY_SLOT, "root slot A: its dm-verity hash tree"),
        (ROOT_LABELS[1], ROOT_SLOT, "root slot B: empty, for the next update"),
        (VERITY_LABELS[1], VERITY_SLOT, "root slot B: its dm-verity hash tree"),
        ("ECP-DATA", None, "/var and /home (btrfs), the rest of the disk"),
    ]


def definitions(esp_source, seed_source, root_uuid, verity_uuid):
    """The repart.d files (name -> text). `esp_source` and `seed_source` are directories holding what the ESP and the
    data volume start with. `root_uuid` and `verity_uuid` are the GPT UUIDs of root slot A's two partitions: the two halves
    of the root hash, which is how the kernel image (whose command line names the hash) finds them (see verity.py)."""
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
Label={ROOT_LABELS[0]}
UUID={root_uuid}
ReadOnly=yes
SizeMinBytes={ROOT_SLOT}
SizeMaxBytes={ROOT_SLOT}
""",
        "11-root-verity-a.conf": f"""[Partition]
Type=root-verity
Label={VERITY_LABELS[0]}
UUID={verity_uuid}
ReadOnly=yes
SizeMinBytes={VERITY_SLOT}
SizeMaxBytes={VERITY_SLOT}
""",
        "15-root-b.conf": f"""[Partition]
Type=root
Label={ROOT_LABELS[1]}
ReadOnly=yes
SizeMinBytes={ROOT_SLOT}
SizeMaxBytes={ROOT_SLOT}
""",
        "16-root-verity-b.conf": f"""[Partition]
Type=root-verity
Label={VERITY_LABELS[1]}
ReadOnly=yes
SizeMinBytes={VERITY_SLOT}
SizeMaxBytes={VERITY_SLOT}
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
