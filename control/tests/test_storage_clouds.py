"""Azure Blob and GCS backends: the shared backend contract over fakes of each SDK's client surface, their
specific behaviour (resume, corruption, error text, URLs, configuration), and (opt in) against a local
Azurite or fake-gcs-server emulator.

The fakes need neither SDK. Emulator tests run only when pointed at one and when the SDK is installed:

  VOLT_TEST_AZURITE_URL   e.g. http://127.0.0.1:10000/devstoreaccount1   (needs azure-storage-blob)
  VOLT_TEST_GCS_ENDPOINT  e.g. http://127.0.0.1:4443                      (needs google-cloud-storage)

Both use throwaway containers/buckets and Azurite's published development account key (a public constant,
not a credential). They never read or ask for real cloud credentials. Nothing here has run against a real
Azure or Google account."""

import hashlib
import importlib.util
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from volt import storage
from volt.storage import (
    AzureBlobBackend, AzureConfig, GcsBackend, GcsConfig, StorageConfigError, StorageError, StoredObject,
)
from tests.test_storage import BackendContract

MB = 1024 * 1024
AZURITE = os.environ.get("VOLT_TEST_AZURITE_URL")
GCS_EMULATOR = os.environ.get("VOLT_TEST_GCS_ENDPOINT")
HAVE_AZURE = bool(importlib.util.find_spec("azure.storage.blob")) if importlib.util.find_spec("azure") else False
HAVE_GCS = bool(importlib.util.find_spec("google.cloud.storage")) if importlib.util.find_spec("google") else False
# Azurite's documented, public development account. Not a secret.
AZURITE_KEY = ("Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==")


def _crc32c_b64(data: bytes) -> str:
    crc = 0xFFFFFFFF
    for byte in data:
        crc = storage._CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    import base64
    return base64.b64encode((crc ^ 0xFFFFFFFF).to_bytes(4, "big")).decode()


def _md5_b64(data: bytes) -> str:
    import base64
    return base64.b64encode(hashlib.md5(data).digest()).decode()


# ---- a fake of the azure-storage-blob surface the backend uses -------------------------------------

class _AzureMissing(Exception):
    status_code = 404
    error_code = "BlobNotFound"


class _AzureDenied(Exception):
    status_code = 403
    error_code = "AuthenticationFailed"


class _Block:
    def __init__(self, id, size):
        self.id, self.size = id, size


class _Download:
    def __init__(self, data):
        self.data = data

    def chunks(self):
        for i in range(0, len(self.data), 700):
            yield self.data[i:i + 700]


class FakeAzureBlob:
    def __init__(self, service, container, name):
        self.svc, self.key = service, (container, name)
        self.url = f"https://fake.invalid/{container}/{name}"

    def stage_block(self, block_id, data, length=None, validate_content=False):
        svc = self.svc
        svc.stage_calls += 1
        if svc.fail_stage_at is not None and svc.stage_calls == svc.fail_stage_at:
            raise svc.fail
        if svc.fail_always:
            raise svc.fail_always
        assert validate_content, "blocks must be sent with a Content-MD5"
        assert length == len(data) and data, "empty blocks are not allowed"
        svc.staged.setdefault(self.key, {})[block_id] = bytes(data)

    def get_block_list(self, kind):
        staged = self.svc.staged.get(self.key)
        if not staged and self.key not in self.svc.objects:
            raise _AzureMissing()
        return [], [_Block(i, len(d)) for i, d in (staged or {}).items()]

    def commit_block_list(self, blocks, content_settings=None, metadata=None):
        staged = self.svc.staged.get(self.key, {})
        data = b"".join(staged[getattr(b, "id", None) or b.block_id] for b in blocks)
        if self.svc.corrupt and data:
            data = bytes([data[0] ^ 1]) + data[1:]
        settings = content_settings if isinstance(content_settings, dict) else vars(content_settings)
        self.svc.objects[self.key] = {"data": data, "type": settings.get("content_type"),
                                      "md5": settings.get("content_md5"), "meta": metadata}
        self.svc.staged.pop(self.key, None)

    def get_blob_properties(self):
        obj = self.svc.objects.get(self.key)
        if obj is None:
            raise _AzureMissing()
        return type("Props", (), {"size": len(obj["data"])})()

    def download_blob(self):
        return _Download(self.svc.objects[self.key]["data"])

    def exists(self):
        return self.key in self.svc.objects

    def delete_blob(self, delete_snapshots=None):
        if self.key not in self.svc.objects:
            raise _AzureMissing()
        del self.svc.objects[self.key]


