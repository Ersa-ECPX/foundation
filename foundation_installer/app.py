"""The installer window, laid out like the macOS installer: a centred page, Go Back and Continue below.

Pages: welcome, choose a disk, (confirm dialog), install, done. There is no sidebar of steps.

Pages are plain Gtk.Stack children so the flow is easy to follow in one file. Nothing here decides
whether a disk is allowed; the service says so (`usable`, `reason`, `possible`, `problems`).
"""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import __version__, model  # noqa: E402
from .backend import Backend, ClientError  # noqa: E402

APP_ID = "org.ersa.Installer"
HERE = os.path.dirname(os.path.abspath(__file__))


CSS = b"""
.install-progress trough, .install-progress progress { min-height: 6px; border-radius: 3px; }
.install-progress trough { min-height: 6px; }
.install-progress progress { min-height: 6px; }
.disk-card { padding: 18px 12px; border-radius: 12px; }
.disk-card.unusable { opacity: 0.5; }
flowboxchild { border-radius: 14px; }
flowboxchild:selected { background-color: alpha(@accent_bg_color, 0.25); outline: 2px solid @accent_bg_color; }
"""


def _page(title, description, icon, *widgets):
    """A centred status page with optional extra widgets under the description."""
    status = Adw.StatusPage(title=title, description=description, icon_name=icon, vexpand=True)
    if widgets:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        for widget in widgets:
            box.append(widget)
        status.set_child(box)
    return status


def _button(label, handler, style=None):
    button = Gtk.Button(label=label, halign=Gtk.Align.CENTER)
    if style:
        button.add_css_class(style)
    button.add_css_class("pill")
    button.connect("clicked", lambda _b: handler())
    return button


