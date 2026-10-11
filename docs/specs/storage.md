# Storage

Where a finished deliverable lives and the URL it is reached by. `control/volt/storage.py` is one small interface with a local-disk backend (the default, `file://` URLs as before) and an S3 backend (AWS S3 or any S3-compatible server). Azure Blob and GCS are named stubs that raise `NotImplementedError`.

## Interface

```
Backend.id                                -> "local", "s3:<bucket>[/<prefix>]"
Backend.put_file(local_path, key)         -> StoredObject(backend, key, url, size, sha256)   raises StorageError
Backend.url(key, expires=None)            -> URL (signed and expiring on S3 unless a public base is set)
Backend.exists(key)                       -> bool
Backend.delete(key)                       -> None (a missing key is not an error)
```

Keys are relative, `/`-separated, with no `..`, empty segments or backslashes (`asset/span-kind.mp4`). `put_file` replaces an existing object, verifies what arrived and never leaves a partial object visible. A new backend must pass `BackendContract` in `control/tests/test_storage.py` (subclass it and supply `make_backend`).

## Configuration

Environment only. Nothing here holds a credential.

| Variable | Meaning |
|---|---|
| `VOLT_STORAGE` | `local` (default), `s3`. `azure` and `gcs` fail at start with a clear message. |
| `VOLT_S3_BUCKET` | Required for s3. |
| `VOLT_S3_PREFIX` | Key prefix inside the bucket, e.g. `shows`. Part of the backend id; changing it strands old objects until they are moved. |
| `VOLT_S3_REGION` | Region (optional; boto3 can read it from its own config). |
| `VOLT_S3_ENDPOINT_URL` | For S3-compatible servers (MinIO, Ceph, R2). Switches to path-style addressing. |
| `VOLT_S3_URL_EXPIRES` | Presigned URL lifetime in seconds. Default and maximum 604800 (7 days). |
| `VOLT_S3_PUBLIC_URL_BASE` | If set (a CDN or a public bucket), URLs are `<base>/<prefix><key>`: unsigned, never expiring. The bucket or CDN must allow public reads; Volt does not change bucket policy. |
| `VOLT_S3_PART_MB` | Multipart threshold and part size, default 16, minimum 5. |
| `VOLT_STORAGE_SCRATCH` | Where cuts wait for upload. Default `<publish dir>/.scratch`, else the system temp dir. |

Credentials come only from boto3's standard provider chain (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, a shared profile, or an instance, task or pod role). Volt never accepts, stores or logs them: error text keeps only the S3 error code and message. Prefer a role. boto3 is imported only when s3 is first used (`pip install boto3`); a stdlib-only install runs unchanged.

## Publish flow

