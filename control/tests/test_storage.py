"""Storage seam: the backend contract, the S3 backend over a fake client, and (opt in) over a real
S3-compatible server.

Live tests run only when VOLT_TEST_S3_ENDPOINT is set (for example http://localhost:59000 for MinIO). They
need boto3 and credentials in the standard AWS environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) and
use the bucket VOLT_TEST_S3_BUCKET (default volt-test), creating it if it is missing. They never read or ask
for real AWS credentials; point them only at a throwaway local server."""

import base64
import hashlib
import importlib.util
import os
import subprocess
import sys
import unittest
import urllib.request
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from volt import deliverables, publish as publish_mod, storage
from volt.publish import publish_files, refresh_url
from volt.store import Store
from volt.storage import (
    AzureBlobBackend, GcsBackend, LocalBackend, S3Backend, S3Config, StorageConfigError, StorageError,
    StoredObject,
)

MB = 1024 * 1024
LIVE = os.environ.get("VOLT_TEST_S3_ENDPOINT")


class BackendContract:
    """What any backend must do. A new backend (Azure, GCS) subclasses this and supplies make_backend."""

    def make_backend(self) -> storage.Backend:
        raise NotImplementedError

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = self.make_backend()

    def _file(self, data: bytes, name="src.mp4") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_put_returns_a_verified_reference(self):
        data = os.urandom(5000)
        obj = self.backend.put_file(self._file(data), "asset1/span1-house.mp4")
        self.assertIsInstance(obj, StoredObject)
        self.assertEqual(obj.backend, self.backend.id)
        self.assertEqual(obj.key, "asset1/span1-house.mp4")
        self.assertEqual(obj.size, 5000)
        self.assertEqual(obj.sha256, hashlib.sha256(data).hexdigest())
        self.assertTrue(obj.url)

    def test_exists_and_delete(self):
        key = "asset1/span1-ott.mp4"
        self.assertFalse(self.backend.exists(key))
        self.backend.put_file(self._file(b"x" * 10), key)
        self.assertTrue(self.backend.exists(key))
        self.backend.delete(key)
        self.assertFalse(self.backend.exists(key))
        self.backend.delete(key)  # deleting a missing key is not an error

    def test_put_replaces_an_existing_object(self):
        key = "a/b.mp4"
        self.backend.put_file(self._file(b"one"), key)
        obj = self.backend.put_file(self._file(b"two!", "src2.mp4"), key)
        self.assertEqual(obj.size, 4)
        self.assertEqual(self.read(key), b"two!")

    def test_url_reads_back_the_bytes(self):
        data = os.urandom(3000)
        self.backend.put_file(self._file(data), "a/c.mp4")
        self.assertEqual(self.read("a/c.mp4"), data)

    def test_missing_source_is_a_storage_error(self):
        with self.assertRaises(StorageError):
            self.backend.put_file(self.root / "nope.mp4", "a/d.mp4")

    def test_unsafe_keys_are_refused(self):
        src = self._file(b"x")
        for key in ("", "/abs", "a/../b", "a//b", "../x", "a\\b", "a/./b", "x" * 901):
            with self.assertRaises(StorageError, msg=key):
                self.backend.put_file(src, key)

    def read(self, key: str) -> bytes:
        with urllib.request.urlopen(self.backend.url(key)) as response:
            return response.read()


class LocalContract(BackendContract, unittest.TestCase):
    def make_backend(self):
        return LocalBackend(Path(self.tmp.name) / "out")

    def test_url_is_a_file_url_and_in_place_files_are_kept(self):
        dest = Path(self.tmp.name) / "out" / "a" / "b.mp4"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"cut")
        obj = self.backend.put_file(dest, "a/b.mp4")  # already where it belongs: verified, not copied
        self.assertTrue(obj.url.startswith("file:///"))
        self.assertEqual(dest.read_bytes(), b"cut")


# ---- a fake S3 client that behaves like the real checksum surface --------------------------------

class FakeS3Client:
    """upload_file / head_object / generate_presigned_url / delete_object with S3's checksum rules: a
    single PUT has a SHA-256 and an MD5 ETag; a multipart upload has a composite of the part digests."""

    def __init__(self, part=5 * MB, checksum=True, etag=True, corrupt=False, fail=None, kms=False):
        self.objects, self.part = {}, part
        self.checksum, self.etag, self.corrupt, self.fail, self.kms = checksum, etag, corrupt, fail, kms
        self.uploads = 0

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
        self.uploads += 1
        if self.fail:
            raise self.fail
        data = Path(Filename).read_bytes()
        stored = bytes([data[0] ^ 1]) + data[1:] if self.corrupt and data else data
        part = self.part
        pieces = [stored[i:i + part] for i in range(0, max(len(stored), 1), part)] if len(stored) >= part \
            else [stored]
        multipart = len(stored) >= part
        if multipart:
            sha = base64.b64encode(hashlib.sha256(b"".join(hashlib.sha256(p).digest() for p in pieces)).digest())
            etag = hashlib.md5(b"".join(hashlib.md5(p).digest() for p in pieces)).hexdigest() + f"-{len(pieces)}"
            sha = sha.decode() + f"-{len(pieces)}"
        else:
            sha = base64.b64encode(hashlib.sha256(stored).digest()).decode()
            etag = hashlib.md5(stored).hexdigest()
        self.objects[(Bucket, Key)] = {
            "data": stored, "type": (ExtraArgs or {}).get("ContentType"), "ChecksumSHA256": sha, "ETag": f'"{etag}"'}

    def head_object(self, Bucket, Key, ChecksumMode=None):
        obj = self.objects.get((Bucket, Key))
        if obj is None:
            raise _NotFound()
        head = {"ContentLength": len(obj["data"])}
        if self.checksum:
            head["ChecksumSHA256"] = obj["ChecksumSHA256"]
        if self.etag:
            head["ETag"] = obj["ETag"]
        if self.kms:
            head["ServerSideEncryption"] = "aws:kms"
        return head

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://fake.invalid/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class _NotFound(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 404}, "Error": {"Code": "404", "Message": "Not Found"}}


