"""API handlers, one module per interface group. Each has `register(service) -> {key: Method}`."""

from . import apps, boot, compat, config, deploy, extensions, manager, repos, updates

# Capability name -> module. Clients see these names in Manager.GetStatus.
MODULES = {
    "manager": manager,
    "config": config,
    "boot": boot,
    "apps": apps,
    "compat": compat,
    "deploy": deploy,
    "extensions": extensions,
    "repos": repos,
    "updates": updates,
}
