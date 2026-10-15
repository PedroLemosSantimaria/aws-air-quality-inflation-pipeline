"""Incremental ingestion from the World Bank API.

Flow:
  1. reads the checkpoint (last year loaded + the source's 'lastupdated')
  2. requests page 1; if 'lastupdated' did not change, stops without reprocessing
  3. otherwise paginates the rest (only the necessary year window)
  4. writes RAW (original response) -> PROCESSED (normalized CSV, upsert) -> checkpoint
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Iterator, Optional

from .config import Settings, get_settings
from .http import build_session
from .incremental import upsert
from .metadata import track_run
from .storage import Storage, get_storage
from .tabular import from_csv_bytes, to_csv_bytes

log = logging.getLogger(__name__)

BASE_URL = "https://api.worldbank.org/v2"
SOURCE_ID = "worldbank_api"
PER_PAGE = 1000
REFRESH_YEARS = 2  # reload the last N years to capture source revisions

OBS_FIELDS = [
    "country_iso3", "country_name", "indicator_id", "year", "value",
    "unit", "obs_status", "source_lastupdated", "ingested_at",
]
OBS_CASTS = {"year": int, "value": float}
COUNTRY_FIELDS = ["country_iso3", "country_iso2", "country_name", "region", "income_level"]


# ---------------------------------------------------------------- HTTP / pagination
def _get(session, url: str, params: dict, timeout: int) -> tuple[dict, list]:
    resp = session.get(url, params={**params, "format": "json"}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Unexpected API response: {str(payload)[:200]}")
    meta = payload[0]
    if "message" in meta:  # the API returns errors with HTTP 200 and a 'message' field
        raise RuntimeError(f"World Bank API error: {meta['message']}")
    rows = payload[1] if len(payload) > 1 and payload[1] else []
    return meta, rows


def _paginate(session, url: str, params: dict, timeout: int) -> Iterator[tuple[int, dict, list]]:
    page = 1
    while True:
        meta, rows = _get(session, url, {**params, "per_page": PER_PAGE, "page": page}, timeout)
        yield page, meta, rows
        if page >= int(meta.get("pages", 1)):
            break
        page += 1


# ---------------------------------------------------------------- normalization
def normalize_countries(rows: list[dict]) -> list[dict]:
    """Keeps only real countries (the API mixes in aggregates such as 'World', 'Euro area')."""
    out = []
    for c in rows:
        if c["region"]["value"].strip() == "Aggregates":
            continue
        out.append({
            "country_iso3": c["id"],
            "country_iso2": c["iso2Code"],
            "country_name": c["name"].strip(),
            "region": c["region"]["value"].strip(),
            "income_level": c["incomeLevel"]["value"].strip(),
        })
    return sorted(out, key=lambda r: r["country_iso3"])


def normalize_observations(rows: list[dict], valid_iso3: set[str],
                           source_lastupdated: str, ingested_at: str) -> tuple[list[dict], dict]:
    stats = {"aggregates_filtered": 0, "null_values_dropped": 0}
    out = []
    for r in rows:
        iso3 = r.get("countryiso3code")
        if iso3 not in valid_iso3:
            stats["aggregates_filtered"] += 1
            continue
        if r.get("value") is None:
            stats["null_values_dropped"] += 1
            continue
        out.append({
            "country_iso3": iso3,
            "country_name": r["country"]["value"].strip(),
            "indicator_id": r["indicator"]["id"],
            "year": int(r["date"]),
            "value": float(r["value"]),
            "unit": r.get("unit") or "",
            "obs_status": r.get("obs_status") or "",
            "source_lastupdated": source_lastupdated,
            "ingested_at": ingested_at,
        })
    return out, stats


# ---------------------------------------------------------------- execution
def run(settings: Optional[Settings] = None, session=None,
        storage: Optional[Storage] = None, force: bool = False) -> dict:
    settings = settings or get_settings()
    storage = storage or get_storage(settings)
    session = session or build_session(settings.user_agent)
    indicator = settings.wb_indicator
    ckpt_key = f"metadata/checkpoints/{SOURCE_ID}_{indicator}.json"

    with track_run(storage, SOURCE_ID, indicator=indicator) as run_info:
        metrics = run_info["metrics"]
        now = datetime.now(timezone.utc)
        ingested_at = now.isoformat(timespec="seconds")
        ingest_date = now.strftime("%Y-%m-%d")
        run_tag = run_info["run_id"][:8]

        # 1) checkpoint -> year window to fetch
        ckpt = storage.get_json(ckpt_key) or {}
        start_year = settings.wb_start_year
        if ckpt.get("max_year_loaded"):
            start_year = max(start_year, ckpt["max_year_loaded"] - REFRESH_YEARS + 1)
        end_year = now.year
        metrics.update(start_year=start_year, end_year=end_year, skipped=False)

        # 2) the first page decides whether the source has news
        pages = _paginate(session, f"{BASE_URL}/country/all/indicator/{indicator}",
                          {"date": f"{start_year}:{end_year}"}, settings.http_timeout)
        first = next(pages)
        src_lastupdated = first[1].get("lastupdated", "")
        if not force and src_lastupdated and ckpt.get("source_lastupdated") == src_lastupdated:
            metrics.update(skipped=True, reason="source unchanged since the last checkpoint")
            log.info("Source unchanged (%s). Nothing to do.", src_lastupdated)
            return run_info

        # 3) RAW: store each page exactly as returned by the API
        all_pages = [first, *pages]
        raw_prefix = f"raw/worldbank/indicator={indicator}/ingest_date={ingest_date}/run={run_tag}"
        raw_rows: list[dict] = []
        for page_no, meta, rows in all_pages:
            storage.put_json(f"{raw_prefix}/page_{page_no:03d}.json", {"meta": meta, "data": rows})
            raw_rows.extend(rows)

        # 4) countries (dimension): raw + processed
        c_pages = list(_paginate(session, f"{BASE_URL}/country", {}, settings.http_timeout))
        c_raw: list[dict] = []
        for page_no, meta, rows in c_pages:
            storage.put_json(
                f"raw/worldbank/countries/ingest_date={ingest_date}/run={run_tag}/page_{page_no:03d}.json",
                {"meta": meta, "data": rows})
            c_raw.extend(rows)
        countries = normalize_countries(c_raw)
        storage.put_bytes("processed/worldbank/countries.csv", to_csv_bytes(countries, COUNTRY_FIELDS))
        valid_iso3 = {c["country_iso3"] for c in countries}

        # 5) PROCESSED: normalize + upsert
        clean, stats = normalize_observations(raw_rows, valid_iso3, src_lastupdated, ingested_at)
        proc_key = f"processed/worldbank/{indicator}.csv"
        existing_bytes = storage.get_bytes(proc_key)
        existing = from_csv_bytes(existing_bytes, OBS_CASTS) if existing_bytes else []
        merged, counts = upsert(existing, clean, ("country_iso3", "year"), "value")
        if counts["inserted"] or counts["updated"] or existing_bytes is None:
            storage.put_bytes(proc_key, to_csv_bytes(merged, OBS_FIELDS))

        # 6) checkpoint (only advances if everything above succeeded)
        max_year = max((r["year"] for r in merged), default=None)
        storage.put_json(ckpt_key, {
            "source_lastupdated": src_lastupdated,
            "max_year_loaded": max_year,
            "last_success_at": ingested_at,
            "last_run_id": run_info["run_id"],
        })

        metrics.update(
            pages_fetched=len(all_pages), rows_fetched=len(raw_rows),
            rows_total_processed=len(merged), countries=len(countries),
            max_year_loaded=max_year, **stats, **{f"rows_{k}": v for k, v in counts.items()},
        )
        log.info("World Bank OK: %s", metrics)
        return run_info


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental World Bank ingestion")
    parser.add_argument("--force", action="store_true", help="ignore the checkpoint and reload the window")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(force=args.force), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
