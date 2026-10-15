import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipeline import analysis, curate, wikipedia, worldbank
from pipeline.config import Settings
from pipeline.storage import LocalStorage
from pipeline.tabular import from_csv_bytes, to_csv_bytes

IND = "EN.ATM.PM25.MC.M3"
COUNTRIES = [
    ("BRA", "Brazil", "Latin America & Caribbean", "Upper middle income"),
    ("USA", "United States", "North America", "High income"),
    ("TUR", "Turkiye", "Europe & Central Asia", "Upper middle income"),
    ("EGY", "Egypt, Arab Rep.", "Middle East & North Africa", "Lower middle income"),
    ("CIV", "Cote d'Ivoire", "Sub-Saharan Africa", "Lower middle income"),
]
WIKI_NAMES = {"BRA": "Brazil", "USA": "United States of America", "TUR": "Turkey",
              "EGY": "Egypt", "CIV": "Ivory Coast"}


def seed(storage, wiki_extra=None, years=(2020, 2021, 2022)):
    storage.put_bytes("processed/worldbank/countries.csv", to_csv_bytes(
        [dict(country_iso3=i, country_iso2="", country_name=n, region=r, income_level=inc)
         for i, n, r, inc in COUNTRIES], worldbank.COUNTRY_FIELDS))
    pm = [dict(country_iso3=i, country_name=n, indicator_id=IND, year=y, value=10.0 + k * 5 + y % 10,
               unit="", obs_status="", source_lastupdated="x", ingested_at="t")
          for k, (i, n, _, _) in enumerate(COUNTRIES) for y in (2020, 2021)]
    storage.put_bytes(f"processed/worldbank/{IND}.csv", to_csv_bytes(pm, worldbank.OBS_FIELDS))
    wiki = [dict(country_name=WIKI_NAMES[i], year=y, inflation_pct=2.0 + k * 3, wiki_revision_id="1", ingested_at="t")
            for k, (i, _, _, _) in enumerate(COUNTRIES) for y in years]
    wiki += (wiki_extra or []) + [dict(country_name="Atlantis", year=2020, inflation_pct=1.0,
                                       wiki_revision_id="1", ingested_at="t")]
    storage.put_bytes(curate.WIKI_KEY, to_csv_bytes(wiki, wikipedia.FIELDS))


class CurateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(self.tmp.name)
        self.settings = Settings("local", "b", "us-east-1", self.tmp.name, IND, 2010, 5)
        seed(self.storage)

    def test_normalize_and_alias_matching(self):
        self.assertEqual(curate.normalize_name("C\u00f4te d'Ivoire"), "cote d ivoire")
        idx = {curate.normalize_name(n): i for i, n, _, _ in COUNTRIES}
        valid = {c[0] for c in COUNTRIES}
        self.assertEqual(curate.match_country("Turkey", idx, valid), "TUR")           # alias
        self.assertEqual(curate.match_country("Brazil", idx, valid), "BRA")           # name
        self.assertIsNone(curate.match_country("Atlantis", idx, valid))

    def test_join_matches_names_and_reports_unmatched(self):
        m = curate.run(self.settings, self.storage)["metrics"]
        self.assertEqual(m["rows_curated"], 10)  # 5 countries x 2 common years
        self.assertEqual(m["wiki_countries_matched"], 5)
        self.assertEqual(m["wiki_countries_unmatched"], 1)
        self.assertEqual(m["unmatched_sample"], ["Atlantis"])
        rows = from_csv_bytes(self.storage.get_bytes(curate.CURATED_KEY))
        self.assertEqual({r["country_iso3"] for r in rows}, {"BRA", "USA", "TUR", "EGY", "CIV"})

    def test_skips_when_inputs_unchanged_and_reruns_when_changed(self):
        curate.run(self.settings, self.storage)
        self.assertTrue(curate.run(self.settings, self.storage)["metrics"]["skipped"])
        seed(self.storage, years=(2020, 2021))  # inputs change
        self.assertFalse(curate.run(self.settings, self.storage)["metrics"]["skipped"])

    def test_missing_inputs_fail_clearly(self):
        empty = LocalStorage(tempfile.mkdtemp())
        with self.assertRaises(FileNotFoundError):
            curate.run(self.settings, empty)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(self.tmp.name)
        self.settings = Settings("local", "b", "us-east-1", self.tmp.name, IND, 2010, 5)

    def _curated(self, monotone=True):
        rows = []
        for k in range(8):
            for y in (2019, 2020, 2021):
                rows.append(dict(country_iso3=f"C{k:02d}", country_name=f"Country {k}", region="R",
                                 income_level="High income" if k < 4 else "Low income", year=y,
                                 pm25_ugm3=10.0 + k * 3, inflation_pct=(1.0 + k if monotone else 9.0 - k) + y % 3 * 0.1,
                                 curated_at="t"))
        self.storage.put_bytes(curate.CURATED_KEY, to_csv_bytes(rows, curate.CURATED_FIELDS))

    def test_spearman_helpers(self):
        self.assertAlmostEqual(analysis.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(analysis.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertEqual(analysis._ranks([5, 5, 1]), [2.5, 2.5, 1.0])
        self.assertIsNone(analysis.spearman([1, 1, 1], [1, 2, 3]))  # constant: undefined

    def test_run_generates_all_artifacts_from_curated(self):
        self._curated()
        m = analysis.run(self.settings, self.storage)["metrics"]
        self.assertAlmostEqual(m["spearman"], 1.0, places=2)
        self.assertEqual(m["countries_in_correlation"], 8)
        for name in ("report.html", "summary.json", "scatter_pm25_vs_inflation.svg", "trend_pm25.svg", "trend_inflation.svg"):
            self.assertIsNotNone(self.storage.get_bytes(f"analytics/{name}"), name)
        report = self.storage.get_bytes("analytics/report.html").decode()
        self.assertIn("<svg", report)
        self.assertIn("strong positive", report)
        summary = self.storage.get_json("analytics/summary.json")
        self.assertEqual([g["group"] for g in summary["by_income"]], ["High income", "Low income"])

    def test_negative_relationship_is_described(self):
        self._curated(monotone=False)
        analysis.run(self.settings, self.storage)
        self.assertIn("strong negative", self.storage.get_bytes("analytics/report.html").decode())

    def test_missing_curated_fails_clearly(self):
        with self.assertRaises(FileNotFoundError):
            analysis.run(self.settings, self.storage)


if __name__ == "__main__":
    unittest.main()
