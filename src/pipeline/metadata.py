"""Metadata tracking: each run writes a JSON record to storage."""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .storage import Storage


@contextmanager
def track_run(storage: Storage, source: str, **context):
    started = datetime.now(timezone.utc)
    run = {
        "run_id": str(uuid.uuid4()),
        "source": source,
        **context,
        "ingestion_timestamp": started.isoformat(timespec="seconds"),
        "status": "running",
        "metrics": {},
    }
    t0 = time.monotonic()
    try:
        yield run
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = repr(exc)
        raise
    else:
        run["status"] = "success"
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run["duration_seconds"] = round(time.monotonic() - t0, 3)
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        key = f"metadata/runs/source={source}/{stamp}_{run['run_id'][:8]}.json"
        storage.put_json(key, run)
