"""What an extension needs after it is merged, defined as data and done the same for every desktop.

Some files a package ships are only useful after a tool has processed them (settings schemas, for example, are compiled
into one cache that a package's scriptlet would rebuild). Scriptlets are not run, and the cache in the base image is
read-only, so the result is generated into a directory of its own, `<state>/generated/<name>/`, which sessions are
pointed at by an environment setting shipped in the image.

Each job is a file in `/usr/lib/foundation/post-merge.d/*.json` (image-owned, so trusted; a bundle cannot add one):

    {"name": "gsettings", "collect": ["usr/share/glib-2.0/schemas/*.gschema.xml"], "run": ["glib-compile-schemas", "{out}"]}

`collect` are glob patterns, relative to an extension's root, of the files to gather from every enabled extension into
`{out}`; `run` is the command (no shell) that turns them into the result. With nothing to collect the output is removed.
A job that fails is logged and its output removed, never left half made. Nothing here is specific to one desktop:
supporting another is one more file.
"""

import glob
import json
import logging
import os
import shutil
import tempfile

from . import compat
from .runner import run

log = logging.getLogger(__name__)
JOB_DIR = "usr/lib/foundation/post-merge.d"
NAME = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def jobs(paths):
    """The job definitions of the running image, in file order. A file that cannot be read is logged and skipped."""
    directory = os.path.join(paths["root"], JOB_DIR)
    found = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return found
    for filename in names:
        try:
            with open(os.path.join(directory, filename), encoding="utf-8") as handle:
                job = json.load(handle)
            if not NAME.match(str(job.get("name", ""))):
                raise ValueError("unusable name")
            if not (isinstance(job.get("collect"), list) and all(isinstance(p, str) and not p.startswith("/") and ".." not in p
                                                                 for p in job["collect"])):
                raise ValueError("collect must be a list of relative patterns")
            if not (isinstance(job.get("run"), list) and job["run"] and all(isinstance(a, str) for a in job["run"])):
                raise ValueError("run must be a command as a list")
        except (OSError, ValueError) as error:
            log.warning("ignoring %s: %s", filename, error)
            continue
        found.append(job)
    return found


def output_dir(paths, name):
    return os.path.join(paths["state"], "generated", name)


def run_job(paths, job, trees, runner=run):
    """Generate the output of one job from the extension trees. Returns the number of files gathered (0: no output)."""
    out = output_dir(paths, job["name"])
    gathered = []
    for tree in trees:
        for pattern in job["collect"]:
            for path in sorted(glob.glob(os.path.join(tree, pattern))):
                if os.path.isfile(path) and not os.path.islink(path):
                    gathered.append(path)
    if not gathered:
        shutil.rmtree(out, ignore_errors=True)
        return 0
    base = os.path.dirname(out)
    os.makedirs(base, exist_ok=True)
    staging = tempfile.mkdtemp(dir=base, prefix=f".{job['name']}.")
    try:
        names = set()
        for path in gathered:
            target = os.path.join(staging, os.path.basename(path))
            if os.path.basename(path) in names:
                continue                       # the first extension with that file name wins
            names.add(os.path.basename(path))
            shutil.copyfile(path, target)
        command = [argument.replace("{out}", staging) for argument in job["run"]]
        code, _out, err = runner(command, 120)
        if code != 0:
            raise RuntimeError((err or "").strip().splitlines()[-1:] or f"exit {code}")
        os.chmod(staging, 0o755)
        for name in os.listdir(staging):
            os.chmod(os.path.join(staging, name), 0o644)
        old = out + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.lexists(out):
            os.rename(out, old)
        os.rename(staging, out)
        shutil.rmtree(old, ignore_errors=True)
        return len(names)
    except (RuntimeError, OSError) as error:
        log.warning("post-merge job %s failed: %s", job["name"], error)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
        return 0


def apply(paths, runner=run):
    """Run every job against the enabled extensions. Returns {job name: files gathered}."""
    trees = [compat._enabled_dir(paths, meta["name"]) for meta in compat.installed_metas(paths)]
    trees = [tree for tree in trees if os.path.isdir(tree)]
    return {job["name"]: run_job(paths, job, trees, runner) for job in jobs(paths)}