def _s3(client, **kw):
    cfg = S3Config(bucket="b", prefix=kw.pop("prefix", "pre"), **kw)
    backend = S3Backend(cfg, client=client)
    cfg.part_mb = 1  # the constructor enforces S3's 5 MiB floor; tests use small parts against the fake
    client.part = MB
    return backend


class FakeS3Contract(BackendContract, unittest.TestCase):
    def make_backend(self):
        self.client = FakeS3Client()
        return _s3(self.client)

    def read(self, key):  # the fake's URLs are not fetchable; read what it holds
        return self.client.objects[("b", "pre/" + key)]["data"]


class S3Behaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, size):
        path = self.root / f"f{size}.bin"
        path.write_bytes(os.urandom(size))
        return path

    def test_key_gets_the_prefix_and_content_type(self):
        client = FakeS3Client()
        _s3(client).put_file(self.file(10), "a/b.mp4")
        self.assertEqual(list(client.objects), [("b", "pre/a/b.mp4")])
        self.assertEqual(client.objects[("b", "pre/a/b.mp4")]["type"], "video/mp4")

    def test_multipart_is_verified_with_the_composite_checksum(self):
        for size in (MB - 1, MB, 2 * MB, 3 * MB + 17):
            client = FakeS3Client()
            obj = _s3(client).put_file(self.file(size), "a/m.mp4")
            self.assertEqual(obj.size, size)

    def test_a_corrupted_upload_fails_and_is_removed(self):
        for size in (100, 3 * MB):
            client = FakeS3Client(corrupt=True)
            with self.assertRaisesRegex(StorageError, "mismatch"):
                _s3(client).put_file(self.file(size), "a/x.mp4")
            self.assertEqual(client.objects, {})

    def test_etag_is_the_fallback_when_there_is_no_sha256(self):
        client = FakeS3Client(checksum=False)
        _s3(client).put_file(self.file(2 * MB + 5), "a/e.mp4")
        with self.assertRaisesRegex(StorageError, "mismatch"):
            _s3(FakeS3Client(checksum=False, corrupt=True)).put_file(self.file(500), "a/e2.mp4")

    def test_an_unverifiable_upload_is_refused(self):
        with self.assertRaisesRegex(StorageError, "cannot be verified"):
            _s3(FakeS3Client(checksum=False, etag=False)).put_file(self.file(10), "a/n.mp4")
        with self.assertRaisesRegex(StorageError, "cannot be verified"):  # a KMS ETag is not an MD5
            _s3(FakeS3Client(checksum=False, kms=True)).put_file(self.file(10), "a/k.mp4")

    def test_a_failed_upload_names_the_reason_only(self):
        class Denied(Exception):
            response = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"},
                        "ResponseMetadata": {"HTTPStatusCode": 403}}
        with self.assertRaises(StorageError) as caught:
            _s3(FakeS3Client(fail=Denied("secret detail should not leak"))).put_file(self.file(10), "a/f.mp4")
        self.assertIn("AccessDenied", str(caught.exception))
        self.assertNotIn("secret detail", str(caught.exception))

    def test_urls(self):
        backend = _s3(FakeS3Client(), url_expires=600)
        self.assertTrue(backend.url("a/b.mp4").endswith("?expires=600"))
        self.assertTrue(backend.url("a/b.mp4", 30).endswith("?expires=30"))
        for bad in (0, 7 * 24 * 3600 + 1):
            with self.assertRaises(StorageError):
                backend.url("a/b.mp4", bad)
        public = _s3(FakeS3Client(), public_url_base="https://cdn.example.com/")
        self.assertEqual(public.url("a/b c.mp4"), "https://cdn.example.com/pre/a/b%20c.mp4")

    def test_config_is_validated(self):
        with self.assertRaises(StorageConfigError):
            S3Backend(S3Config(bucket=""))
        with self.assertRaises(StorageConfigError):
            S3Backend(S3Config(bucket="b", url_expires=0))
        with self.assertRaises(StorageConfigError):
            S3Backend(S3Config(bucket="b", part_mb=1))


