"""Configuration read from environment variables (the same locally and in Lambda)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    storage_backend: str
    s3_bucket: str
    aws_region: str
    local_data_dir: str
    wb_indicator: str
    wb_start_year: int
    http_timeout: int
    user_agent: str = "case-pipeline/1.0 (data engineering case)"


def get_settings() -> Settings:
    return Settings(
        storage_backend=os.getenv("STORAGE_BACKEND", "local"),
        s3_bucket=os.getenv("S3_BUCKET", "case-pipeline-pedro"),
        aws_region=os.getenv("AWS_REGION", "us-east-1"),
        local_data_dir=os.getenv("LOCAL_DATA_DIR", "data"),
        wb_indicator=os.getenv("WB_INDICATOR", "EN.ATM.PM25.MC.M3"),
        wb_start_year=int(os.getenv("WB_START_YEAR", "2010")),
        http_timeout=int(os.getenv("HTTP_TIMEOUT", "30")),
        user_agent=os.getenv("HTTP_USER_AGENT", "case-pipeline/1.0 (data engineering case)"),
    )
