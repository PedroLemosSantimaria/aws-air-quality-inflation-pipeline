"""Shortcut: python run_analysis.py  (generates analytics/report.html from the curated dataset)"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from pipeline.analysis import main  # noqa: E402

if __name__ == "__main__":
    main()
