"""The task panel at the foot of the sidebar: one line that says what is running, which opens a list of the tasks (running and
finished) with a progress bar each. A task is a foundationd job that Deck started and follows."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from foundation.i18n import Domain

_ = Domain("ersa-deck")
from . import icons  # noqa: E402

MAX_FINISHED = 12


class Task:
    """One row. The panel creates it; the caller reports step, progress and the end."""

    def __init__(self, panel, title):
        self.panel, self.title = panel, title
        self.running, self.ok, self.fraction, self.on_cancel = True, None, -1.0, None
        self.step = Gtk.Label(label=_("Starting…"), xalign=0, css_classes=["dim-label", "caption"], ellipsize=3)
        self.bar = Gtk.ProgressBar(css_classes=["task-progress"], margin_top=2)
        self.icon = Gtk.Image(valign=Gtk.Align.START, margin_top=2)
        self.cancel = icons.button("window-close-symbolic", _("Cancel: {title}").format(title=title), css=("flat", "circular"), valign=Gtk.Align.CENTER)
        self.cancel.connect("clicked", lambda _b: self.on_cancel and self.on_cancel())
        self.heading = Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=3, css_classes=["heading"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        for widget in (self.heading, self.step, self.bar):
            text.append(widget)
        box = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=10, margin_end=6)
        box.append(self.icon)
        box.append(text)
        box.append(self.cancel)
        self.row = Gtk.ListBoxRow(activatable=False, selectable=False, child=box)
        self._render()
        self._pulse = GLib.timeout_add(120, self._tick)

    def _tick(self):
        if not self.running:
            return False
        if self.fraction < 0:
            self.bar.pulse()
        return True

    def set_step(self, text):
        self.step.set_label(text or "")
        self.panel.changed()

    def set_fraction(self, fraction):
        self.fraction = fraction
        if fraction >= 0:
            self.bar.set_fraction(max(0.0, min(1.0, fraction)))
        self.panel.changed()

    def finish(self, ok, text):
        self.running, self.ok = False, ok
        self.step.set_label(text)
        self.step.set_wrap(True)
        self.step.set_ellipsize(0)
        self._render()
        self.panel.finished(self)

    def _render(self):
        done = not self.running
        self.bar.set_visible(not done)
        self.cancel.set_visible(not done)
        self.icon.set_visible(done)
        if done:
            icons.set_image(self.icon, "emblem-ok-symbolic" if self.ok else "dialog-warning-symbolic")
            self.icon.remove_css_class("success")
            self.icon.remove_css_class("error")
            self.icon.add_css_class("success" if self.ok else "error")


class TaskPanel(Gtk.Box):
    """The summary line (a toggle) over a revealer that holds the list."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, margin_start=8, margin_end=8, margin_bottom=8, margin_top=4)
        self.tasks = []
        self.set_visible(False)               # nothing to show until a task has been started
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["task-list"])
        self.scroller = Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                           max_content_height=170)
        self.clear = Gtk.Button(label=_("Clear finished"), css_classes=["flat", "caption"], halign=Gtk.Align.END, visible=False)
        self.clear.connect("clicked", lambda _b: self.clear_finished())
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_bottom=6)
        inner.append(self.scroller)
        inner.append(self.clear)
        self.revealer = Gtk.Revealer(child=inner, transition_type=Gtk.RevealerTransitionType.SLIDE_UP, reveal_child=False)
        self.append(self.revealer)
        self.spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner()
        self.idle_icon = icons.image("view-list-symbolic")
        self.title = Gtk.Label(label=_("Tasks"), xalign=0, hexpand=True, ellipsize=3)
        self.sub = Gtk.Label(label=_("Nothing running"), xalign=0, css_classes=["dim-label", "caption"], ellipsize=3)
        self.chevron = icons.image("pan-up-symbolic")
        self.meter = Gtk.ProgressBar(visible=False, css_classes=["task-progress"])
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        texts.append(self.title)
        texts.append(self.sub)
        row = Gtk.Box(spacing=10)
        self.icon_slot = Gtk.Box()
        self.icon_slot.append(self.idle_icon)
        row.append(self.icon_slot)
        row.append(texts)
        row.append(self.chevron)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        column.append(row)
        column.append(self.meter)
        self.toggle = Gtk.ToggleButton(child=column, css_classes=["flat", "task-toggle"])
        self.toggle.update_property([Gtk.AccessibleProperty.LABEL], [_("Tasks")])
        self.toggle.connect("toggled", lambda b: self.show_list(b.get_active()))
        self.append(self.toggle)

    def show_list(self, shown):
        self.revealer.set_reveal_child(shown)
        icons.set_image(self.chevron, "pan-down-symbolic" if shown else "pan-up-symbolic")

    def add(self, title):
        task = Task(self, title)
        self.set_visible(True)
        self.tasks.insert(0, task)
        self.list.prepend(task.row)
        self.toggle.set_active(True)          # show what has begun
        self.changed()
        return task

    def running(self):
        return [t for t in self.tasks if t.running]

    def changed(self):
        running = self.running()
        child = self.icon_slot.get_first_child()
        if child is not None:
            self.icon_slot.remove(child)
        if running:
            self.icon_slot.append(self.spinner)
            first = running[0]
            self.title.set_label(first.title if len(running) == 1 else _.ngettext("{count} task running", "{count} tasks running", len(running)).format(count=len(running)))
            self.sub.set_label(first.step.get_label() if len(running) == 1 else ", ".join(t.title for t in running)[:80])
            self.meter.set_visible(True)
            if first.fraction >= 0:
                self.meter.set_fraction(first.fraction)
            else:
                self.meter.pulse()
        else:
            self.icon_slot.append(self.idle_icon)
            self.meter.set_visible(False)
            last = self.tasks[0] if self.tasks else None
            self.title.set_label(_("Tasks"))
            self.sub.set_label("Nothing running" if last is None else (_("Done: {title}").format(title=last.title) if last.ok else _("Failed: {title}").format(title=last.title)))
        self.clear.set_visible(any(not t.running for t in self.tasks))

    def finished(self, task):
        done = [t for t in self.tasks if not t.running]
        for old in done[MAX_FINISHED:]:
            self.tasks.remove(old)
            self.list.remove(old.row)
        self.changed()

    def clear_finished(self):
        for task in [t for t in self.tasks if not t.running]:
            self.tasks.remove(task)
            self.list.remove(task.row)
        self.changed()
        if not self.tasks:
            self.toggle.set_active(False)
            self.set_visible(False)
