"""Runs the full pipeline locally: python run_all.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from pipeline.orchestrator import main  # noqa: E402

if __name__ == "__main__":
    main()
