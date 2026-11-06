"""org.ersa.Foundation1.Compat: legacy RPMs as system extensions (workers: compat.*).

The RPM arrives as a file descriptor. It is copied into a private staging directory first (without
blocking the daemon), and the worker reads that copy. The daemon never opens a path a client chose.
"""

import os

from ..errors import Failed, FoundationError
from ..service import DEFERRED, Method
from ..variants import reply_dict, reply_list
from . import validate



COMPAT = "org.ersa.Foundation1.Compat"
ACTION_COMPAT = "org.ersa.foundation.compat"
LOCKS = ("extensions",)


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def register(service):
    jobs = service.jobs

    def inspect(call):
        fd = call.take_fd(call.args[0])

        def staged(path, error):
            if error:
                call.fail(Failed(f"could not read the RPM: {error}"))
                return
            service.query(call, "compat.inspect", {"path": path}, reply_dict, cleanup=lambda: _remove(path))

        service.stage(fd, ".rpm", staged)
        return DEFERRED

    def check_locks(call):
        jobs.check_locks(LOCKS)

    def install(call):
        handle, force = call.args
        if not isinstance(force, bool):
            raise FoundationError("force must be a boolean")
        fd = call.take_fd(handle)

        def staged(path, error):
            if error:
                call.fail(Failed(f"could not read the RPM: {error}"))
                return
            try:
                body = service.start_job("compat.install", {"path": path, "force": force}, LOCKS,
                                         cleanup=lambda: _remove(path))
            except FoundationError as failure:
                _remove(path)
                call.fail(failure)
                return
            call.reply(body)

        service.stage(fd, ".rpm", staged)
        return DEFERRED

    def check_name(call):
        validate.name(call.args[0])
        jobs.check_locks(LOCKS)

    def remove(call):
        return service.start_job("compat.remove", {"name": validate.name(call.args[0])}, LOCKS)

    def list_all(call):
        return service.query(call, "compat.list", {}, reply_list)

    def revalidate(call):
        return service.start_job("compat.revalidate", {}, LOCKS)

    # -- bundles: many packages from the repositories in one extension --------------------------------

    def list_bundles(call):
        return service.query(call, "bundles.list", {}, reply_list)

    def list_catalog(call):
        return service.query(call, "bundles.catalog", {}, reply_list)

    def check_bundle_install(call):
        validate.name(call.args[0])
        validate.packages(call.args[1])
        validate.aliases(call.args[2])
        jobs.check_locks(LOCKS + ("repos",))

    def install_packages(call):
        bundle, packages, allow = call.args
        return service.start_job("bundles.install", {"bundle": validate.name(bundle), "packages": validate.packages(packages),
                                                      "allow_repos": validate.aliases(allow)}, LOCKS + ("repos",))

    def check_catalog_install(call):
        validate.name(call.args[0])
        if call.args[1]:
            validate.name(call.args[1])
        jobs.check_locks(LOCKS + ("repos",))

    def install_catalog(call):
        entry, bundle, allow = call.args
        return service.start_job("bundles.install_catalog", {"entry": validate.name(entry), "bundle": bundle and validate.name(bundle),
                                                              "allow_repos": bool(allow)}, LOCKS + ("repos",))

    def check_bundle_remove(call):
        validate.name(call.args[0])
        if call.args[1]:
            validate.packages(call.args[1])
        jobs.check_locks(LOCKS + ("repos",))

    def remove_packages(call):
        bundle, packages = call.args
        return service.start_job("bundles.remove", {"bundle": validate.name(bundle),
                                                    "packages": validate.packages(packages) if packages else []}, LOCKS + ("repos",))

    def check_bundle_update(call):
        if call.args[0]:
            validate.name(call.args[0])
        jobs.check_locks(LOCKS + ("repos",))

    def check_bundle_update_all(call):
        jobs.check_locks(LOCKS + ("repos",))

    def update_bundles(call):
        return service.start_job("bundles.update", {"bundle": call.args[0] and validate.name(call.args[0])}, LOCKS + ("repos",))

    def refresh_bundles(call):
        return service.start_job("bundles.refresh", {}, LOCKS + ("repos",))

    return {
        (COMPAT, "RefreshBundles"): Method(refresh_bundles, ACTION_COMPAT, check_bundle_update_all),
        (COMPAT, "ListBundles"): Method(list_bundles),
        (COMPAT, "ListCatalog"): Method(list_catalog),
        (COMPAT, "InstallPackages"): Method(install_packages, ACTION_COMPAT, check_bundle_install),
        (COMPAT, "InstallCatalog"): Method(install_catalog, ACTION_COMPAT, check_catalog_install),
        (COMPAT, "RemovePackages"): Method(remove_packages, ACTION_COMPAT, check_bundle_remove),
        (COMPAT, "UpdateBundles"): Method(update_bundles, ACTION_COMPAT, check_bundle_update),
        (COMPAT, "Inspect"): Method(inspect),
        (COMPAT, "Install"): Method(install, ACTION_COMPAT, check_locks),
        (COMPAT, "Remove"): Method(remove, ACTION_COMPAT, check_name),
        (COMPAT, "List"): Method(list_all),
        (COMPAT, "Revalidate"): Method(revalidate, ACTION_COMPAT, check_locks),
    }
