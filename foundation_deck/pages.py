"""Deck's pages. Each is a scrolling column of groups that loads itself from foundationd when shown (`load`) and holds no policy.

A page gets the window (`win`) for the shared plumbing: win.ask (a call whose reply feeds a handler), win.run_job (a job that is followed
to its end), win.confirm, win.toast and win.open_fd (a descriptor for a file the person chose; the service never opens a path).
"""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__, icons, model  # noqa: E402


def clear(group):
    for row in list(getattr(group, "_rows", [])):
        group.remove(row)
    group._rows = []


def add_row(group, row):
    group.add(row)
    group._rows = getattr(group, "_rows", []) + [row]


def flat_button(icon, tip, handler):
    button = icons.button(icon, tip, valign=Gtk.Align.CENTER)
    button.connect("clicked", lambda _b: handler())
    return button


def spacer():
    """An invisible stand-in for a flat icon button, so rows with and without one keep their switches in one column."""
    ghost = icons.button("user-trash-symbolic", "", valign=Gtk.Align.CENTER)
    ghost.set_opacity(0)
    ghost.set_sensitive(False)
    ghost.set_focusable(False)
    ghost.set_can_target(False)
    return ghost


def text_button(label, handler, *css):
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=list(css))
    button.connect("clicked", lambda _b: handler())
    return button


def hero_box():
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=8)


class Page(Gtk.ScrolledWindow):
    """A scrolling, centred column; add() appends to it."""
    loaded = False

    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=32,
                              margin_start=16, margin_end=16)
        self.set_child(Adw.Clamp(maximum_size=600, tightening_threshold=500, child=self.column))
        self.error = Adw.Banner(title="", revealed=False, css_classes=["page-error"])
        self.column.append(self.error)

    def ask(self, interface, method, signature, args, handler=None, failed=None, **kwargs):
        """win.ask, but a failure is shown on this page (a banner at its top) instead of a toast that follows you around."""
        self.win.ask(interface, method, signature, args, handler, failed or self.show_error, **kwargs)

    def show_error(self, error):
        self.error.set_title(model.error_text(error.name, error.message))
        self.error.set_revealed(True)

    def reload(self):
        self.error.set_revealed(False)
        self.load()

    def add(self, widget):
        self.column.append(widget)

    def group(self, title=None, description=None, suffix=None):
        group = Adw.PreferencesGroup(title=title or "", description=description or "")
        if suffix is not None:
            group.set_header_suffix(suffix)
        self.add(group)
        return group

    def load(self):
        """(Re)load from foundationd. Subclasses override."""

    def empty(self, group, text):
        add_row(group, Adw.ActionRow(title=text, css_classes=["dim-label"]))


# -- Overview ------------------------------------------------------------------------------------

class Overview(Page):
    """The running version and whether this start was good, then the system's facts, the software versions and the project."""

    def __init__(self, win):
        super().__init__(win)
        hero = hero_box()
        self.hero_logo = Gtk.Image(pixel_size=88, halign=Gtk.Align.CENTER)
        self.version_label = Gtk.Label(label="\u2026", css_classes=["hero-version"])
        self.slot_label = Gtk.Label(label="", css_classes=["dim-label"])
        self.pill = Gtk.Label(label="", halign=Gtk.Align.CENTER, margin_top=4, css_classes=["status-pill", "unknown"])
        for widget in (self.hero_logo, Gtk.Label(label=model.PRODUCT.upper(), css_classes=["hero-product"], margin_top=8),
                       self.version_label, self.slot_label, self.pill):
            hero.append(widget)
        self.add(hero)
        self.system = self.group("This system")
        self.software = self.group("Software")
        project = self.group("Project")
        row = Adw.ActionRow(title="Website", subtitle=model.WEBSITE, activatable=True)
        row.add_suffix(icons.image("adw-external-link-symbolic"))
        row.connect("activated", lambda _r: Gtk.UriLauncher(uri=model.WEBSITE).launch(win, None, None, None))
        project.add(row)
        self.facts = {}

    def load(self):
        self.facts = {}

        def state(value):
            self.version_label.set_label(value.get("running_version") or "?")
            self.slot_label.set_label(model.running_subtitle(value))
            style, text = model.assessment(value)
            for css in ("good", "pending", "unknown"):
                self.pill.remove_css_class(css)
            self.pill.add_css_class(style)
            self.pill.set_label(text)

        def show():
            clear(self.system)
            for label, value in model.about_rows(self.facts.get("info", {}), self.facts.get("status", {})):
                add_row(self.system, Adw.ActionRow(title=label, subtitle=value, subtitle_selectable=True))
            clear(self.software)
            for label, value in model.software_rows(__version__, self.facts.get("api", "")):
                add_row(self.software, Adw.ActionRow(title=label, subtitle=value, subtitle_selectable=True))

        def got(key):
            return lambda value: (self.facts.update({key: value}), show())
        self.ask("Boot", "GetBootState", None, None, state)
        self.ask("Manager", "GetSystemInfo", None, None, got("info"))
        self.ask("Manager", "GetStatus", None, None, got("status"))
        self.ask("org.freedesktop.DBus.Properties", "Get", "(ss)", ("org.ersa.Foundation1.Manager", "Version"), got("api"))