class FakeAzureService:
    def __init__(self, corrupt=False, fail=None, fail_stage_at=None, fail_always=None):
        self.objects, self.staged = {}, {}
        self.corrupt, self.fail, self.fail_stage_at, self.fail_always = corrupt, fail, fail_stage_at, fail_always
        self.stage_calls = 0

    def get_blob_client(self, container, name):
        return FakeAzureBlob(self, container, name)


class _OfflineAzure(AzureBlobBackend):
    def _sas(self, object_key, seconds):   # the real signer needs the SDK; the fake only records the lifetime
        return f"se={seconds}&sig=fake"


def _azure(service, **kw):
    cfg = AzureConfig(account="acct", container="vids", prefix=kw.pop("prefix", "pre"), **kw)
    backend = _OfflineAzure(cfg, client=service)
    cfg.block_mb = 1
    return backend


class FakeAzureContract(BackendContract, unittest.TestCase):
    def make_backend(self):
        self.service = FakeAzureService()
        return _azure(self.service)

    def read(self, key):
        return self.service.objects[("vids", "pre/" + key)]["data"]


class AzureBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, size):
        path = self.root / f"f{size}.bin"
        path.write_bytes(os.urandom(size))
        return path

    def test_key_prefix_content_type_and_metadata(self):
        service = FakeAzureService()
        src = self.file(10)
        obj = _azure(service).put_file(src, "a/b.mp4")
        stored = service.objects[("vids", "pre/a/b.mp4")]
        self.assertEqual(stored["type"], "video/mp4")
        self.assertEqual(stored["meta"], {"sha256": obj.sha256})
        self.assertEqual(bytes(stored["md5"]), hashlib.md5(src.read_bytes()).digest())
        self.assertEqual(obj.backend, "azure:acct/vids/pre")

    def test_blocks_span_the_file_and_empty_files_work(self):
        for size in (0, MB - 1, MB, 2 * MB, 3 * MB + 17):
            service = FakeAzureService()
            src = self.file(size)
            obj = _azure(service).put_file(src, "a/m.mp4")
            self.assertEqual(obj.size, size)
            self.assertEqual(service.objects[("vids", "pre/a/m.mp4")]["data"], src.read_bytes())
            self.assertEqual(service.stage_calls, -(-size // MB))

    def test_a_retry_restages_only_the_blocks_the_server_lacks(self):
        service = FakeAzureService(fail=_AzureDenied(), fail_stage_at=3)
        src = self.file(4 * MB + 5)
        with self.assertRaises(StorageError):
            _azure(service).put_file(src, "a/r.mp4")
        self.assertEqual(service.objects, {})               # nothing visible after a failed attempt
        self.assertEqual(len(service.staged[("vids", "pre/a/r.mp4")]), 2)
        service.stage_calls, service.fail_stage_at = 0, None
        _azure(service).put_file(src, "a/r.mp4")
        self.assertEqual(service.stage_calls, 3)             # 5 blocks, 2 already held
        self.assertEqual(service.objects[("vids", "pre/a/r.mp4")]["data"], src.read_bytes())

    def test_a_changed_file_does_not_reuse_stale_blocks(self):
        service = FakeAzureService(fail=_AzureDenied(), fail_stage_at=2)
        src = self.file(2 * MB)
        with self.assertRaises(StorageError):
            _azure(service).put_file(src, "a/s.mp4")
        src.write_bytes(os.urandom(2 * MB))                  # same size, new content
        _azure(service).put_file(src, "a/s.mp4")
        self.assertEqual(service.objects[("vids", "pre/a/s.mp4")]["data"], src.read_bytes())

    def test_a_corrupted_blob_fails_verification_and_is_removed(self):
        service = FakeAzureService(corrupt=True)
        with self.assertRaisesRegex(StorageError, "SHA-256"):
            _azure(service).put_file(self.file(100), "a/x.mp4")
        self.assertEqual(service.objects, {})

    def test_errors_name_the_reason_only(self):
        class Leaky(Exception):
            status_code, error_code = 403, "AuthenticationFailed"
        service = FakeAzureService(fail_always=Leaky(
            "https://x/vids/b?sig=SECRETSIG AccountKey=SECRETKEY"))
        with self.assertRaises(StorageError) as caught:
            _azure(service).put_file(self.file(10), "a/f.mp4")
        text = str(caught.exception)
        self.assertRegex(text, "Authentication|Authorization")
        for secret in ("SECRETSIG", "SECRETKEY"):
            self.assertNotIn(secret, text)
        plain = RuntimeError("connect failed: https://h/c/b?sv=1&sig=SECRETSIG&se=2")
        self.assertNotIn("SECRETSIG", AzureBlobBackend._explain(plain))
        self.assertNotIn("SECRETKEY", AzureBlobBackend._explain(RuntimeError("AccountKey=SECRETKEY;x")))

    def test_urls(self):
        backend = _azure(FakeAzureService(), url_expires=600)
        self.assertTrue(backend.url("a/b.mp4").endswith("?se=600&sig=fake"))
        self.assertTrue(backend.url("a/b.mp4", 30).endswith("?se=30&sig=fake"))
        for bad in (0, 7 * 24 * 3600 + 1):
            with self.assertRaises(StorageError):
                backend.url("a/b.mp4", bad)
        public = _azure(FakeAzureService(), public_url_base="https://cdn.example.com/")
        self.assertEqual(public.url("a/b c.mp4"), "https://cdn.example.com/pre/a/b%20c.mp4")

    def test_config_is_validated(self):
        for bad in (dict(account=""), dict(container=""), dict(url_expires=0), dict(block_mb=0)):
            cfg = dict(account="a", container="c")
            cfg.update(bad)
            with self.assertRaises(StorageConfigError, msg=str(bad)):
                AzureBlobBackend(AzureConfig(**cfg))

    @unittest.skipIf(HAVE_AZURE, "azure-storage-blob is installed; the missing-SDK path cannot be shown here")
    def test_missing_sdk_is_a_config_error(self):
        backend = AzureBlobBackend(AzureConfig(account="a", container="c"))
        with self.assertRaisesRegex(StorageConfigError, "azure-storage-blob"):
            backend.put_file(self.file(10), "a/b.mp4")

    def test_blocked_sdk_import_is_a_config_error(self):
        backend = AzureBlobBackend(AzureConfig(account="a", container="c"))
        blocked = {"azure": None, "azure.storage": None, "azure.storage.blob": None}
        with mock.patch.dict(sys.modules, blocked):
            with self.assertRaisesRegex(StorageConfigError, "pip install azure-storage-blob"):
                backend.put_file(self.file(10), "a/b.mp4")
            with self.assertRaises(StorageConfigError):
                backend.exists("a/b.mp4")


# ---- a fake of the google-cloud-storage surface the backend uses ------------------------------------

class _GcsMissing(Exception):
    code = 404
    reason = "Not Found"


class _GcsWriter:
    def __init__(self, blob, kw):
        self.blob, self.buf, self.kw = blob, bytearray(), kw

    def write(self, data):
        self.buf += data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.blob.client._finish_upload(self.blob.name, bytes(self.buf), self.kw)


class _GcsReader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeGcsBlob:
    def __init__(self, client, name):
        self.client, self.name = client, name
        self.size = self.md5_hash = self.crc32c = None

    def open(self, mode, chunk_size=None, **kw):
        assert chunk_size and chunk_size % (256 * 1024) == 0, "resumable chunk size must be a multiple of 256 KiB"
        if mode == "wb":
            assert kw.get("checksum") == "crc32c" and kw.get("content_type")
            if self.client.fail:
                raise self.client.fail
            return _GcsWriter(self, kw)
        if self.name not in self.client.objects:
            raise _GcsMissing()
        return _GcsReader(self.client.objects[self.name]["data"])

    def reload(self):
        obj = self.client.objects.get(self.name)
        if obj is None:
            raise _GcsMissing()
        self.size = len(obj["data"])
        claimed = obj["claimed"]
        self.md5_hash = _md5_b64(claimed) if self.client.md5 else None
        self.crc32c = _crc32c_b64(claimed) if self.client.crc else None

    def exists(self):
        return self.name in self.client.objects

    def delete(self):
        if self.name not in self.client.objects:
            raise _GcsMissing()
        del self.client.objects[self.name]

    def generate_signed_url(self, version, expiration, method, **kw):
        assert (version, method) == ("v4", "GET")
        return f"https://fake.invalid/{self.name}?X-Goog-Expires={int(expiration.total_seconds())}"


class FakeGcsClient:
    def __init__(self, corrupt=False, lie=False, md5=True, crc=True, fail=None):
        self.objects, self.corrupt, self.lie, self.md5, self.crc, self.fail = {}, corrupt, lie, md5, crc, fail

    def bucket(self, name):
        client = self
        return type("Bucket", (), {"blob": lambda _self, key: FakeGcsBlob(client, key)})()

    def _finish_upload(self, name, data, kw):
        stored = bytes([data[0] ^ 1]) + data[1:] if (self.corrupt or self.lie) and data else data
        # `corrupt`: the server's own hashes describe what it holds. `lie`: they describe what was sent.
        self.objects[name] = {"data": stored, "claimed": data if self.lie else stored, "type": kw["content_type"]}


def _gcs(client, **kw):
    cfg = GcsConfig(bucket="bkt", prefix=kw.pop("prefix", "pre"), **kw)
    return GcsBackend(cfg, client=client)


class FakeGcsContract(BackendContract, unittest.TestCase):
    def make_backend(self):
        self.client = FakeGcsClient()
        return _gcs(self.client)

    def read(self, key):
        return self.client.objects["pre/" + key]["data"]


class GcsBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, size):
        path = self.root / f"f{size}.bin"
        path.write_bytes(os.urandom(size))
        return path

    def test_key_prefix_and_content_type(self):
        client = FakeGcsClient()
        obj = _gcs(client).put_file(self.file(10), "a/b.mp4")
        self.assertEqual(client.objects["pre/a/b.mp4"]["type"], "video/mp4")
        self.assertEqual(obj.backend, "gcs:bkt/pre")

    def test_sizes_including_empty(self):
        for size in (0, 1, MB, 3 * MB + 17):
            client = FakeGcsClient()
            src = self.file(size)
            self.assertEqual(_gcs(client).put_file(src, "a/m.mp4").size, size)

    def test_corruption_is_caught_by_the_server_hashes_and_removed(self):
        for size in (100, 2 * MB):
            client = FakeGcsClient(corrupt=True)
            with self.assertRaisesRegex(StorageError, "mismatch"):
                _gcs(client).put_file(self.file(size), "a/x.mp4")
            self.assertEqual(client.objects, {})

    def test_sha256_readback_catches_what_the_server_hashes_miss(self):
        client = FakeGcsClient(lie=True)
        with self.assertRaisesRegex(StorageError, "SHA-256"):
            _gcs(client).put_file(self.file(100), "a/x.mp4")
        self.assertEqual(client.objects, {})

    def test_one_server_hash_is_enough_none_is_refused(self):
        _gcs(FakeGcsClient(md5=False)).put_file(self.file(10), "a/k.mp4")
        _gcs(FakeGcsClient(crc=False)).put_file(self.file(10), "a/k.mp4")
        with self.assertRaisesRegex(StorageError, "cannot be verified"):
            _gcs(FakeGcsClient(md5=False, crc=False)).put_file(self.file(10), "a/k.mp4")

    def test_errors_name_the_reason_only(self):
        class Denied(Exception):
            code, reason = 403, "Forbidden"
        with self.assertRaises(StorageError) as caught:
            _gcs(FakeGcsClient(fail=Denied("token=SECRETTOKEN"))).put_file(self.file(10), "a/f.mp4")
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("SECRETTOKEN", str(caught.exception))
        self.assertNotIn("SECRETTOKEN", GcsBackend._explain(RuntimeError("x X-Goog-Signature=SECRETTOKEN&y")))

    def test_urls(self):
        backend = _gcs(FakeGcsClient(), url_expires=600)
        self.assertTrue(backend.url("a/b.mp4").endswith("X-Goog-Expires=600"))
        self.assertTrue(backend.url("a/b.mp4", 30).endswith("X-Goog-Expires=30"))
        for bad in (0, 7 * 24 * 3600 + 1):
            with self.assertRaises(StorageError):
                backend.url("a/b.mp4", bad)
        public = _gcs(FakeGcsClient(), public_url_base="https://cdn.example.com/")
        self.assertEqual(public.url("a/b c.mp4"), "https://cdn.example.com/pre/a/b%20c.mp4")

    def test_a_credential_without_a_key_falls_back_to_iam_signing(self):
        class NoKey(FakeGcsBlob):
            def generate_signed_url(self, **kw):
                if "access_token" not in kw:
                    raise AttributeError("you need a private key to sign credentials")
                return f"https://fake.invalid/{self.name}?signed-by={kw['service_account_email']}"
        client = FakeGcsClient()
        client._credentials = mock.Mock(service_account_email="svc@p.iam.gserviceaccount.com", token="t")
        backend = _gcs(client)
        backend._blob = lambda key: NoKey(client, key)
        if not HAVE_GCS:
            self.skipTest("google-auth is needed for the IAM fallback transport")
        self.assertIn("signed-by=svc@p.iam", backend.url("a/b.mp4"))

    def test_config_is_validated(self):
        for bad in (dict(bucket=""), dict(url_expires=0), dict(chunk_mb=0)):
            cfg = dict(bucket="b")
            cfg.update(bad)
            with self.assertRaises(StorageConfigError, msg=str(bad)):
                GcsBackend(GcsConfig(**cfg))

    def test_blocked_sdk_import_is_a_config_error(self):
        backend = GcsBackend(GcsConfig(bucket="b"))
        blocked = {"google": None, "google.cloud": None, "google.cloud.storage": None}
        with mock.patch.dict(sys.modules, blocked):
            with self.assertRaisesRegex(StorageConfigError, "pip install google-cloud-storage"):
                backend.put_file(self.file(10), "a/b.mp4")
            with self.assertRaises(StorageConfigError):
                backend.delete("a/b.mp4")


class Configuration(unittest.TestCase):
    def test_azure_from_env_with_a_url(self):
        backend = storage.from_env({
            "VOLT_STORAGE": "azure", "VOLT_AZURE_ACCOUNT_URL": "https://myacct.blob.core.windows.net",
            "VOLT_AZURE_CONTAINER": "shows", "VOLT_AZURE_PREFIX": "/cuts/", "VOLT_AZURE_URL_EXPIRES": "3600",
            "VOLT_AZURE_BLOCK_MB": "8"})
        self.assertEqual(backend.id, "azure:myacct/shows/cuts")
        self.assertEqual((backend.config.url_expires, backend.config.block_mb), (3600, 8))

    def test_azure_account_is_read_from_a_connection_string_without_touching_the_key(self):
        backend = storage.from_env({
            "VOLT_STORAGE": "azure", "VOLT_AZURE_CONTAINER": "c",
            "AZURE_STORAGE_CONNECTION_STRING": "DefaultEndpointsProtocol=http;AccountName=devacct;AccountKey=K3Y;"})
        self.assertEqual(backend.id, "azure:devacct/c")
        self.assertNotIn("K3Y", repr(backend.config))

    def test_azure_errors(self):
        base = {"VOLT_STORAGE": "azure"}
        with self.assertRaises(StorageConfigError):     # no account
            storage.from_env({**base, "VOLT_AZURE_CONTAINER": "c"})
        with self.assertRaises(StorageConfigError):     # no container
            storage.from_env({**base, "VOLT_AZURE_ACCOUNT_URL": "https://a.blob.core.windows.net"})
        with self.assertRaises(StorageConfigError):
            storage.from_env({**base, "VOLT_AZURE_ACCOUNT_URL": "https://a.blob.core.windows.net",
                              "VOLT_AZURE_CONTAINER": "c", "VOLT_AZURE_URL_EXPIRES": "soon"})

    def test_gcs_from_env(self):
        backend = storage.from_env({
            "VOLT_STORAGE": "gcs", "VOLT_GCS_BUCKET": "bkt", "VOLT_GCS_PREFIX": "shows/",
            "VOLT_GCS_PROJECT": "proj", "VOLT_GCS_ENDPOINT_URL": "http://x:1", "VOLT_GCS_URL_EXPIRES": "60",
            "VOLT_GCS_CHUNK_MB": "4"})
        self.assertEqual(backend.id, "gcs:bkt/shows")
        self.assertEqual((backend.config.project, backend.config.endpoint_url, backend.config.url_expires,
                          backend.config.chunk_mb), ("proj", "http://x:1", 60, 4))
        with self.assertRaises(StorageConfigError):
            storage.from_env({"VOLT_STORAGE": "gcs"})
        with self.assertRaises(StorageConfigError):
            storage.from_env({"VOLT_STORAGE": "gcs", "VOLT_GCS_BUCKET": "b", "VOLT_GCS_CHUNK_MB": "big"})

    def test_refresh_reads_the_expiry_of_azure_and_gcs_urls(self):
        from datetime import datetime, timezone
        from volt.publish import url_expiry
        self.assertEqual(url_expiry("https://a.blob.core.windows.net/c/b?se=2026-10-19T00%3A00%3A00Z&sig=x&sp=r"),
                         datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertEqual(url_expiry("https://h/b/o?X-Goog-Date=20261012T120000Z&X-Goog-Expires=90&X-Goog-Signature=x"),
                         datetime(2026, 10, 12, 12, 1, 30, tzinfo=timezone.utc))
        self.assertIsNone(url_expiry("https://a.blob.core.windows.net/c/b?se=not-a-date&sig=x"))
        self.assertIsNone(url_expiry("https://cdn.example.com/c/b?se=2026-10-19T00:00:00Z"))   # no sig: not a SAS

    def test_the_sdks_are_not_imported_until_used(self):
        code = ("import sys; from volt import storage, publish, runner;"
                "storage.from_env({'VOLT_STORAGE': 'azure', 'VOLT_AZURE_ACCOUNT_URL': 'https://a.blob.core.windows.net',"
                " 'VOLT_AZURE_CONTAINER': 'c'});"
                "storage.from_env({'VOLT_STORAGE': 'gcs', 'VOLT_GCS_BUCKET': 'b'});"
                "assert not [m for m in sys.modules if m.split('.')[0] in ('azure', 'google', 'boto3')]; print('ok')")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)


# ---- emulators (opt in) --------------------------------------------------------------------------------

def _azurite_connection(url: str) -> str:
    return ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey={AZURITE_KEY};BlobEndpoint={url};")


@unittest.skipUnless(AZURITE and HAVE_AZURE, "set VOLT_TEST_AZURITE_URL and install azure-storage-blob")
class AzuriteContract(BackendContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from azure.storage.blob import BlobServiceClient
        cls.container = "volt-" + uuid.uuid4().hex[:12]
        cls.conn = _azurite_connection(AZURITE)
        cls.service = BlobServiceClient.from_connection_string(cls.conn)
        cls.service.create_container(cls.container)

    @classmethod
    def tearDownClass(cls):
        cls.service.delete_container(cls.container)

    def make_backend(self):
        patcher = mock.patch.dict(os.environ, {"AZURE_STORAGE_CONNECTION_STRING": self.conn})
        patcher.start()
        self.addCleanup(patcher.stop)
        return storage.from_env({"VOLT_STORAGE": "azure", "VOLT_AZURE_CONTAINER": self.container,
                                 "AZURE_STORAGE_CONNECTION_STRING": self.conn, "VOLT_AZURE_BLOCK_MB": "1",
                                 "VOLT_AZURE_PREFIX": "t"})

    def test_block_boundaries_content_type_and_stored_hash(self):
        for size in (0, MB - 1, MB, 2 * MB + 7):
            data = os.urandom(size)
            obj = self.backend.put_file(self._file(data, "b.mp4"), f"sz/{size}.mp4")
            self.assertEqual(obj.sha256, hashlib.sha256(data).hexdigest())
            props = self.service.get_blob_client(self.container, f"t/sz/{size}.mp4").get_blob_properties()
            self.assertEqual((props.size, props.content_settings.content_type), (size, "video/mp4"))
            self.assertEqual(props.metadata["sha256"], obj.sha256)
            self.assertEqual(self.read(f"sz/{size}.mp4"), data)

    def test_sas_url_expires_and_rejects_tampering(self):
        self.backend.put_file(self._file(b"hello"), "x/exp.mp4")
        url = self.backend.url("x/exp.mp4", 2)
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.read(), b"hello")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url.replace("sig=", "sig=A"))
        self.assertIn(caught.exception.code, (401, 403))
        time.sleep(4)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url)
        self.assertIn(caught.exception.code, (401, 403))

    def test_an_interrupted_upload_resumes_from_the_staged_blocks(self):
        from azure.storage.blob import BlobClient
        real, calls, dropped = BlobClient.stage_block, [], []

        def flaky(client, *a, **kw):
            calls.append(kw.get("block_id") or a[0])
            if len(calls) == 3 and not dropped:
                dropped.append(1)
                raise ConnectionError("link dropped")
            return real(client, *a, **kw)
        data = os.urandom(4 * MB + 5)
        src = self._file(data)
        with mock.patch.object(BlobClient, "stage_block", flaky):
            with self.assertRaises(StorageError):
                self.backend.put_file(src, "x/resume.mp4")
            self.assertFalse(self.backend.exists("x/resume.mp4"))      # nothing visible yet
            calls.clear()
            obj = self.backend.put_file(src, "x/resume.mp4")
        self.assertEqual(len(calls), 3)                                # 5 blocks, 2 already on the server
        self.assertEqual(obj.sha256, hashlib.sha256(data).hexdigest())

    def test_bad_credentials_fail_with_a_reason_and_no_leak(self):
        bad = _azurite_connection(AZURITE).replace(AZURITE_KEY, "AAAA" + AZURITE_KEY[4:])
        with mock.patch.dict(os.environ, {"AZURE_STORAGE_CONNECTION_STRING": bad}):
            backend = storage.from_env({"VOLT_STORAGE": "azure", "VOLT_AZURE_CONTAINER": self.container,
                                        "AZURE_STORAGE_CONNECTION_STRING": bad})
            with self.assertRaises(StorageError) as caught:
                backend.put_file(self._file(b"x"), "x/bad.mp4")
        text = str(caught.exception)
        self.assertNotIn(AZURITE_KEY[4:], text)
        self.assertRegex(text, "Authentication|Authorization")


