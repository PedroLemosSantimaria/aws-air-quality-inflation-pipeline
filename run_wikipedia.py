"""Shortcut to run the Wikipedia crawler locally: python run_wikipedia.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from pipeline.wikipedia import main  # noqa: E402

if __name__ == "__main__":
    main()
