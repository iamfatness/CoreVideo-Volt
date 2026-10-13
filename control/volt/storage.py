"""Storage seam. Where a finished deliverable lives, and the URL it is reached by.

One small interface (`Backend`) with a local-disk implementation (the default, `file://` URLs exactly as
before) and an S3 implementation for AWS S3 or any S3-compatible server. Azure Blob and GCS backends implement the
same interface. A backend never sees the database; the caller records `backend.id` and the
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


# ---- Azure Blob and Google Cloud Storage ---------------------------------------------------------
# Neither cloud stores a SHA-256 for an object (Azure keeps only a client-supplied MD5, GCS keeps CRC32C and
# MD5). So, after the upload is committed, these backends read the object back and hash it: the SHA-256 that
# is reported is the SHA-256 of the bytes the server now holds, and a mismatch deletes the object.

from datetime import datetime, timedelta, timezone  # noqa: E402

MAX_SAS = 7 * 24 * 3600        # Azure user-delegation SAS and GCS V4 signed URLs both stop at 7 days
DEFAULT_SAS = MAX_SAS
MAX_BLOCKS = 50000             # Azure block blob limit

_SECRET_PARAM = re.compile(
    r"(?i)(sig|signature|x-goog-signature|x-goog-credential|accountkey|sharedaccesssignature|"
    r"access_token|token|key|password|secret)=[^&\s\"']+")


def _scrub(text: str) -> str:
    """Blank anything that looks like `name=secret` (SAS signatures, account keys, tokens) in error text."""
    return _SECRET_PARAM.sub(lambda m: m.group(1) + "=***", text)


def _status_of(exc) -> Optional[int]:
    for name in ("status_code", "code"):   # azure.core: status_code; google.api_core: code
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


def _read_back(chunks) -> tuple[str, int]:
    digest, got = hashlib.sha256(), 0
    for block in chunks:
        digest.update(block)
        got += len(block)
    return digest.hexdigest(), got


def _file_blocks(path: Path, block: int):
    """Yield (index, bytes) for each non-empty `block`-sized slice of the file."""
    with open(path, "rb") as fh:
        index = 0
        while data := fh.read(block):
            yield index, data
            index += 1


def _file_md5(path: Path) -> bytes:
    digest = hashlib.md5()  # noqa: S324 - the Content-MD5 / object MD5 both clouds use, not a security use
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.digest()


# ---- Azure ---------------------------------------------------------------------------------------

@dataclass
class AzureConfig:
    account: str                                # storage account name (not a secret; part of the backend id)
    container: str
    prefix: str = ""
    account_url: Optional[str] = None           # https://<account>.blob.core.windows.net, for Entra ID auth
    url_expires: int = DEFAULT_SAS              # seconds, for SAS URLs
    public_url_base: Optional[str] = None       # if set, URLs are base/<prefix><key>: no SAS, no expiry
    block_mb: int = 16                          # block size


class AzureBlobBackend:
    """Azure Blob Storage (block blobs). `client` (a BlobServiceClient) is injectable for tests; otherwise
    azure-storage-blob is imported on first use. Credentials are never arguments: a connection string in the
    standard AZURE_STORAGE_CONNECTION_STRING variable (account key, or Azurite), else Entra ID through
    azure-identity's DefaultAzureCredential against `account_url`.

    Upload: the file is staged as blocks, each sent with a Content-MD5 the server checks; block ids carry a
    hash of their content, so a retry after a failure skips blocks the server already holds. Nothing is
    visible until the block list is committed, which replaces the blob atomically. The committed blob is then
    read back and its SHA-256 compared with the local file."""

    def __init__(self, config: AzureConfig, client=None):
        if not config.account:
            raise StorageConfigError("VOLT_AZURE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING is required "
                                     "for VOLT_STORAGE=azure")
        if not config.container:
            raise StorageConfigError("VOLT_AZURE_CONTAINER is required for VOLT_STORAGE=azure")
        if not 1 <= config.url_expires <= MAX_SAS:
            raise StorageConfigError(f"VOLT_AZURE_URL_EXPIRES must be 1..{MAX_SAS} seconds")
        if not 1 <= config.block_mb <= 4000:
            raise StorageConfigError("VOLT_AZURE_BLOCK_MB must be 1..4000")
        self.config = config
        prefix = config.prefix.strip("/")
        self.prefix = prefix + "/" if prefix else ""
        self.id = f"azure:{config.account}/{config.container}" + (f"/{prefix}" if prefix else "")
        self._client = client
        self._delegation = None   # (user delegation key, its expiry) when signing with Entra ID

    # -- plumbing
    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _make_client(self):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise StorageConfigError(
                "VOLT_STORAGE=azure needs azure-storage-blob (pip install azure-storage-blob); "
                "it is not installed") from exc
        connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if connection:
            try:
                return BlobServiceClient.from_connection_string(connection)
            except Exception as exc:  # noqa: BLE001
                raise StorageConfigError(
                    f"AZURE_STORAGE_CONNECTION_STRING is not valid ({type(exc).__name__})") from None
        if not self.config.account_url:
            raise StorageConfigError("set AZURE_STORAGE_CONNECTION_STRING, or VOLT_AZURE_ACCOUNT_URL "
                                     "with Entra ID credentials")
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise StorageConfigError(
                "VOLT_AZURE_ACCOUNT_URL needs azure-identity (pip install azure-identity); "
                "it is not installed") from exc
        return BlobServiceClient(self.config.account_url, credential=DefaultAzureCredential())

    def _object_key(self, key: str) -> str:
        return self.prefix + check_key(key)

    def _blob(self, object_key: str):
        return self.client.get_blob_client(self.config.container, object_key)

    @staticmethod
    def _explain(exc) -> str:
        """A short reason with no credential material: Azure's error code and status, else the exception type
        and a scrubbed first line."""
        code = getattr(exc, "error_code", None)
        status = _status_of(exc)
        if code is not None:
            return f"{getattr(code, 'value', code)}" + (f" (HTTP {status})" if status else "")
        first = (str(exc).strip().splitlines() or [""])[0]
        return f"{type(exc).__name__}: {_scrub(first)[:200]}"

    @staticmethod
    def _types():
        """(BlobBlock, ContentSettings). Stand-ins when the SDK is absent and a client was injected."""
        try:
            from azure.storage.blob import BlobBlock, ContentSettings
            return BlobBlock, ContentSettings
        except ImportError:
            from collections import namedtuple
            return namedtuple("BlobBlock", "block_id"), (lambda **kw: dict(kw))

    def _block_size(self, size: int) -> int:
        block = self.config.block_mb * 1024 * 1024
        return max(block, math.ceil(size / (MAX_BLOCKS - 1))) if size else block

    # -- the interface
    def put_file(self, local_path, key: str) -> StoredObject:
        src = Path(local_path)
        object_key = self._object_key(key)
        if not src.is_file():
            raise StorageError(f"nothing to upload: {src.name} does not exist")
        digest, size = sha256_file(src)
        md5 = _file_md5(src)
        BlobBlock, ContentSettings = self._types()
        try:
            blob = self._blob(object_key)
            held = self._uncommitted(blob)
            order = []
            for index, data in _file_blocks(src, self._block_size(size)):
                block_id = _b64(f"{index:06d}-{hashlib.sha256(data).hexdigest()[:32]}".encode("ascii"))
                order.append(block_id)
                if held.get(block_id) != len(data):
                    # validate_content: the SDK sends this block's Content-MD5; the server rejects a mismatch
                    blob.stage_block(block_id=block_id, data=data, length=len(data), validate_content=True)
            blob.commit_block_list(
                [BlobBlock(block_id=b) for b in order],
                content_settings=ContentSettings(content_type=content_type(key), content_md5=bytearray(md5)),
                metadata={"sha256": digest})
            remote_size = blob.get_blob_properties().size
            problem = f"uploaded size {remote_size} != local size {size}" if remote_size != size else None
            if problem is None:
                back, got = _read_back(blob.download_blob().chunks())
                if got != size or back != digest:
                    problem = "checksum mismatch after upload (SHA-256 of the stored blob)"
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - the SDK raises many types; the reason is what matters
            raise StorageError(f"upload of {key} failed: {self._explain(exc)}") from exc
        if problem:
            try:  # do not leave an object that failed verification looking like a good one
                self._blob(object_key).delete_blob()
            except Exception:  # noqa: BLE001
                pass
            raise StorageError(problem)
        return StoredObject(self.id, key, self.url(key), size, digest)

    @staticmethod
    def _uncommitted(blob) -> dict:
        """{block id: size} the server already holds for this blob from an earlier, interrupted attempt."""
        try:
            _committed, uncommitted = blob.get_block_list("uncommitted")
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) == 404:
                return {}
            raise
        return {b.id: b.size for b in uncommitted}

    def url(self, key: str, expires: Optional[int] = None) -> str:
        object_key = self._object_key(key)
        base = self.config.public_url_base
        if base:
            return base.rstrip("/") + "/" + quote(object_key, safe="/")
        seconds = expires if expires is not None else self.config.url_expires
        if not 1 <= seconds <= MAX_SAS:
            raise StorageError(f"URL expiry must be 1..{MAX_SAS} seconds")
        try:
            return f"{self._blob(object_key).url}?{self._sas(object_key, seconds)}"
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"could not sign a URL for {key}: {self._explain(exc)}") from exc

    def _sas(self, object_key: str, seconds: int) -> str:
        """A read-only SAS token valid for `seconds`. Signed with the account key when the client has one,
        else with a user delegation key fetched through Entra ID."""
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        service = self.client
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=seconds)
        common = dict(account_name=service.account_name, container_name=self.config.container,
                      blob_name=object_key, permission=BlobSasPermissions(read=True), expiry=expiry,
                      start=now - timedelta(minutes=5))
        account_key = getattr(service.credential, "account_key", None)
        if account_key:
            return generate_blob_sas(account_key=account_key, **common)
        if self._delegation is None or self._delegation[1] < expiry:
            key_expiry = min(now + timedelta(seconds=MAX_SAS), max(expiry, now + timedelta(days=1)))
            key = service.get_user_delegation_key(now - timedelta(minutes=5), key_expiry)
            self._delegation = (key, key_expiry)
        return generate_blob_sas(user_delegation_key=self._delegation[0], **common)

    def exists(self, key: str) -> bool:
        try:
            return bool(self._blob(self._object_key(key)).exists())
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"could not look up {key}: {self._explain(exc)}") from exc

    def delete(self, key: str) -> None:
        try:
            self._blob(self._object_key(key)).delete_blob(delete_snapshots="include")
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) == 404:
                return
            raise StorageError(f"could not delete {key}: {self._explain(exc)}") from exc


# ---- Google Cloud Storage -----------------------------------------------------------------------

@dataclass
class GcsConfig:
    bucket: str
    prefix: str = ""
    project: Optional[str] = None
    endpoint_url: Optional[str] = None        # an emulator or private endpoint; uses anonymous credentials
    url_expires: int = DEFAULT_SAS            # seconds, for V4 signed URLs
    public_url_base: Optional[str] = None     # if set, URLs are base/<prefix><key>: unsigned, never expiring
    chunk_mb: int = 16                        # resumable-upload chunk size (a multiple of 256 KiB)


class GcsBackend:
    """Google Cloud Storage. `client` (a storage.Client) is injectable for tests; otherwise google-cloud-storage
    is imported on first use. Credentials come only from Google application default credentials (a service
    account key file named by GOOGLE_APPLICATION_CREDENTIALS, gcloud's user login, or an attached service
    account). Never arguments, never logged.

    Upload: always a resumable session sent in `chunk_mb` chunks (the library retries a failed chunk against the
    same session), CRC32C checked by the library as it goes. The object becomes visible only when the session
    finalises. It is then compared with the local file by size, MD5 and CRC32C, and read back for SHA-256.
    There is no resume across process restarts: a failed call starts a fresh session."""

    def __init__(self, config: GcsConfig, client=None):
        if not config.bucket:
            raise StorageConfigError("VOLT_GCS_BUCKET is required for VOLT_STORAGE=gcs")
        if not 1 <= config.url_expires <= MAX_SAS:
            raise StorageConfigError(f"VOLT_GCS_URL_EXPIRES must be 1..{MAX_SAS} seconds")
        if config.chunk_mb < 1:
            raise StorageConfigError("VOLT_GCS_CHUNK_MB must be at least 1")
        self.config = config
        prefix = config.prefix.strip("/")
        self.prefix = prefix + "/" if prefix else ""
        self.id = f"gcs:{config.bucket}/{prefix}" if prefix else f"gcs:{config.bucket}"
        self._client = client

    # -- plumbing
    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _make_client(self):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise StorageConfigError(
                "VOLT_STORAGE=gcs needs google-cloud-storage (pip install google-cloud-storage); "
                "it is not installed") from exc
        if self.config.endpoint_url:
            from google.auth.credentials import AnonymousCredentials
            return storage.Client(project=self.config.project or "volt", credentials=AnonymousCredentials(),
                                  client_options={"api_endpoint": self.config.endpoint_url})
        try:
            return storage.Client(project=self.config.project or None)
        except Exception as exc:  # noqa: BLE001 - DefaultCredentialsError and friends
            raise StorageConfigError(
                f"no usable Google application default credentials ({type(exc).__name__}); set "
                "GOOGLE_APPLICATION_CREDENTIALS or run `gcloud auth application-default login`") from None

    def _object_key(self, key: str) -> str:
        return self.prefix + check_key(key)

    def _blob(self, object_key: str):
        return self.client.bucket(self.config.bucket).blob(object_key)

    @staticmethod
    def _explain(exc) -> str:
        """A short reason with no credential material: the HTTP status and Google's reason, else the type."""
        status = _status_of(exc)
        if status:
            reason = str(getattr(exc, "reason", "") or "")
            return f"HTTP {status}" + (f": {_scrub(reason)[:200]}" if reason else "")
        first = (str(exc).strip().splitlines() or [""])[0]
        return f"{type(exc).__name__}: {_scrub(first)[:200]}"

    # -- the interface
    def put_file(self, local_path, key: str) -> StoredObject:
        src = Path(local_path)
        object_key = self._object_key(key)
        if not src.is_file():
            raise StorageError(f"nothing to upload: {src.name} does not exist")
        digest, size = sha256_file(src)
        md5, crc = _file_md5(src), _file_crc32c(src)
        chunk = self.config.chunk_mb * 1024 * 1024
        try:
            blob = self._blob(object_key)
            with open(src, "rb") as fh, blob.open("wb", chunk_size=chunk, content_type=content_type(key),
                                                  checksum="crc32c") as out:
                while block := fh.read(1 << 20):
                    out.write(block)
            blob.reload()
            problem = self._compare(blob, size, md5, crc)
            if problem is None:
                back, got = _read_back(self._chunks(blob, chunk))
                if got != size or back != digest:
                    problem = "checksum mismatch after upload (SHA-256 of the stored object)"
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - the SDK raises many types; the reason is what matters
            raise StorageError(f"upload of {key} failed: {self._explain(exc)}") from exc
        if problem:
            try:  # do not leave an object that failed verification looking like a good one
                self._blob(object_key).delete()
            except Exception:  # noqa: BLE001
                pass
            raise StorageError(problem)
        return StoredObject(self.id, key, self.url(key), size, digest)

    @staticmethod
    def _compare(blob, size: int, md5: bytes, crc: int) -> Optional[str]:
        if blob.size != size:
            return f"uploaded size {blob.size} != local size {size}"
        if blob.md5_hash and blob.md5_hash != _b64(md5):
            return "checksum mismatch after upload (MD5)"
        if blob.crc32c and blob.crc32c != _b64(crc.to_bytes(4, "big")):
            return "checksum mismatch after upload (CRC32C)"
        if not blob.md5_hash and not blob.crc32c:
            return "upload cannot be verified: the server returned neither an MD5 nor a CRC32C"
        return None

    @staticmethod
    def _chunks(blob, chunk: int):
        with blob.open("rb", chunk_size=chunk) as fh:
            while block := fh.read(1 << 20):
                yield block

    def url(self, key: str, expires: Optional[int] = None) -> str:
        object_key = self._object_key(key)
        base = self.config.public_url_base
        if base:
            return base.rstrip("/") + "/" + quote(object_key, safe="/")
        seconds = expires if expires is not None else self.config.url_expires
        if not 1 <= seconds <= MAX_SAS:
            raise StorageError(f"URL expiry must be 1..{MAX_SAS} seconds")
        blob = self._blob(object_key)
        options = dict(version="v4", expiration=timedelta(seconds=seconds), method="GET")
        if self.config.endpoint_url:
            options["api_access_endpoint"] = self.config.endpoint_url
        try:
            try:
                return blob.generate_signed_url(**options)
            except (AttributeError, TypeError, ValueError):
                # The credential holds no private key (an attached service account, say): sign through the
                # IAM signBlob API, which needs iam.serviceAccounts.signBlob on that account.
                from google.auth.transport.requests import Request
                creds = self.client._credentials
                creds.refresh(Request())
                return blob.generate_signed_url(
                    service_account_email=creds.service_account_email, access_token=creds.token, **options)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"could not sign a URL for {key}: {self._explain(exc)}") from exc

    def exists(self, key: str) -> bool:
        try:
            return bool(self._blob(self._object_key(key)).exists())
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"could not look up {key}: {self._explain(exc)}") from exc

    def delete(self, key: str) -> None:
        try:
            self._blob(self._object_key(key)).delete()
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) == 404:
                return
            raise StorageError(f"could not delete {key}: {self._explain(exc)}") from exc


