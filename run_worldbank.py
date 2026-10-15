"""Shortcut to run the World Bank ingestion locally: python run_worldbank.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from pipeline.worldbank import main  # noqa: E402

if __name__ == "__main__":
    main()
