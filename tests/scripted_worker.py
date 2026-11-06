"""A worker for tests: real kinds, with some replaced by scripted behaviour.

Used as `--worker-command "python3 tests/scripted_worker.py"`. Which API method reaches which script:

  Extensions.Merge     steps with progress and a result
  Extensions.Unmerge   fails with NotFound
  Extensions.Refresh   sleeps for a long time (cancellation and locking tests)
  Compat.Revalidate    crashes without a result
  Boot.ReleaseSlot     passes the point of no return, then runs for a second
  Compat.Install       reports the size of the staged file it was given
  Compat.Inspect       likewise (a query)
  Boot.GetSlots / GetBootState / Apps.ListInstalled / Apps.GetInfo / Apps.Search: scripted queries
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foundationd import workerlib, workers  # noqa: E402,F401  (importing registers the real kinds)
from foundationd.errors import NotFound  # noqa: E402

R = workerlib.REGISTRY


def steps(ctx):
    ctx.step("one", "First")
    ctx.progress(0.25)
    ctx.log("hello")
    ctx.step("two", "Second")
    ctx.progress(0.75)
    return {"answer": 42, "items": ["a", "b"]}


def fail(ctx):
    ctx.step("x", "Working")
    raise NotFound("no such thing")


def sleeper(ctx):
    ctx.step("wait", "Waiting")
    time.sleep(60)
    return {}


def crash(ctx):
    os._exit(3)


def point_of_no_return(ctx):
    ctx.step("a", "Before")
    ctx.commit()
    time.sleep(1.5)
    return {"done": True}


def staged(ctx):
    path = ctx.arg("path")
    with open(path, "rb") as handle:
        data = handle.read()
    return {"size": len(data), "content": data.decode(), "force": ctx.args.get("force", False)}


R["ext.merge"] = steps
R["ext.unmerge"] = fail
R["ext.refresh"] = sleeper
R["compat.revalidate"] = crash
R["boot.release"] = point_of_no_return
R["compat.install"] = staged
R["compat.inspect"] = staged
R["boot.slots"] = lambda ctx: [{"name": "a", "size": 5, "running": True, "tries_left": -1}, {"name": "b", "size": 6, "running": False}]
R["boot.state"] = lambda ctx: {"efi": True, "tries_left": -1, "assessment": "good"}
R["apps.list"] = lambda ctx: []
R["apps.info"] = lambda ctx: (_ for _ in ()).throw(NotFound("not in the remote"))
R["apps.search"] = crash

sys.exit(workerlib.main(sys.argv[1]))
