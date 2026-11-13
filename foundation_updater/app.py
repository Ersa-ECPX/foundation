"""The updater window (GTK 4 / libadwaita).

One page with the running version and a single button: Check for Updates, then Update. Installing from a file
and the boot slots are in the three-dot menu. Everything the window changes goes through foundationd over D-Bus:
Updates.Install takes the archive as a file descriptor and needs the administrator's password (polkit; the call allows
interactive authorization, so the desktop's authentication dialog opens), and the restart is Boot.Reboot. Whether an update
exists is read from the feed at `ota.url` (Config.Get); see model.py for its format.
"""

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from foundation import fetch  # noqa: E402
from foundation.gbackend import Backend, ClientError  # noqa: E402

from . import __version__, model  # noqa: E402

APP_ID = "org.ersa.Updater"
HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = "/var/tmp"
WORDMARK_WIDTH = 190            # logical pixels; ersa-wordmark-*.svg is 6.68 : 1, so about 28 px high

CSS = b"""
.hero-version { font-size: 48px; font-weight: 200; letter-spacing: -0.5px; }
.hero-product { font-weight: 500; letter-spacing: 1.2px; font-size: 0.85em; opacity: 0.6; }
.status-pill { padding: 3px 14px; border-radius: 999px; font-size: 0.9em; font-weight: 600; }
.status-pill.good { background-color: alpha(@success_bg_color, 0.22); color: @success_color; }
.status-pill.pending { background-color: alpha(@warning_bg_color, 0.25); color: @warning_color; }
.status-pill.unknown { background-color: alpha(currentColor, 0.1); }
.update-progress trough, .update-progress progress { min-height: 8px; border-radius: 4px; }
.result-line { opacity: 0.8; }
.done-ok image { color: @success_color; }
.done-fail image { color: @error_color; }
.check-status.error { color: @error_color; }
.check-status.available { color: @accent_color; font-weight: 600; }
"""


def _pill_button(label, handler, style=None):
    button = Gtk.Button(label=label, halign=Gtk.Align.CENTER)
    if style:
        button.add_css_class(style)
    button.add_css_class("pill")
    button.connect("clicked", lambda _b: handler())
    return button


class UpdaterWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend):
        super().__init__(application=app, title="System Update", default_width=480, default_height=620)
        self.backend = backend
        self.state, self.pending = {}, {}
        self.available = None         # the update the feed offers: {version, url, size, sha256}
        self.mode = "check"           # what the main button does: check, checking, update, restart
        self.working = False
        self.downloaded = ""          # a file this window downloaded and must delete afterwards
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True, hexpand=True)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="More", menu_model=self._menu(),
                                       primary=True)
        header.pack_end(self.menu_btn)
        view.add_top_bar(header)
        view.set_content(self.toasts)
        self.set_content(view)
        self.connect("close-request", lambda _w: self.working)     # never close in the middle of a write
        self._actions()

        self._build_home()
        self._build_working()
        self._build_done()
        self.stack.set_visible_child_name("home")
        self._set_logo()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._set_logo())
        self.refresh()

    # -- the menu ------------------------------------------------------------------------------

    def _menu(self):
        menu = Gio.Menu()
        local = Gio.Menu()
        local.append("Install from a File…", "win.install-file")
        menu.append_section(None, local)
        info = Gio.Menu()
        info.append("Boot Slots", "win.slots")
        menu.append_section(None, info)
        return menu

    def _actions(self):
        for name, handler in (("install-file", self.choose_file), ("slots", self.show_slots)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)

    # -- home ----------------------------------------------------------------------------------

    def _build_home(self):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                         margin_start=32, margin_end=32, margin_bottom=32)
        # the mark and the wordmark side by side, the product name under them, then the version as the main thing
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, halign=Gtk.Align.CENTER)
        self.logo = Gtk.Image(pixel_size=44, valign=Gtk.Align.CENTER)
        self.wordmark = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        brand.append(self.logo)
        # the SVG is 561 px wide by itself; the clamp keeps it at WORDMARK_WIDTH and the picture keeps the aspect ratio
        brand.append(Adw.Clamp(maximum_size=WORDMARK_WIDTH, tightening_threshold=WORDMARK_WIDTH, valign=Gtk.Align.CENTER,
                               child=self.wordmark))
        self.product_label = Gtk.Label(label=model.PRODUCT.upper(), css_classes=["hero-product"], margin_top=2)
        self.version_label = Gtk.Label(label="\u2026", css_classes=["hero-version"], margin_top=34)
        self.slot_label = Gtk.Label(label="", css_classes=["dim-label"])
        self.pill = Gtk.Label(label="", halign=Gtk.Align.CENTER, margin_top=6, css_classes=["status-pill", "unknown"])
        for widget in (brand, self.product_label, self.version_label, self.slot_label, self.pill):
            column.append(widget)
        self.check_status = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=44, margin_top=26,
                                      css_classes=["check-status", "dim-label"])
        column.append(self.check_status)
        self.main_btn = _pill_button("Check for Updates", self.main_action, "suggested-action")
        self.main_btn.set_margin_top(6)
        column.append(self.main_btn)
        self.stack.add_named(column, "home")

    def _set_logo(self):
        name = "ersa-mark-dark.svg" if Adw.StyleManager.get_default().get_dark() else "ersa-mark-light.svg"
        dark = "dark" if Adw.StyleManager.get_default().get_dark() else "light"
        self.wordmark.set_paintable(Gdk.Texture.new_from_filename(os.path.join(HERE, f"ersa-wordmark-{dark}.svg")))
        texture = Gdk.Texture.new_from_filename(os.path.join(HERE, name))
        self.logo.set_from_paintable(texture)
        if hasattr(self, "working_logo"):
            self.working_logo.set_from_paintable(texture)

    def set_mode(self, mode, status="", style="", button=None):
        """The main button and the line above it: mode is check, checking, update or restart."""
        self.mode = mode
        for css in ("error", "available"):
            self.check_status.remove_css_class(css)
        if style:
            self.check_status.add_css_class(style)
        self.check_status.set_label(status)
        labels = {"check": "Check for Updates", "checking": "Checking…", "restart": "Restart to Finish Updating…"}
        if mode == "update":
            label = f"Update to {self.available['version']}"
        else:
            label = labels[mode]
        self.main_btn.set_label(button or label)
        self.main_btn.set_sensitive(mode != "checking")

    def main_action(self):
        if self.mode == "check":
            self.check()
        elif self.mode == "update":
            self.ask_install(self.available["url"], self.available["sha256"], feed=True)
        elif self.mode == "restart":
            self.ask_restart()

    # -- working and done ----------------------------------------------------------------------

    def _build_working(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                      margin_start=32, margin_end=32)
        self.working_logo = Gtk.Image(pixel_size=96, halign=Gtk.Align.CENTER, margin_bottom=8)
        box.append(self.working_logo)
        self.working_title = Gtk.Label(label="Updating", css_classes=["title-1"], wrap=True)
        self.working_step = Gtk.Label(label="", css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        self.bar = Gtk.ProgressBar(width_request=320, css_classes=["update-progress"], margin_top=8)
        note = Gtk.Label(label="The system keeps running while this happens. Nothing changes until you restart.",
                         css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=44,
                         margin_top=10)
        for widget in (self.working_title, self.working_step, self.bar, note):
            box.append(widget)
        self.stack.add_named(box, "working")

    def _build_done(self):
        self.done = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Update installed")
        self.done_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.CENTER)
        self.restart_btn = _pill_button("Restart Now…", self.ask_restart, "suggested-action")
        self.later_btn = _pill_button("Later", self.go_home)
        self.later_btn.add_css_class("flat")
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, margin_top=12)
        buttons.append(self.restart_btn)
        buttons.append(self.later_btn)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.done_lines)
        content.append(buttons)
        self.done.set_child(content)
        self.stack.add_named(self.done, "done")

    def show_done(self, ok, title, description, lines=()):
        self.done.set_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
        self.done.remove_css_class("done-fail" if ok else "done-ok")
        self.done.add_css_class("done-ok" if ok else "done-fail")
        self.done.set_title(title)
        self.done.set_description(description)
        child = self.done_lines.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.done_lines.remove(child)
            child = following
        for line in lines:
            self.done_lines.append(Gtk.Label(label=line, wrap=True, justify=Gtk.Justification.CENTER, css_classes=["result-line"]))
        self.restart_btn.set_visible(ok)
        self.later_btn.set_label("Later" if ok else "Back")
        self.stack.set_visible_child_name("done")

    def go_home(self):
        self.stack.set_visible_child_name("home")
        self.refresh()

    # -- state ---------------------------------------------------------------------------------

    def refresh(self):
        def got_state(values, error):
            if error is None:
                self.state = values[0]
                self._show_state()

        def got_pending(values, error):
            if error is None:
                self.pending = values[0]
                if self.pending and not self.working:
                    self.set_mode("restart", model.pending_text(self.pending), "available")
                elif not self.pending and self.mode == "restart":
                    self.set_mode("check")
        self.backend.call("Boot", "GetBootState", None, None, got_state)
        self.backend.call("Updates", "Pending", None, None, got_pending)

    def _show_state(self):
        self.version_label.set_label(model.running_title(self.state).removeprefix("Version "))
        self.slot_label.set_label(model.running_subtitle(self.state))
        style, text = model.assessment(self.state)
        self.pill.set_label(text)
        for css in ("good", "pending", "unknown"):
            self.pill.remove_css_class(css)
        self.pill.add_css_class(style)

    def show_slots(self):
        def got(values, error):
            if error is not None:
                self.toast(error.message)
                return
            page = Adw.PreferencesPage()
            group = Adw.PreferencesGroup(
                title="Boot slots", description="The system is kept in two slots. An update goes into the one that is not running.")
            for slot in sorted(values[0], key=lambda s: s["name"]):
                row = Adw.ActionRow(title=model.slot_title(slot), subtitle=model.slot_subtitle(slot))
                row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic" if slot.get("running") else "drive-harddisk-symbolic"))
                group.add(row)
            page.add(group)
            view = Adw.ToolbarView(content=page)
            view.add_top_bar(Adw.HeaderBar())
            dialog = Adw.Dialog(title="Boot Slots", child=view, content_width=460, content_height=380)
            dialog.present(self)
        self.backend.call("Boot", "GetSlots", None, None, got)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))

    # -- checking ------------------------------------------------------------------------------

    def check(self):
        self.set_mode("checking", "Looking for updates…")

        def got(values, error):
            base = values[0] if error is None and values else ""
            if error is not None or not isinstance(base, str):
                self._checked(None, "The update source could not be read from the settings.")
            elif model.is_placeholder(base):
                self._checked(None, "No update server is set up yet (the setting ota.url). You can still install an update from a file (the ⋯ menu).", soft=True)
            else:
                self._check_feed(base)
        self.backend.call("Config", "Get", "(s)", ("ota.url",), got)

    def _check_feed(self, base):
        arch = os.uname().machine

        def work():
            try:
                found = model.parse_feed(fetch.fetch_text(model.feed_url(base, arch)), base, arch)
            except (fetch.FetchError, model.FeedError) as error:
                GLib.idle_add(self._checked, None, str(error))
                return
            GLib.idle_add(self._checked, found, "")
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, found, problem, soft=False):
        if found is None:
            self.set_mode("check", problem, "" if soft else "error", button="Try Again" if not soft else None)
        elif model.is_newer(found["version"], self.state.get("running_version", "")):
            self.available = found
            size = model.size_text(found["size"])
            self.set_mode("update", f"Version {found['version']} is available" + (f" ({size})" if size else ""), "available")
        else:
            self.set_mode("check", "Your system is up to date.", button="Check Again")
        return False

    # -- installing ----------------------------------------------------------------------------

    def ask_install(self, source, sha256="", feed=False):
        """Confirm, then install. `source` is the feed's address (downloaded first) or a local file. A local install can skip the
        signature check or reinstall a version; an update from the feed never can."""
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8)
        skip = Gtk.CheckButton(label="Skip the signature check (an update I built myself)")
        force = Gtk.CheckButton(label="Install again even if this version is already installed")
        if not feed:
            extra.append(skip)
            extra.append(force)
        # a local file is named so the person sees which one; the address of a download is not shown
        name = os.path.basename(source) if os.path.isfile(source) else ""
        dialog = Adw.AlertDialog(
            heading="Install this update?",
            body=("The new system is written to the spare slot. The running system is not changed and keeps working. "
                  "Your bundles are rebuilt for the new system, and it starts at the next restart. If it does not start "
                  "properly, the computer returns to the system that works.\n\nYou will be asked for the administrator's "
                  "password." + (f"\n\n{name}" if name else "")))
        if not feed:
            dialog.set_extra_child(extra)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "install"
                       and self.begin(source, sha256, skip.get_active(), force.get_active()))
        dialog.present(self)

    def begin(self, source, sha256, unsigned, force):
        """Download if `source` is an address, then install."""
        self.working = True
        self.working_title.set_label("Updating")
        self.bar.set_fraction(0.0)
        self.stack.set_visible_child_name("working")
        if source.startswith(("http://", "https://")):
            self.working_step.set_label("Downloading the update…")
            self._download(source, sha256, unsigned, force)
        else:
            self.install(source, unsigned, force)

    def _download(self, url, sha256, unsigned, force):
        last = {"percent": -1}

        def progress(done, total):
            percent = int(done * 100 / total) if total else -1
            if percent != last["percent"]:
                last["percent"] = percent
                GLib.idle_add(self._download_progress, done, total)

        def work():
            try:
                path = fetch.fetch(url, DOWNLOAD_DIR, progress=progress)
                if sha256 and fetch.file_sha256(path) != sha256:
                    os.unlink(path)
                    raise fetch.FetchError("the downloaded update does not match the checksum in the update feed")
            except (fetch.FetchError, OSError) as error:
                GLib.idle_add(self._download_failed, str(error))
                return
            GLib.idle_add(self._downloaded, path, unsigned, force)
        threading.Thread(target=work, daemon=True).start()

    def _download_progress(self, done, total):
        if total:
            self.bar.set_fraction(done / total)
            self.working_step.set_label(f"Downloading the update… {done / 1024 ** 2:.0f} of {total / 1024 ** 2:.0f} MB")
        else:
            self.bar.pulse()
            self.working_step.set_label(f"Downloading the update… {done / 1024 ** 2:.0f} MB")
        return False

    def _download_failed(self, problem):
        self.working = False
        self.show_done(False, "The update was not downloaded", problem, ["The running system was not changed."])
        return False

    def _downloaded(self, path, unsigned, force):
        self.downloaded = path
        self.bar.set_fraction(0.0)
        self.install(path, unsigned, force)
        return False

    def install(self, path, unsigned, force):
        try:
            handle = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as error:
            self._finished(None, ClientError("Failed", f"Cannot open the file: {error.strerror}"))
            return
        fds = Gio.UnixFDList.new()
        index = fds.append(handle)
        os.close(handle)
        self.working_step.set_label("Waiting for authorization…")

        def start(callback):
            def got(values, error):
                callback(values[0] if error is None else None, error)
            # a polkit agent may open an authentication dialog before this call returns
            self.backend.call("Updates", "Install", "(hsbb)", (index, "", unsigned, force), got, 600_000, fds=fds)

        self.backend.follow(start, self._event, self._finished)

    def _event(self, kind, *args):
        if kind == "step":
            self.working_step.set_label(args[1])
        elif kind == "progress":
            self.bar.set_fraction(max(0.0, min(1.0, args[0])))

    def _finished(self, job, error):
        self.working = False
        if self.downloaded:
            try:
                os.unlink(self.downloaded)
            except OSError:
                pass
            self.downloaded = ""
        self.available = None
        if error is not None:
            self.show_done(False, "The update was not installed", model.error_text(error.name, error.message),
                           ["The running system was not changed."])
        elif not job.ok:
            self.show_done(False, "The update was not installed", model.error_text(job.error_name, job.error_message),
                           ["The running system was not changed."])
        else:
            result = job.result
            version = result.get("version") or ""
            self.show_done(True, "Update installed",
                           f"Version {version} is ready. Restart to start using it." if version else "Restart to start using it.",
                           model.result_lines(result))
        self.set_mode("check")
        self.refresh()

    # -- local updates (menu) ------------------------------------------------------------------

    def choose_file(self):
        dialog = Gtk.FileDialog(title="Choose an update file")
        archives = Gtk.FileFilter(name="Update archives")
        for pattern in ("*.tar.zst", "*.tar", "*.zst"):
            archives.add_pattern(pattern)
        everything = Gtk.FileFilter(name="All files")
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        filters.append(everything)
        dialog.set_filters(filters)
        dialog.set_default_filter(archives)

        def chosen(source, result):
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return                      # cancelled
            path = file.get_path()
            kind, problem = model.classify_source(path)
            if kind != "file":
                self.toast(problem or "That file cannot be used.")
                return
            self.ask_install(path)
        dialog.open(self, None, chosen)

    # -- restarting ----------------------------------------------------------------------------

    def ask_restart(self):
        dialog = Adw.AlertDialog(heading="Restart now?",
                                 body="Open applications will be closed. The computer starts the updated system.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restart", "Restart")
        dialog.set_response_appearance("restart", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "restart" and self.restart())
        dialog.present(self)

    def restart(self):
        def done(values, error):
            if error is not None:
                self.toast(model.reboot_failed(error.name, error.message))
        self.backend.call("Boot", "Reboot", "(b)", (False,), done)


class UpdaterApp(Adw.Application):
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
                holder = Adw.ApplicationWindow(application=self)
                holder.present()
                dialog = Adw.AlertDialog(heading="The updater cannot start", body=error.message)
                dialog.add_response("quit", "Quit")
                dialog.connect("response", lambda *_: self.quit())
                dialog.present(holder)
                return
            self.window = UpdaterWindow(self, backend)
        self.window.present()


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="ersa-updater", description=f"{model.PRODUCT} updater")
    parser.add_argument("--version", action="version", version=f"ersa-updater {__version__}")
    parser.add_argument("--bus", choices=("system", "session"), default="system", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    GLib.set_application_name(f"{model.PRODUCT} Updater")
    return UpdaterApp(args.bus).run([sys.argv[0]])
