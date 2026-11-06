"""repos.* and bundles.* workers: package repositories and bundles of packages as system extensions."""

from ..backends import bundles, repos
from ..workerlib import worker


def _described(paths):
    result = []
    for repo in repos.load_repos(paths).values():
        age = repos.metadata_age(paths, repo.alias)
        result.append({**repo.describe(), "refreshed_seconds_ago": -1 if age is None else age})
    return sorted(result, key=lambda r: (r["priority"], r["alias"]))


@worker("repos.list")
def list_repos(ctx):
    return _described(ctx.paths)


@worker("repos.enable")
def enable(ctx):
    repo = repos.set_enabled(ctx.paths, ctx.arg("alias"), ctx.arg("enabled", bool, True))
    return {"alias": repo.alias, "enabled": repo.enabled}


@worker("repos.inspect_key")
def inspect_key(ctx):
    return repos.key_info(ctx.arg("key"))


@worker("repos.add")
def add(ctx):
    ctx.step("add", "Adding the repository")
    repo = repos.add_repo(ctx.paths, ctx.arg("alias"), ctx.arg("name"), ctx.arg("url"), ctx.arg("priority", int, 99), ctx.arg("key"))
    return {"alias": repo.alias, "url": repo.baseurl}


@worker("repos.remove")
def remove(ctx):
    alias = ctx.arg("alias")
    used = {r for b in bundles.list_bundles(ctx.paths) for r in b["repos"]}
    ctx.step("remove", "Removing the repository")
    return repos.remove_repo(ctx.paths, alias, used)


@worker("repos.refresh")
def refresh(ctx):
    wanted = ctx.arg("aliases", list, [])
    available = repos.load_repos(ctx.paths)
    chosen = [available[a] for a in wanted if a in available] if wanted else [r for r in available.values() if r.enabled]
    unknown = [a for a in wanted if a not in available]
    if unknown:
        from ..errors import NotFound
        raise NotFound("no repository named " + ", ".join(unknown))
    results = []
    ctx.step("refresh", "Updating the package lists")
    for index, repo in enumerate(chosen):
        ctx.log(f"refreshing {repo.alias}")
        results.append(repos.refresh(repo, ctx.paths, force=ctx.arg("force", bool, False)))
        ctx.progress((index + 1) / max(1, len(chosen)))
    return {"repositories": results}


@worker("repos.search")
def search(ctx):
    return repos.Pool(repos.enabled(ctx.paths), ctx.paths).search(ctx.arg("query"))


@worker("repos.info")
def info(ctx):
    return repos.Pool(repos.enabled(ctx.paths), ctx.paths).info(ctx.arg("name"))


@worker("bundles.list")
def list_bundles(ctx):
    return bundles.list_bundles(ctx.paths)


@worker("bundles.catalog")
def catalog(ctx):
    available = repos.load_repos(ctx.paths)
    result = []
    for entry in bundles.catalog(ctx.paths):
        installed = any(b["name"] == entry["name"] for b in bundles.list_bundles(ctx.paths))
        result.append({"name": entry["name"], "title": entry["title"], "description": entry["description"],
                       "notice": entry["notice"], "repos": entry["repos"],
                       "packages": [p["name"] for p in entry["packages"]], "installed": installed,
                       "needs_enabling": [a for a in entry["repos"] if a in available and not available[a].enabled]})
    return result


@worker("bundles.install")
def install(ctx):
    return bundles.add_packages(ctx.paths, ctx, ctx.arg("bundle"), ctx.arg("packages", list),
                                allow_repos=tuple(ctx.arg("allow_repos", list, [])))


@worker("bundles.install_catalog")
def install_catalog(ctx):
    allow = ctx.arg("allow_repos", bool, False)
    entry = bundles.catalog_entry(ctx.paths, ctx.arg("entry"))
    return bundles.install_catalog(ctx.paths, ctx, entry["name"], ctx.arg("bundle", str, ""),
                                   allow_repos=tuple(entry["repos"]) if allow else ())


@worker("bundles.remove")
def remove(ctx):
    names = ctx.arg("packages", list, [])
    if names:
        return bundles.remove_packages(ctx.paths, ctx, ctx.arg("bundle"), names)
    return bundles.remove_bundle(ctx.paths, ctx, ctx.arg("bundle"))


@worker("bundles.update")
def update(ctx):
    return bundles.update(ctx.paths, ctx, ctx.arg("bundle", str, ""))