`publish_files(store, span_id, out_dir, backend=None, scratch=None)` (the runner's publish handler and `serve.py` both go through it):

1. A done version is skipped, so a retry touches only failed versions (existing rule).
2. Local backend: the cut is written straight to `out_dir/<asset>/<span>-<kind>.mp4`, as before. Any other backend: the cut is rendered into scratch.
3. `put_file` uploads (single PUT below the part size, multipart at or above it), then reads the object back with a HEAD and verifies size and checksum: the SHA-256 the server holds (composite SHA-256 for multipart), else the ETag (MD5, or MD5-of-part-MD5s for multipart; not usable under SSE-KMS). If neither is available the upload is refused as unverifiable. A failed verification deletes the object.
4. Only after a verified upload is the scratch file deleted. A failed upload fails that version with the reason on its job; the scratch file stays and the retry renders over it.
5. The version row records `url`, `storage_backend`, `storage_key`, `storage_sha256`. The rundown item's URLs are read from those versions, so they are the live URLs.

S3 makes a PUT, or a completed multipart upload, visible all at once, so there is no temp key and copy.

## Expiring URLs

Without `VOLT_S3_PUBLIC_URL_BASE` the stored `version.url` is a presigned URL and expires (default 7 days; with temporary role credentials it dies when the session does, whichever is first). `publish.refresh_url(store, version_id, expires=None)` makes a new one from the stored backend and key and writes it back. It refuses when the configured bucket and prefix differ from the ones the version was stored under. A presigned URL embeds the access key id (not the secret); treat it as a bearer link.

`publish.refresh_expiring(store, backend=None, within_seconds=86400, expires=None, now=None)` is the opt-in pass that calls it for you. It re-signs every `done` version stored under the configured bucket and prefix (`version.storage_backend == backend.id`; versions under another bucket or prefix are not touched) whose URL expires within `within_seconds`, or already has. It does nothing for the local backend or when `VOLT_S3_PUBLIC_URL_BASE` is set (those URLs do not expire). It returns a `RefreshReport` (`refreshed` ids, `skipped` count, `failed` as `(version id, reason)`); one version that fails to sign is recorded and the pass carries on.

The issue time is not stored (no schema change). The expiry is read from the URL: SigV4 `X-Amz-Date` + `X-Amz-Expires`, or SigV2 `Expires` (absolute epoch). A URL with neither is counted as skipped, never guessed at. Run it from cron: `python -m volt.publish refresh-urls <db> --within 86400 [--expires SECONDS]` (storage from the usual `VOLT_STORAGE` / `VOLT_S3_*` variables). It prints `refreshed= skipped= failed=` and exits 1 if any version failed. Pick `--within` larger than the cron interval plus the URL lifetime you can tolerate losing; nothing runs it unless you schedule it.

## IAM

Minimum for the identity Volt runs as, on bucket `BUCKET` and prefix `PREFIX`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::BUCKET",
      "Condition": {"StringLike": {"s3:prefix": "PREFIX/*"}}
    }
  ]
}
```

`s3:GetObject` is needed because presigned URLs carry the signer's permissions, and for the verification HEAD. `s3:ListBucket` makes `exists()` of a missing key a 404 instead of a 403 (publish itself does not call `exists`). With SSE-KMS add `kms:GenerateDataKey` and `kms:Decrypt` on the key.

## What is proven and what is not

Proven by tests that run in CI (no network, no boto3): the interface contract on the local backend and on `S3Backend` over a fake client that follows S3's checksum rules; key validation; config parsing; azure and gcs stubs; boto3 not imported until used; publish through a backend (URLs, columns, scratch cleanup, only-failed retry, corruption caught, refresh, runner handler), on SQLite and, with `VOLT_TEST_PG_DSN`, on Postgres.

Proven live only against a throwaway MinIO (`bitnamilegacy/minio`, release 2025-05-24) with boto3 1.43, when `VOLT_TEST_S3_ENDPOINT` is set (plus `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for that server): single and multipart uploads at 0, 5 MiB-1, 5 MiB and 12 MiB+7 with the composite checksum verified, URL read-back, expiry and tamper rejection, content type, bad credentials and unreachable endpoint failing with a reason and no leak, and real ffmpeg cuts published, fetched and probed. The Docker Hub `minio/minio` and quay images could not be pulled on the test box, so a Bitnami legacy build was used.

Proven on real AWS S3 (2026-10-11, bucket in us-east-2, boto3 1.43, an IAM user holding only the policy above, `bench/s3_live_check.py`, 12/12): single and multipart uploads at 0, 5 MiB-1, 5 MiB and 12 MiB+7 with checksum verified, read-back through the presigned URL, expiry (403) and tampered-signature rejection (403), `video/mp4` content type, `exists()` true and a clean `False` for a missing key (so `s3:ListBucket` on the prefix does what is said above), a write outside the policy's prefix denied, bad credentials refused with no secret in the message, and nothing left behind. This found a real bug the MinIO runs could not: for a bucket outside us-east-1 boto3 signed URLs for the region but pointed them at `s3.amazonaws.com`, which AWS rejects ("the region is wrong"), so every stored `version.url` would have been dead. AWS addressing is now virtual-hosted (`bucket.s3.<region>.amazonaws.com`); S3-compatible endpoints stay path style. If the machine's clock runs ahead of AWS, a short-lived URL outlives its expiry by the skew (seen: 3.9 s ahead, so a 1 s URL still worked).

Not proven: SSE-KMS, bucket policies, CDN or public-read setups, cross-region latency, a real ffmpeg cut published through `publish_files` to AWS (only the backend was exercised), and runs from an instance or task role. Corruption detection is proven against the fake client, not against a server that corrupts. Azure Blob and GCS do not exist yet. Proxies and other essences are not routed through the seam.

## Next backends

Azure Blob: block upload with a Content-MD5 per block and `get_blob_properties` for size and the stored MD5; `url` is a SAS URL (also expiring); credentials from `DefaultAzureCredential`. GCS: resumable upload with the `crc32c` or `md5` in the object metadata checked; `url` is a V4 signed URL; credentials from application default credentials. Both must pass `BackendContract`.
