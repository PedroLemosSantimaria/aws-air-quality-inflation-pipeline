import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipeline import analysis, curate, orchestrator, wikipedia, worldbank
from pipeline.config import Settings
from pipeline.storage import LocalStorage

SETTINGS = Settings("local", "b", "us-east-1", "unused", "X", 2010, 5)


def fake(name, calls):
    def _run(*args, **kwargs):
        calls.append(name)
        return {"run_id": name, "status": "success", "metrics": {"skipped": name == "wikipedia"}}
    return _run


class OrchestratorTests(unittest.TestCase):
    def test_runs_stages_in_order(self):
        calls = []
        with mock.patch.object(worldbank, "run", fake("worldbank", calls)), \
             mock.patch.object(wikipedia, "run", fake("wikipedia", calls)), \
             mock.patch.object(curate, "run", fake("curation", calls)), \
             mock.patch.object(analysis, "run", fake("analysis", calls)):
            out = orchestrator.run_all(SETTINGS, LocalStorage("unused"))
        self.assertEqual(calls, ["worldbank", "wikipedia", "curation", "analysis"])
        self.assertTrue(out["wikipedia"]["skipped"])
        self.assertFalse(out["worldbank"]["skipped"])

    def test_failure_stops_following_stages(self):
        calls = []

        def boom(*a, **k):
            raise RuntimeError("api down")

        with mock.patch.object(worldbank, "run", fake("worldbank", calls)), \
             mock.patch.object(wikipedia, "run", boom), \
             mock.patch.object(curate, "run", fake("curation", calls)), \
             mock.patch.object(analysis, "run", fake("analysis", calls)):
            with self.assertRaises(RuntimeError):
                orchestrator.run_all(SETTINGS, LocalStorage("unused"))
        self.assertEqual(calls, ["worldbank"])  # curation and analysis did not run

    def test_lambda_handler_returns_summary_and_reads_force(self):
        with mock.patch.object(orchestrator, "run_all", return_value={"x": 1}) as ra:
            out = orchestrator.lambda_handler({"force": True}, None)
        ra.assert_called_once_with(force=True)
        self.assertEqual(out, {"status": "success", "stages": {"x": 1}})


if __name__ == "__main__":
    unittest.main()
