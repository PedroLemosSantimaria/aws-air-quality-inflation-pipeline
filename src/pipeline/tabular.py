"""Helpers to read/write CSV as bytes (no pandas dependency in ingestion)."""
import csv
import io
from typing import Callable, Optional


def to_csv_bytes(rows: list[dict], fieldnames: list[str]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def from_csv_bytes(data: bytes, casts: Optional[dict[str, Callable]] = None) -> list[dict]:
    casts = casts or {}
    rows = []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8"))):
        for col, fn in casts.items():
            if col in row:
                row[col] = fn(row[col]) if row[col] != "" else None
        rows.append(row)
    return rows
