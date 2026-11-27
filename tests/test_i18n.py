"""Gettext catalogs and GUI language selection."""
import importlib
import gettext
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from foundation.i18n import Domain
from foundation_deck import model as deck_model
from foundation_installer import model as installer_model
HAS_CATALOGS = (ROOT / "tools/i18n.py").exists() and (ROOT / "data/po/ersa-installer/hi.po").exists()
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    DISPLAY = bool(os.environ.get("ERSA_GUI_TESTS")) and Gtk.init_check()
except (ImportError, ValueError):
    DISPLAY = False


@unittest.skipUnless(HAS_CATALOGS, "catalog sources absent from archive")
class Catalog(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import i18n
        self.tool = i18n
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for domain in i18n.DOMAINS:
            i18n.check(domain)
            i18n.compile_po(ROOT / "data/po" / domain / "hi.po",
                            Path(self.tmp.name) / "hi/LC_MESSAGES" / f"{domain}.mo")
        env = patch.dict(os.environ, {"ECP_LOCALEDIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(Domain("ersa-installer").set_language, None)
        self.addCleanup(Domain("ersa-deck").set_language, None)

    def test_compiled_catalog_and_fallback(self):
        installer = Domain("ersa-installer")
        installer.set_language("hi_IN.UTF-8")
        self.assertEqual(installer("Continue"), "आगे बढ़ें")
        self.assertEqual(installer.ngettext("About {count} minute remaining", "About {count} minutes remaining", 2),
                         "लगभग {count} मिनट बाकी हैं")
        installer.set_language("xx_XX.UTF-8")
        self.assertEqual(installer("Continue"), "Continue")

    def test_models_render_hindi_and_fall_back(self):
        deck = Domain("ersa-deck")
        deck.set_language("hi")
        self.assertIn("घंटे पहले", deck_model.age_text(7200))
        self.assertIn("पैकेज", deck_model.bundle_subtitle({"packages": ["a", "b"]}))
        installer = Domain("ersa-installer")
        installer.set_language("hi_IN.UTF-8")
        self.assertIn("मिनट बाकी", installer_model.remaining_text(300, .5))
        deck.set_language("xx")
        self.assertEqual(deck_model.age_text(7200), "2 hours ago")

    def test_session_language(self):
        deck = Domain("ersa-deck")
        deck.set_language(None)
        with patch.dict(os.environ, {"LANGUAGE": "hi"}):
            self.assertEqual(deck("Overview"), "अवलोकन")

    def test_context_multiline_and_escapes_compile(self):
        import gettext
        po = Path(self.tmp.name) / "context.po"
        po.write_text('msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\nPlural-Forms: nplurals=2; plural=(n > 1);\\n"\n\nmsgctxt "button"\nmsgid "Open"\nmsgstr "खोलें"\n\nmsgid "First\\n"\n"second"\nmsgstr "पहली\\n"\n"दूसरी"\n')
        mo = Path(self.tmp.name) / "context.mo"
        self.tool.compile_po(po, mo)
        with mo.open("rb") as stream:
            catalog = gettext.GNUTranslations(stream)
        self.assertEqual(catalog.pgettext("button", "Open"), "खोलें")
        self.assertEqual(catalog.gettext("First\nsecond"), "पहली\nदूसरी")

    def test_checker_rejects_placeholder_change(self):
        self.assertNotEqual(self.tool.markers("{version}"), self.tool.markers("{release}"))
        self.assertNotEqual(self.tool.markers("%s"), self.tool.markers("%d"))

    def test_every_catalog_compiles_and_loads(self):
        samples = {"ersa-installer": "Go Back", "ersa-deck": "Updates"}
        for domain in self.tool.DOMAINS:
            self.tool.check(domain, strict=False)
            paths = sorted((ROOT / "data/po" / domain).glob("*.po"))
            self.assertTrue(paths)
            for path in paths:
                with self.subTest(domain=domain, language=path.stem):
                    catalog = self.tool.parse(path)
                    header = catalog[(None, "", None)]
                    count = int(re.search(r"nplurals=(\d+)", header).group(1))
                    for (context, msgid, plural), value in catalog.items():
                        if not msgid:
                            continue
                        values = value if isinstance(value, dict) else {0: value}
                        if not any(values.values()):          # not translated yet: allowed, it shows the English text
                            continue
                        self.assertTrue(all(values.values()), msgid)
                        if plural is not None:
                            self.assertEqual(set(values), set(range(count)), msgid)
                        for index, translated in values.items():
                            source = plural if index else msgid
                            self.assertEqual(self.tool.markers(translated), self.tool.markers(source), msgid)
                    target = Path(self.tmp.name) / path.stem / "LC_MESSAGES" / f"{domain}.mo"
                    self.tool.compile_po(path, target)
                    with target.open("rb") as stream:
                        gettext.GNUTranslations(stream)
                    instance = Domain(domain)
                    instance.set_language(path.stem)
                    self.assertNotEqual(instance(samples[domain]), samples[domain])

    def test_checker_uses_plural_count_in_header(self):
        root = Path(self.tmp.name)
        directory = root / "data/po/example"
        directory.mkdir(parents=True)
        key = (None, "{count} item", "{count} items")
        (directory / "example.pot").write_text(self.tool.header("example") + "\n" + self.tool.entry(key))
        for count in (1, 3, 6):
            with self.subTest(nplurals=count):
                forms = {index: f"{{count}} item form {index}" for index in range(count)}
                header = self.tool.entry((None, "", None),
                                         f"Content-Type: text/plain; charset=UTF-8\n"
                                         f"Plural-Forms: nplurals={count}; plural=0;\n")
                path = directory / "sample.po"
                path.write_text(header + "\n" + self.tool.entry(key, forms, nplurals=count))
                with patch.object(self.tool, "ROOT", root), patch.object(self.tool, "extract", return_value={key: {}}):
                    self.tool.check("example")
                    if count > 1:
                        path.write_text(header + "\n" + self.tool.entry(key, forms, nplurals=count - 1))
                        with self.assertRaisesRegex(ValueError, "wrong plural count"):
                            self.tool.check("example")

    def test_update_creates_language_plural_forms(self):
        root = Path(self.tmp.name)
        directory = root / "data/po/example"
        directory.mkdir(parents=True)
        key = (None, "{count} item", "{count} items")
        for language, count in (("ja", 1), ("ru", 3), ("ar", 6)):
            path = directory / f"{language}.po"
            path.touch()
            with patch.object(self.tool, "ROOT", root), patch.object(self.tool, "extract", return_value={key: {"comments": [], "refs": []}}):
                self.tool.update("example")
            catalog = self.tool.parse(path)
            self.assertEqual(set(catalog[key]), set(range(count)))


class FakeBackend:
    def call(self, interface, method, signature, args, callback, timeout=0, **kwargs):
        answers = {
            ("Manager", "GetSystemInfo"): {"os": {"image_version": "1.0"}},
            ("Manager", "GetStatus"): {"capabilities": ["deploy"]},
            ("Boot", "GetBootState"): {},
            ("Boot", "GetSlots"): [],
            ("Updates", "Pending"): {},
            ("Updates", "Available"): {},
        }
        callback([answers.get((interface, method), {})], None)


@unittest.skipUnless(DISPLAY and HAS_CATALOGS, "requires xvfb and catalog sources")
class Windows(Catalog):
    APP = None

    def setUp(self):
        super().setUp()
        if Windows.APP is None:
            Windows.APP = Adw.Application(application_id="org.ersa.I18nTest")
            Windows.APP.register(None)

    def test_installer_switches_immediately(self):
        from foundation_installer.app import InstallerWindow
        window = InstallerWindow(Windows.APP, FakeBackend())
        self.addCleanup(window.destroy)
        window._go_language()
        row = next(r for r in window.language_rows if r.value == "hi_IN.UTF-8")
        window.language_list.select_row(row)
        self.assertEqual(window.stack.get_child_by_name("language").get_first_child().get_label(), "अपनी भाषा चुनें")
        self.assertEqual(window.back_btn.get_label(), "वापस जाएँ")
        self.assertEqual(window.next_btn.get_label(), "आगे बढ़ें")
        self.assertIn("डिस्क चुनें", window.stack.get_child_by_name("disks").get_first_child().get_label())
        row = next(r for r in window.language_rows if r.value == "en_US.UTF-8")
        window.language_list.select_row(row)
        self.assertEqual(window.back_btn.get_label(), "Go Back")
        for code in ("ar_EG.UTF-8", "he_IL.UTF-8"):
            row = next(r for r in window.language_rows if r.value == code)
            window.language_list.select_row(row)
            self.assertEqual(Gtk.Widget.get_default_direction(), Gtk.TextDirection.RTL)
        window._pick_language("en_US.UTF-8")
        self.assertEqual(Gtk.Widget.get_default_direction(), Gtk.TextDirection.LTR)

    def test_deck_uses_session_language(self):
        from foundation_deck.app import DeckWindow
        with patch.dict(os.environ, {"LANGUAGE": "hi"}):
            Domain("ersa-deck").set_language(None)
            importlib.reload(deck_model)
            window = DeckWindow(Windows.APP, FakeBackend())
            try:
                self.assertEqual(window.pages["updates"].heading.get_label(), "अपडेट")
                self.assertIn("अवलोकन", str(window.rows["overview"].get_child().get_last_child().get_label()))
            finally:
                window.destroy()
        importlib.reload(deck_model)

    def test_arabic_selection_restores_ltr(self):
        from foundation_installer.app import InstallerWindow
        window = InstallerWindow(Windows.APP, FakeBackend())
        self.addCleanup(window.destroy)
        window._go_language()
        arabic = next(r for r in window.language_rows if r.value == "ar_EG.UTF-8")
        window.language_list.select_row(arabic)
        self.assertEqual(Gtk.Widget.get_default_direction(), Gtk.TextDirection.RTL)
        english = next(r for r in window.language_rows if r.value == "en_US.UTF-8")
        window.language_list.select_row(english)
        self.assertEqual(Gtk.Widget.get_default_direction(), Gtk.TextDirection.LTR)


class Untranslated(unittest.TestCase):
    def test_a_message_without_a_translation_is_left_out_of_the_catalog_so_that_the_english_text_shows(self):
        import tempfile
        spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            po = Path(tmp) / "xx.po"
            po.write_text('msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\nPlural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
                          'msgid "Done"\nmsgstr "Fertig"\n\nmsgid "New"\nmsgstr ""\n')
            mo = Path(tmp) / "xx" / "LC_MESSAGES" / "d.mo"
            module.compile_po(po, mo)
            with mo.open("rb") as stream:
                catalog = gettext.GNUTranslations(stream)
            self.assertEqual((catalog.gettext("Done"), catalog.gettext("New")), ("Fertig", "New"))
