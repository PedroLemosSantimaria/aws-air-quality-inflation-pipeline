"""Shortcut: python run_curate.py  (joins World Bank + Wikipedia)"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from pipeline.curate import main  # noqa: E402

if __name__ == "__main__":
    main()
