"""Run an external tool, for the few jobs that have no D-Bus or library interface.

Prefer a D-Bus service or a library binding; a command line is the last resort and is used only
inside workers (never in the broker), with an argument list (never a shell).
"""

import subprocess

from ..errors import Failed, Unavailable


def run(cmd, timeout=60, input_bytes=None):
    """Run `cmd` (a list). Returns (returncode, stdout, stderr) as text. A missing tool is 127."""
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    return done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")


def check(cmd, timeout=60, runner=run):
    """Run `cmd` and return stdout; raise Unavailable if the tool is missing, Failed if it fails."""
    code, out, err = runner(cmd, timeout)
    if code == 127:
        raise Unavailable(f"{cmd[0]} is not installed")
    if code != 0:
        detail = (err or out).strip().splitlines()
        raise Failed(f"{cmd[0]} failed (exit {code}): {detail[-1] if detail else 'no output'}")
    return out
