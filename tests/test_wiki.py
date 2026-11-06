"""The documentation wiki generator (scripts/build-wiki): the Markdown converter and the site it builds."""

import importlib.machinery
import importlib.util
import os
import re
import sys
import unittest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "..", "scripts", "build-wiki")
sys.path.insert(0, HERE)


def load():
    if not os.path.isfile(SCRIPT):
        raise unittest.SkipTest("not in the source tree")          # the RPM build has only the foundation/ directory
    loader = importlib.machinery.SourceFileLoader("build_wiki", SCRIPT)
    spec = importlib.util.spec_from_loader("build_wiki", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Converter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wiki = load()

    def md(self, text, resolve=lambda target: None):
        return self.wiki.Converter(resolve).convert(text)

    def test_headings_paragraphs_emphasis_and_code(self):
        out = self.md("# Title\n\nSome **bold**, *italic* and `code with <b> & |pipes|`.\nA second line.\n")
        self.assertIn('<h1 id="title">Title</h1>', out)
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>italic</em>", out)
        self.assertIn("<code>code with &lt;b&gt; &amp; |pipes|</code>", out)
        self.assertIn("italic</em> and", out)
        self.assertIn("A second line.", out)

    def test_html_in_the_text_is_escaped(self):
        out = self.md("A <script>alert(1)</script> and ecp-root_<version>.")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_underscores_in_identifiers_are_not_italics(self):
        self.assertNotIn("<em>", self.md("ecp_root_0 and some_name_here and 2*3*4"))

    def test_fenced_code_is_kept_verbatim(self):
        out = self.md("```sh\nfoundation status   # <ok>\n  indented\n```\n")
        self.assertIn("<pre>", out)
        self.assertIn("foundation status   # &lt;ok&gt;\n  indented", out)

    def test_tables_keep_pipes_inside_code_and_escaped_pipes(self):
        out = self.md("| A | B |\n| --- | :---: |\n| `x|y` | plain \\| pipe |\n| two | cells |\n")
        self.assertEqual(out.count("<tr>"), 3)
        self.assertIn("<td><code>x|y</code></td>", out)
        self.assertIn('<td style="text-align:center">plain | pipe</td>', out)
        self.assertIn('<th style="text-align:center">B</th>', out)

    def test_lists_with_wrapped_lines_and_nesting(self):
        out = self.md("- first item\n  continues here\n- second\n  - nested one\n  - nested two\n- third\n\n1. one\n2. two\n")
        self.assertIn("<li>first item continues here</li>", out)
        self.assertIn("<ul><li>nested one</li><li>nested two</li></ul>", out)
        self.assertEqual(out.count("<ol>"), 1)
        self.assertEqual(out.count("<ul>"), 2)

    def test_links_are_resolved_or_shown_as_plain_text(self):
        resolve = lambda target: "#/page" if target == "other.md" else None
        out = self.md("See [the page](other.md), [a file](../foundation/x.py) and [the web](https://example.org/a?b=1&c=2).", resolve)
        self.assertIn('<a href="#/page">the page</a>', out)
        self.assertIn('<span class="filelink" title="../foundation/x.py">a file</span>', out)
        self.assertIn('href="https://example.org/a?b=1&amp;c=2"', out)

    def test_explicit_anchors_and_duplicate_headings_get_unique_ids(self):
        out = self.md('## <a id="known-gap"></a>Known gap\n\n## Same\n\n## Same\n')
        self.assertIn('<h2 id="known-gap">Known gap</h2>', out)
        self.assertIn('<h2 id="same">', out)
        self.assertIn('<h2 id="same-1">', out)

    def test_status_badges(self):
        badge = self.wiki.badge
        self.assertEqual(badge("# T\n\n**Status:** built. Tested.\n")[1], "ok")
        self.assertEqual(badge("**Status:** partly built. x\n")[1], "mid")
        self.assertEqual(badge("**Status: scaffolding only.** The pieces\n")[1], "bad")
        self.assertEqual(badge("**Status:** the package list is built; the rest is not\n")[1], "ok")
        self.assertIsNone(badge("# No status here\n"))


class Site(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wiki = load()
        cls.document, cls.problems = cls.wiki.build()

    def test_every_page_of_the_documentation_is_included_with_a_title(self):
        pages = self.wiki.page_list()
        self.assertGreaterEqual(len(pages), 20)
        for pid, _src, _group in pages:
            self.assertIn(f'data-page="{pid}"', self.document)
        self.assertEqual(len(re.findall(r'<section class="page"', self.document)), len(pages))
        self.assertNotIn('data-title=""', self.document)

    def test_every_in_page_link_leads_to_a_page_that_exists(self):
        ids = {pid for pid, _s, _g in self.wiki.page_list()}
        content = self.document.split("<main>", 1)[1].split("</main>", 1)[0]
        targets = re.findall(r'href="#/([^":]+)', content)
        self.assertGreater(len(targets), 20)
        for target in targets:
            self.assertIn(target, ids)

    def test_features_follow_the_order_of_the_overview_table(self):
        features = [p for p, _s, g in self.wiki.page_list() if g == "Features"]
        overview = open(os.path.join(HERE, "..", "..", "docs", "README.md"), encoding="utf-8").read()
        table = overview.split("\n## Features", 1)[1].split("\n## ", 1)[0]
        listed = list(dict.fromkeys(re.findall(r"\(features/([a-z0-9-]+)\.md\)", table)))
        self.assertEqual(features[:len(listed)], [f"features/{n}" for n in listed])

    def test_nothing_from_outside_the_site_is_loaded(self):
        self.assertNotRegex(self.document, r'(src|href)="https?://(?!github)[^"]*\.(js|css)')
        self.assertNotIn("<link ", self.document)
        self.assertNotIn("<script src", self.document)

    def test_the_unfinished_features_are_marked(self):
        section = self.document.split('data-page="features/updates"', 1)[1].split("</section>", 1)[0]
        self.assertIn('class="badge bad"', section)

    def test_the_site_is_written_to_the_requested_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "sub", "index.html")
            self.wiki.build(target)
            with open(target, encoding="utf-8") as handle:
                self.assertIn("<title>Ersa Compute Platform wiki</title>", handle.read())


if __name__ == "__main__":
    unittest.main()
