"""Storage seam. Where a finished deliverable lives, and the URL it is reached by.

One small interface (`Backend`) with a local-disk implementation (the default, `file://` URLs exactly as
before) and an S3 implementation for AWS S3 or any S3-compatible server. Azure Blob and GCS are named stubs
that raise NotImplementedError. A backend never sees the database; the caller records `backend.id` and the
key on the version row so a URL can be re-made (`volt.publish.refresh_url`).

Credentials are never accepted as arguments, never read from volt config and never logged. S3 gets them only
from boto3's standard provider chain (environment, shared profile, web identity, container or instance role).
No key is ever required: with none set, boto3 resolves whatever the host provides. KMS key ids are not secrets
and may come from VOLT_S3_KMS_KEY_ID.

boto3 is imported inside `S3Backend._make_client` and nowhere else, so a stdlib-only install works until S3
is actually configured.
"""

from __future__ import annotations

import base64
import hashlib
import math
import mimetypes
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import quote


class StorageError(Exception):
    """An upload, verification or lookup failed. The message is safe to store and show (no secrets)."""


class StorageConfigError(StorageError):
    """The storage configuration is missing something or names a backend that does not exist."""


@dataclass(frozen=True)
class StoredObject:
    backend: str          # Backend.id, e.g. "local" or "s3:bucket/prefix"
    key: str              # the key as passed to put_file (relative, '/'-separated)
    url: str              # a URL for it now; may expire (see Backend.url)
    size: int
    sha256: str           # hex digest of the bytes that were uploaded and verified


