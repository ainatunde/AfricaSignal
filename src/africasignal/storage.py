"""S3-compatible object storage (Cloudflare R2, Backblaze B2, MinIO in development).

Keys are always relative, for example ``evidence/ab/cd/<sha256>.html``. No absolute filesystem
path is ever stored in the database.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.db import session_scope
from africasignal.settings_store import StorageConfig, storage_config


class ObjectStore(Protocol):
    def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


def evidence_key(sha256: str, extension: str) -> str:
    """``evidence/<aa>/<bb>/<sha256>.<ext>``: the object key for a captured document."""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    if not extension.isalnum():
        raise ValueError("extension must be alphanumeric")
    return f"evidence/{sha256[:2]}/{sha256[2:4]}/{sha256}.{extension}"


def _check_key(key: str) -> None:
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
        raise ValueError(f"object keys must be relative and clean, got {key!r}")


class S3Store:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls) -> S3Store:
        settings = get_settings()
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return cls(client, settings.s3_bucket)

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        _check_key(key)
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        _check_key(key)
        body: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        _check_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        _check_key(key)
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        """Every key that starts with ``prefix``, in key order."""
        _check_key(prefix)
        keys: list[str] = []
        for page in self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix
        ):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys


@lru_cache(maxsize=4)
def _store_for(config: StorageConfig) -> S3Store:
    client = boto3.client(
        "s3",
        endpoint_url=config.endpoint_url or None,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return S3Store(client, config.bucket)


def store_for_session(session: Session) -> S3Store | None:
    """The evidence store for code that already holds a database session, or ``None`` when no
    bucket or credentials are configured yet (nothing then tries the network)."""
    config = storage_config(session)
    if not (config.bucket and config.access_key_id and config.secret_access_key):
        return None
    return _store_for(config)


def get_store() -> S3Store:
    """The evidence store, from the console settings (environment variables as the fallback).
    Read on each call, so a change in the console applies without a restart."""
    with session_scope() as session:
        config = storage_config(session)
    return _store_for(config)
