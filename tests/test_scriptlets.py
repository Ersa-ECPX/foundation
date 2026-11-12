"""Reading what an RPM's install scripts do, and translating accounts and services."""

import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundationd.backends import scriptlets  # noqa: E402

POST = "postinstall scriptlet (using /bin/sh):\n"
PRE = "preinstall scriptlet (using /bin/sh):\n"


class Accounts(unittest.TestCase):
    def parse(self, text):
        lines, unparsed, _homes = scriptlets.parse_accounts(text)
        return lines, unparsed

    def test_the_usual_pattern_with_getent_guards(self):
        text = ("getent group foo >/dev/null || groupadd -r foo\n"
                'getent passwd foo >/dev/null || useradd -r -g foo -d /var/lib/foo -s /sbin/nologin -c "Foo daemon" foo\n')
        self.assertEqual(self.parse(text), (["g foo -", 'u foo -:foo "Foo daemon" /var/lib/foo /sbin/nologin'], []))

    def test_groups_come_first_then_users_then_memberships_without_duplicates(self):
        text = "useradd -r bar\nusermod -a -G video bar\ngroupadd -r bar\ngroupadd -r bar\nuseradd -r -G audio,video baz\n"
        lines, unparsed = self.parse(text)
        self.assertEqual(unparsed, [])
        self.assertEqual(lines, ["g bar -", 'u bar - "" - -', 'u baz - "" - -', "m bar video", "m baz audio", "m baz video"])

    def test_long_options_ids_and_paths_to_the_tools(self):
        lines, unparsed = self.parse("/usr/sbin/groupadd --system --gid 123 x\n/usr/sbin/useradd --system --uid 123 --gid x "
                                     "--home-dir /srv/x --shell /bin/false x\n")
        self.assertEqual(unparsed, [])
        self.assertEqual(lines, ["g x 123", 'u x 123:x "" /srv/x /bin/false'])

    def test_what_cannot_be_said_exactly_is_left_alone(self):
        for command in ("useradd human", "useradd -r -o x", "useradd -r -Z x", "useradd -r BadName", "groupadd -r ../x",
                        "useradd -r -d '/var/lib/x y' x", "usermod -s /bin/sh x", "useradd -r -u abc x"):
            lines, unparsed = self.parse(command + "\n")
            self.assertEqual(lines, [], command)
            self.assertEqual(len(unparsed), 1, command)

    def test_a_home_directory_is_reported_for_tmpfiles_since_sysusers_cannot_make_one(self):
        lines, unparsed, homes = scriptlets.parse_accounts("useradd -r -m -g g -d /var/lib/x x\nuseradd -r -m y\n")
        self.assertEqual(unparsed, [])
        self.assertEqual(homes, [("x", "g", "/var/lib/x"), ("y", "y", "/home/y")])

    def test_a_comment_cannot_break_out_of_its_quotes(self):
        lines, _ = self.parse('useradd -r -c "a\\" b" x\n')
        self.assertEqual(lines, ['u x - "a b" - -'])


class Services(unittest.TestCase):
    def test_presets_and_enables_are_found_and_stops_and_disables_are_not(self):
        text = ("/usr/bin/systemctl preset foo.service foo.socket >/dev/null 2>&1 || :\n"
                "systemctl enable bar.timer\n"
                "systemctl disable --now old.service\n"
                "systemctl stop busy.service\n"
                "systemctl daemon-reload\n")
        self.assertEqual(scriptlets.service_units(text), ["foo.service", "foo.socket", "bar.timer"])


