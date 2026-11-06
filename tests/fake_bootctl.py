#!/usr/bin/env python3
"""A stand-in for `bootctl set-default|set-oneshot ID`: logs the call and writes the loader variable."""
import os
import sys

LOADER_GUID = "4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
command, entry = sys.argv[1], sys.argv[2]
with open(os.environ["FAKE_BOOTCTL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(f"{command} {entry}\n")
variable = {"set-default": "LoaderEntryDefault", "set-oneshot": "LoaderEntryOneShot"}[command]
with open(os.path.join(os.environ["FAKE_EFIVARS"], f"{variable}-{LOADER_GUID}"), "wb") as handle:
    handle.write(b"\x07\x00\x00\x00" + entry.encode("utf-16-le") + b"\x00\x00")