# -- Updates -------------------------------------------------------------------------------------

class Updates(Page):
    def __init__(self, win):
        super().__init__(win)
        self.available, self.pending = {}, {}
        hero = hero_box()
        self.title = Gtk.Label(label="Updates", css_classes=["title-1"])
        self.status = Gtk.Label(label="", css_classes=["update-status", "dim-label"], wrap=True,
                                justify=Gtk.Justification.CENTER, max_width_chars=44)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, halign=Gtk.Align.CENTER, margin_top=10)
        self.check_button = text_button("Check for Updates", self.check, "pill")
        self.install_button = text_button("Install", self.install, "pill", "suggested-action")
        self.restart_button = text_button("Restart…", win.ask_restart, "pill", "suggested-action")
        self.install_button.set_visible(False)
        self.restart_button.set_visible(False)
        for widget in (self.check_button, self.install_button, self.restart_button):
            buttons.append(widget)
        for widget in (self.title, self.status, buttons):
            hero.append(widget)
        self.add(hero)
        auto = self.group("Automatic updates", "Nothing restarts the computer by itself.")
        self.switches = {}
        for key, title in (("ota.auto_check", "Check automatically"), ("ota.auto_update", "Install automatically")):
            row = Adw.SwitchRow(title=title)
            row.connect("notify::active", lambda r, _p, k=key: self.set_switch(r, k))
            self.switches[key] = row
            auto.add(row)
        self.developer_group = self.group("Developer mode", "Developer mode is on, so an update without a signature may be installed.")
        self.unsigned_row = Adw.SwitchRow(title="Install without checking the signature",
                                          subtitle="For images you built yourself. Anything can be installed this way.")
        self.developer_group.add(self.unsigned_row)
        self.developer_group.set_visible(False)
        local = self.group("Install from a file", "For an update you built or were given. It is checked against the trusted keys.")
        row = Adw.ActionRow(title="Update file", subtitle="A .tar.zst archive")
        row.add_suffix(text_button("Choose…", self.choose_file))
        local.add(row)

    def set_switch(self, row, key):
        if getattr(row, "_loading", False):
            return
        value = row.get_active()

        def failed(_e):
            self.load_switch(key, row)
        self.ask("Config", "Set", "(sv)", (key, GLib.Variant.new_boolean(value)), None, failed)

    def load_switch(self, key, row):
        def got(value):
            if isinstance(value, bool):
                row._loading = True
                row.set_active(value)
                row._loading = False
        self.ask("Config", "Get", "(s)", (key,), got)

    def load(self):
        for key, row in self.switches.items():
            self.load_switch(key, row)

        def developer(value):
            self.developer_group.set_visible(bool(value))
            if not value:
                self.unsigned_row.set_active(False)
        self.ask("Config", "Get", "(s)", ("system.developer_mode",), developer, lambda _e: None)

        def pending(value):
            self.pending = value
            self.show()

        def available(value):
            self.available = value
            self.show()
        self.ask("Updates", "Pending", None, None, pending)
        self.ask("Updates", "Available", None, None, available)

    def show(self):
        style, text = model.update_status(self.available, self.pending)
        self.status.remove_css_class("available")
        if style:
            self.status.add_css_class(style)
        self.status.set_label(text)
        self.install_button.set_visible(bool(self.available.get("available")) and not self.pending)
        self.restart_button.set_visible(bool(self.pending))

    def check(self):
        self.check_button.set_sensitive(False)
        self.status.set_label("Looking for updates…")

        def got(value):
            self.check_button.set_sensitive(True)
            self.available = value
            self.show()
            if not value.get("available"):
                self.status.set_label("Your system is up to date.")

        def failed(error):
            self.check_button.set_sensitive(True)
            no_source = error.name == "Unavailable" and "no update source" in error.message
            self.status.set_label("No update source is set up yet." if no_source else model.error_text(error.name, error.message))
        self.ask("Updates", "Check", None, None, got, failed, timeout=120_000)

    def install(self):
        method = "InstallLatestUnsigned" if self.unsigned_row.get_active() else "InstallLatest"
        self.win.run_job(self.win.job_call("Updates", method, None, None),
                         lambda r: self.win.toast(f"Version {r.get('version', '')} is ready. Restart to use it."),
                         "Installing the update…")

    def choose_file(self):
        def chosen(path):
            kind, problem = model.classify_source(path)
            if kind != "file":
                self.win.toast(problem or "That file cannot be used.")
                return
            choices = [("ok", "Install", "suggested")]
            if self.developer_group.get_visible():          # developer mode: an image you built yourself may have no signature
                choices.append(("unsigned", "Install Without Signature", "destructive"))
            self.win.confirm_choice(
                "Install this update?", f"{os.path.basename(path)}\n\nThe signature is checked against the trusted keys.",
                choices, lambda response: self.install_file(path, response == "unsigned"))
        self.win.choose_file("Choose an update file", ("*.tar.zst", "*.tar", "*.zst"), "Update archives", chosen)

    def install_file(self, path, unsigned):
        opened = self.win.open_fd(path)
        if opened is None:
            return
        fds, index = opened
        self.win.run_job(self.win.job_call("Updates", "Install", "(hsbb)", (index, "", unsigned, False), fds=fds),
                         lambda r: self.win.toast(f"Version {r.get('version', '')} is ready. Restart to use it."),
                         "Installing the update…")