class Configuration(unittest.TestCase):
    def test_default_is_local_under_the_publish_dir(self):
        backend = storage.from_env({}, local_root="out")
        self.assertIsInstance(backend, LocalBackend)
        with self.assertRaises(StorageConfigError):
            storage.from_env({})

    def test_s3_from_env(self):
        backend = storage.from_env({
            "VOLT_STORAGE": "s3", "VOLT_S3_BUCKET": "bkt", "VOLT_S3_PREFIX": "/shows/",
            "VOLT_S3_REGION": "eu-west-2", "VOLT_S3_ENDPOINT_URL": "http://x:1", "VOLT_S3_URL_EXPIRES": "3600"})
        self.assertEqual(backend.id, "s3:bkt/shows")
        self.assertEqual((backend.config.region, backend.config.endpoint_url, backend.config.url_expires),
                         ("eu-west-2", "http://x:1", 3600))
        with self.assertRaises(StorageConfigError):
            storage.from_env({"VOLT_STORAGE": "s3"})
        with self.assertRaises(StorageConfigError):
            storage.from_env({"VOLT_STORAGE": "s3", "VOLT_S3_BUCKET": "b", "VOLT_S3_URL_EXPIRES": "soon"})
        with self.assertRaises(StorageConfigError):
            storage.from_env({"VOLT_STORAGE": "ftp"})

    def test_azure_and_gcs_need_their_own_settings(self):
        # the backends themselves are covered in test_storage_clouds.py
        for kind in ("azure", "gcs"):
            with self.assertRaises(StorageConfigError):
                storage.from_env({"VOLT_STORAGE": kind})

    def test_boto3_is_not_imported_until_s3_is_used(self):
        code = ("import sys; from volt import storage, publish, runner;"
                "b = storage.from_env({'VOLT_STORAGE': 's3', 'VOLT_S3_BUCKET': 'b'});"
                "assert 'boto3' not in sys.modules; print('ok')")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)


# ---- publish through a backend -------------------------------------------------------------------

def _asset_and_span(store, root):
    from tests.test_deliverables import _asset, _span
    asset = _asset(store, root)
    return asset, _span(store, asset)


@unittest.skipUnless(importlib.util.find_spec("boto3"), "boto3 is not installed")
class PresignedUrlHost(unittest.TestCase):
    """Real AWS rejects a URL signed for one region but pointed at the global host (seen live, us-east-2)."""

    def _url(self, **config):
        backend = S3Backend(S3Config(bucket="volt-bucket", prefix="p", **config))
        backend._client = None
        env = {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "x", "AWS_EC2_METADATA_DISABLED": "true"}
        with mock.patch.dict(os.environ, env):
            return backend.url("k.mp4")

    def test_aws_url_names_the_regional_host(self):
        url = self._url(region="us-east-2")
        self.assertTrue(url.startswith("https://volt-bucket.s3.us-east-2.amazonaws.com/p/k.mp4?"), url)

    def test_s3_compatible_endpoint_stays_path_style(self):
        url = self._url(region="us-east-1", endpoint_url="http://localhost:9000")
        self.assertTrue(url.startswith("http://localhost:9000/volt-bucket/p/k.mp4?"), url)


