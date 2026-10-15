import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipeline import wikipedia
from pipeline.config import Settings
from pipeline.storage import LocalStorage

HTML_TEMPLATE = """<html><head><script>RLCONF={{"wgRevisionId":{rev}}};</script></head><body>
<table class="infobox"><tr><th>Quick facts</th><td>decoy</td></tr></table>
<table class="wikitable sortable">
<tr><th>Country</th><th>2024</th><th>2023</th><th>2022<sup>[a]</sup></th></tr>
<tr><td><a href="/wiki/Brazil">Brazil</a></td><td>4.4</td><td>{brazil_2023}</td><td>9.3</td></tr>
<tr><td>Argentina</td><td>219.9</td><td></td><td>72.43<sup class="reference">[1]</sup></td></tr>
<tr><td>New Caledonia New Caledonia</td><td></td><td></td><td>0.6</td></tr>
<tr><td>Japan</td><td>2.7</td><td>3.3</td><td>\u22120.0</td></tr>
<tr><td>Chile</td><td>\u22121.2</td><td>n/a</td><td>11.6</td></tr>
<tr><td>Peru<span style="display:none">Peru</span></td><td>1.0</td><td>1.0</td><td>1.0</td></tr>
<tr><td>Broken row</td><td>1.0</td></tr>
</table></body></html>"""


def make_html(rev=100, brazil_2023="4.6"):
    return HTML_TEMPLATE.format(rev=rev, brazil_2023=brazil_2023)


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, html):
        self.html = html

    def get(self, url, timeout=None):
        return FakeResponse(self.html)


class CleaningTests(unittest.TestCase):
    def test_parse_value_handles_symbols_and_missing(self):
        self.assertEqual(wikipedia.parse_value("4.6"), 4.6)
        self.assertEqual(wikipedia.parse_value("\u22121.2"), -1.2)
        self.assertEqual(wikipedia.parse_value("1,234.5%"), 1234.5)
        self.assertIsNone(wikipedia.parse_value(""))
        self.assertIsNone(wikipedia.parse_value("n/a"))
        self.assertIsNone(wikipedia.parse_value("\u2014"))
        zero = wikipedia.parse_value("-0.0")
        self.assertEqual(math.copysign(1, zero), 1.0)  # no negative zero

    def test_clean_country_name_dedup(self):
        self.assertEqual(wikipedia.clean_country_name("New Caledonia New Caledonia"), "New Caledonia")
        self.assertEqual(wikipedia.clean_country_name("South Korea"), "South Korea")
        self.assertEqual(wikipedia.clean_country_name("Papua New Guinea"), "Papua New Guinea")


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(self.tmp.name)
        self.settings = Settings("local", "b", "us-east-1", self.tmp.name, "X", 2010, 5)

    def _run(self, html, **kw):
        return wikipedia.run(self.settings, FakeSession(html), self.storage, **kw)

    def _processed(self):
        data = self.storage.get_bytes("processed/wikipedia/inflation_by_country_year.csv")
        from pipeline.tabular import from_csv_bytes
        return from_csv_bytes(data, wikipedia.CASTS)

    def test_first_run_parses_and_cleans_table(self):
        m = self._run(make_html())["metrics"]
        self.assertEqual(m["rows_inserted"], 14)
        self.assertEqual(m["countries_parsed"], 6)
        self.assertEqual(m["malformed_rows_skipped"], 1)
        self.assertEqual(m["duplicated_names_fixed"], 1)
        self.assertEqual(m["empty_cells_dropped"], 4)  # Argentina 2023, NC 2024/2023, Chile 2023
        self.assertGreaterEqual(m["footnotes_removed"], 1)
        self.assertEqual(m["wiki_revision_id"], "100")
        rows = {(r["country_name"], r["year"]): r["inflation_pct"] for r in self._processed()}
        self.assertEqual(rows[("Chile", 2024)], -1.2)
        self.assertEqual(rows[("Argentina", 2022)], 72.43)
        self.assertEqual(rows[("Peru", 2024)], 1.0)
        self.assertIn(("New Caledonia", 2022), rows)

    def test_second_run_skips_when_table_unchanged(self):
        self._run(make_html())
        self.assertTrue(self._run(make_html(rev=101))["metrics"]["skipped"])  # only the revision changed

    def test_changed_value_is_updated_not_duplicated(self):
        self._run(make_html())
        m = self._run(make_html(rev=102, brazil_2023="4.9"))["metrics"]
        self.assertEqual((m["rows_inserted"], m["rows_updated"], m["rows_unchanged"]), (0, 1, 13))
        self.assertEqual(m["rows_total_processed"], 14)

    def test_raw_html_and_metadata_are_stored(self):
        self._run(make_html())
        self.assertEqual(len(self.storage.list_keys("raw/wikipedia/inflation/")), 1)
        rec = self.storage.get_json(self.storage.list_keys("metadata/runs/source=wikipedia_crawler/")[0])
        self.assertEqual(rec["status"], "success")
        self.assertIn("duration_seconds", rec)

    def test_layout_change_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            self._run("<html><body><table><tr><th>Other</th></tr></table></body></html>")
        rec = self.storage.get_json(self.storage.list_keys("metadata/runs/")[0])
        self.assertEqual(rec["status"], "failed")


if __name__ == "__main__":
    unittest.main()
