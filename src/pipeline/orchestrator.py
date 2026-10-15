"""Orchestration: runs the 4 stages in order (ingestions -> curation -> analysis).

Used by the local shortcut (run_all.py) and by AWS Lambda (lambda_handler).
If a stage fails, the exception propagates: the following stages do NOT run, the Lambda is marked
as failed (visible in CloudWatch) and the stage's record stays in metadata/runs/ with status=failed.
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Optional

from . import analysis, curate, wikipedia, worldbank
from .config import Settings, get_settings
from .storage import Storage, get_storage

log = logging.getLogger(__name__)


def run_all(settings: Optional[Settings] = None, storage: Optional[Storage] = None,
            force: bool = False) -> dict:
    settings = settings or get_settings()
    storage = storage or get_storage(settings)
    stages = [
        ("worldbank", lambda: worldbank.run(settings=settings, storage=storage, force=force)),
        ("wikipedia", lambda: wikipedia.run(settings=settings, storage=storage, force=force)),
        ("curation", lambda: curate.run(settings, storage, force=force)),
        ("analysis", lambda: analysis.run(settings, storage)),
    ]
    results: dict = {}
    for name, stage in stages:
        log.info("=== stage: %s ===", name)
        info = stage()
        m = info.get("metrics", {})
        results[name] = {"run_id": info.get("run_id"), "status": info.get("status"),
                         "skipped": bool(m.get("skipped", False))}
    return results


def lambda_handler(event, context):
    """AWS Lambda entry point. Optional event: {"force": true}."""
    logging.getLogger().setLevel(logging.INFO)
    force = bool((event or {}).get("force", False))
    return {"status": "success", "stages": run_all(force=force)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full pipeline")
    parser.add_argument("--force", action="store_true", help="ignore checkpoints and reprocess everything")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_all(force=args.force), indent=2))


if __name__ == "__main__":
    main()