@runtime_checkable
class Backend(Protocol):
    id: str

    def put_file(self, local_path, key: str) -> StoredObject:
        """Store the file under `key`, verify what arrived, and return its reference. Raises StorageError.
        An existing object under `key` is replaced. A reader never sees a partial object."""

    def url(self, key: str, expires: Optional[int] = None) -> str:
        """A URL that reads the object, valid for `expires` seconds where the backend signs URLs."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None:
        """Remove the object. Deleting a missing key is not an error."""


_KEY_OK = re.compile(r"^[A-Za-z0-9._~@=+,!()' -]+(/[A-Za-z0-9._~@=+,!()' -]+)*$")


def check_key(key: str) -> str:
    if not isinstance(key, str) or not key or len(key) > 900 or not _KEY_OK.match(key) \
            or any(part in (".", "..") for part in key.split("/")):
        raise StorageError(f"invalid storage key {key!r}")
    return key


def content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def sha256_file(path, chunk: int = 1 << 20) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


# ---- local ---------------------------------------------------------------------------------------

class LocalBackend:
    """Files under `root`. URLs are `file://`, which never expire. The default and the pre-seam behaviour."""

    def __init__(self, root):
        self.root = Path(root)
        self.id = "local"

    def _path(self, key: str) -> Path:
        return self.root / Path(*check_key(key).split("/"))

    def put_file(self, local_path, key: str) -> StoredObject:
        src = Path(local_path)
        dest = self._path(key)
        if not src.is_file():
            raise StorageError(f"nothing to store: {src.name} does not exist")
        digest, size = sha256_file(src)
        if src.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            try:
                shutil.copyfile(src, part)
                if sha256_file(part) != (digest, size):
                    raise StorageError("copy does not match the source")
                os.replace(part, dest)
            except OSError as exc:
                raise StorageError(f"local copy failed: {exc}") from exc
            finally:
                part.unlink(missing_ok=True)
        return StoredObject(self.id, key, self.url(key), size, digest)

    def url(self, key: str, expires: Optional[int] = None) -> str:
        return self._path(key).resolve().as_uri()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


# ---- S3 ------------------------------------------------------------------------------------------

MAX_PRESIGN = 7 * 24 * 3600   # SigV4 ceiling
DEFAULT_PRESIGN = MAX_PRESIGN
MAX_PARTS = 10000


@dataclass
class S3Config:
    bucket: str
    prefix: str = ""
    region: Optional[str] = None
    endpoint_url: Optional[str] = None        # S3-compatible servers (MinIO, Ceph, R2...)
    url_expires: int = DEFAULT_PRESIGN        # seconds, for presigned GET URLs
    public_url_base: Optional[str] = None     # if set, URLs are base/<prefix><key>: no signing, no expiry
    part_mb: int = 16                         # multipart threshold and part size
    sse: Optional[str] = None                 # server-side encryption on every put: "aws:kms" or "AES256"
    kms_key_id: Optional[str] = None          # KMS key id, ARN or alias (with sse="aws:kms"); None = the bucket/AWS default key


class S3Backend:
    """AWS S3 or an S3-compatible server. `client` is injectable for tests; otherwise boto3 is imported on
    first use. Credentials come only from boto3's default provider chain."""

    def __init__(self, config: S3Config, client=None):
        if not config.bucket:
            raise StorageConfigError("VOLT_S3_BUCKET is required for VOLT_STORAGE=s3")
        if not 1 <= config.url_expires <= MAX_PRESIGN:
            raise StorageConfigError(f"VOLT_S3_URL_EXPIRES must be 1..{MAX_PRESIGN} seconds")
        if config.part_mb < 5:
            raise StorageConfigError("VOLT_S3_PART_MB must be at least 5 (the S3 minimum part size)")
        if config.sse not in (None, "aws:kms", "AES256"):
            raise StorageConfigError("VOLT_S3_SSE must be aws:kms or AES256")
        if config.kms_key_id and config.sse != "aws:kms":
            raise StorageConfigError("VOLT_S3_KMS_KEY_ID needs VOLT_S3_SSE=aws:kms")
        if config.public_url_base and not re.match(r"^https?://[^/\s]+", config.public_url_base):
            raise StorageConfigError("VOLT_S3_PUBLIC_URL_BASE must be an http(s) URL")
        self.config = config
        prefix = config.prefix.strip("/")
        self.prefix = prefix + "/" if prefix else ""
        self.id = f"s3:{config.bucket}/{prefix}" if prefix else f"s3:{config.bucket}"
        self._client = client

    # -- plumbing
    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _make_client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise StorageConfigError(
                "VOLT_STORAGE=s3 needs boto3 (pip install boto3); it is not installed") from exc
        cfg = {"signature_version": "s3v4", "retries": {"max_attempts": 5, "mode": "standard"}}
        # Path style for S3-compatible servers. Real AWS: virtual-hosted style, so a presigned URL names the
        # bucket's regional host. Without it boto3 signs for the region but points at s3.amazonaws.com, which
        # AWS rejects ("the region is wrong") for any bucket outside us-east-1.
        cfg["s3"] = {"addressing_style": "path" if self.config.endpoint_url else "virtual"}
        return boto3.client("s3", region_name=self.config.region or None,
                            endpoint_url=self.config.endpoint_url or None, config=Config(**cfg))

    def _object_key(self, key: str) -> str:
        return self.prefix + check_key(key)

    @staticmethod
    def _status(exc) -> Optional[int]:
        response = getattr(exc, "response", None) or {}
        return (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")

    @staticmethod
    def _explain(exc) -> str:
        """A short reason with no credential material: the S3 error code and message, or the exception type."""
        response = getattr(exc, "response", None) or {}
        error = response.get("Error") or {}
        if error.get("Code"):
            return f"{error['Code']}: {error.get('Message', '')}".strip()
        return f"{type(exc).__name__}: {str(exc)[:200]}"

    # -- the interface
    def put_file(self, local_path, key: str) -> StoredObject:
        src = Path(local_path)
        object_key = self._object_key(key)
        if not src.is_file():
            raise StorageError(f"nothing to upload: {src.name} does not exist")
        digest, size = sha256_file(src)
        part = self._part_size(size)
        try:
            from boto3.s3.transfer import TransferConfig
        except ImportError:
            if self._client is None:
                raise StorageConfigError("VOLT_STORAGE=s3 needs boto3 (pip install boto3)") from None
            TransferConfig = None  # an injected client brings its own transfer behaviour
        try:
            # S3 makes a PUT, or a completed multipart upload, visible all at once, so no temp key and copy.
            extra = {"ContentType": content_type(key), "ChecksumAlgorithm": "SHA256"}
            if self.config.sse:  # boto3 applies these to a single PUT and to every part of a multipart upload
                extra["ServerSideEncryption"] = self.config.sse
                if self.config.kms_key_id:
                    extra["SSEKMSKeyId"] = self.config.kms_key_id
            kwargs = {"ExtraArgs": extra}
            if TransferConfig is not None:
                kwargs["Config"] = TransferConfig(multipart_threshold=part, multipart_chunksize=part)
            self.client.upload_file(str(src), self.config.bucket, object_key, **kwargs)
            head = self.client.head_object(Bucket=self.config.bucket, Key=object_key, ChecksumMode="ENABLED")
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - boto raises many types; the reason is what matters
            raise StorageError(f"upload of {key} failed: {self._explain(exc)}") from exc
        try:
            self._verify(src, size, part, head)
        except StorageError:
            try:  # do not leave an object that failed verification looking like a good one
                self.client.delete_object(Bucket=self.config.bucket, Key=object_key)
            except Exception:  # noqa: BLE001
                pass
            raise
        return StoredObject(self.id, key, self.url(key), size, digest)

    def _part_size(self, size: int) -> int:
        part = self.config.part_mb * 1024 * 1024
        return max(part, math.ceil(size / (MAX_PARTS - 1))) if size else part

    def _verify(self, src: Path, size: int, part: int, head: dict) -> None:
        """Size, then the strongest hash the server returned. Refuses to call an unverifiable upload good."""
        if head.get("ContentLength") != size:
            raise StorageError(f"uploaded size {head.get('ContentLength')} != local size {size}")
        if self.config.sse and head.get("ServerSideEncryption") != self.config.sse:
            # asked for encryption and the server did not say it applied it: do not call that stored
            raise StorageError(f"object is not encrypted as configured (wanted {self.config.sse}, "
                               f"server reports {head.get('ServerSideEncryption')})")
        multipart = size >= part
        sums = _part_digests(src, part if multipart else max(size, 1))
        remote = head.get("ChecksumSHA256")
        if remote:
            local = (_b64(hashlib.sha256(b"".join(s for s, _ in sums)).digest()) + f"-{len(sums)}") \
                if multipart else _b64(sums[0][0])
            if remote != local:
                raise StorageError("checksum mismatch after upload (SHA-256)")
            return
        etag = (head.get("ETag") or "").strip('"')
        if etag and head.get("ServerSideEncryption") != "aws:kms":  # a KMS ETag is not an MD5
            joined = hashlib.md5(b"".join(m for _, m in sums)).hexdigest() + f"-{len(sums)}"
            local = joined if multipart else sums[0][1].hex()
            if etag != local:
                raise StorageError("checksum mismatch after upload (ETag/MD5)")
            return
        raise StorageError("upload cannot be verified: the server returned neither a SHA-256 checksum "
                           "nor a usable ETag")

    def url(self, key: str, expires: Optional[int] = None) -> str:
        object_key = self._object_key(key)
        base = self.config.public_url_base
        if base:
            return base.rstrip("/") + "/" + quote(object_key, safe="/")
        seconds = expires if expires is not None else self.config.url_expires
        if not 1 <= seconds <= MAX_PRESIGN:
            raise StorageError(f"URL expiry must be 1..{MAX_PRESIGN} seconds")
        try:
            return self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.config.bucket, "Key": object_key}, ExpiresIn=seconds)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"could not sign a URL for {key}: {self._explain(exc)}") from exc

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=self._object_key(key))
            return True
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._status(exc) == 404:
                return False
            raise StorageError(f"could not look up {key}: {self._explain(exc)}") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.config.bucket, Key=self._object_key(key))
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"could not delete {key}: {self._explain(exc)}") from exc


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _part_digests(path: Path, part: int) -> list[tuple[bytes, bytes]]:
    """(sha256, md5) of each `part`-sized slice of the file, read in 1 MiB blocks."""
    out = []
    with open(path, "rb") as fh:
        while True:
            sha, md5, left = hashlib.sha256(), hashlib.md5(), part  # noqa: S324 - S3's own ETag scheme
            got = 0
            while left > 0:
                block = fh.read(min(1 << 20, left))
                if not block:
                    break
                sha.update(block)
                md5.update(block)
                left -= len(block)
                got += len(block)
            if got == 0 and out:
                break
            out.append((sha.digest(), md5.digest()))
            if got < part:
                break
    return out


