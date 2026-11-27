"""The Deck window (GTK 4 / libadwaita): a sidebar of pages in groups, like a settings app, and one page at a time.

Everything is a foundationd D-Bus call. Calls that change the system are jobs; the window follows them and shows the result.
polkit asks for the administrator's password (the calls allow interactive authorization). Deck checks and decides nothing itself.
"""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from foundation import identity  # noqa: E402
from foundation.gbackend import Backend, ClientError  # noqa: E402

from foundation.i18n import Domain

_ = Domain("ersa-deck")
from . import __version__, icons, model, pages  # noqa: E402
from .tasks import TaskPanel  # noqa: E402

APP_ID = "org.ersa.Deck"
HERE = os.path.dirname(os.path.abspath(__file__))

CSS = b"""
.hero-version { font-size: 56px; font-weight: 200; letter-spacing: -0.5px; }
.hero-product { font-weight: 500; letter-spacing: 1.4px; font-size: 0.85em; opacity: 0.6; }
.status-pill { padding: 3px 14px; border-radius: 999px; font-size: 0.9em; font-weight: 600; }
.status-pill.good { background-color: alpha(@success_bg_color, 0.22); color: @success_color; }
.status-pill.pending { background-color: alpha(@warning_bg_color, 0.25); color: @warning_color; }
.status-pill.unknown { background-color: alpha(currentColor, 0.1); }
.update-status.available { color: @accent_color; font-weight: 600; }
.task-toggle { padding: 8px 10px; border-radius: 8px; background: transparent; }
.task-toggle:hover { background-color: alpha(currentColor, 0.07); }
.task-toggle:checked { background: transparent; }
.task-list { background: transparent; }
.task-list row { background: transparent; border-bottom: 1px solid alpha(currentColor, 0.1); }
.task-list row:last-child { border-bottom: none; }
.task-progress trough, .task-progress progress { min-height: 4px; border-radius: 2px; }
.sidebar-heading { font-size: 0.8em; font-weight: 700; opacity: 0.55; letter-spacing: 0.5px; margin: 14px 1px 2px 1px; }
"""


class DeckWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend):
        super().__init__(application=app, title=_("Deck"), default_width=980, default_height=720)
        self.set_size_request(340, 360)
        self.backend = backend
        self.current = "overview"
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.tasks = TaskPanel()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True, hexpand=True)
        self.pages = {}
        for name, _title, _icon in model.PAGES:
            self.pages[name] = pages.PAGE_CLASSES[name](self)
            self.stack.add_named(self.pages[name], name)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.split = Adw.NavigationSplitView(min_sidebar_width=190, max_sidebar_width=260, sidebar_width_fraction=0.23)
        self.content_page = Adw.NavigationPage(title=_("Overview"), tag="content")
        header = Adw.HeaderBar()
        refresh = icons.button("view-refresh-symbolic", _("Refresh (Ctrl+R)"))
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_end(refresh)
        self.pending_banner = Adw.Banner(title="", button_label=_("Restart\u2026"), revealed=False)
        self.pending_banner.connect("button-clicked", lambda _b: self.ask_restart())
        self.rollback_banner = Adw.Banner(title="", button_label=_("Boot Slots"), revealed=False)
        self.rollback_banner.connect("button-clicked", lambda _b: (self.rollback_banner.set_revealed(False), self.select("slots")))
        view = Adw.ToolbarView(content=self.toasts)
        for bar in (header, self.pending_banner, self.rollback_banner):
            view.add_top_bar(bar)
        self.content_page.set_child(view)
        self.split.set_content(self.content_page)
        self.split.set_sidebar(self._build_sidebar())
        self.set_content(self.split)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(narrow)
        self._actions(app)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._set_logo())
        self._set_logo()
        self.select("overview")
        self.rows["overview"].grab_focus()
        self.refresh_banners()

    def _actions(self, app):
        for name, handler, accels in (("refresh", self.refresh, ["<Control>r", "F5"]), ("search", self.focus_search, ["<Control>f"]),
                                      ("tasks", lambda: self.tasks.toggle.set_active(not self.tasks.toggle.get_active()), ["<Control>t"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
            app.set_accels_for_action(f"win.{name}", accels)
        app.set_accels_for_action("window.close", ["<Control>w", "<Control>q"])

    # -- sidebar -------------------------------------------------------------------------------

    def _build_sidebar(self):
        page = Adw.NavigationPage(title=_("Deck"), tag="sidebar")
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        header.add_css_class("flat")
        view.add_top_bar(header)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search"), margin_start=6, margin_end=6, margin_top=4)
        self.search.connect("search-changed", lambda _e: self.sidebar.invalidate_filter())
        column.append(self.search)
        self.sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"], vexpand=True)
        self.rows, section_rows = {}, {}
        for heading, entries in model.SECTIONS:
            members = []
            if heading:
                head = Gtk.ListBoxRow(selectable=False, activatable=False, focusable=False)
                head.set_child(Gtk.Label(label=heading.upper(), xalign=0, css_classes=["sidebar-heading"]))
                head.members = members
                self.sidebar.append(head)
            for name, title, icon in entries:
                row = Gtk.ListBoxRow(css_classes=[])
                row.page_name, row.title = name, title
                row.update_property([Gtk.AccessibleProperty.LABEL], [title])
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                box.append(icons.image(icon))
                box.append(Gtk.Label(label=title, xalign=0, ellipsize=3))
                row.set_child(box)
                self.sidebar.append(row)
                self.rows[name] = row
                members.append(row)
        self.sidebar.set_filter_func(self._filter)
        self.sidebar.connect("row-selected", self._selected)
        column.append(Gtk.ScrolledWindow(child=self.sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        column.append(Gtk.Separator())
        column.append(self.tasks)
        view.set_content(column)
        page.set_child(view)
        return page

    def _filter(self, row):
        query = self.search.get_text()
        if hasattr(row, "members"):
            return any(model.page_matches(m.page_name, m.title, query) for m in row.members)
        return model.page_matches(row.page_name, row.title, query)

    def focus_search(self):
        self.split.set_show_content(False)
        self.search.grab_focus()

    def _selected(self, _box, row):
        if row is not None:
            self.show_page(row.page_name)

    def select(self, name):
        self.sidebar.select_row(self.rows[name])

    def show_page(self, name):
        self.current = name
        self.stack.set_visible_child_name(name)
        self.content_page.set_title(next(t for n, t, _i in model.PAGES if n == name))
        self.split.set_show_content(True)
        page = self.pages[name]
        if not (name == "packages" and page.loaded):
            page.reload()
            page.loaded = True

    def refresh(self):
        self.pages[self.current].reload()
        self.refresh_banners()

    def refresh_banners(self):
        def pending(value):
            text = model.pending_text(value)
            self.pending_banner.set_title(text)
            self.pending_banner.set_revealed(bool(text))

        def slots(value):
            version = model.failed_update(value)
            if version:
                self.rollback_banner.set_title(model.rollback_text(version))
            self.rollback_banner.set_revealed(bool(version))
        self.ask("Updates", "Pending", None, None, pending, lambda _e: None)
        self.ask("Boot", "GetSlots", None, None, slots, lambda _e: None)

    def _set_logo(self):
        dark = "dark" if Adw.StyleManager.get_default().get_dark() else "light"
        texture = Gdk.Texture.new_from_filename(os.path.join(HERE, f"ersa-mark-{dark}.svg"))
        self.pages["overview"].hero_logo.set_from_paintable(texture)

    # -- plumbing for the pages ----------------------------------------------------------------

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))

    def ask(self, interface, method, signature, args, handler=None, failed=None, timeout=60_000, fds=None, path=None):
        """One call. handler(first reply value or None) on success; failed(error) on failure (default: a toast)."""
        def done(values, error):
            if error is not None:
                (failed or (lambda e: self.toast(model.error_text(e.name, e.message))))(error)
            elif handler:
                handler(values[0] if values else None)
        kwargs = {"fds": fds} if fds is not None else {}
        if path is not None:
            kwargs["path"] = path
        self.backend.call(interface, method, signature, args, done, timeout, **kwargs)

    def job_call(self, interface, method, signature, args, timeout=600_000, fds=None):
        def start(callback):
            def got(values, error):
                callback(values[0] if error is None else None, error)
            kwargs = {"fds": fds} if fds is not None else {}
            self.backend.call(interface, method, signature, args, got, timeout, **kwargs)
        return start

    def run_job(self, call, on_ok=None, label=None):
        """Start a job with call(callback) and follow it as a task in the sidebar panel. The daemon refuses a job that clashes
        with a running one (Busy); that is shown on the task."""
        task = self.tasks.add((label or _("Working")).rstrip("\u2026."))

        def event(kind, *args):
            if kind == "step":
                task.set_step(args[1])
            elif kind == "progress":
                task.set_fraction(args[0])
            elif kind == "message" and args and args[0]:
                task.set_step(args[0])

        def finished(job, error):
            if error is not None:
                task.finish(False, model.error_text(error.name, error.message))
            elif not job.ok:
                task.finish(False, model.error_text(job.error_name, job.error_message))
            else:
                task.finish(True, _("Done"))
                if on_ok:
                    on_ok(job.result)
            self.refresh()
        follower = self.backend.follow(call, event, finished)

        def cancel():
            if follower.path:
                self.ask("org.ersa.Foundation1.Job", "Cancel", None, None, None, path=follower.path)
        task.on_cancel = cancel

    def open_fd(self, path):
        """(UnixFDList, index) for a file the person chose, opened here as them, or None (with a toast)."""
        try:
            handle = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as error:
            self.toast(_("Cannot open the file: {reason}").format(reason=error.strerror))
            return None
        fds = Gio.UnixFDList.new()
        index = fds.append(handle)
        os.close(handle)
        return fds, index

    def choose_file(self, title, patterns, name, chosen):
        dialog = Gtk.FileDialog(title=title)
        wanted = Gtk.FileFilter(name=name)
        for pattern in patterns:
            wanted.add_pattern(pattern)
        everything = Gtk.FileFilter(name=_("All files"))
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(wanted)
        filters.append(everything)
        dialog.set_filters(filters)
        dialog.set_default_filter(wanted)

        def done(source, result):
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return
            chosen(file.get_path())
        dialog.open(self, None, done)

    def confirm(self, heading, body, response, action, destructive=False):
        self.confirm_choice(heading, body, [("ok", response, "destructive" if destructive else "suggested")],
                            lambda _r: action())

    def confirm_choice(self, heading, body, responses, action):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancel"))
        for name, label, style in responses:
            dialog.add_response(name, label)
            dialog.set_response_appearance(name, Adw.ResponseAppearance.DESTRUCTIVE if style == "destructive" else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r != "cancel" and action(r))
        dialog.present(self)

    def alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def ask_restart(self):
        self.confirm(_("Restart now?"), _("Open applications will be closed."), _("Restart"), lambda: self.ask("Boot", "Reboot", "(b)", (False,)))


class OfferWindow(Adw.Window):
    """Shown once, after the first login, when a provider is set up: sign in to Ersa ID (opens Deck on that page) or not now (asked once,
    never forced)."""

    def __init__(self, app):
        super().__init__(application=app, title=_("Ersa ID"), default_width=420, resizable=False)
        page = Adw.StatusPage(icon_name="avatar-default-symbolic", title=_("Sign in to Ersa ID"),
                              description=_("Link this computer to your account. You can skip this and sign in later in Deck."))
        sign_in = Gtk.Button(label=_("Sign In"), css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        later = Gtk.Button(label=_("Not Now"), css_classes=["flat"], halign=Gtk.Align.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)
        box.append(sign_in)
        box.append(later)
        page.set_child(box)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar(show_title=False))
        self.set_content(view)
        sign_in.connect("clicked", lambda _b: app.accept_offer())
        later.connect("clicked", lambda _b: app.decline_offer())


class DeckApp(Adw.Application):
    def __init__(self, bus="system", offer=False, page=None):
        super().__init__(application_id=APP_ID)
        self.bus, self.window, self.offer, self.page = bus, None, offer, page
        self.connect("activate", self._activate)

    # -- the one-time offer after the first login ----------------------------------------------

    def _offer(self):
        self.hold()                                  # no window yet: keep running until the answer is in
        try:
            backend = Backend(self.bus)
        except ClientError:
            self.release()
            return

        def got(values, error):
            provider = identity.settings_from(values[0] if values and error is None else {})
            if error is None and identity.offer_needed(provider):
                self.offer_window = OfferWindow(self)
                self.offer_window.present()
            else:
                self.release()
        backend.call("Config", "List", None, None, got, 15_000)

    def accept_offer(self):
        identity.mark_offered()
        self.offer_window.close()
        self.page = "ersaid"
        self._activate(self)                         # opens Deck on the Ersa ID page, which does the sign-in
        self.release()

    def decline_offer(self):
        identity.mark_offered()
        self.offer_window.close()
        self.release()

    def _activate(self, _app):
        if self.offer and self.window is None and not getattr(self, "offer_window", None):
            self.offer = False
            return self._offer()
        if self.window is None:
            try:
                backend = Backend(self.bus)
            except ClientError as error:
                holder = Adw.ApplicationWindow(application=self)
                holder.present()
                dialog = Adw.AlertDialog(heading=_("Deck cannot start"), body=error.message)
                dialog.add_response("quit", _("Quit"))
                dialog.connect("response", lambda *_: self.quit())
                dialog.present(holder)
                return
            self.window = DeckWindow(self, backend)
        if self.page:
            self.window.select(self.page)
            self.page = None
        self.window.present()


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="ersa-deck", description=_("{product} management window").format(product=model.PRODUCT))
    parser.add_argument("--version", action="version", version=f"ersa-deck {__version__}")
    parser.add_argument("--bus", choices=("system", "session"), default="system", help=argparse.SUPPRESS)
    parser.add_argument("--page", choices=[name for name, _t, _i in model.PAGES], help=_("open this page"))
    parser.add_argument("--offer-sign-in", action="store_true",
                        help=_("after the first login: offer Ersa ID sign-in once, if a provider is set up (used by a user service)"))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if Gdk.Display.get_default() is None:
        # No screen (a service started before the session had one): exit instead of staying alive and holding the application's
        # name, which would make every later launch of Deck do nothing.
        print(_("ersa-deck: no display is available"), file=sys.stderr)
        return 1
    GLib.set_application_name(_("Deck"))
    return DeckApp(args.bus, offer=args.offer_sign_in, page=args.page).run([sys.argv[0]])