class Analyse(unittest.TestCase):
    def test_install_sections_only(self):
        text = ("preuninstall scriptlet (using /bin/sh):\nsystemctl disable --now bar.service\nuserdel x\n"
                "postuninstall scriptlet (using /bin/sh):\nglib-compile-schemas /x\n")
        self.assertEqual(scriptlets.analyse(text), {"effects": [], "sysusers": [], "units": [], "homes": [], "link_share": False})

    def test_what_is_left_after_translation(self):
        cases = {
            "": [],
            POST + "/sbin/ldconfig\n": [],
            POST + "exit 0\n": [],
            PRE + "useradd -r x\n": [],
            PRE + "useradd human\n": ["users"],
            POST + "systemctl preset a.service\n": [],
            POST + "systemctl start something\n": ["services"],
            POST + "glib-compile-schemas /usr/share/glib-2.0/schemas\n": [],
            POST + "gtk-update-icon-cache -f /usr/share/icons/hicolor\n": [],
            POST + "update-mime-database /usr/share/mime\n": ["caches"],
            POST + "update-alternatives --install /usr/bin/x x /usr/bin/y 1\n": ["alternatives"],
            POST + "my-own-tool --init\n": ["other"],
            POST + "getent passwd x >/dev/null\n": [],
        }
        for text, expected in cases.items():
            self.assertEqual(scriptlets.analyse(text)["effects"], expected, text)

    def test_the_translated_parts_are_returned(self):
        found = scriptlets.analyse(PRE + "groupadd -r g\nuseradd -r -g g u\n" + POST + "systemctl preset u.service\n")
        self.assertEqual(found["sysusers"], ["g g -", 'u u -:g "" - -'])
        self.assertEqual(found["units"], ["u.service"])


L10N = ("posttrans scriptlet (using /bin/sh):\nrpm -ql libreoffice-l10n-en > /usr/share/libreoffice/l10n-en_list.txt || true \n"
        "if [ -f /usr/share/libreoffice/l10n-en_list.txt ] ; then \n"
        "    /usr/bin/libreoffice-share-linker /usr/share/libreoffice/l10n-en_list.txt || true \nfi\n"
        "postuninstall scriptlet (using /bin/sh):\nif [ \"$1\" = \"0\" ]; then\n"
        "    /usr/bin/libreoffice-share-linker --unlink /usr/share/libreoffice/l10n-en_list.txt || true\nfi\n")


class LibreOfficeLanguage(unittest.TestCase):
    def test_the_language_pack_script_is_recognised_and_not_reported(self):
        found = scriptlets.analyse(L10N)
        self.assertTrue(found["link_share"])
        self.assertEqual(found["effects"], [])

    def test_removal_alone_does_not_count(self):
        self.assertFalse(scriptlets.analyse("postuninstall scriptlet (using /bin/sh):\n"
                                            "/usr/bin/libreoffice-share-linker --unlink /x\n")["link_share"])

    def test_the_files_are_linked_where_the_program_looks(self):
        import tempfile
        listing = ("/usr/share/libreoffice\n/usr/share/libreoffice/share/registry\n"
                   "/usr/share/libreoffice/share/registry/Langpack-en-US.xcd\n/usr/share/libreoffice/help/en-US\n"
                   "/usr/share/doc/other\n")
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "usr/lib64/libreoffice/program"))
            os.makedirs(os.path.join(tree, "usr/share/libreoffice/share/registry"))
            os.makedirs(os.path.join(tree, "usr/share/libreoffice/help/en-US"))
            with open(os.path.join(tree, "usr/share/libreoffice/share/registry/Langpack-en-US.xcd"), "w") as handle:
                handle.write("x")
            made = scriptlets.link_libreoffice_share(tree, listing)
            link = os.path.join(tree, "usr/lib64/libreoffice/share/registry/Langpack-en-US.xcd")
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), "/usr/share/libreoffice/share/registry/Langpack-en-US.xcd")
            self.assertTrue(os.path.isdir(os.path.join(tree, "usr/lib64/libreoffice/help/en-US")))
            self.assertFalse(os.path.exists(os.path.join(tree, "usr/lib/libreoffice")), "only libdirs that have libreoffice")
            self.assertFalse(os.path.exists(os.path.join(tree, "usr/lib64/doc")))
            self.assertEqual(made, 3)
            self.assertEqual(scriptlets.link_libreoffice_share(tree, listing), 0, "running it again changes nothing")


if __name__ == "__main__":
    unittest.main()
