"""Curation: joins PM2.5 (World Bank) + inflation (Wikipedia) by country and year.

Wikipedia identifies countries by NAME and the World Bank by ISO3 code. Matching is done:
  1. by an alias dictionary (names that differ: 'Russia' vs 'Russian Federation', ...)
  2. by normalized name (lowercase, no accents/punctuation)
Countries without a match go to curated/unmatched_countries.csv (and to the run metrics).

Incremental: curation is only redone if the 'processed' layers changed (hash of the inputs).
Idempotent: the curated dataset is always rebuilt in full from processed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from .config import Settings, get_settings
from .metadata import track_run
from .storage import Storage, get_storage
from .tabular import from_csv_bytes, to_csv_bytes
from . import wikipedia, worldbank

log = logging.getLogger(__name__)

SOURCE_ID = "curation"
CURATED_KEY = "curated/country_year_pm25_inflation.csv"
UNMATCHED_KEY = "curated/unmatched_countries.csv"
CKPT_KEY = "metadata/checkpoints/curation.json"
WIKI_KEY = f"processed/wikipedia/{wikipedia.DATASET}_by_country_year.csv"
COUNTRIES_KEY = "processed/worldbank/countries.csv"

CURATED_FIELDS = ["country_iso3", "country_name", "region", "income_level",
                  "year", "pm25_ugm3", "inflation_pct", "curated_at"]

_ALIASES_RAW = {
    "United States of America": "USA", "Russia": "RUS", "Turkey": "TUR", "Turkiye": "TUR",
    "Iran": "IRN", "DR Congo": "COD", "Republic of the Congo": "COG", "The Gambia": "GMB",
    "Ivory Coast": "CIV", "South Korea": "KOR", "Laos": "LAO", "Vietnam": "VNM",
    "Czech Republic": "CZE", "Czechia": "CZE", "East Timor": "TLS", "Hong Kong": "HKG",
    "Macau": "MAC", "Palestine": "PSE", "Micronesia": "FSM", "St. Vincent and Grenadines": "VCT",
    "Egypt": "EGY", "Syria": "SYR", "Venezuela": "VEN", "Slovakia": "SVK", "Kyrgyzstan": "KGZ",
    "Bahamas": "BHS", "Sint Maarten": "SXM", "Saint Kitts and Nevis": "KNA",
    "Saint Lucia": "LCA", "Kosovo": "XKX", "Yemen": "YEM", "Cape Verde": "CPV",
    "Swaziland": "SWZ", "Burma": "MMR", "Macedonia": "MKD", "Brunei": "BRN",
}


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


ALIASES = {normalize_name(k): v for k, v in _ALIASES_RAW.items()}


def match_country(name: str, name_index: dict[str, str], valid_iso3: set[str]) -> Optional[str]:
    key = normalize_name(name)
    iso3 = ALIASES.get(key) or name_index.get(key)
    return iso3 if iso3 in valid_iso3 else None


def join_datasets(pm25_rows: list[dict], infl_rows: list[dict], countries: list[dict],
                  curated_at: str) -> tuple[list[dict], list[dict], dict]:
    meta = {c["country_iso3"]: c for c in countries}
    name_index = {normalize_name(c["country_name"]): c["country_iso3"] for c in countries}
    valid = set(meta)

    # name (Wikipedia) -> ISO3
    wiki_names = sorted({r["country_name"] for r in infl_rows})
    name_to_iso = {n: match_country(n, name_index, valid) for n in wiki_names}
    unmatched_names = [n for n, iso in name_to_iso.items() if iso is None]

    infl: dict[tuple, float] = {}
    unmatched_count: dict[str, int] = {n: 0 for n in unmatched_names}
    collisions = 0
    for r in infl_rows:
        iso = name_to_iso[r["country_name"]]
        if iso is None:
            unmatched_count[r["country_name"]] += 1
            continue
        key = (iso, r["year"])
        collisions += key in infl
        infl[key] = r["inflation_pct"]

    curated = []
    for r in pm25_rows:
        key = (r["country_iso3"], r["year"])
        if key not in infl:
            continue
        c = meta[r["country_iso3"]]
        curated.append({
            "country_iso3": r["country_iso3"], "country_name": c["country_name"],
            "region": c["region"], "income_level": c["income_level"], "year": r["year"],
            "pm25_ugm3": r["value"], "inflation_pct": infl[key], "curated_at": curated_at,
        })
    curated.sort(key=lambda r: (r["country_iso3"], r["year"]))
    unmatched = [{"country_name": n, "year_rows": unmatched_count[n]} for n in unmatched_names]

    stats = {
        "wiki_countries": len(wiki_names),
        "wiki_countries_matched": len(wiki_names) - len(unmatched_names),
        "wiki_countries_unmatched": len(unmatched_names),
        "unmatched_sample": unmatched_names[:40],
        "iso3_collisions": collisions,
    }
    return curated, unmatched, stats


def run(settings: Optional[Settings] = None, storage: Optional[Storage] = None,
        force: bool = False) -> dict:
    settings = settings or get_settings()
    storage = storage or get_storage(settings)
    pm25_key = f"processed/worldbank/{settings.wb_indicator}.csv"

    with track_run(storage, SOURCE_ID) as run_info:
        metrics = run_info["metrics"]
        curated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        blobs = {k: storage.get_bytes(k) for k in (pm25_key, WIKI_KEY, COUNTRIES_KEY)}
        missing = [k for k, v in blobs.items() if v is None]
        if missing:
            raise FileNotFoundError(f"Processed layer missing (run the ingestion stages first): {missing}")

        fingerprint = hashlib.sha256(b"".join(blobs[k] for k in (pm25_key, WIKI_KEY, COUNTRIES_KEY))).hexdigest()
        ckpt = storage.get_json(CKPT_KEY) or {}
        metrics["skipped"] = False
        if not force and ckpt.get("inputs_fingerprint") == fingerprint and storage.get_bytes(CURATED_KEY):
            metrics.update(skipped=True, reason="processed inputs unchanged since the last curation")
            log.info("Inputs unchanged. Nothing to do.")
            return run_info

        pm25 = from_csv_bytes(blobs[pm25_key], worldbank.OBS_CASTS)
        infl = from_csv_bytes(blobs[WIKI_KEY], wikipedia.CASTS)
        countries = from_csv_bytes(blobs[COUNTRIES_KEY])
        curated, unmatched, stats = join_datasets(pm25, infl, countries, curated_at)

        storage.put_bytes(CURATED_KEY, to_csv_bytes(curated, CURATED_FIELDS), "text/csv")
        storage.put_bytes(UNMATCHED_KEY, to_csv_bytes(unmatched, ["country_name", "year_rows"]), "text/csv")
        storage.put_json(CKPT_KEY, {"inputs_fingerprint": fingerprint, "last_success_at": curated_at,
                                    "last_run_id": run_info["run_id"]})

        years = sorted({r["year"] for r in curated})
        metrics.update(
            pm25_rows=len(pm25), inflation_rows=len(infl), rows_curated=len(curated),
            countries_curated=len({r["country_iso3"] for r in curated}),
            year_min=years[0] if years else None, year_max=years[-1] if years else None, **stats,
        )
        log.info("Curation OK: %s", {k: v for k, v in metrics.items() if k != "unmatched_sample"})
        return run_info


def main() -> None:
    parser = argparse.ArgumentParser(description="Curation: join World Bank + Wikipedia")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(force=args.force), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
