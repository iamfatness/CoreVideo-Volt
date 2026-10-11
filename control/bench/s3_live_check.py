"""Live check of the S3 backend against real AWS S3 (not MinIO).

Needs boto3 and credentials from the standard chain (for example AWS_PROFILE=volt-test). It uses only the
bucket and prefix given, deletes every object it creates, and never reads or prints a secret.

    AWS_PROFILE=volt-test python bench/s3_live_check.py --bucket BUCKET --region us-east-2 --prefix volt-test

Exit status is 0 only if every check passed.
"""

import argparse
import hashlib
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from volt.storage import S3Backend, S3Config, StorageError  # noqa: E402

MB = 1024 * 1024
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read(), response.headers


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--prefix", default="volt-test")
    args = ap.parse_args(argv)

    run = f"{args.prefix}/live-{os.getpid()}-{int(time.time())}"
    backend = S3Backend(S3Config(bucket=args.bucket, prefix=run, region=args.region, part_mb=5, url_expires=300))
    tmp = Path(tempfile.mkdtemp(prefix="volt-s3-"))
    keys = []

    def put(data, key):
        path = tmp / key.replace("/", "_")
        path.write_bytes(data)
        keys.append(key)
        return backend.put_file(path, key)

    try:
        # 1. single PUT and multipart, at the part boundaries; checksum verified, read back through the URL
        for size in (0, 5 * MB - 1, 5 * MB, 12 * MB + 7):
            data = os.urandom(size)
            started = time.time()
            try:
                obj = put(data, f"m/{size}.bin")
                body, _ = fetch(backend.url(f"m/{size}.bin"))
                check(f"upload {size} bytes, checksum + URL read-back",
                      obj.sha256 == hashlib.sha256(data).hexdigest() and body == data,
                      f"{time.time() - started:.1f}s")
            except Exception as exc:  # noqa: BLE001
                check(f"upload {size} bytes, checksum + URL read-back", False, type(exc).__name__ + ": " + str(exc)[:200])

        # 2. presigned URL expiry and tamper rejection
        try:
            put(b"abc", "p/a.bin")
            # a local clock ahead of AWS stretches the lifetime by the skew, so wait well past 2 s + skew
            expiring = backend.url("p/a.bin", expires=2)
            time.sleep(10)
            try:
                fetch(expiring)
                check("expired URL is refused", False, "fetched after expiry")
            except urllib.error.HTTPError as exc:
                check("expired URL is refused", exc.code == 403, f"HTTP {exc.code}")
            tampered = backend.url("p/a.bin").replace("Signature=", "Signature=00")
            try:
                fetch(tampered)
                check("tampered signature is refused", False, "fetched")
            except urllib.error.HTTPError as exc:
                check("tampered signature is refused", exc.code == 403, f"HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            check("presigned URL checks", False, type(exc).__name__ + ": " + str(exc)[:200])

        # 3. content type
        try:
            put(b"abc", "p/video.mp4")
            _, headers = fetch(backend.url("p/video.mp4"))
            check("content type is video/mp4", headers.get("Content-Type") == "video/mp4", headers.get("Content-Type"))
        except Exception as exc:  # noqa: BLE001
            check("content type is video/mp4", False, type(exc).__name__ + ": " + str(exc)[:200])

        # 4. exists(): present, and missing is False (needs s3:ListBucket on the prefix)
        try:
            check("exists() true for an uploaded key", backend.exists("p/a.bin"))
            check("exists() false for a missing key (404, not 403)", backend.exists("p/never.bin") is False)
        except Exception as exc:  # noqa: BLE001
            check("exists() semantics", False, type(exc).__name__ + ": " + str(exc)[:200])

        # 5. the policy really is scoped: a write outside the prefix must be denied
        outside = S3Backend(S3Config(bucket=args.bucket, prefix="not-volt-test", region=args.region, part_mb=5))
        path = tmp / "outside.bin"
        path.write_bytes(b"x")
        try:
            outside.put_file(path, "o.bin")
            check("write outside the IAM prefix is denied", False, "it succeeded")
            outside.delete("o.bin")
        except StorageError as exc:
            check("write outside the IAM prefix is denied", True, str(exc)[:120])

        # 6. bad credentials fail with a reason and no secret in the message
        import boto3
        bad = S3Backend(S3Config(bucket=args.bucket, prefix=run, region=args.region))
        bad._client = boto3.client("s3", region_name=args.region, aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                                   aws_secret_access_key="nope-secret-value")
        try:
            bad.put_file(path, "p/denied.bin")
            check("bad credentials are refused", False, "it succeeded")
        except StorageError as exc:
            check("bad credentials are refused, no secret leaked", "nope-secret-value" not in str(exc), str(exc)[:120])
    finally:
        # cleanup: everything this run created, then prove the prefix is empty
        for key in keys:
            try:
                backend.delete(key)
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup failed for {key}: {type(exc).__name__}", file=sys.stderr)
        left = backend.client.list_objects_v2(Bucket=args.bucket, Prefix=run + "/").get("KeyCount", -1)
        check("cleanup leaves nothing behind", left == 0, f"{left} objects left")

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