# -- Bundles and extensions ----------------------------------------------------------------------

class Bundles(Page):
    def __init__(self, win):
        super().__init__(win)
        self.bundles_group = self.group("Installed bundles", "Extra software added to the system without changing it.",
                                        text_button("Update All", lambda: win.run_job(
                                            win.job_call("Compat", "UpdateBundles", "(s)", ("",)),
                                            lambda _r: win.toast("Bundles are up to date."), "Updating bundles…")))
        self.catalog_group = self.group("Available")
        add = text_button("Add…", self.choose_rpm)
        self.extensions_group = self.group("Extensions", "Single packages added as extensions.", add)

    def load(self):
        self.ask("Compat", "ListBundles", None, None, self.show_bundles)
        self.ask("Compat", "List", None, None, self.show_extensions)
        self.ask("Compat", "ListCatalog", None, None, self.show_catalog)

    def show_bundles(self, bundles):
        clear(self.bundles_group)
        if not bundles:
            self.empty(self.bundles_group, "None installed")
        for bundle in bundles:
            row = Adw.ExpanderRow(title=model.bundle_title(bundle), subtitle=model.bundle_subtitle(bundle))
            row.add_suffix(flat_button("user-trash-symbolic", "Remove", lambda n=bundle["name"]: self.win.confirm(
                f"Remove {n}?", "Its software is taken away. Your files are not touched.", "Remove",
                lambda: self.win.run_job(self.win.job_call("Compat", "RemovePackages", "(sas)", (n, [])), None, "Removing…"), True)))
            requested = set(bundle.get("requested", []))
            for name in model.package_names(bundle.get("packages", [])):
                package = Adw.ActionRow(title=name)
                if name in requested or name.rsplit("-", 2)[0] in requested:
                    package.set_subtitle("asked for")
                row.add_row(package)
            add_row(self.bundles_group, row)

    def show_extensions(self, extensions):
        clear(self.extensions_group)
        if not extensions:
            self.empty(self.extensions_group, "None installed")
        for extension in extensions:
            row = Adw.ActionRow(title=extension.get("name", ""), subtitle=model.extension_subtitle(extension))
            row.add_suffix(flat_button("user-trash-symbolic", "Remove", lambda n=extension["name"]: self.win.confirm(
                f"Remove {n}?", "The extension is removed.", "Remove",
                lambda: self.win.run_job(self.win.job_call("Compat", "Remove", "(s)", (n,)), None, "Removing…"), True)))
            add_row(self.extensions_group, row)

    def show_catalog(self, entries):
        clear(self.catalog_group)
        shown = [e for e in entries if not e.get("installed")]
        self.catalog_group.set_visible(bool(shown))
        for entry in shown:
            row = Adw.ExpanderRow(title=entry.get("title") or entry.get("name", ""), subtitle=model.shorten(entry.get("description", "")))
            row.add_suffix(text_button("Install", lambda e=entry: self.install_entry(e)))
            if entry.get("description"):
                about = Gtk.Label(label=entry["description"], wrap=True, xalign=0, margin_top=8, margin_bottom=8,
                                  margin_start=12, margin_end=12, css_classes=["dim-label"])
                holder = Adw.ActionRow(activatable=False)
                holder.set_child(about)
                row.add_row(holder)
            for name in model.package_names(entry.get("packages", [])):
                row.add_row(Adw.ActionRow(title=name))
            add_row(self.catalog_group, row)

    def install_entry(self, entry):
        enable = bool(entry.get("needs_enabling"))
        title = entry.get("title") or entry["name"]

        def go():
            self.win.run_job(self.win.job_call("Compat", "InstallCatalog", "(ssb)", (entry["name"], "", enable)),
                             lambda _r: self.win.toast(f"{title} is installed."), "Installing…")
        notice = model.catalog_notice(entry)
        if notice:
            self.win.confirm(f"Install {title}?", notice, "Install", go)
        else:
            go()

    def choose_rpm(self):
        def chosen(path):
            problem = model.classify_rpm(path)
            if problem:
                self.win.toast(problem)
                return
            opened = self.win.open_fd(path)
            if opened is None:
                return
            fds, index = opened

            def report(values):
                ok, text = model.inspect_summary(values)
                if not ok:
                    self.win.alert("This package does not fit", text)
                    return
                self.win.confirm(f"Add {os.path.basename(path)}?", text, "Add", lambda: self.add_rpm(path))
            self.ask("Compat", "Inspect", "(h)", (index,), report, fds=fds)
        self.win.choose_file("Choose a package", ("*.rpm",), "RPM packages", chosen)

    def add_rpm(self, path):
        opened = self.win.open_fd(path)
        if opened is None:
            return
        fds, index = opened
        self.win.run_job(self.win.job_call("Compat", "Install", "(hb)", (index, False), fds=fds),
                         lambda _r: self.win.toast("The extension is installed."), "Installing…")


