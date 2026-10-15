# Cloud Execution Guide (AWS)

How the pipeline runs in the cloud, what it needs, and how to deploy it from scratch using the AWS console.

## 1. How it runs

| Aspect | Value |
|---|---|
| Provider / region | AWS, `us-east-2` (Ohio): keep **every** resource in the same region |
| Trigger | **EventBridge Scheduler**, schedule `case-pipeline-daily`, cron `0 8 * * ? *`, time zone `America/Sao_Paulo`, target: Lambda *Invoke*, payload `{}`, max retries `2` |
| Compute | **Lambda** `case-pipeline-ingest`: Python 3.12, x86_64, 512 MB, timeout 300 s, handler `pipeline.orchestrator.lambda_handler` |
| Storage | **S3** bucket `case-pipeline-pedro` (all layers + metadata) |
| Logs | CloudWatch Logs group `/aws/lambda/case-pipeline-ingest` |

Each invocation runs four stages in order (World Bank -> Wikipedia -> curation -> analysis) and writes everything to S3. A manual run uses the same code path: send the test event `{}` (or `{"force": true}` to ignore checkpoints).

## 2. Configuration

### Lambda environment variables

| Variable | Value | Required |
|---|---|---|
| `STORAGE_BACKEND` | `s3` | yes |
| `S3_BUCKET` | `case-pipeline-pedro` | yes |
| `WB_INDICATOR` | `EN.ATM.PM25.MC.M3` (default) | no |
| `WB_START_YEAR` | `2010` (default) | no |
| `HTTP_TIMEOUT` | `30` (default) | no |
| `HTTP_USER_AGENT` | descriptive default | no |

Do **not** set `AWS_REGION`: it is reserved and injected by the Lambda runtime. No access keys are needed or stored: the function authenticates through its IAM role.

### IAM

Role `case-pipeline-lambda-role` (trusted entity: Lambda):

1. AWS managed policy `AWSLambdaBasicExecutionRole` (write logs).
2. Inline policy `case-pipeline-s3-access` = [`infra/lambda-s3-policy.json`](../infra/lambda-s3-policy.json):
   - `s3:ListBucket` on `arn:aws:s3:::case-pipeline-pedro`
   - `s3:GetObject`, `s3:PutObject` on `arn:aws:s3:::case-pipeline-pedro/*`

`s3:ListBucket` is required on purpose: without it, reading a missing object (for example the first run, with no checkpoint yet) returns `AccessDenied` instead of `NoSuchKey`.

The Scheduler uses its own automatically created execution role, which only allows invoking this one function.

### S3 bucket and paths

Bucket `case-pipeline-pedro`, region `us-east-2`, Block Public Access **on**, versioning recommended. Prefixes are created by the code on first write:

```
raw/worldbank/...           raw/wikipedia/...
processed/worldbank/...     processed/wikipedia/...
curated/...                 analytics/...
metadata/runs/...           metadata/checkpoints/...
```

## 3. Deploy step by step

1. **Build the package** on your machine (needs internet):
   ```bash
   python scripts/build_lambda.py        # creates dist/pipeline-lambda.zip
   ```
   The script downloads **Linux** wheels of the dependencies (`requests`, `beautifulsoup4`), even on Windows/macOS, and adds `src/pipeline`. `boto3` is not bundled (it is part of the Lambda runtime). Use `--python-version 3.13` if you pick another runtime.
2. **S3:** create the bucket (`us-east-2`, Block Public Access on, versioning on).
3. **IAM:** create the role `case-pipeline-lambda-role` and attach the two policies from section 2.
4. **Lambda:** *Create function* -> author from scratch -> name `case-pipeline-ingest`, runtime Python 3.12, x86_64, existing role `case-pipeline-lambda-role`.
5. **Code:** *Upload from* -> `.zip file` -> `dist/pipeline-lambda.zip`. In *Runtime settings*, set the handler to `pipeline.orchestrator.lambda_handler`.
6. **Configuration:** memory 512 MB, timeout 5 min, environment variables from section 2.
7. **Test:** *Test* tab -> event `{}` -> *Test*. Expect:
   ```json
   {"status": "success", "stages": {"worldbank": {...}, "wikipedia": {...}, "curation": {...}, "analysis": {...}}}
   ```
   The first run on an empty bucket takes roughly 30-60 s (World Bank pagination dominates). Later runs where sources are unchanged take ~3 s (stages report `"skipped": true`).
8. **Schedule:** EventBridge -> *Scheduler* -> *Create schedule* -> recurring, cron `0 8 * * ? *`, time zone `America/Sao_Paulo`, flexible window off; target *Templated targets -> AWS Lambda -> Invoke* -> `case-pipeline-ingest`, payload `{}`; create a new execution role; set *Maximum retries* to 2.
9. **Log retention (recommended):** CloudWatch -> log group -> *Edit retention* -> 1 month.

## 4. Verifying a run

- **Lambda result / CloudWatch logs:** one line per stage (`=== stage: ... ===`) and the metrics of each one.
- **S3:** `raw/`, `processed/`, `curated/`, `analytics/` and `metadata/` populated; `analytics/report.html` can be downloaded and opened in a browser (the bucket is private).
- **Run records:** `metadata/runs/source=<source>/...json` has `status`, row counts and `duration_seconds` for every stage of every run.
- **Checkpoints:** `metadata/checkpoints/*.json` hold the last successful state used for incremental loads.

## 5. Operating notes

- **Re-run everything ignoring checkpoints:** test event `{"force": true}`. Results stay identical because upserts are idempotent (rows show as `unchanged`).
- **Failure behaviour:** a failing stage raises, later stages do not run, the invocation fails, EventBridge retries (max 2) and the stage's run record shows `status: failed` with the error.
- **Troubleshooting**

  | Symptom | Likely cause |
  |---|---|
  | `Runtime.ImportModuleError: No module named 'pipeline'` | Handler is wrong or the zip has an extra top-level folder; handler must be `pipeline.orchestrator.lambda_handler` and `pipeline/` must be at the zip root |
  | `No module named 'requests'` / `bs4` | Package was zipped from `src/` only; rebuild with `scripts/build_lambda.py` |
  | `AccessDenied` on S3 | Missing or wrong inline policy, or a different bucket name in `S3_BUCKET` |
  | `Task timed out after ...` | Timeout too low for a first load; raise to 300 s |
  | Stage `curation` fails with "processed layer missing" | Ingestion stages did not complete earlier; run again |

## 6. Cost and safety

- Everything fits comfortably in the AWS Free Plan and free tiers (a few MB in S3, ~30 short invocations per month).
- Recommended: a **zero-spend budget** in AWS Budgets with e-mail alerts; never upgrade the account to a paid plan by accident; no public access to the bucket; no credentials in code or in the repository.
- Under the Free Plan terms at the time of writing, the account is limited in time/credits; export results (S3 download, screenshots) if you need to keep them.

## 7. Teardown

Delete, in this order: the EventBridge schedule, the Lambda function, the CloudWatch log group, the IAM role (and its Scheduler role), and finally empty and delete the S3 bucket (with versioning enabled, delete all versions).
