"""Live check: a real ffmpeg cut published through publish_files to real AWS S3, fetched back by its stored URL.

    AWS_PROFILE=volt-test python bench/s3_live_publish.py --bucket BUCKET --region us-east-2 --prefix volt-test

Needs boto3 and ffmpeg. Deletes everything it uploads, then checks the prefix is empty.
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_storage import _asset_and_span  # noqa: E402
from volt import deliverables  # noqa: E402
from volt.publish import publish_files, refresh_url  # noqa: E402
from volt.storage import S3Backend, S3Config  # noqa: E402
from volt.store import Store  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--prefix", default="volt-test")
    args = ap.parse_args(argv)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)

    run = f"{args.prefix}/publish-{int(time.time())}"
    backend = S3Backend(S3Config(bucket=args.bucket, prefix=run, region=args.region))
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store()
        asset, span = _asset_and_span(store, root)
        scratch = root / "scratch"
        try:
            versions = publish_files(store, span, None, backend=backend, scratch=scratch)
            check("three versions published, all done", [v.status for v in versions] == ["done"] * 3,
                  ",".join(f"{v.kind}:{v.status}" for v in versions))
            for v in versions:
                with urllib.request.urlopen(v.url) as response:
                    body = response.read()
                path = root / f"fetched-{v.kind}.mp4"
                path.write_bytes(body)
                check(f"{v.kind}: stored URL plays (mp4, duration > 0)",
                      body[4:8] == b"ftyp" and deliverables._duration(path) > 0,
                      f"{len(body)} bytes, {deliverables._duration(path):.2f}s")
            row = store.conn.execute(
                "SELECT storage_backend, storage_key, storage_sha256 FROM version WHERE kind = 'house'").fetchone()
            check("version row records backend, key and sha256", all(row), f"{row['storage_backend']}")
            check("scratch is empty after verified upload", [p for p in scratch.rglob("*") if p.is_file()] == [])
            vid = store.conn.execute("SELECT id FROM version WHERE kind = 'house'").fetchone()["id"]
            fresh = refresh_url(store, vid, backend=backend, expires=600)
            with urllib.request.urlopen(fresh) as response:
                check("refresh_url gives a working new URL", response.status == 200)
        finally:
            for kind in ("house", "ott", "social"):
                try:
                    backend.delete(f"{asset.id}/{span}-{kind}.mp4")
                except Exception as exc:  # noqa: BLE001
                    print(f"cleanup failed for {kind}: {type(exc).__name__}", file=sys.stderr)
            left = backend.client.list_objects_v2(Bucket=args.bucket, Prefix=run + "/").get("KeyCount", -1)
            check("cleanup leaves nothing behind", left == 0, f"{left} objects left")
    print("ALL PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