# -- Repositories --------------------------------------------------------------------------------

class Repositories(Page):
    def __init__(self, win):
        super().__init__(win)
        buttons = Gtk.Box(spacing=6)
        buttons.append(text_button("Refresh", lambda: win.run_job(win.job_call("Repos", "Refresh", "(asb)", ([], False)),
                                                                  lambda _r: win.toast("Repositories are up to date."), "Refreshing…")))
        buttons.append(text_button("Add…", self.add_dialog))
        self.group_ = self.group("Package sources", "Where bundles get their software. Every source must be signed.", buttons)
        self.find = Gtk.SearchEntry(placeholder_text="Find a package in these sources")
        self.find.connect("activate", lambda e: self.search(e.get_text().strip()))
        self.add(self.find)
        self.results = self.group("Results")
        self.results.set_visible(False)

    def load(self):
        self.ask("Repos", "List", None, None, self.show)

    def show(self, repos):
        clear(self.group_)
        if not repos:
            self.empty(self.group_, "No repositories")
        for repo in repos:
            row = Adw.SwitchRow(title=model.repo_title(repo), subtitle=model.repo_subtitle(repo))
            row._loading = True
            row.set_active(bool(repo.get("enabled")))
            row._loading = False
            row.connect("notify::active", lambda r, _p, a=repo["alias"]: self.toggle(r, a))
            if repo.get("shipped"):
                row.add_suffix(spacer())
            else:
                row.add_suffix(flat_button("user-trash-symbolic", "Remove", lambda a=repo["alias"], t=model.repo_title(repo): self.win.confirm(
                    f"Remove {t}?", "Bundles that use it must be removed first.", "Remove",
                    lambda: self.win.run_job(self.win.job_call("Repos", "Remove", "(s)", (a,)), None, "Removing…"), True)))
            add_row(self.group_, row)

    def search(self, term):
        if len(term) < 2:
            return
        self.results.set_visible(True)
        self.results.set_description("Searching\u2026")
        clear(self.results)

        def got(packages):
            clear(self.results)
            shown = packages[:40]
            self.results.set_description(f"{len(packages)} found" + (" (showing the first 40)" if len(packages) > 40 else ""))
            for package in shown:
                row = Adw.ActionRow(title=package.get("name", ""), subtitle=" \u00b7 ".join(
                    p for p in (package.get("evr", ""), package.get("repo", ""), package.get("summary", "")) if p))
                row.add_suffix(text_button("Add to Bundle\u2026", lambda n=package.get("name", ""): self.add_to_bundle(n)))
                add_row(self.results, row)
        self.ask("Repos", "Search", "(s)", (term,), got, timeout=120_000)

    def add_to_bundle(self, package):
        entry = Gtk.Entry(text="my-software", placeholder_text="Bundle name")
        dialog = Adw.AlertDialog(heading=f"Add {package} to a bundle",
                                 body="A bundle is one group of extra software. A new name makes a new bundle; an existing name adds to it.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Add")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_d, r):
            name = model.alias_from(entry.get_text())
            if r == "ok" and name:
                self.win.run_job(self.win.job_call("Compat", "InstallPackages", "(sasas)", (name, [package], [])),
                                 lambda _r: self.win.toast(f"{package} was added to {name}."), f"Adding {package}")
        dialog.connect("response", response)
        dialog.present(self.win)

    def toggle(self, row, alias):
        if getattr(row, "_loading", False):
            return
        self.win.run_job(self.win.job_call("Repos", "SetEnabled", "(sb)", (alias, row.get_active())), None, "Changing…")

    def add_dialog(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        name = Gtk.Entry(placeholder_text="Name")
        url = Gtk.Entry(placeholder_text="https://example.org/repo")
        key = Gtk.TextView(wrap_mode=Gtk.WrapMode.CHAR, height_request=110, monospace=True)
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=key))
        for widget in (name, url, Gtk.Label(label="Public signing key (the text of the .asc file)", xalign=0, css_classes=["dim-label"]), frame):
            box.append(widget)
        dialog = Adw.AlertDialog(heading="Add a repository", body="Only add sources you trust: their software is installed as bundles.")
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_d, r):
            if r != "ok":
                return
            buffer = key.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            problem = model.repo_form_problem(name.get_text(), url.get_text().strip(), text)
            if problem:
                self.win.toast(problem)
                return
            self.inspect_key(name.get_text().strip(), url.get_text().strip(), text)
        dialog.connect("response", response)
        dialog.present(self.win)

    def inspect_key(self, name, url, key):
        def got(keys):
            if not keys:
                self.win.toast("That is not a usable public key.")
                return
            lines = [f"{k.get('owner', '')}\n{k.get('fingerprint', '')}" for k in keys]
            self.win.confirm(f"Trust this key for {name}?", "\n\n".join(lines) + "\n\nSoftware signed with it will be installed.", "Trust and Add",
                             lambda: self.win.run_job(self.win.job_call(
                                 "Repos", "Add", "(sssis)", (model.alias_from(name), name, url, 99, key)), None, "Adding…"))
        self.ask("Repos", "InspectKey", "(s)", (key,), got)


