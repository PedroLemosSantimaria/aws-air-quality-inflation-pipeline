import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipeline import worldbank
from pipeline.config import Settings
from pipeline.storage import LocalStorage


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Simulates the World Bank API without network."""

    COUNTRIES = [
        {"id": "BRA", "iso2Code": "BR", "name": "Brazil", "region": {"value": "Latin America & Caribbean "},
         "incomeLevel": {"value": "Upper middle income"}},
        {"id": "USA", "iso2Code": "US", "name": "United States", "region": {"value": "North America"},
         "incomeLevel": {"value": "High income"}},
        {"id": "CHN", "iso2Code": "CN", "name": "China", "region": {"value": "East Asia & Pacific"},
         "incomeLevel": {"value": "Upper middle income"}},
        {"id": "WLD", "iso2Code": "1W", "name": "World", "region": {"value": "Aggregates"},
         "incomeLevel": {"value": "Aggregates"}},
    ]

    def __init__(self):
        self.lastupdated = "2025-01-01"
        self.obs = {("BRA", 2019): 12.5, ("BRA", 2020): 11.9, ("USA", 2020): 7.4,
                    ("WLD", 2020): 40.0, ("CHN", 2020): None}
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        page, per_page = params["page"], params["per_page"]
        if url.endswith("/country"):
            return FakeResponse([{"page": 1, "pages": 1, "total": 4}, self.COUNTRIES])
        rows = [{"indicator": {"id": "EN.ATM.PM25.MC.M3"}, "country": {"value": "X "},
                 "countryiso3code": iso, "date": str(year), "value": val, "unit": "", "obs_status": ""}
                for (iso, year), val in self.obs.items()]
        pages = -(-len(rows) // per_page)
        chunk = rows[(page - 1) * per_page: page * per_page]
        meta = {"page": page, "pages": pages, "per_page": per_page, "total": len(rows),
                "lastupdated": self.lastupdated}
        return FakeResponse([meta, chunk])


class WorldBankTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(self.tmp.name)
        self.settings = Settings("local", "b", "us-east-1", self.tmp.name,
                                 "EN.ATM.PM25.MC.M3", 2010, 5)
        self.session = FakeSession()
        worldbank.PER_PAGE = 2  # force pagination with little data
        self.addCleanup(setattr, worldbank, "PER_PAGE", 1000)

    def _run(self, **kw):
        return worldbank.run(self.settings, self.session, self.storage, **kw)

    def test_first_run_paginates_filters_and_loads(self):
        m = self._run()["metrics"]
        self.assertEqual(m["pages_fetched"], 3)
        self.assertEqual(m["rows_inserted"], 3)
        self.assertEqual(m["aggregates_filtered"], 1)
        self.assertEqual(m["null_values_dropped"], 1)
        self.assertEqual(m["rows_total_processed"], 3)

    def test_second_run_skips_when_source_unchanged(self):
        self._run()
        self.assertTrue(self._run()["metrics"]["skipped"])

    def test_revision_updates_without_duplicates(self):
        self._run()
        self.session.lastupdated = "2025-02-01"
        self.session.obs[("BRA", 2020)] = 10.0
        m = self._run()["metrics"]
        self.assertEqual((m["rows_inserted"], m["rows_updated"], m["rows_unchanged"]), (0, 1, 2))
        self.assertEqual(m["rows_total_processed"], 3)

    def test_new_year_is_inserted_incrementally(self):
        self._run()
        self.session.lastupdated = "2025-03-01"
        self.session.obs[("BRA", 2021)] = 11.0
        m = self._run()["metrics"]
        self.assertEqual(m["rows_inserted"], 1)
        self.assertEqual(m["max_year_loaded"], 2021)

    def test_metadata_and_layers_written(self):
        self._run()
        runs = self.storage.list_keys("metadata/runs/source=worldbank_api/")
        self.assertEqual(len(runs), 1)
        rec = self.storage.get_json(runs[0])
        for field in ("run_id", "source", "ingestion_timestamp", "duration_seconds", "status", "metrics"):
            self.assertIn(field, rec)
        self.assertEqual(rec["status"], "success")
        self.assertTrue(self.storage.list_keys("raw/worldbank/indicator="))
        self.assertIsNotNone(self.storage.get_bytes("processed/worldbank/EN.ATM.PM25.MC.M3.csv"))

    def test_api_error_marks_run_failed(self):
        class Broken(FakeSession):
            def get(self, url, params=None, timeout=None):
                return FakeResponse([{"message": [{"id": "120", "value": "Invalid value"}]}])

        with self.assertRaises(RuntimeError):
            worldbank.run(self.settings, Broken(), self.storage)
        rec = self.storage.get_json(self.storage.list_keys("metadata/runs/")[0])
        self.assertEqual(rec["status"], "failed")


if __name__ == "__main__":
    unittest.main()
