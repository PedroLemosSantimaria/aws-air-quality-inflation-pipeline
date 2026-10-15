"""Storage abstraction: the rest of the code only knows 'keys' (paths).

LocalStorage writes to disk (development); S3Storage writes to the bucket (cloud).
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

from .config import Settings


class Storage(ABC):
    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None) -> None: ...

    @abstractmethod
    def get_bytes(self, key: str) -> Optional[bytes]: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]: ...

    def put_json(self, key: str, obj: Any) -> None:
        self.put_bytes(key, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

    def get_json(self, key: str) -> Optional[Any]:
        data = self.get_bytes(key)
        return json.loads(data.decode("utf-8")) if data is not None else None


class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = root

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.split("/"))

    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def get_bytes(self, key: str) -> Optional[bytes]:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def list_keys(self, prefix: str) -> list[str]:
        keys = []
        for folder, _, files in os.walk(self.root):
            for name in files:
                full = os.path.join(folder, name)
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)


class S3Storage(Storage):
    def __init__(self, bucket: str, region: str):
        import boto3  # lazy import: not needed in local mode

        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region)

    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def get_bytes(self, key: str) -> Optional[bytes]:
        from botocore.exceptions import ClientError

        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def list_keys(self, prefix: str) -> list[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)


def get_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage(settings.s3_bucket, settings.aws_region)
    return LocalStorage(settings.local_data_dir)