# -- Application sources (Flatpak remotes) -------------------------------------------------------

class AppSources(Page):
    def __init__(self, win):
        super().__init__(win)
        self.group_ = self.group("Application sources", "Where Flatpak applications come from. Every source must be signed. They also "
                                 "show up in app stores such as Bazaar.", text_button("Add\u2026", self.add_dialog))

    def load(self):
        self.ask("Apps", "ListRemotes", None, None, self.show)

    def show(self, remotes):
        clear(self.group_)
        if not remotes:
            self.empty(self.group_, "No sources. Flathub is added when the computer is first online.")
        for remote in remotes:
            row = Adw.SwitchRow(title=remote.get("title") or remote["name"], subtitle=model.remote_subtitle(remote))
            row._loading = True
            row.set_active(bool(remote.get("enabled")))
            row._loading = False
            row.connect("notify::active", lambda r, _p, n=remote["name"]: self.toggle(r, n))
            if remote.get("default"):
                row.add_suffix(spacer())
            else:
                row.add_suffix(flat_button("user-trash-symbolic", "Remove", lambda n=remote["name"], t=remote.get("title") or remote["name"]: self.win.confirm(
                    f"Remove {t}?", "Applications installed from it must be removed first.", "Remove",
                    lambda: self.win.run_job(self.win.job_call("Apps", "RemoveRemote", "(s)", (n,)), None, "Removing\u2026"), True)))
            add_row(self.group_, row)

    def toggle(self, row, name):
        if getattr(row, "_loading", False):
            return
        self.win.run_job(self.win.job_call("Apps", "SetRemoteEnabled", "(sb)", (name, row.get_active())), None, "Changing\u2026")

    def add_dialog(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        location = Gtk.Entry(placeholder_text="https://example.org/apps.flatpakrepo")
        name = Gtk.Entry(placeholder_text="Short name, for example acme")
        title = Gtk.Entry(placeholder_text="Title (optional)")
        key = Gtk.TextView(wrap_mode=Gtk.WrapMode.CHAR, height_request=90, monospace=True)
        location.connect("changed", lambda e: not name.get_text() and name.set_text(model.suggest_remote_name(e.get_text())))
        for widget in (location, name, title, Gtk.Label(label="Public signing key (only if the address is not a .flatpakrepo file)", xalign=0,
                                                         css_classes=["dim-label"]), Gtk.Frame(child=Gtk.ScrolledWindow(child=key))):
            box.append(widget)
        dialog = Adw.AlertDialog(heading="Add an application source", body="Only add sources you trust: their applications run on this computer.")
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_d, r):
            if r != "ok":
                return
            buffer = key.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            problem = model.remote_form_problem(name.get_text(), location.get_text().strip(), text)
            if problem:
                self.win.toast(problem)
                return
            self.inspect(name.get_text().strip(), location.get_text().strip(), text, title.get_text().strip())
        dialog.connect("response", response)
        dialog.present(self.win)

    def inspect(self, name, location, key, title):
        def got(info):
            self.win.confirm(f"Trust {title or info.get('title') or name}?", model.remote_trust_text(info), "Trust and Add",
                             lambda: self.win.run_job(self.win.job_call("Apps", "AddRemote", "(ssss)", (name, location, key, title)), None, "Adding\u2026"))
        self.ask("Apps", "InspectRemote", "(ss)", (location, key), got, timeout=60_000)


