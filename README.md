# Cloud-Native Data Pipeline: Air Pollution vs. Inflation

An incremental, serverless data pipeline written in Python that combines two independent public sources and produces a curated dataset plus a programmatically generated analytical report.

| Source | Type | Content |
|---|---|---|
| [World Bank Indicators API](https://api.worldbank.org/v2) | REST API (JSON, paginated) | PM2.5 air pollution exposure by country and year (`EN.ATM.PM25.MC.M3`) |
| [Wikipedia: List of countries by inflation rate](https://en.wikipedia.org/wiki/List_of_countries_by_inflation_rate) | Web crawler (HTML table) | Annual inflation by country, 2015-2024 |

**Result of the reference run:** 1,583 curated country-year rows covering 181 countries (2015-2023). Across 178 countries with enough overlapping years, mean PM2.5 exposure and median inflation show a weak positive rank correlation (Spearman rho = +0.28; Pearson = +0.08). See [Analytical output](#analytical-output).

> Detailed documentation: [`docs/TECHNICAL_DOCUMENTATION.md`](docs/TECHNICAL_DOCUMENTATION.md) (architecture diagram, schemas, operations) and [`docs/CLOUD_EXECUTION.md`](docs/CLOUD_EXECUTION.md) (how to deploy and run in AWS).

---

## 1. Architecture Overview

```
EventBridge Scheduler (daily) --> AWS Lambda (Python 3.12) --> Amazon S3
                                       |                         raw/        original API pages and HTML
                                       |-- 1. World Bank API     processed/  cleaned, normalized CSVs
                                       |-- 2. Wikipedia crawler  curated/    joined analytical dataset
                                       |-- 3. Curation (join)    analytics/  report.html, SVG charts, summary.json
                                       '-- 4. Analysis           metadata/   run records + checkpoints
                                       |
                                  CloudWatch Logs
```

A single Lambda function runs four stages in order (`pipeline.orchestrator.lambda_handler`). Every stage reads from and writes to S3 through a small storage abstraction, so the same code runs locally (disk) and in the cloud (S3) by changing one environment variable. The full diagram (Mermaid source plus a static image) is in the technical documentation.

![Architecture diagram](docs/images/architecture.png)

## 2. Chosen Cloud Services & Justification

Provider: **AWS** (region `us-east-2`).

| Service | Role | Why |
|---|---|---|
| **Amazon S3** | Storage for all layers (raw, processed, curated, analytics, metadata/checkpoints) | Durable, cheap, no servers; prefixes map naturally to data layers |
| **AWS Lambda** | Runs ingestion, curation and analysis | The whole run takes seconds and uses ~116 MB, so serverless is cheaper and simpler than a cluster; no idle cost |
| **Amazon EventBridge Scheduler** | Daily trigger (cron) with retries | Managed scheduling, no scheduler infrastructure to run |
| **IAM** | Least-privilege role for the function | Lambda can only read/write the project bucket and write logs |
| **CloudWatch Logs** | Execution logs | Built into Lambda; used to inspect every run |

Orchestrators such as Airflow/MWAA and ETL services such as Glue were deliberately **not** used: the workload is tiny, and they would add cost and operational weight without benefit. The trade-off is that incremental state (checkpoints) is implemented in the code itself.

## 3. Incremental Ingestion Strategy

State is kept as JSON checkpoints in `metadata/checkpoints/`. A checkpoint only advances after the stage fully succeeds.

- **World Bank API:** the checkpoint stores the source's `lastupdated` value and the latest year loaded. Each run requests the first page and compares `lastupdated`; if unchanged, the run ends (**skip**). Otherwise it paginates only the necessary year window (latest loaded year minus a 2-year refresh window, to capture revisions) and **upserts** by `(country_iso3, year)`.
- **Wikipedia crawler:** the checkpoint stores a SHA-256 hash of the raw table content. If the hash is unchanged, the run is skipped; otherwise the table is reprocessed and **upserted** by `(country_name, year)`. Edits elsewhere on the page do not trigger reprocessing.
- **Curation:** rebuilt only if the SHA-256 fingerprint of its three processed inputs changed.
- **Analysis:** always regenerated (cheap and deterministic).

Re-running any stage with the same inputs produces the same outputs and no duplicates (see *Idempotency* in the technical documentation). Pass `{"force": true}` as the Lambda event, or `--force` locally, to ignore checkpoints.

## 4. Data Schema Decisions

Layers are S3 prefixes: `raw/` (untouched source payloads, partitioned by `ingest_date` and run), `processed/` (typed, cleaned CSV per source), `curated/` (one analytical table), `analytics/` (outputs), `metadata/` (run records and checkpoints).

- **Naming:** `snake_case` columns; country identified by ISO3 code (`country_iso3`) in all joinable data; units in column names (`pm25_ugm3`, `inflation_pct`).
- **Format:** CSV for processed/curated data. Chosen to keep the Lambda package tiny (standard library only for I/O, no pandas/pyarrow layer) and to be human-readable. Trade-off: no embedded types, so types are re-applied on read.
- **Long format** for the Wikipedia table (one row per country and year) instead of the wide year-per-column layout, so both sources join on the same keys.
- **Curated key:** `(country_iso3, year)`, inner join of both sources.

Full column-level schemas are in [`docs/TECHNICAL_DOCUMENTATION.md`](docs/TECHNICAL_DOCUMENTATION.md#4-data-models).

## 5. Instructions to Run the Entire Project

### Locally (writes to `./data/`)

Requirements: Python 3.10+ (tested on 3.12) and internet access.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run_all.py          # runs the four stages; add --force to ignore checkpoints
```

Open `data/analytics/report.html` in a browser. Individual stages: `run_worldbank.py`, `run_wikipedia.py`, `run_curate.py`, `run_analysis.py`.

Run the tests (no network needed):

```bash
python -m unittest discover -s tests -v
```

### In AWS

Follow [`docs/CLOUD_EXECUTION.md`](docs/CLOUD_EXECUTION.md): build the package (`python scripts/build_lambda.py`), create the IAM role, the Lambda function, the environment variables and the schedule.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `STORAGE_BACKEND` | `local` | `local` (disk) or `s3` |
| `S3_BUCKET` | `case-pipeline-pedro` | Bucket name when using `s3` |
| `LOCAL_DATA_DIR` | `data` | Output folder for `local` |
| `WB_INDICATOR` | `EN.ATM.PM25.MC.M3` | World Bank indicator code |
| `WB_START_YEAR` | `2010` | First year of the initial load |
| `HTTP_TIMEOUT` | `30` | HTTP timeout in seconds |
| `HTTP_USER_AGENT` | descriptive default | User-Agent sent to the sources |

In Lambda, `AWS_REGION` is provided automatically by the runtime.

## Analytical output

Generated only from the curated dataset (`analytics/report.html`, `summary.json` and two SVG charts):

- Spearman and Pearson correlation between each country's mean PM2.5 and median inflation (178 countries: Spearman = +0.28, Pearson = +0.08).
- Yearly trend of both metrics (2015-2023).
- Summary by income group and by region.

The gap between Spearman and Pearson is expected: a few extreme-inflation countries distort the linear correlation, while the rank-based Spearman is robust to them. The report states that this is an association, not causation.

## Evidence

Screenshots of the pipeline running in AWS and of the analytical output (account identifiers hidden):

| File | What it shows |
|---|---|
| [`docs/evidence/01-lambda-test-result.png`](docs/evidence/01-lambda-test-result.png) | Lambda test invocation result: all four stages `success` |
| [`docs/evidence/02-cloudwatch-scheduled-run.png`](docs/evidence/02-cloudwatch-scheduled-run.png) | CloudWatch log streams, including the run triggered by the EventBridge schedule |
| [`docs/evidence/03-s3-bucket-layers.png`](docs/evidence/03-s3-bucket-layers.png) | S3 bucket with the `raw/`, `processed/`, `curated/`, `analytics/` and `metadata/` layers |
| [`docs/evidence/04-report-local.pdf`](docs/evidence/04-report-local.pdf) | Analytical report generated from the curated dataset (`analytics/report.html`, saved as PDF) |
| [`docs/evidence/05-eventbridge-schedule.png`](docs/evidence/05-eventbridge-schedule.png) | EventBridge Scheduler: daily schedule `0 8 * * ? *` targeting the Lambda |

Sample output files from a real run (report, summary, curated dataset) are in [`docs/sample-output/`](docs/sample-output/), so the results can be inspected without AWS access.

## Project structure

```
src/pipeline/   config, storage, http (retries), metadata, tabular, incremental,
                worldbank, wikipedia, curate, analysis, orchestrator (+ Lambda handler)
scripts/        build_lambda.py (Lambda deployment package)
infra/          lambda-s3-policy.json (IAM permissions for the function)
tests/          24 unit tests (no network required)
docs/           technical documentation, cloud execution guide, evidence/ and sample-output/
run_*.py        local entry points
```

## 6. Assumptions & Limitations

- **Overlap window:** PM2.5 data ends in 2023 and the inflation table in 2024, so the curated dataset covers 2015-2023.
- **Incremental granularity:** the World Bank's `lastupdated` is dataset-level and the data is annual, so most daily runs correctly *skip*; real changes arrive when the Bank publishes an update.
- **Country matching:** Wikipedia uses country names while the World Bank uses ISO3 codes. Matching uses normalized names plus an alias dictionary. All 189 Wikipedia countries matched in the reference run, but new naming variants would show up in `curated/unmatched_countries.csv` and need an alias.
- **Crawler fragility:** the table is located by its header (not its position), which tolerates layout shifts; if the table disappears, the run fails loudly and is recorded as `failed`.
- **Analysis scope:** country-level, unweighted by population; an association between two indicators that both correlate with development, not a causal claim. The scatter plot's Y axis is truncated at 20% inflation (truncated points are marked).
- **No alerting** beyond CloudWatch logs and EventBridge retries (2).
- **AWS Free Plan:** per AWS's terms at the time of writing, the plan is limited in time/credits; export the results if the account is to be closed.
- Source data licensing/terms remain those of the World Bank and Wikipedia.