# ---- the other clouds, named so the interface has somewhere to grow --------------------------------

class AzureBlobBackend:
    """Next backend. Not built: the interface above is the contract it must pass (tests/test_storage.py)."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "Azure Blob storage is not implemented yet. Use VOLT_STORAGE=s3 or local; "
            "see docs/specs/storage.md for the contract an Azure backend must meet.")


class GcsBackend:
    """Next backend. Not built: the interface above is the contract it must pass (tests/test_storage.py)."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "Google Cloud Storage is not implemented yet. Use VOLT_STORAGE=s3 or local; "
            "see docs/specs/storage.md for the contract a GCS backend must meet.")


# ---- configuration -------------------------------------------------------------------------------

def _sse(raw) -> Optional[str]:
    """VOLT_S3_SSE: kms/aws:kms or aes256/s3, case-insensitive; empty or unset = no explicit header."""
    value = (raw or "").strip().lower()
    if not value:
        return None
    mapping = {"aws:kms": "aws:kms", "kms": "aws:kms", "aes256": "AES256", "s3": "AES256"}
    if value not in mapping:
        raise StorageConfigError("VOLT_S3_SSE must be aws:kms or AES256")
    return mapping[value]


def from_env(env=None, local_root=None) -> Backend:
    """VOLT_STORAGE = local (default) | s3 | azure | gcs. `local_root` is the local backend's directory."""
    env = os.environ if env is None else env
    kind = (env.get("VOLT_STORAGE") or "local").strip().lower()
    if kind == "local":
        if local_root is None:
            raise StorageConfigError("local storage needs a publish directory (VOLT_PUBLISH_DIR or --publish-dir)")
        return LocalBackend(local_root)
    if kind == "s3":
        def number(name, default):
            raw = env.get(name)
            try:
                return int(raw) if raw else default
            except ValueError:
                raise StorageConfigError(f"{name} must be a whole number") from None
        return S3Backend(S3Config(
            bucket=(env.get("VOLT_S3_BUCKET") or "").strip(),
            prefix=env.get("VOLT_S3_PREFIX") or "",
            region=env.get("VOLT_S3_REGION") or None,
            endpoint_url=env.get("VOLT_S3_ENDPOINT_URL") or None,
            url_expires=number("VOLT_S3_URL_EXPIRES", DEFAULT_PRESIGN),
            public_url_base=env.get("VOLT_S3_PUBLIC_URL_BASE") or None,
            part_mb=number("VOLT_S3_PART_MB", 16),
            sse=_sse(env.get("VOLT_S3_SSE")),
            kms_key_id=(env.get("VOLT_S3_KMS_KEY_ID") or "").strip() or None,
        ))
    if kind == "azure":
        return AzureBlobBackend()
    if kind == "gcs":
        return GcsBackend()
    raise StorageConfigError(f"unknown VOLT_STORAGE {kind!r} (local, s3; azure and gcs are not built yet)")