# -- Base packages -------------------------------------------------------------------------------

class Packages(Page):
    def __init__(self, win):
        super().__init__(win)
        self.store = Gtk.StringList()
        self.count = Gtk.Label(label="", css_classes=["dim-label"], xalign=0)
        self.search = Gtk.SearchEntry(placeholder_text="Search the system's packages")
        flt = Gtk.StringFilter(expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"),
                               ignore_case=True, match_mode=Gtk.StringFilterMatchMode.SUBSTRING)
        self.search.connect("search-changed", lambda e: (flt.set_search(e.get_text()), self.update_count()))
        self.filtered = Gtk.FilterListModel(model=self.store, filter=flt)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0, margin_top=5, margin_bottom=5, margin_start=12,
                                                                         margin_end=12, ellipsize=3)))
        factory.connect("bind", lambda _f, item: item.get_child().set_label(item.get_item().get_string().replace("\t", "   ·   ")))
        self.view = Gtk.ListView(model=Gtk.NoSelection(model=self.filtered), factory=factory, css_classes=["card"], height_request=420)
        holder = Gtk.ScrolledWindow(child=self.view, min_content_height=420, vexpand=True, css_classes=["card"])
        self.add(Gtk.Label(label="What the read-only system is made of. Changes come with system updates, not from here.",
                           xalign=0, wrap=True, css_classes=["dim-label"]))
        self.add(self.search)
        self.add(self.count)
        self.add(holder)

    def update_count(self):
        total, shown = self.store.get_n_items(), self.filtered.get_n_items()
        self.count.set_label(f"{total} packages" if shown == total else f"{shown} of {total} packages")

    def load(self):
        def got(packages):
            self.store.splice(0, self.store.get_n_items(), [f"{n}\t{v}" for n, v in
                                                          sorted((p.get("name", ""), p.get("version", "")) for p in packages)])
            self.update_count()
        self.ask("Manager", "ListBasePackages", None, None, got, timeout=120_000)


# -- Boot slots ----------------------------------------------------------------------------------

class Slots(Page):
    def __init__(self, win):
        super().__init__(win)
        self.slots_group = self.group("Boot slots", "Two copies of the system. An update goes to the one that is not running.")
        restart = self.group()
        row = Adw.ActionRow(title="Restart", subtitle="Open applications will be closed.")
        row.add_suffix(text_button("Restart…", win.ask_restart))
        restart.add(row)

    def load(self):
        self.ask("Boot", "GetSlots", None, None, self.show)

    def show(self, slots):
        clear(self.slots_group)
        for slot in sorted(slots, key=lambda s: s["name"]):
            row = Adw.ActionRow(title=model.slot_title(slot), subtitle=model.slot_subtitle(slot))
            for action in model.slot_actions(slot):
                row.add_suffix(text_button(model.slot_action_text(action), lambda s=slot, a=action: self.choose(s, a)))
            if model.slot_can_release(slot):
                row.add_suffix(flat_button("user-trash-symbolic", "Empty this slot", lambda s=slot: self.release(s)))
            add_row(self.slots_group, row)

    def choose(self, slot, action):
        heading, body = model.slot_confirm(slot, action)
        method = "SetNextBoot" if action == "once" else "SetDefaultSlot"
        self.win.confirm(heading, body, model.slot_action_text(action),
                         lambda: self.win.run_job(self.win.job_call("Boot", method, "(s)", (slot["name"],), timeout=60_000),
                                                  lambda _r: self.win.toast("Done. Restart to use it."), "Changing the boot choice…"))

    def release(self, slot):
        self.win.confirm(f"Empty {model.slot_title(slot)}?", "Its system is removed. The next update has more room to choose from, "
                         "but this slot cannot be started until then.", "Empty Slot",
                         lambda: self.win.run_job(self.win.job_call("Boot", "ReleaseSlot", "(s)", (slot["name"],), timeout=120_000),
                                                  None, "Emptying the slot…"), True)


# -- Appearance ----------------------------------------------------------------------------------

