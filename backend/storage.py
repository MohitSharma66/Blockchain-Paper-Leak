"""
storage.py

Where the encrypted blobs live.

  * Cloudflare R2 (or any S3-compatible bucket: AWS S3, Backblaze B2, MinIO)
    when the settings below are present;
  * a local folder (backend/data/blobs/) otherwise, so the demo still runs offline.

Settings (in backend/.env):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
      -> Cloudflare R2; the endpoint is derived from the account id.
  or the generic names: S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
      S3_ENDPOINT_URL (omit for AWS S3), S3_REGION (default "auto").
  BLOB_BACKEND=local   forces the local folder even if bucket settings exist.

If bucket settings are only partly filled in, this raises instead of silently
falling back to local disk, so you never think you're on R2 when you're not.

Blobs are ciphertext, so the bucket can stay private and still isn't a secret
store; the AES keys are NOT kept here (see keystore.py).
"""

from pathlib import Path

import config


class BlobStoreError(Exception):
    """Storage is unreachable, misconfigured, or the object is missing."""


# ---------------------------------------------------------------------
# Local folder
# ---------------------------------------------------------------------

class LocalBlobStore:
    kind = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise BlobStoreError(f"Invalid object key: {key!r}")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise BlobStoreError(f"Blob not found: {key}")
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def check(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict:
        return {"backend": "local", "location": str(self.root)}


# ---------------------------------------------------------------------
# S3-compatible bucket (Cloudflare R2, AWS S3, B2, MinIO)
# ---------------------------------------------------------------------

class S3BlobStore:
    kind = "s3"

    def __init__(self, bucket, endpoint_url, access_key, secret_key, region, label):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.label = label
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=30,
            ),
        )

    def _err(self, action: str, key: str, e: Exception) -> BlobStoreError:
        code = ""
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            code = resp.get("Error", {}).get("Code", "")
        return BlobStoreError(f"{self.label}: could not {action} '{key}' ({code or e})")

    def put(self, key: str, data: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except Exception as e:
            raise self._err("upload", key, e)

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:
            raise self._err("download", key, e)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise self._err("check", key, e)

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            raise self._err("delete", key, e)

    def check(self) -> None:
        """Verify credentials + bucket access (raises BlobStoreError)."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as e:
            raise self._err("access bucket", self.bucket, e)

    def describe(self) -> dict:
        return {"backend": "s3", "provider": self.label, "bucket": self.bucket,
                "endpoint": self.endpoint_url or "AWS default"}


# ---------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------

_store = None


def _build():
    if config.env("BLOB_BACKEND").lower() == "local":
        return LocalBlobStore(config.DATA_DIR / "blobs")

    s3 = {k: config.env(k) for k in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL")}

    if s3["S3_BUCKET"] or s3["S3_ACCESS_KEY_ID"] or s3["S3_SECRET_ACCESS_KEY"]:
        missing = [k for k in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY") if not s3[k]]
        if missing:
            raise BlobStoreError("S3 settings are incomplete, missing: " + ", ".join(missing))
        return S3BlobStore(s3["S3_BUCKET"], s3["S3_ENDPOINT_URL"] or None, s3["S3_ACCESS_KEY_ID"],
                           s3["S3_SECRET_ACCESS_KEY"], config.env("S3_REGION") or "auto", "S3-compatible bucket")

    return LocalBlobStore(config.DATA_DIR / "blobs")


def get_blob_store():
    global _store
    if _store is None:
        _store = _build()
    return _store
