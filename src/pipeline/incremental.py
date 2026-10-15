"""Shared incremental logic: idempotent upsert by business key."""
from __future__ import annotations

from typing import Sequence


def upsert(existing: list[dict], new: list[dict], key_fields: Sequence[str],
           value_field: str) -> tuple[list[dict], dict]:
    """Merges 'new' into 'existing' using key_fields as the key.

    - unknown key       -> inserted
    - different value   -> updated (source revision)
    - same value        -> unchanged
    Re-running with the same data changes and duplicates nothing (idempotency).
    """
    def key(row: dict) -> tuple:
        return tuple(row[f] for f in key_fields)

    index = {key(r): r for r in existing}
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for r in new:
        old = index.get(key(r))
        if old is None:
            counts["inserted"] += 1
            index[key(r)] = r
        elif old[value_field] != r[value_field]:
            counts["updated"] += 1
            index[key(r)] = r
        else:
            counts["unchanged"] += 1
    return sorted(index.values(), key=key), counts