class Appearance(Page):
    def __init__(self, win):
        super().__init__(win)
        group = self.group("Boot splash", "The picture shown while the computer starts. It changes at the next restart. "
                           "Colours, wallpaper and fonts of the desktop are in GNOME Settings.")
        row = Adw.ActionRow(title="Style")
        box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self.light = Gtk.ToggleButton(label="Light")
        self.dark = Gtk.ToggleButton(label="Dark", group=self.light)
        self.loading = False
        for button, theme in ((self.light, "light"), (self.dark, "dark")):
            button.connect("toggled", lambda b, t=theme: b.get_active() and self.choose(t))
            box.append(button)
        row.add_suffix(box)
        group.add(row)
        self.note = Gtk.Label(label="", xalign=0, wrap=True, css_classes=["dim-label"], visible=False)
        self.add(self.note)

    def load(self):
        def got(info):
            self.loading = True
            (self.dark if info.get("theme") == "dark" else self.light).set_active(True)
            self.loading = False
            text = model.splash_text(info)
            self.note.set_label(text)
            self.note.set_visible(bool(text))
        self.ask("Boot", "GetSplash", None, None, got)

    def choose(self, theme):
        if self.loading:
            return
        self.win.run_job(self.win.job_call("Boot", "SetSplash", "(s)", (theme,), timeout=60_000),
                         lambda _r: self.win.toast(f"The {theme} boot splash starts at the next restart."), "Changing the boot splash")


# -- Remote access -------------------------------------------------------------------------------