def _signing_credentials():
    """Anonymous credentials that can also sign (a throwaway RSA key), so V4 URLs can be made against an
    emulator that holds no real service account."""
    from google.auth import crypt
    from google.auth.credentials import AnonymousCredentials, Signing
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    signer = crypt.RSASigner.from_string(pem)

    class SigningAnonymous(AnonymousCredentials, Signing):
        def sign_bytes(self, message):
            return signer.sign(message)

        @property
        def signer_email(self):
            return "volt-test@example.iam.gserviceaccount.com"

        @property
        def signer(self):
            return signer
    return SigningAnonymous()


@unittest.skipUnless(GCS_EMULATOR and HAVE_GCS, "set VOLT_TEST_GCS_ENDPOINT and install google-cloud-storage")
class FakeGcsServerContract(BackendContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from google.cloud import storage as gcs
        cls.bucket_name = "volt-" + uuid.uuid4().hex[:12]
        cls.client = gcs.Client(project="volt-test", credentials=_signing_credentials(),
                                client_options={"api_endpoint": GCS_EMULATOR})
        cls.client.create_bucket(cls.bucket_name)

    @classmethod
    def tearDownClass(cls):
        for blob in list(cls.client.list_blobs(cls.bucket_name)):
            blob.delete()
        cls.client.bucket(cls.bucket_name).delete()

    def make_backend(self):
        return GcsBackend(GcsConfig(bucket=self.bucket_name, prefix="t", endpoint_url=GCS_EMULATOR,
                                    chunk_mb=1), client=self.client)

    def test_chunk_boundaries_content_type_and_server_hashes(self):
        for size in (0, MB - 1, MB, 2 * MB + 7):
            data = os.urandom(size)
            obj = self.backend.put_file(self._file(data, "b.mp4"), f"sz/{size}.mp4")
            self.assertEqual(obj.sha256, hashlib.sha256(data).hexdigest())
            blob = self.client.bucket(self.bucket_name).get_blob(f"t/sz/{size}.mp4")
            self.assertEqual((blob.size, blob.content_type), (size, "video/mp4"))
            self.assertEqual(blob.md5_hash, _md5_b64(data))
            self.assertEqual(self.read(f"sz/{size}.mp4"), data)

    def read(self, key):
        # fake-gcs-server does not serve the path-style signed URL for names containing "/", so read the object
        # through its JSON API instead. Fetching through the signed URL is NOT proven on GCS.
        from urllib.parse import quote
        url = f"{GCS_EMULATOR}/storage/v1/b/{self.bucket_name}/o/{quote('t/' + key, safe='')}?alt=media"
        with urllib.request.urlopen(url) as response:
            return response.read()

    def test_the_signed_url_carries_the_requested_lifetime(self):
        # fake-gcs-server does not validate signatures or expiry, so only the URL's shape is proven here.
        url = self.backend.url("x/exp.mp4", 90)
        self.assertIn("X-Goog-Algorithm=GOOG4-RSA-SHA256", url)
        self.assertIn("X-Goog-Expires=90", url)
        self.assertIn("X-Goog-Signature=", url)



if __name__ == "__main__":
    unittest.main()
