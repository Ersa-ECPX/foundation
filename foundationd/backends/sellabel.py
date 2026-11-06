"""SELinux labels for an extension's files, taken from the policy's file_contexts.

A system extension is overlaid on /usr, and an overlay shows the *extension's* directories on top of the
real ones, with the extension's labels. Files created under /var/lib/extensions get the labels of that path
(var_lib_t), so a merged /usr/lib64 would be unreadable to confined services (dbus-broker, logind and GDM were
denied this way when an unlabelled extension was merged). So every file of an extension is labelled for the
path it will have once merged, not the path it is stored at.

The labels come from the policy's own file_contexts (`/etc/selinux/<type>/contexts/files/file_contexts`, the
file is sorted from least to most specific so the last matching entry wins). POSIX bracket classes are
translated; entries whose regular expression Python still cannot read are skipped and counted.
"""

import os
import re
import stat

from ..errors import Failed

KINDS = {"--": stat.S_ISREG, "-d": stat.S_ISDIR, "-l": stat.S_ISLNK}
# POSIX bracket classes, which file_contexts uses and Python's re would read as a different, nested set.
POSIX_CLASSES = {"alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9", "upper": "A-Z", "lower": "a-z",
                 "xdigit": "0-9A-Fa-f", "space": " \\t\\n\\r\\f\\v", "punct": "!-/:-@\\[-`{-~"}


def posix_to_python(regex):
    return re.sub(r"\[:([a-z]+):\]", lambda m: POSIX_CLASSES.get(m.group(1), m.group(0)), regex)
XATTR = "security.selinux"


def policy_dir(root="/"):
    """The directory with the loaded policy type's file contexts, or None if SELinux is not set up."""
    base = os.path.join(root, "etc/selinux")
    kind = "targeted"
    try:
        with open(os.path.join(base, "config"), encoding="utf-8") as handle:
            for line in handle:
                if line.strip().startswith("SELINUXTYPE="):
                    kind = line.split("=", 1)[1].strip() or kind
    except OSError:
        pass
    directory = os.path.join(base, kind, "contexts/files")
    return directory if os.path.exists(os.path.join(directory, "file_contexts")) else None


class Contexts:
    def __init__(self, files):
        self.entries, self.skipped = [], 0
        for name in files:
            try:
                handle = open(name, encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    line = line.rstrip("\n")
                    if not line.strip() or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 2 or parts[-1] == "<<none>>":
                        continue
                    kind = parts[1] if len(parts) == 3 else None
                    try:
                        self.entries.append((re.compile(posix_to_python(parts[0]) + r"\Z"), kind, parts[-1]))
                    except re.error:
                        self.skipped += 1

    @classmethod
    def load(cls, root="/"):
        directory = policy_dir(root)
        if directory is None:
            return None
        return cls([os.path.join(directory, "file_contexts"), os.path.join(directory, "file_contexts.local")])

    def label(self, path, mode):
        """The context for `path` (absolute, as it will be on the running system) of file type `mode`, or None."""
        for regex, kind, context in reversed(self.entries):
            if (kind is None or KINDS.get(kind, lambda _m: False)(mode)) and regex.match(path):
                return context
        return None


def label_tree(tree, contexts, setter=os.setxattr):
    """Label every file and directory of `tree` for the path it will have when the tree is merged onto `/`.

    Raises Failed if a path has no label in the policy. Returns the number of labelled paths.
    """
    count = 0
    for directory, dirs, files in os.walk(tree):
        names = [""] + files + [name for name in dirs if os.path.islink(os.path.join(directory, name))]
        for name in names:
            full = os.path.join(directory, name) if name else directory
            relative = os.path.relpath(full, tree)
            merged = "/" if relative == "." else "/" + relative
            context = contexts.label(merged, os.lstat(full).st_mode)
            if context is None:
                raise Failed(f"the SELinux policy has no label for {merged}")
            setter(full, XATTR, context.encode(), follow_symlinks=False)
            count += 1
    return count