class Remote(Page):
    def __init__(self, win):
        super().__init__(win)
        self.status, self.keys, self.want_password = {}, [], False
        self.ssh_group = self.group("SSH", "Sign in from another computer over the network.")
        self.ssh_row = Adw.SwitchRow(title="Remote login (SSH)")
        self.password_row = Adw.SwitchRow(title="Allow password sign-in", subtitle="Keys are safer than passwords.")
        for row in (self.ssh_row, self.password_row):
            row._loading = False
            self.ssh_group.add(row)
        self.ssh_row.connect("notify::active", lambda r, _p: None if r._loading else self.request_ssh(r.get_active()))
        self.password_row.connect("notify::active", lambda r, _p: None if r._loading else self.choose_password(r.get_active()))
        self.keys_group = self.group("Authorized keys", "Computers that may sign in without a password.", text_button("Add\u2026", self.add_dialog))

    def load(self):
        self.ask("Remote", "GetStatus", None, None, self.show_status)
        self.ask("Remote", "ListKeys", "(s)", ("",), self.show_keys)

    def is_on(self):
        return bool(self.status.get("enabled") or self.status.get("active"))

    def show_status(self, status):
        self.status = status
        title, detail = model.remote_summary(status)
        self.ssh_row.set_subtitle(f"{title}" + (f" \u00b7 {detail}" if detail else ""))
        installed = status.get("installed", False)
        for row in (self.ssh_row, self.password_row):
            row.set_sensitive(installed)
        if self.is_on():
            self.want_password = bool(status.get("password_auth", False))      # the server's setting is the truth while it runs
        self.set_switches()

    def set_switches(self):
        self.ssh_row._loading = self.password_row._loading = True
        self.ssh_row.set_active(self.is_on())
        self.password_row.set_active(self.want_password)
        self.ssh_row._loading = self.password_row._loading = False

    def choose_password(self, allowed):
        """The password switch. While SSH is off it only records the choice for when SSH is turned on; while it is on, it applies it."""
        self.want_password = allowed
        if self.is_on():
            self.apply(True)

    def request_ssh(self, enable):
        if enable and model.ssh_needs_a_way_in(bool(self.keys), self.want_password):
            self.set_switches()                                  # nothing has changed yet: put the switch back until the person chooses
            self.win.confirm_choice(
                "Remote login needs a way to sign in",
                "No user has an SSH key, and password sign-in is off, so nobody could sign in. Allow password sign-in, or add a key first.",
                [("password", "Allow Password Sign-in", "suggested"), ("key", "Add a Key\u2026", "suggested")],
                lambda response: self.password_chosen() if response == "password" else self.add_dialog())
            return
        self.apply(enable)

    def password_chosen(self):
        self.want_password = True
        self.apply(True)

    def apply(self, enable):
        self.win.run_job(self.win.job_call("Remote", "SetSsh", "(bb)", (bool(enable), bool(self.want_password))),
                         None, "Changing remote access\u2026")

    def show_keys(self, keys):
        self.keys = list(keys or [])
        clear(self.keys_group)
        if not keys:
            self.empty(self.keys_group, "No keys")
        for key in keys:
            row = Adw.ActionRow(title=model.key_title(key), subtitle=model.key_subtitle(key))
            row.add_suffix(flat_button("user-trash-symbolic", "Remove", lambda k=key: self.win.confirm(
                "Remove this key?", f"{model.key_title(k)} can no longer sign in with it.", "Remove",
                lambda: self.win.run_job(self.win.job_call("Remote", "RemoveKey", "(ss)", (k["user"], k["fingerprint"])), None, "Removing…"), True)))
            add_row(self.keys_group, row)

    def add_dialog(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        user = Gtk.Entry(text=GLib.get_user_name(), placeholder_text="User")
        key = Gtk.TextView(wrap_mode=Gtk.WrapMode.CHAR, height_request=90, monospace=True)
        box.append(user)
        box.append(Gtk.Frame(child=Gtk.ScrolledWindow(child=key)))
        dialog = Adw.AlertDialog(heading="Add an SSH key", body="Paste the public key (one line, from a .pub file).")
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Add")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_d, r):
            if r != "ok":
                return
            buffer = key.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
            problem = model.key_form_problem(user.get_text(), text)
            if problem:
                self.win.toast(problem)
                return
            self.win.run_job(self.win.job_call("Remote", "AddKey", "(ss)", (user.get_text().strip(), text)), None, "Adding the key…")
        dialog.connect("response", response)
        dialog.present(self.win)


# -- Advanced ------------------------------------------------------------------------------------

class Advanced(Page):
    """Every registered setting, grouped by its prefix. Each is one row of the type the schema gives it."""

    def __init__(self, win):
        super().__init__(win)
        self.groups = {}

    def load(self):
        def listed(values):
            for key in sorted(values):
                self.ask("Config", "Describe", "(s)", (key,), lambda info, k=key, v=values[key]: self.build(k, info, v))
        self.ask("Config", "List", None, None, listed)

    def group_for(self, key):
        title = model.config_group(key)
        if title not in self.groups:
            self.groups[title] = self.group(title)
        return self.groups[title]

    def build(self, key, info, value):
        group = self.group_for(key)
        old = getattr(self, "_rows", {}).get(key)
        if old is not None:
            group.remove(old)
        kind = info.get("type", "s")
        writable = info.get("writable", True)
        description = info.get("description", "")
        title = model.config_title(key)
        if kind == "b":
            row = Adw.SwitchRow(title=title, subtitle=description)
            row.set_active(bool(value))
            row.connect("notify::active", lambda r, _p: self.set(key, GLib.Variant.new_boolean(r.get_active())))
        elif info.get("choices"):
            choices = list(info["choices"])
            row = Adw.ComboRow(title=title, subtitle=description, model=Gtk.StringList.new(choices))
            row.set_selected(choices.index(value) if value in choices else 0)
            row.connect("notify::selected", lambda r, _p: self.set(key, GLib.Variant.new_string(choices[r.get_selected()])))
        else:
            row = Adw.EntryRow(title=title, text=str(value))
            row.set_show_apply_button(True)
            row.connect("apply", lambda r: self.set(key, GLib.Variant.new_string(r.get_text()) if kind == "s"
                                                     else GLib.Variant.new_int32(int(r.get_text() or 0))))
        row.set_sensitive(writable)
        self._rows = getattr(self, "_rows", {})
        self._rows[key] = row
        group.add(row)

    def set(self, key, variant):
        self.ask("Config", "Set", "(sv)", (key, variant), None, lambda e: (self.win.toast(model.error_text(e.name, e.message)), self.load_one(key)))

    def load_one(self, key):
        self.ask("Config", "Get", "(s)", (key,), lambda v: self.ask(
            "Config", "Describe", "(s)", (key,), lambda info: self.build(key, info, v)))


# -- Activity ------------------------------------------------------------------------------------

class Activity(Page):
    def __init__(self, win):
        super().__init__(win)
        self.jobs_group = self.group("Recent changes", "What the system did on request, newest first. foundationd keeps the last fifty.")

    def load(self):
        self.ask("Manager", "ListJobs", None, None, self.show)

    def show(self, paths):
        clear(self.jobs_group)
        if not paths:
            self.empty(self.jobs_group, "Nothing yet since the system started")
        for path in reversed(paths):
            self.win.backend.properties(path, lambda props, error, p=path: props and self.add_job(p, props))

    def add_job(self, path, props):
        job = {k: props[k] for k in props}
        row = Adw.ExpanderRow(title=model.job_title(job.get("Kind", "")), subtitle=model.job_subtitle(job))
        log = Gtk.Label(label="", xalign=0, wrap=True, selectable=True, css_classes=["monospace", "dim-label"],
                        margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        holder = Adw.ActionRow(activatable=False)
        holder.set_child(log)
        row.add_row(holder)

        def expanded(_r, _p):
            if row.get_expanded() and not log.get_label():
                self.ask("org.ersa.Foundation1.Job", "Logs", "(i)", (60,), lambda lines: log.set_label("\n".join(lines) or "No log."),
                             path=path)
        row.connect("notify::expanded", expanded)
        add_row(self.jobs_group, row)


PAGE_CLASSES = {"overview": Overview, "appsources": AppSources, "updates": Updates, "bundles": Bundles, "repositories": Repositories, "packages": Packages,
                "slots": Slots, "appearance": Appearance, "remote": Remote, "advanced": Advanced, "activity": Activity}