def _file_crc32c(path: Path) -> int:
    try:
        import google_crc32c
    except ImportError:   # a stdlib-only fallback, slow but correct (only used with an injected client)
        crc = 0xFFFFFFFF
        with open(path, "rb") as fh:
            while block := fh.read(1 << 20):
                for byte in block:
                    crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
        return crc ^ 0xFFFFFFFF
    checksum = google_crc32c.Checksum()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            checksum.update(block)
    return int.from_bytes(checksum.digest(), "big")


def _crc32c_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC32C_TABLE = _crc32c_table()


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
        return _azure_from_env(env)
    if kind == "gcs":
        return _gcs_from_env(env)
    raise StorageConfigError(f"unknown VOLT_STORAGE {kind!r} (local, s3, azure, gcs)")


def _env_int(env, name, default):
    raw = env.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        raise StorageConfigError(f"{name} must be a whole number") from None


def _azure_from_env(env) -> AzureBlobBackend:
    account_url = (env.get("VOLT_AZURE_ACCOUNT_URL") or "").strip() or None
    if account_url:   # https://<account>.blob.core.windows.net
        account = re.sub(r"^https?://", "", account_url).split("/")[0].split(".")[0]
    else:             # AccountName=<account>;... in the standard connection string (the key is never read here)
        found = re.search(r"(?i)(?:^|;)AccountName=([^;]+)", env.get("AZURE_STORAGE_CONNECTION_STRING") or "")
        account = found.group(1) if found else ""
    return AzureBlobBackend(AzureConfig(
        account=account,
        container=(env.get("VOLT_AZURE_CONTAINER") or "").strip(),
        prefix=env.get("VOLT_AZURE_PREFIX") or "",
        account_url=account_url,
        url_expires=_env_int(env, "VOLT_AZURE_URL_EXPIRES", DEFAULT_SAS),
        public_url_base=env.get("VOLT_AZURE_PUBLIC_URL_BASE") or None,
        block_mb=_env_int(env, "VOLT_AZURE_BLOCK_MB", 16),
    ))


def _gcs_from_env(env) -> GcsBackend:
    return GcsBackend(GcsConfig(
        bucket=(env.get("VOLT_GCS_BUCKET") or "").strip(),
        prefix=env.get("VOLT_GCS_PREFIX") or "",
        project=env.get("VOLT_GCS_PROJECT") or None,
        endpoint_url=env.get("VOLT_GCS_ENDPOINT_URL") or None,
        url_expires=_env_int(env, "VOLT_GCS_URL_EXPIRES", DEFAULT_SAS),
        public_url_base=env.get("VOLT_GCS_PUBLIC_URL_BASE") or None,
        chunk_mb=_env_int(env, "VOLT_GCS_CHUNK_MB", 16),
    ))