class InstallerWindow(Adw.ApplicationWindow):
    """One centred page at a time with Go Back and Continue at the bottom right."""

    FOOTER_PAGES = ("welcome", "language", "timezone", "disks", "review", "done")

    def __init__(self, app, backend):
        super().__init__(application=app, title=f"Install {model.PRODUCT}", default_width=900, default_height=640)
        self.backend = backend
        self.disks, self.selected, self.plan, self.installing = [], None, None, False
        self.version, self.started_at = "", 0.0
        self.language, self.timezone = model.DEFAULT_LANGUAGE, model.current_timezone()
        self._back_target, self._next_handler = None, None
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True, hexpand=True)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        header.add_css_class("flat")
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.footer = Gtk.ActionBar()
        self.back_btn = Gtk.Button(label="Go Back")
        self.back_btn.connect("clicked", lambda _b: self._back())
        self.next_btn = Gtk.Button(label="Continue")
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.connect("clicked", lambda _b: self._next())
        self.refresh_btn = Gtk.Button(label="Refresh Disks", visible=False)
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.connect("clicked", lambda _b: self._load_disks())
        self.footer.pack_start(self.back_btn)
        self.customize_btn = Gtk.Button(label="Customize\u2026", visible=False, sensitive=False)
        self.customize_btn.add_css_class("flat")
        self.customize_btn.connect("clicked", lambda _b: self._go_review(review=True))
        self.footer.pack_start(self.refresh_btn)
        self.footer.pack_start(self.customize_btn)
        self.footer.pack_end(self.next_btn)
        view.add_bottom_bar(self.footer)
        self.set_content(view)
        self.connect("close-request", lambda _w: self.installing)  # never close in the middle of a write
        self._build_welcome()
        self._build_language()
        self._build_timezone()
        self._build_disks()
        self._build_review()
        self._build_progress()
        self.show("loading")
        self._probe()

    # -- navigation ----------------------------------------------------------------------------

    def show(self, name):
        self.stack.set_visible_child_name(name)
        self.footer.set_visible(name in self.FOOTER_PAGES)
        self.refresh_btn.set_visible(name == "disks")
        self.customize_btn.set_visible(name == "disks")

    def _nav(self, back=None, label="Continue", handler=None, sensitive=True, style="suggested-action"):
        """Configure the footer: where Go Back leads (None hides it) and what Continue does."""
        self._back_target, self._next_handler = back, handler
        self.back_btn.set_visible(back is not None)
        self.next_btn.set_label(label)
        self.next_btn.set_sensitive(sensitive)
        for css in ("suggested-action", "destructive-action"):
            self.next_btn.remove_css_class(css)
        self.next_btn.add_css_class(style)

    def _back(self):
        if self._back_target:
            self._back_target()

    def _next(self):
        if self._next_handler:
            self._next_handler()

    # -- pages ---------------------------------------------------------------------------------

    def _build_welcome(self):
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.stack.add_named(_page(model.PRODUCT, "Starting…", "system-software-install-symbolic", spinner), "loading")
        self.welcome_text = Adw.StatusPage(vexpand=True)
        self.stack.add_named(self.welcome_text, "welcome")
        self._set_logo()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._set_logo())

    def _set_logo(self):
        name = "ersa-mark-dark.svg" if Adw.StyleManager.get_default().get_dark() else "ersa-mark-light.svg"
        self.welcome_text.set_paintable(Gdk.Texture.new_from_filename(os.path.join(HERE, name)))

    def _heading(self, title_text, hint):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=24, margin_bottom=12,
                        margin_start=32, margin_end=32)
        title = Gtk.Label(label=title_text, xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        hint_label = Gtk.Label(label=hint, xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        hint_label.add_css_class("dim-label")
        outer.append(title)
        outer.append(hint_label)
        return outer

    def _chooser(self, title, hint, items, current, on_pick):
        """A page with a search box and a list; items are (name, detail, value)."""
        outer = self._heading(title, hint)
        search = Gtk.SearchEntry(placeholder_text="Search", halign=Gtk.Align.CENTER, width_request=360)
        outer.append(search)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, margin_top=6, margin_bottom=6,
                              margin_start=6, margin_end=6, width_request=480, halign=Gtk.Align.CENTER)
        listbox.add_css_class("boxed-list")
        rows = []
        for name, detail, value in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=GLib.markup_escape_text(detail))
            row.value, row.search = value, f"{name} {detail}".lower()
            listbox.append(row)
            rows.append(row)
            if value == current:
                listbox.select_row(row)
        listbox.set_filter_func(lambda row: search.get_text().lower() in row.search)
        search.connect("search-changed", lambda _e: listbox.invalidate_filter())
        listbox.connect("row-selected", lambda _l, row: on_pick(row.value) if row is not None else None)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(listbox)
        outer.append(scroll)
        return outer, listbox, rows

    def _build_language(self):
        items = [(name, code.split(".")[0], code) for name, code in model.LANGUAGES]
        outer, self.language_list, self.language_rows = self._chooser(
            "Select your language", "This sets the language of the installed system.", items, self.language,
            lambda value: setattr(self, "language", value))
        self.stack.add_named(outer, "language")

    def _build_timezone(self):
        items = []
        for zone in model.timezones():
            region, _slash, city = zone.partition("/")
            items.append((city.replace("_", " ") if city else zone, region if city else "", zone))
        outer, self.timezone_list, self.timezone_rows = self._chooser(
            "Select your time zone", "This sets the clock of the installed system.", items, self.timezone,
            lambda value: setattr(self, "timezone", value))
        self.stack.add_named(outer, "timezone")

    def _reveal_selected(self, listbox):
        row = listbox.get_selected_row()
        if row is not None:
            GLib.idle_add(lambda: row.grab_focus() and False)

    def _build_disks(self):
        outer = self._heading("Select the disk where you want to install",
                              f"{model.PRODUCT} is installed on the whole disk. Nothing is changed until you confirm.")
        self.flow = Gtk.FlowBox(margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,selection_mode=Gtk.SelectionMode.SINGLE, max_children_per_line=4,
                                min_children_per_line=2, homogeneous=True, halign=Gtk.Align.CENTER, row_spacing=12, column_spacing=12,
                                valign=Gtk.Align.START)
        self.flow.connect("selected-children-changed", lambda _f: self._picked())
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.flow)
        outer.append(scroll)
        self.disks_empty = Gtk.Label(label="No disks were found.", visible=False)
        outer.append(self.disks_empty)
        self.stack.add_named(outer, "disks")

    def _build_review(self):
        outer = self._heading("Ready to install", "Review what will happen.")
        self.review_banner = Adw.Banner(revealed=True)
        outer.append(self.review_banner)
        self.problem_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.problem_label.add_css_class("error")
        outer.append(self.problem_label)
        self.partition_group = Adw.PreferencesGroup(title="What will be installed")
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.partition_group)
        outer.append(scroll)
        self._partition_rows = []
        self.stack.add_named(outer, "review")

    def _build_progress(self):
        self.progress_bar = Gtk.ProgressBar(width_request=420)
        self.progress_bar.add_css_class("install-progress")
        self.progress_step = Gtk.Label(label="Preparing…")
        self.progress_left = Gtk.Label(label="")
        self.progress_left.add_css_class("dim-label")
        self.stack.add_named(_page(f"Installing {model.PRODUCT}", "Do not turn off the computer or remove the disk.",
                                   "drive-harddisk-symbolic", self.progress_bar, self.progress_step,
                                   self.progress_left), "progress")

    # -- flow ----------------------------------------------------------------------------------

    def _probe(self):
        def got(values, error):
            if error is not None:
                self._fatal(model.failure_text(error))
                return
            info = values[0]
            os_info = info.get("os", {}) if isinstance(info, dict) else {}
            self.version = os_info.get("image_version", "") if isinstance(os_info, dict) else ""
            self.backend.call("Manager", "GetStatus", None, None, self._status)
        self.backend.call("Manager", "GetSystemInfo", None, None, got)

    def _status(self, values, error):
        if error is not None:
            self._fatal(model.failure_text(error))
            return
        if "deploy" not in values[0].get("capabilities", []):
            self._fatal("This system's management service cannot install to a disk.")
            return
        version = f" {self.version}" if self.version else ""
        self.welcome_text.set_title(f"Install {model.PRODUCT}{version}")
        self.welcome_text.set_description(
            "This installs the system on a disk in this computer. You choose the disk next, and you see "
            "exactly what will be erased before anything is changed.")
        self._nav(handler=self._go_language)
        self.show("welcome")

    def _fatal(self, message):
        self._replace("fatal", _page("The installer cannot start", message, "dialog-error-symbolic",
                                     _button("Try Again", self._retry), _button("Quit", self.close)))
        self.show("fatal")

    def _retry(self):
        self.show("loading")
        self._probe()

    def _go_language(self):
        self._nav(back=self._go_welcome, handler=self._go_timezone)
        self.show("language")
        self._reveal_selected(self.language_list)

    def _go_timezone(self):
        self._nav(back=self._go_language, handler=self._go_disks)
        self.show("timezone")
        self._reveal_selected(self.timezone_list)

    def _go_disks(self):
        self._nav(back=self._go_timezone, label="Install", handler=self._go_review, sensitive=False)
        self.show("disks")
        self._load_disks()

    def _go_welcome(self):
        self._nav(handler=self._go_language)
        self.show("welcome")

    def _load_disks(self):
        def got(values, error):
            if error is not None:
                self._fatal(model.failure_text(error))
                return
            self._fill_disks(values[0])
        self.backend.call("Deploy", "ListDisks", None, None, got)

    def _disk_card(self, disk):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.CENTER)
        box.add_css_class("disk-card")
        if not disk["usable"]:
            box.add_css_class("unusable")
        icon = Gtk.Image(icon_name="media-removable-symbolic" if disk.get("removable") else "drive-harddisk-symbolic",
                         pixel_size=96)
        name = Gtk.Label(label=model.disk_title(disk), wrap=True, justify=Gtk.Justification.CENTER)
        name.add_css_class("heading")
        detail = Gtk.Label(label=f"{model.human_size(disk['size'])} · {disk['path']}", wrap=True,
                           justify=Gtk.Justification.CENTER)
        detail.add_css_class("dim-label")
        for widget in (icon, name, detail):
            box.append(widget)
        if not disk["usable"]:
            reason = Gtk.Label(label=disk.get("reason") or "cannot be used", wrap=True, justify=Gtk.Justification.CENTER)
            reason.add_css_class("caption")
            box.append(reason)
        return box

    def _fill_disks(self, disks):
        self.disks = sorted(disks, key=lambda d: (not d["usable"], d["path"]))
        self.selected = None
        self.next_btn.set_sensitive(False)
        self.flow.remove_all()
        self.flow.set_max_children_per_line(max(2, min(4, len(self.disks))))
        for disk in self.disks:
            self.flow.append(self._disk_card(disk))
            self.flow.get_child_at_index(self.flow.get_last_child().get_index()).set_sensitive(disk["usable"])
        self.disks_empty.set_visible(not disks)

    def _picked(self):
        chosen = self.flow.get_selected_children()
        self.selected = self.disks[chosen[0].get_index()]["path"] if chosen else None
        if self._next_handler == self._go_review:
            self.next_btn.set_sensitive(self.selected is not None)
        self.customize_btn.set_sensitive(self.selected is not None)

    def _go_review(self, review=False):
        """Ask the service for a plan; show the review page (Customize) or go straight to the erase dialog."""
        if not self.selected:
            return
        self._show_review = review
        self.next_btn.set_sensitive(False)

        def got(values, error):
            if error is not None:
                self._fatal(model.failure_text(error))
                return
            self._fill_review(values[0])
        self.backend.call("Deploy", "PlanDeploy", "(s)", (self.selected,), got)

    def _fill_review(self, plan):
        self.plan = plan
        self.next_btn.set_sensitive(True)
        problems = plan.get("problems", [])
        if not plan["possible"]:
            dialog = Adw.AlertDialog(heading="This disk cannot be used",
                                     body="\n".join(problems) or "The service refused this disk.")
            dialog.add_response("ok", "OK")
            dialog.present(self)
            return
        if getattr(self, "_show_review", False):
            self._fill_partitions(plan)
            return
        self._confirm()

    def _fill_partitions(self, plan):
        self.review_banner.set_title(model.erase_summary(plan))
        self.problem_label.set_visible(False)
        for row in self._partition_rows:
            self.partition_group.remove(row)
        self._partition_rows = []
        for title, value in (("Language", model.language_name(self.language)), ("Time zone", self.timezone)):
            row = Adw.ActionRow(title=title)
            row.add_suffix(Gtk.Label(label=value, css_classes=["dim-label"]))
            self.partition_group.add(row)
            self._partition_rows.append(row)
        for part in plan["partitions"]:
            row = Adw.ActionRow(title=model.partition_title(part["label"]), subtitle=part["note"])
            row.add_suffix(Gtk.Label(label=model.partition_size(part), css_classes=["dim-label"]))
            self.partition_group.add(row)
            self._partition_rows.append(row)
        self._nav(back=self._go_disks_keep, label="Install", handler=self._confirm, style="destructive-action")
        self.show("review")

    def _go_disks_keep(self):
        """Back from the review page to the disk list, keeping the choice."""
        self._nav(back=self._go_timezone, label="Install", handler=self._go_review, sensitive=self.selected is not None)
        self.show("disks")

    def _confirm(self):
        plan = self.plan
        dialog = Adw.AlertDialog(heading=f"Erase {model.disk_title(plan)}?", body=model.erase_summary(plan))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Erase and Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: self._install() if response == "install" else None)
        dialog.present(self)

    def _install(self):
        disk = self.plan["disk"]
        self.installing = True
        self.started_at = GLib.get_monotonic_time() / 1e6
        self.progress_bar.set_fraction(0)
        self.progress_step.set_label("Preparing…")
        self.progress_left.set_label("")
        self.show("progress")

        def start(callback):
            def got(values, error):
                callback(values[0] if error is None else None, error)
            self.backend.call("Deploy", "Deploy", "(ssss)", (disk, disk, self.language, self.timezone), got, 600_000)

        self.backend.follow(start, self._event, self._finished)

    def _event(self, kind, *args):
        if kind == "step":
            self.progress_step.set_label(args[1])
        elif kind == "progress":
            fraction = max(0.0, min(1.0, args[0]))
            self.progress_bar.set_fraction(fraction)
            self.progress_left.set_label(model.remaining_text(GLib.get_monotonic_time() / 1e6 - self.started_at, fraction))

    def _finished(self, job, error):
        self.installing = False
        if error is not None:
            self._show_failure(model.failure_text(error), None)
        elif job.ok:
            self._show_done()
        else:
            self._show_failure(model.failure_text(job), job.path)

    def _show_done(self):
        self._replace("done", _page("Installation complete",
                                    "Remove the installation medium, then restart to start the new system.",
                                    "object-select-symbolic"))
        self._nav(label="Restart", handler=self._restart)
        self.show("done")

    def _show_failure(self, message, job_path):
        details = Gtk.Label(label="", xalign=0, wrap=True, selectable=True, visible=False)
        details.add_css_class("monospace")
        toggle = Gtk.Button(label="Show Details", halign=Gtk.Align.CENTER, visible=job_path is not None)

        def reveal(_b):
            details.set_visible(not details.get_visible())
            toggle.set_label("Hide Details" if details.get_visible() else "Show Details")
            if details.get_visible() and not details.get_label() and job_path:
                self.backend.call("Job", "Logs", "(i)", (60,), lambda v, e: details.set_label(
                    "\n".join(v[0]) if e is None else model.failure_text(e)), 10_000, job_path)
        toggle.connect("clicked", reveal)
        self._replace("failed", _page(
            "Installation failed",
            f"{message}\n\nThe disk may have been erased and will not start. It is safe to try again.",
            "dialog-error-symbolic", toggle, details,
            _button("Choose a Disk", self._go_disks, "suggested-action"), _button("Quit", self.close)))
        self.show("failed")

    def _replace(self, name, page):
        old = self.stack.get_child_by_name(name)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(page, name)

    def _restart(self):
        def done(_values, error):
            if error is not None:
                self._show_failure(model.failure_text(error), None)
        self.backend.call("Boot", "Reboot", "(b)", (False,), done)


class InstallerApp(Adw.Application):
    def __init__(self, bus="system"):
        super().__init__(application_id=APP_ID)
        self.bus = bus
        self.window = None
        self.connect("activate", self._activate)

    def _activate(self, _app):
        if self.window is None:
            try:
                backend = Backend(self.bus)
            except ClientError as error:
                backend = None
                message = error.message
            self.window = InstallerWindow(self, backend) if backend else None
            if self.window is None:
                dialog = Adw.AlertDialog(heading="The installer cannot start", body=message)
                dialog.add_response("quit", "Quit")
                dialog.connect("response", lambda *_: self.quit())
                holder = Adw.ApplicationWindow(application=self)
                holder.present()
                dialog.present(holder)
                return
        self.window.present()


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="ersa-installer", description=f"{model.PRODUCT} installer")
    parser.add_argument("--version", action="version", version=f"ersa-installer {__version__}")
    parser.add_argument("--bus", choices=("system", "session"), default="system", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    GLib.set_application_name(f"{model.PRODUCT} Installer")
    return InstallerApp(args.bus).run([sys.argv[0]])