HAVE_BOTO = bool(importlib.util.find_spec("boto3"))
_NO_AWS = {"AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "us-east-2"}


def _aws_free_env(tmp, **extra):
    """os.environ patch with every AWS credential source removed (env keys, profile, shared files)."""
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("AWS_"):
            env.pop(name)
    env.update(_NO_AWS)
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(Path(tmp) / "no-credentials")
    env["AWS_CONFIG_FILE"] = str(Path(tmp) / "no-config")
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class S3Encryption(unittest.TestCase):
    """SSE-KMS (or SSE-S3) on every put, single and multipart."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, size):
        path = self.root / "f.mp4"
        path.write_bytes(os.urandom(size))
        return path

    def test_from_env_reads_sse_and_key_id(self):
        base = {"VOLT_STORAGE": "s3", "VOLT_S3_BUCKET": "b"}
        c = storage.from_env({**base, "VOLT_S3_SSE": "KMS", "VOLT_S3_KMS_KEY_ID": " alias/volt "}).config
        self.assertEqual((c.sse, c.kms_key_id), ("aws:kms", "alias/volt"))
        self.assertEqual(storage.from_env({**base, "VOLT_S3_SSE": "aes256"}).config.sse, "AES256")
        none = storage.from_env(base).config
        self.assertEqual((none.sse, none.kms_key_id), (None, None))
        with self.assertRaises(StorageConfigError):
            storage.from_env({**base, "VOLT_S3_SSE": "rot13"})
        with self.assertRaisesRegex(StorageConfigError, "needs VOLT_S3_SSE"):
            storage.from_env({**base, "VOLT_S3_KMS_KEY_ID": "k"})
        with self.assertRaisesRegex(StorageConfigError, "http"):
            storage.from_env({**base, "VOLT_S3_PUBLIC_URL_BASE": "cdn.example.com"})

    def test_put_sends_the_encryption_arguments(self):
        seen = {}

        class Recording(FakeS3Client):
            def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
                seen.update(ExtraArgs)
                super().upload_file(Filename, Bucket, Key, ExtraArgs, Config)

            def head_object(self, Bucket, Key, ChecksumMode=None):
                head = super().head_object(Bucket, Key, ChecksumMode)
                head["ServerSideEncryption"] = "aws:kms"
                return head

        backend = _s3(Recording(), sse="aws:kms", kms_key_id="alias/volt")
        backend.put_file(self.file(100), "a/k.mp4")
        self.assertEqual((seen["ServerSideEncryption"], seen["SSEKMSKeyId"]), ("aws:kms", "alias/volt"))
        plain = {}

        class Plain(FakeS3Client):
            def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
                plain.update(ExtraArgs)
                super().upload_file(Filename, Bucket, Key, ExtraArgs, Config)
        _s3(Plain()).put_file(self.file(100), "a/k.mp4")
        self.assertNotIn("ServerSideEncryption", plain)
        self.assertNotIn("SSEKMSKeyId", plain)

    def test_a_server_that_did_not_encrypt_is_not_a_good_upload(self):
        client = FakeS3Client()
        with self.assertRaisesRegex(StorageError, "not encrypted as configured"):
            _s3(client, sse="aws:kms").put_file(self.file(100), "a/k.mp4")
        self.assertEqual(client.objects, {})  # deleted, not left looking good

    def test_kms_upload_verifies_by_sha256_when_the_etag_is_not_an_md5(self):
        client = FakeS3Client(kms=True, etag=True)
        obj = _s3(client, sse="aws:kms").put_file(self.file(1000), "a/k.mp4")
        self.assertEqual(obj.size, 1000)

    @unittest.skipUnless(HAVE_BOTO, "boto3 is not installed")
    def test_real_boto3_client_puts_the_kms_headers_single_and_multipart(self):
        """botocore Stubber: boto3 validates the SSE arguments on the real PutObject / CreateMultipartUpload calls
        (parameter validation, not a wire capture); no network, no AWS."""
        import boto3
        from botocore.stub import ANY, Stubber
        key_arn = "arn:aws:kms:us-east-2:111122223333:key/abc"
        for size in (2048, 5 * MB + 1):
            with self.subTest(size=size), _aws_free_env(self.tmp.name, AWS_ACCESS_KEY_ID="AKIAFAKE",
                                                        AWS_SECRET_ACCESS_KEY="fake"):
                boto3.setup_default_session()  # a fresh session: the default one caches resolved credentials
                backend = S3Backend(S3Config(bucket="b", prefix="p", region="us-east-2", sse="aws:kms",
                                             kms_key_id=key_arn, part_mb=5))
                client = backend.client
                src = self.file(size)
                multipart = size >= 5 * MB
                sums = storage._part_digests(src, 5 * MB if multipart else size)
                sha = (storage._b64(hashlib.sha256(b"".join(d for d, _ in sums)).digest()) + f"-{len(sums)}") \
                    if multipart else storage._b64(sums[0][0])
                sse = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": key_arn}
                with Stubber(client) as stub:
                    if multipart:
                        stub.add_response("create_multipart_upload", {"UploadId": "u", "Bucket": "b", "Key": "k"},
                                          {"Bucket": "b", "Key": "p/a/k.mp4", "ContentType": "video/mp4",
                                           "ChecksumAlgorithm": "SHA256", **sse})
                        for n in range(len(sums)):
                            stub.add_response("upload_part", {"ETag": f'"e{n}"',
                                                              "ChecksumSHA256": storage._b64(sums[n][0])})
                        stub.add_response("complete_multipart_upload", {"Bucket": "b", "Key": "k"})
                    else:
                        stub.add_response("put_object", {"ETag": '"x"', "ServerSideEncryption": "aws:kms"},
                                          {"Bucket": "b", "Key": "p/a/k.mp4", "Body": ANY, "ContentType": "video/mp4",
                                           "ChecksumAlgorithm": "SHA256", **sse})
                    stub.add_response("head_object", {"ContentLength": size, "ChecksumSHA256": sha,
                                                      "ServerSideEncryption": "aws:kms"})
                    backend.put_file(src, "a/k.mp4")
                    stub.assert_no_pending_responses()


class S3PublicMode(unittest.TestCase):
    def test_public_base_urls_are_stable_unsigned_and_ignore_expiry(self):
        backend = _s3(FakeS3Client(), public_url_base="https://cdn.example.com/media/", prefix="shows")
        first = backend.url("a/b.mp4")
        self.assertEqual(first, "https://cdn.example.com/media/shows/a/b.mp4")
        self.assertEqual(backend.url("a/b.mp4", expires=5), first)
        self.assertNotIn("?", first)

    def test_put_returns_the_public_url_and_never_signs(self):
        class NoSign(FakeS3Client):
            def generate_presigned_url(self, *a, **k):
                raise AssertionError("public mode must not sign")
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "f.mp4"
            f.write_bytes(b"x" * 50)
            obj = _s3(NoSign(), public_url_base="https://cdn.example.com").put_file(f, "a/b.mp4")
        self.assertEqual(obj.url, "https://cdn.example.com/pre/a/b.mp4")


@unittest.skipUnless(HAVE_BOTO, "boto3 is not installed")
class S3DefaultCredentialChain(unittest.TestCase):
    """No static key is needed or read: whatever boto3's chain finds is used. The container-role endpoint is
    served from a local port 0 socket, so no AWS is contacted. EC2 IMDS and web-identity are NOT exercised."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _fresh_session(self):
        import boto3
        boto3.setup_default_session()  # the default session caches resolved credentials across tests
        self.addCleanup(boto3.setup_default_session)

    def _serve(self):
        import http.server
        import json
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            seen_auth = []

            def do_GET(self):
                Handler.seen_auth.append(self.headers.get("Authorization"))
                body = json.dumps({"AccessKeyId": "ASIAROLEKEY", "SecretAccessKey": "rolesecret",
                                   "Token": "roletoken", "Expiration": "2099-01-01T00:00:00Z"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, Handler

    def test_url_is_signed_with_role_credentials_and_no_static_keys(self):
        server, handler = self._serve()
        uri = f"http://127.0.0.1:{server.server_address[1]}/creds"
        with _aws_free_env(self.tmp.name, AWS_CONTAINER_CREDENTIALS_FULL_URI=uri,
                           AWS_CONTAINER_AUTHORIZATION_TOKEN="tok"):
            self._fresh_session()
            backend = storage.from_env({"VOLT_STORAGE": "s3", "VOLT_S3_BUCKET": "b", "VOLT_S3_REGION": "us-east-2"})
            url = backend.url("a/k.mp4")
        self.assertIn("X-Amz-Credential=ASIAROLEKEY", url)
        self.assertIn("X-Amz-Security-Token=roletoken", url)
        self.assertEqual(handler.seen_auth, ["tok"])

    def test_no_credentials_anywhere_is_a_storage_error_with_a_reason(self):
        with _aws_free_env(self.tmp.name):
            self._fresh_session()
            backend = storage.from_env({"VOLT_STORAGE": "s3", "VOLT_S3_BUCKET": "b", "VOLT_S3_REGION": "us-east-2"})
            with self.assertRaisesRegex(StorageError, "could not sign a URL"):
                backend.url("a/k.mp4")

    def test_the_client_is_built_without_any_key_arguments(self):
        import boto3
        with _aws_free_env(self.tmp.name), mock.patch.object(boto3, "client") as client:
            S3Backend(S3Config(bucket="b", region="us-east-2"))._make_client()
        kwargs = client.call_args.kwargs
        self.assertEqual(sorted(kwargs), ["config", "endpoint_url", "region_name"])


class PublishThroughStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.make_store()
        self.asset, self.span = _asset_and_span(self.store, self.root)

    def make_store(self):
        return Store()

    def versions(self):
        return {r["kind"]: r for r in self.store.conn.execute("SELECT * FROM version")}

    def test_local_default_records_the_backend_and_key(self):
        publish_files(self.store, self.span, self.root / "out", backend=LocalBackend(self.root / "out"))
        for kind, row in self.versions().items():
            self.assertEqual((row["status"], row["storage_backend"]), ("done", "local"))
            self.assertEqual(row["storage_key"], f"{self.asset.id}/{self.span}-{kind}.mp4")
            self.assertTrue(row["url"].startswith("file:///"))
            self.assertEqual(len(row["storage_sha256"]), 64)

    def test_remote_upload_stores_the_url_and_clears_scratch(self):
        client = FakeS3Client()
        backend = _s3(client)
        scratch = self.root / "scratch"
        versions = publish_files(self.store, self.span, None, backend=backend, scratch=scratch)
        self.assertEqual([v.status for v in versions], ["done"] * 3)
        self.assertEqual(len(client.objects), 3)
        for kind, row in self.versions().items():
            self.assertEqual(row["storage_backend"], "s3:b/pre")
            self.assertEqual(row["storage_key"], f"{self.asset.id}/{self.span}-{kind}.mp4")
            self.assertTrue(row["url"].startswith("https://fake.invalid/b/pre/"))
        self.assertEqual([p for p in scratch.rglob("*") if p.is_file()], [])

    def test_a_failed_upload_fails_only_that_version_and_retry_uploads_only_it(self):
        class Flaky(FakeS3Client):
            def upload_file(self, Filename, Bucket, Key, **kw):
                if "-ott" in Key and not getattr(self, "healed", False):
                    self.uploads += 1
                    raise OSError("connection reset")
                return super().upload_file(Filename, Bucket, Key, **kw)
        client = Flaky()
        backend = _s3(client)
        scratch = self.root / "scratch"
        first = publish_files(self.store, self.span, None, backend=backend, scratch=scratch)
        self.assertEqual({v.kind: v.status for v in first}, {"house": "done", "ott": "failed", "social": "done"})
        row = self.versions()["ott"]
        self.assertIsNone(row["url"])
        self.assertIsNone(row["storage_key"])
        job = self.store.conn.execute(
            "SELECT status, error FROM job WHERE idempotency_key = ?", (f"publish:{self.span}:ott",)).fetchone()
        self.assertIn("connection reset", job["error"])
        self.assertTrue((scratch / self.asset.id / f"{self.span}-ott.mp4").exists())  # not deleted unverified
        client.healed, before = True, client.uploads
        again = publish_files(self.store, self.span, None, backend=backend, scratch=scratch)
        self.assertEqual([v.status for v in again], ["done"] * 3)
        self.assertEqual(client.uploads - before, 1)  # only the failed version went up again
        self.assertEqual([p for p in scratch.rglob("*") if p.is_file()], [])

    def test_corruption_in_transit_fails_the_version(self):
        versions = publish_files(self.store, self.span, None, backend=_s3(FakeS3Client(corrupt=True)),
                                 scratch=self.root / "scratch")
        self.assertEqual([v.status for v in versions], ["failed"] * 3)

    def test_refresh_url_re_signs_from_the_stored_key(self):
        backend = _s3(FakeS3Client(), url_expires=100)
        publish_files(self.store, self.span, None, backend=backend, scratch=self.root / "s")
        row = self.versions()["house"]
        new = refresh_url(self.store, row["id"], backend=backend, expires=900)
        self.assertTrue(new.endswith("?expires=900"))
        self.assertEqual(self.versions()["house"]["url"], new)
        other = _s3(FakeS3Client(), prefix="elsewhere")
        with self.assertRaisesRegex(StorageError, "stored in s3:b/pre"):
            refresh_url(self.store, row["id"], backend=other)

    def _sigv4_client(self, stamp, bad_key=None):
        """A fake whose presigned URLs carry X-Amz-Date / X-Amz-Expires like real SigV4 ones."""
        class Signed(FakeS3Client):
            def generate_presigned_url(self, op, Params, ExpiresIn):
                if bad_key and bad_key in Params["Key"]:
                    raise RuntimeError("signer down")
                return (f"https://fake.invalid/{Params['Bucket']}/{Params['Key']}"
                        f"?X-Amz-Date={stamp[0]}&X-Amz-Expires={ExpiresIn}&X-Amz-Signature=abc")
        return Signed()

    def test_refresh_expiring_only_touches_urls_near_expiry_under_this_bucket_and_prefix(self):
        from datetime import datetime, timedelta, timezone
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stamp = [t0.strftime("%Y%m%dT%H%M%SZ")]
        backend = _s3(self._sigv4_client(stamp), url_expires=1000)
        publish_files(self.store, self.span, None, backend=backend, scratch=self.root / "s")
        v = self.versions()
        self.store.conn.execute("UPDATE version SET storage_backend = 's3:b/elsewhere' WHERE id = ?", (v["social"]["id"],))
        self.store.conn.commit()
        before = {k: r["url"] for k, r in self.versions().items()}
        # 500s in, 100s window: nothing is close to expiry
        report = publish_mod.refresh_expiring(self.store, backend, 100, now=t0 + timedelta(seconds=500))
        self.assertEqual((report.refreshed, report.failed), ([], []))
        self.assertEqual({k: r["url"] for k, r in self.versions().items()}, before)
        # 950s in: house and ott are inside the window and get new URLs; the other prefix does not
        stamp[0] = (t0 + timedelta(seconds=950)).strftime("%Y%m%dT%H%M%SZ")
        report = publish_mod.refresh_expiring(self.store, backend, 100, now=t0 + timedelta(seconds=950))
        self.assertEqual(sorted(report.refreshed), sorted([v["house"]["id"], v["ott"]["id"]]))
        self.assertEqual(report.failed, [])
        after = {k: r["url"] for k, r in self.versions().items()}
        self.assertNotEqual(after["house"], before["house"])
        self.assertIn(stamp[0], after["ott"])
        self.assertEqual(after["social"], before["social"])

    def test_refresh_expiring_records_a_bad_version_and_carries_on(self):
        from datetime import datetime, timedelta, timezone
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stamp = [t0.strftime("%Y%m%dT%H%M%SZ")]
        publish_files(self.store, self.span, None, backend=_s3(self._sigv4_client(stamp), url_expires=1000),
                      scratch=self.root / "s")
        flaky = _s3(self._sigv4_client(stamp, bad_key="-ott"), url_expires=1000)
        report = publish_mod.refresh_expiring(self.store, flaky, 100, now=t0 + timedelta(seconds=990))
        self.assertEqual(len(report.refreshed), 2)
        self.assertEqual([vid for vid, _ in report.failed], [self.versions()["ott"]["id"]])
        self.assertIn("signer down", report.failed[0][1])

    def test_refresh_expiring_skips_local_and_public_url_versions(self):
        from datetime import datetime, timezone
        publish_files(self.store, self.span, self.root / "out", backend=LocalBackend(self.root / "out"))
        now = datetime(2100, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(publish_mod.refresh_expiring(self.store, LocalBackend(self.root / "out"), 10**9, now=now).refreshed, [])
        public = _s3(FakeS3Client(), public_url_base="https://cdn.example/v")
        self.assertEqual(publish_mod.refresh_expiring(self.store, public, 10**9, now=now).refreshed, [])

    def test_public_mode_moves_old_presigned_urls_to_the_stable_url_once(self):
        from datetime import datetime, timezone
        presigned = _s3(self._sigv4_client(["20260101T000000Z"]), url_expires=1000)
        publish_files(self.store, self.span, None, backend=presigned, scratch=self.root / "s")
        public = _s3(FakeS3Client(), public_url_base="https://cdn.example/v")
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        report = publish_mod.refresh_expiring(self.store, public, 60, now=now)
        self.assertEqual(len(report.refreshed), 3)
        for kind, row in self.versions().items():
            self.assertEqual(row["url"], f"https://cdn.example/v/pre/{self.asset.id}/{self.span}-{kind}.mp4")
        again = publish_mod.refresh_expiring(self.store, public, 60, now=now)
        self.assertEqual((again.refreshed, again.skipped), ([], 3))

    def test_a_second_refresher_does_not_double_refresh_a_version(self):
        """Another runner commits its refresh between our read and our write: our compare-and-set loses."""
        from datetime import datetime, timedelta, timezone
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stamp = [t0.strftime("%Y%m%dT%H%M%SZ")]
        base = self._sigv4_client(stamp)
        publish_files(self.store, self.span, None, backend=_s3(base, url_expires=1000), scratch=self.root / "s")
        winner = "https://winner.invalid/u?X-Amz-Date=20260101T000900Z&X-Amz-Expires=1000"
        store = self.store

        class Racy(type(base)):
            def generate_presigned_url(self, op, Params, ExpiresIn):
                url = super().generate_presigned_url(op, Params, ExpiresIn)
                if "house" in Params["Key"]:
                    store.conn.execute("UPDATE version SET url = ? WHERE storage_key LIKE '%house%'", (winner,))
                    store.conn.commit()
                return url
        racy = _s3(Racy(), url_expires=1000)
        report = publish_mod.refresh_expiring(self.store, racy, 100, now=t0 + timedelta(seconds=990))
        house = self.versions()["house"]
        self.assertNotIn(house["id"], report.refreshed)
        self.assertEqual(house["url"], winner)
        self.assertEqual(len(report.refreshed), 2)

    def _aging(self):
        """Published 990 s into a 1000 s lifetime, and a stamp the test can move to 'now' for new signs."""
        from datetime import datetime, timedelta, timezone
        t0 = datetime.now(timezone.utc)
        stamp = [(t0 - timedelta(seconds=990)).strftime("%Y%m%dT%H%M%SZ")]
        backend = _s3(self._sigv4_client(stamp), url_expires=1000)
        publish_files(self.store, self.span, None, backend=backend, scratch=self.root / "s")
        stamp[0] = t0.strftime("%Y%m%dT%H%M%SZ")
        return backend

    def _runner(self, config, clock):
        from volt import runner as runner_mod
        r = runner_mod.Runner(self.store, {}, mono=lambda: clock[0])
        r.periodic.append(runner_mod.Periodic("refresh-urls", 60, runner_mod.refresh_urls_task(config)))
        return r

    def test_runner_periodic_refresh_is_opt_in_timed_and_survives_failure(self):
        from volt import runner as runner_mod
        backend = self._aging()
        before = {k: r["url"] for k, r in self.versions().items()}
        clock = [0.0]
        off = runner_mod.Runner(self.store, {}, mono=lambda: clock[0])
        self.assertEqual(off.run_periodic(), {})  # no task configured: nothing happens
        self.assertEqual({k: r["url"] for k, r in self.versions().items()}, before)
        on = self._runner(runner_mod.Config(storage=backend, refresh_urls_every=60, refresh_urls_within=100), clock)
        self.assertEqual(len(on.run_periodic()["refresh-urls"].refreshed), 3)  # due at start
        after = {k: r["url"] for k, r in self.versions().items()}
        self.assertTrue(all(after[k] != before[k] for k in before))
        clock[0] = 30
        self.assertEqual(on.run_periodic(), {})  # not due yet
        clock[0] = 61
        self.assertEqual(len(on.run_periodic()["refresh-urls"].refreshed), 0)  # due; nothing near expiry now

        def boom(store):
            raise storage.StorageError("bucket gone")
        bad = runner_mod.Runner(self.store, {}, mono=lambda: clock[0])
        bad.periodic.append(runner_mod.Periodic("refresh-urls", 60, boom))
        with self.assertLogs("volt.runner", "WARNING"):
            self.assertEqual(bad.run_periodic(), {})
        self.assertEqual(bad.periodic_failures, 1)

    def test_two_runners_refresh_each_version_once(self):
        from volt import runner as runner_mod
        backend = self._aging()
        config = runner_mod.Config(storage=backend, refresh_urls_every=60, refresh_urls_within=100)
        runners = [self._runner(config, [0.0]) for _ in range(2)]
        done = [len(r.run_periodic()["refresh-urls"].refreshed) for r in runners]
        self.assertEqual(sorted(done), [0, 3])

    def test_runner_periodic_task_does_nothing_for_local_storage(self):
        from volt import runner as runner_mod
        publish_files(self.store, self.span, self.root / "out", backend=LocalBackend(self.root / "out"))
        config = runner_mod.Config(publish_dir=self.root / "out", refresh_urls_every=60)
        report = runner_mod.refresh_urls_task(config)(self.store)
        self.assertEqual((report.refreshed, report.failed), ([], []))

    def test_runner_env_for_the_refresh(self):
        from volt import runner as runner_mod
        self.assertEqual(runner_mod.Config.from_env({}).refresh_urls_every, 0)
        c = runner_mod.Config.from_env({"VOLT_REFRESH_URLS_EVERY": "3600", "VOLT_REFRESH_URLS_WITHIN": "7200"})
        self.assertEqual((c.refresh_urls_every, c.refresh_urls_within), (3600, 7200))

    def test_url_expiry_reads_sigv4_and_sigv2_and_ignores_the_rest(self):
        from datetime import datetime, timezone
        got = publish_mod.url_expiry("https://h/k?X-Amz-Date=20260101T000000Z&X-Amz-Expires=60&X-Amz-Signature=a")
        self.assertEqual(got, datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(publish_mod.url_expiry("https://h/k?Expires=1767225600&Signature=a"),
                         datetime(2026, 1, 1, tzinfo=timezone.utc))
        for url in (None, "", "file:///x.mp4", "volt://house/a/s", "https://cdn/k.mp4", "https://h/k?X-Amz-Date=bad&X-Amz-Expires=x"):
            self.assertIsNone(publish_mod.url_expiry(url), url)

    def test_runner_publish_handler_uses_the_configured_backend(self):
        from volt.runner import Config, Runner, default_handlers, request_publish
        client = FakeS3Client()
        cfg = Config(storage=_s3(client), storage_scratch=self.root / "scratch")  # no publish_dir needed
        job_id = request_publish(self.store, self.span)
        Runner(self.store, {"publish": default_handlers(cfg)["publish"]}).drain()
        self.assertEqual(len(client.objects), 3)
        self.assertEqual(self.store.conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0],
                         "done")

    def test_unconfigured_s3_fails_the_job_with_the_reason(self):
        from volt.runner import Config, Runner, default_handlers, request_publish
        old = {k: os.environ.pop(k, None) for k in ("VOLT_STORAGE", "VOLT_S3_BUCKET")}
        os.environ["VOLT_STORAGE"] = "s3"
        try:
            job_id = request_publish(self.store, self.span)
            Runner(self.store, {"publish": default_handlers(Config(publish_dir=self.root))["publish"]}).drain()
        finally:
            os.environ.pop("VOLT_STORAGE", None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
        row = self.store.conn.execute("SELECT status, error FROM job WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("VOLT_S3_BUCKET", row["error"])


PG_DSN = os.environ.get("VOLT_TEST_PG_DSN")


@unittest.skipUnless(PG_DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class PostgresPublishThroughStorage(PublishThroughStorage):
    """The same publish-through-storage tests with the storage columns on Postgres."""

    def make_store(self):
        import uuid
        from urllib.parse import quote
        import psycopg

        self.schema = "volt_s_" + uuid.uuid4().hex[:12]
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        sep = "&" if "?" in PG_DSN else "?"
        self.addCleanup(self._drop)
        return Store(f"{PG_DSN}{sep}options={quote('-csearch_path=' + self.schema)}")

    def _drop(self):
        import psycopg

        self.store.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")


# ---- live: a real S3-compatible server -----------------------------------------------------------

@unittest.skipUnless(LIVE, "set VOLT_TEST_S3_ENDPOINT to run against a local S3-compatible server")
class LiveS3Contract(BackendContract, unittest.TestCase):
    bucket = os.environ.get("VOLT_TEST_S3_BUCKET", "volt-test")

    def make_backend(self):
        backend = S3Backend(S3Config(bucket=self.bucket, prefix=f"live-{os.getpid()}/{id(self)}",
                                     endpoint_url=LIVE, region="us-east-1", part_mb=5, url_expires=300))
        try:
            backend.client.create_bucket(Bucket=self.bucket)
        except Exception as exc:  # noqa: BLE001 - an existing bucket is fine
            if "BucketAlready" not in str(exc):
                raise
        self.addCleanup(self._clean, backend)
        return backend

    def _clean(self, backend):
        listing = backend.client.list_objects_v2(Bucket=self.bucket, Prefix=backend.prefix)
        for obj in listing.get("Contents", []):
            backend.client.delete_object(Bucket=self.bucket, Key=obj["Key"])

    def test_multipart_sizes_around_the_part_boundary(self):
        for size in (0, 5 * MB - 1, 5 * MB, 12 * MB + 7):
            data = os.urandom(size)
            obj = self.backend.put_file(self._file(data, f"m{size}.bin"), f"m/{size}.bin")
            self.assertEqual(obj.sha256, hashlib.sha256(data).hexdigest())
            self.assertEqual(self.read(f"m/{size}.bin"), data)

    def test_presigned_url_expires_and_signature_is_checked(self):
        self.backend.put_file(self._file(b"abc"), "p/a.bin")
        url = self.backend.url("p/a.bin", expires=1)
        import time
        time.sleep(2.5)
        with self.assertRaises(Exception):
            urllib.request.urlopen(url)
        tampered = self.backend.url("p/a.bin").replace("Signature=", "Signature=00")
        with self.assertRaises(Exception):
            urllib.request.urlopen(tampered)

    def test_content_type_is_set(self):
        self.backend.put_file(self._file(b"abc"), "p/video.mp4")
        with urllib.request.urlopen(self.backend.url("p/video.mp4")) as response:
            self.assertEqual(response.headers["Content-Type"], "video/mp4")

    def test_bad_credentials_fail_with_a_reason_and_no_leak(self):
        bad = S3Backend(S3Config(bucket=self.bucket, endpoint_url=LIVE, region="us-east-1"))
        import boto3
        bad._client = boto3.client("s3", endpoint_url=LIVE, region_name="us-east-1",
                                   aws_access_key_id="nope-key", aws_secret_access_key="nope-secret-value")
        with self.assertRaises(StorageError) as caught:
            bad.put_file(self._file(b"abc"), "p/denied.bin")
        self.assertNotIn("nope-secret-value", str(caught.exception))

    def test_an_unreachable_endpoint_fails_the_upload(self):
        dead = S3Backend(S3Config(bucket=self.bucket, endpoint_url="http://127.0.0.1:1", region="us-east-1"))
        dead.client.meta.config.retries = {"max_attempts": 1}
        with self.assertRaises(StorageError):
            dead.put_file(self._file(b"abc"), "p/x.bin")


@unittest.skipUnless(LIVE, "set VOLT_TEST_S3_ENDPOINT to run against a local S3-compatible server")
class LivePublish(unittest.TestCase):
    """The real cut, uploaded to a real server, fetched back through the stored URL."""

    def test_real_cuts_land_in_the_bucket_and_play(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            asset, span = _asset_and_span(store, root)
            backend = S3Backend(S3Config(bucket=LiveS3Contract.bucket, prefix=f"live-publish-{os.getpid()}",
                                         endpoint_url=LIVE, region="us-east-1"))
            try:
                backend.client.create_bucket(Bucket=LiveS3Contract.bucket)
            except Exception:  # noqa: BLE001 - already there
                pass
            scratch = root / "scratch"
            versions = publish_files(store, span, None, backend=backend, scratch=scratch)
            try:
                self.assertEqual([v.status for v in versions], ["done"] * 3)
                for v in versions:
                    with urllib.request.urlopen(v.url) as response:
                        body = response.read()
                    path = root / f"fetched-{v.kind}.mp4"
                    path.write_bytes(body)
                    self.assertGreater(deliverables._duration(path), 0)
                    self.assertEqual(body[4:8], b"ftyp")
                self.assertEqual([p for p in scratch.rglob("*") if p.is_file()], [])
                row = store.conn.execute("SELECT id, url FROM version WHERE kind = 'house'").fetchone()
                self.assertNotEqual(refresh_url(store, row["id"], backend=backend, expires=600), "")
            finally:
                for kind in ("house", "ott", "social"):
                    backend.delete(f"{asset.id}/{span}-{kind}.mp4")


if __name__ == "__main__":
    unittest.main()
