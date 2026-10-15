"""Incremental crawler for the inflation-by-country table (Wikipedia).

Flow:
  1. downloads the HTML and finds the right table by its HEADER (not by position on the page)
  2. hashes the raw table content; if equal to the checkpoint, stops
  3. otherwise writes RAW (original HTML) -> cleans/normalizes -> PROCESSED (long format, upsert)

HTML inconsistencies handled (counted in the run metrics):
  - <sup> footnotes and [a], [1] markers
  - hidden text (display:none) and duplicated names ("New Caledonia New Caledonia")
  - empty cells, 'n/a', dashes and other missing-data markers
  - Unicode minus sign (U+2212) and '-0.0' (negative zero)
  - rows with a different number of columns than the header
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

from .config import Settings, get_settings
from .http import build_session
from .incremental import upsert
from .metadata import track_run
from .storage import Storage, get_storage
from .tabular import from_csv_bytes, to_csv_bytes

log = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_countries_by_inflation_rate"
SOURCE_ID = "wikipedia_crawler"
DATASET = "inflation"

FIELDS = ["country_name", "year", "inflation_pct", "wiki_revision_id", "ingested_at"]
CASTS = {"year": int, "inflation_pct": float}

MISSING_TOKENS = {"", "-", "\u2013", "\u2014", "n/a", "na", "no data", "...", "\u2026"}
_FOOTNOTE_RE = re.compile(r"\[[^\]]*\]")
_YEAR_RE = re.compile(r"^\d{4}$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_REVISION_RE = re.compile(r'"wgRevisionId"\s*:\s*(\d+)')


# ---------------------------------------------------------------- cell cleaning
def _is_hidden(tag) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def cell_text(cell, stats: Optional[dict] = None) -> str:
    """Visible cell text, without <sup> (footnotes) or hidden elements."""
    cell = copy.copy(cell)  # does not mutate the original tree
    for sup in cell.find_all("sup"):
        sup.decompose()
        if stats is not None:
            stats["footnotes_removed"] = stats.get("footnotes_removed", 0) + 1
    for hidden in [t for t in cell.find_all(True) if _is_hidden(t)]:
        hidden.decompose()
        if stats is not None:
            stats["hidden_text_removed"] = stats.get("hidden_text_removed", 0) + 1
    text = unicodedata.normalize("NFKC", cell.get_text(" ", strip=True))  # NFKC: nbsp -> space
    text = _FOOTNOTE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_country_name(text: str, stats: Optional[dict] = None) -> str:
    tokens = text.split()
    half = len(tokens) // 2
    if len(tokens) >= 2 and len(tokens) % 2 == 0 and tokens[:half] == tokens[half:]:
        if stats is not None:
            stats["duplicated_names_fixed"] = stats.get("duplicated_names_fixed", 0) + 1
        return " ".join(tokens[:half])
    return text


def parse_value(text: str, stats: Optional[dict] = None) -> Optional[float]:
    """Converts text to float. Returns None for missing/unreadable data."""
    t = text.strip().replace("\u2212", "-").replace("%", "").replace(",", "").replace(" ", "")
    t = re.sub(r"^\u2013(?=\d)", "-", t)  # en dash used as a minus sign
    if t.lower() in MISSING_TOKENS:
        return None
    if not _NUMBER_RE.match(t):
        if stats is not None:
            stats["unparseable_values"] = stats.get("unparseable_values", 0) + 1
        return None
    value = float(t)
    return 0.0 if value == 0 else value  # removes negative zero (-0.0)


# ---------------------------------------------------------------- table location
def find_table(soup: BeautifulSoup):
    """Finds the table whose header is 'Country' + year columns (resists layout changes)."""
    for table in soup.find_all("table"):
        header_row = next((tr for tr in table.find_all("tr") if tr.find("th")), None)
        if header_row is None:
            continue
        headers = [cell_text(c) for c in header_row.find_all(["th", "td"])]
        years = [h for h in headers if _YEAR_RE.match(h)]
        if headers and headers[0].lower().startswith("country") and len(years) >= 3:
            return table, header_row, headers
    raise RuntimeError("Inflation table not found: the page layout may have changed")


def extract_raw_cells(table) -> list[list[str]]:
    """Raw text (uncleaned) of every cell: basis of the change hash."""
    return [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")]


def parse_table(table, header_row, headers: list[str]) -> tuple[list[dict], dict]:
    """Wide table (country x years) -> long format [{country_name, year, inflation_pct}]."""
    stats: dict = {"rows_in_table": 0, "malformed_rows_skipped": 0, "empty_cells_dropped": 0,
                   "footnotes_removed": 0, "hidden_text_removed": 0,
                   "duplicated_names_fixed": 0, "unparseable_values": 0}
    year_cols = [(i, int(h)) for i, h in enumerate(headers) if _YEAR_RE.match(h)]
    out: list[dict] = []
    for tr in table.find_all("tr"):
        if tr is header_row:
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        stats["rows_in_table"] += 1
        if len(cells) != len(headers):
            stats["malformed_rows_skipped"] += 1
            continue
        country = clean_country_name(cell_text(cells[0], stats), stats)
        if not country:
            stats["malformed_rows_skipped"] += 1
            continue
        for idx, year in year_cols:
            value = parse_value(cell_text(cells[idx], stats), stats)
            if value is None:
                stats["empty_cells_dropped"] += 1
                continue
            out.append({"country_name": country, "year": year, "inflation_pct": value})
    return out, stats


# ---------------------------------------------------------------- execution
def run(settings: Optional[Settings] = None, session=None,
        storage: Optional[Storage] = None, force: bool = False, url: str = WIKI_URL) -> dict:
    settings = settings or get_settings()
    storage = storage or get_storage(settings)
    session = session or build_session(settings.user_agent)
    ckpt_key = f"metadata/checkpoints/{SOURCE_ID}_{DATASET}.json"

    with track_run(storage, SOURCE_ID, dataset=DATASET, url=url) as run_info:
        metrics = run_info["metrics"]
        now = datetime.now(timezone.utc)
        ingested_at = now.isoformat(timespec="seconds")
        ingest_date = now.strftime("%Y-%m-%d")
        run_tag = run_info["run_id"][:8]

        # 1) download the page
        resp = session.get(url, timeout=settings.http_timeout)
        resp.raise_for_status()
        html_bytes: bytes = resp.content
        html_text: str = resp.text
        metrics["html_bytes"] = len(html_bytes)

        # 2) find the table and decide whether anything changed (hash of the raw table content)
        soup = BeautifulSoup(html_text, "html.parser")
        table, header_row, headers = find_table(soup)
        table_hash = hashlib.sha256(
            json.dumps(extract_raw_cells(table), ensure_ascii=False).encode("utf-8")).hexdigest()
        rev = _REVISION_RE.search(html_text)
        revision_id = rev.group(1) if rev else ""
        metrics.update(table_hash=table_hash[:16], wiki_revision_id=revision_id, skipped=False)

        ckpt = storage.get_json(ckpt_key) or {}
        if not force and ckpt.get("table_hash") == table_hash:
            metrics.update(skipped=True, reason="table unchanged since the last checkpoint")
            log.info("Table unchanged. Nothing to do.")
            return run_info

        # 3) RAW: HTML exactly as received
        storage.put_bytes(
            f"raw/wikipedia/{DATASET}/ingest_date={ingest_date}/run={run_tag}/page.html", html_bytes)

        # 4) PROCESSED: cleaning + long format + upsert
        clean, stats = parse_table(table, header_row, headers)
        for r in clean:
            r["wiki_revision_id"] = revision_id
            r["ingested_at"] = ingested_at
        proc_key = f"processed/wikipedia/{DATASET}_by_country_year.csv"
        existing_bytes = storage.get_bytes(proc_key)
        existing = from_csv_bytes(existing_bytes, CASTS) if existing_bytes else []
        merged, counts = upsert(existing, clean, ("country_name", "year"), "inflation_pct")
        if counts["inserted"] or counts["updated"] or existing_bytes is None:
            storage.put_bytes(proc_key, to_csv_bytes(merged, FIELDS))

        # 5) checkpoint (only advances if everything above succeeded)
        storage.put_json(ckpt_key, {
            "table_hash": table_hash,
            "wiki_revision_id": revision_id,
            "last_success_at": ingested_at,
            "last_run_id": run_info["run_id"],
        })

        years = sorted({r["year"] for r in clean})
        metrics.update(
            countries_parsed=len({r["country_name"] for r in clean}),
            rows_fetched=len(clean), rows_total_processed=len(merged),
            year_min=years[0] if years else None, year_max=years[-1] if years else None,
            **stats, **{f"rows_{k}": v for k, v in counts.items()},
        )
        log.info("Wikipedia OK: %s", metrics)
        return run_info


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental Wikipedia crawler (inflation)")
    parser.add_argument("--force", action="store_true", help="ignore the checkpoint and reprocess the table")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(force=args.force), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
