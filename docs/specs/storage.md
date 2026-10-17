# Storage

Where a finished deliverable lives and the URL it is reached by. `control/volt/storage.py` is one small interface with a local-disk backend (the default, `file://` URLs as before) and an S3 backend (AWS S3 or any S3-compatible server), an Azure Blob backend and a Google Cloud Storage backend.

## Interface

```
Backend.id                                -> "local", "s3:<bucket>[/<prefix>]", "azure:<account>/<container>[/<prefix>]", "gcs:<bucket>[/<prefix>]"
Backend.put_file(local_path, key)         -> StoredObject(backend, key, url, size, sha256)   raises StorageError
Backend.url(key, expires=None)            -> URL (signed and expiring on S3 unless a public base is set)
Backend.exists(key)                       -> bool
Backend.delete(key)                       -> None (a missing key is not an error)
```

Keys are relative, `/`-separated, with no `..`, empty segments or backslashes (`asset/span-kind.mp4`). `put_file` replaces an existing object, verifies what arrived and never leaves a partial object visible. A new backend must pass `BackendContract` in `control/tests/test_storage.py` (subclass it and supply `make_backend`).

Two optional methods exist for verification (`python -m volt.backup verify-storage`, see [backup](backup.md)): `stat(key) -> ObjectInfo(size, sha256 or None)` on all four backends (S3 gives a SHA-256 only for single-part objects; Azure returns the `sha256` metadata Volt wrote; GCS gives size only), and `list_keys()` on the local and S3 backends, so orphan detection works there and not on Azure or GCS. Neither writes anything. They are proven on the fakes only, like the rest of the cloud code.

## Configuration

Environment only. Nothing here holds a credential.

| Variable | Meaning |
|---|---|
| `VOLT_STORAGE` | `local` (default), `s3`, `azure`, `gcs`. |
| `VOLT_S3_BUCKET` | Required for s3. |
| `VOLT_S3_PREFIX` | Key prefix inside the bucket, e.g. `shows`. Part of the backend id; changing it strands old objects until they are moved. |
| `VOLT_S3_REGION` | Region (optional; boto3 can read it from its own config). |
| `VOLT_S3_ENDPOINT_URL` | For S3-compatible servers (MinIO, Ceph, R2). Switches to path-style addressing. |
| `VOLT_S3_URL_EXPIRES` | Presigned URL lifetime in seconds. Default and maximum 604800 (7 days). |
| `VOLT_S3_PUBLIC_URL_BASE` | If set (a CDN or a public bucket), URLs are `<base>/<prefix><key>`: unsigned, never expiring. The bucket or CDN must allow public reads; Volt does not change bucket policy. |
| `VOLT_S3_SSE` | Server-side encryption sent on every put (single and every multipart upload): `aws:kms` (alias `kms`) or `AES256`. Unset = no header, the bucket default applies. |
| `VOLT_S3_KMS_KEY_ID` | KMS key id, ARN or alias; only with `VOLT_S3_SSE=aws:kms`. Unset with `aws:kms` = the account's `aws/s3` key. A key id is not a secret. |
| `VOLT_S3_PART_MB` | Multipart threshold and part size, default 16, minimum 5. |
| `VOLT_AZURE_CONTAINER` | Required for azure. |
| `VOLT_AZURE_ACCOUNT_URL` | `https://<account>.blob.core.windows.net`. Needed for Entra ID auth; the account name in the backend id comes from it. Not needed with a connection string. |
| `VOLT_AZURE_PREFIX` | Key prefix inside the container. Part of the backend id. |
| `VOLT_AZURE_URL_EXPIRES` | SAS URL lifetime in seconds. Default and maximum 604800 (7 days). |
| `VOLT_AZURE_PUBLIC_URL_BASE` | If set, URLs are `<base>/<prefix><key>`: no SAS, never expiring. Volt does not change container access. |
| `VOLT_AZURE_BLOCK_MB` | Block size, default 16 (raised automatically so a file never needs more than 50000 blocks). |
| `VOLT_GCS_BUCKET` | Required for gcs. |
| `VOLT_GCS_PREFIX` | Key prefix inside the bucket. Part of the backend id. |
| `VOLT_GCS_PROJECT` | Project for the client (optional; ADC usually supplies it). |
| `VOLT_GCS_ENDPOINT_URL` | An emulator or private endpoint. Switches to anonymous credentials and points signed URLs at that endpoint. For emulators only. |
| `VOLT_GCS_URL_EXPIRES` | V4 signed URL lifetime in seconds. Default and maximum 604800 (7 days). |
| `VOLT_GCS_PUBLIC_URL_BASE` | If set, URLs are `<base>/<prefix><key>`: unsigned, never expiring. |
| `VOLT_GCS_CHUNK_MB` | Resumable-upload chunk size, default 16. |
| `VOLT_STORAGE_SCRATCH` | Where cuts wait for upload. Default `<publish dir>/.scratch`, else the system temp dir. |

Credentials come only from boto3's standard provider chain, and none is required: Volt passes no key, token or profile to boto3 and never reads `AWS_*` variables itself, so on an instance, task, pod or web-identity role the host's credentials are simply what signs and uploads (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, a shared profile, or an instance, task or pod role). Volt never accepts, stores or logs them: error text keeps only the S3 error code and message. Prefer a role. With no credential anywhere, signing and uploads fail with a `StorageError` carrying boto3's reason (no secrets). boto3 is imported only when s3 is first used (`pip install boto3`); a stdlib-only install runs unchanged.

Azure credentials: the standard `AZURE_STORAGE_CONNECTION_STRING` (an account key, or Azurite) if set, else Entra ID through `azure-identity`'s `DefaultAzureCredential` against `VOLT_AZURE_ACCOUNT_URL` (managed identity, workload identity, `az login`, environment). GCS credentials: Google application default credentials only (`GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default login`, or an attached service account). Volt never accepts, stores or logs them. The SDKs are imported only when the backend is first used (`pip install azure-storage-blob` plus `azure-identity` for Entra ID; `pip install google-cloud-storage`); a missing SDK is a `StorageConfigError` naming the package. Error text carries only the provider's error code or HTTP status and reason, with `sig=`, `AccountKey=`, `token=` and similar parameters blanked.

## Publish flow

`publish_files(store, span_id, out_dir, backend=None, scratch=None)` (the runner's publish handler and `serve.py` both go through it):

1. A done version is skipped, so a retry touches only failed versions (existing rule).
2. Local backend: the cut is written straight to `out_dir/<asset>/<span>-<kind>.mp4`, as before. Any other backend: the cut is rendered into scratch.
3. `put_file` uploads (single PUT below the part size, multipart at or above it), then reads the object back with a HEAD and verifies size and checksum: the SHA-256 the server holds (composite SHA-256 for multipart), else the ETag (MD5, or MD5-of-part-MD5s for multipart; not usable under SSE-KMS). If neither is available the upload is refused as unverifiable. A failed verification deletes the object.
4. Only after a verified upload is the scratch file deleted. A failed upload fails that version with the reason on its job; the scratch file stays and the retry renders over it.
5. The version row records `url`, `storage_backend`, `storage_key`, `storage_sha256`. The rundown item's URLs are read from those versions, so they are the live URLs.

S3 makes a PUT, or a completed multipart upload, visible all at once, so there is no temp key and copy.

### Azure and GCS uploads

Neither cloud stores a SHA-256 for an object, so both backends read the object back after the upload completes and compare its SHA-256 with the local file's. The reported `sha256` is that of the bytes the server holds, and a mismatch deletes the object. The cost is one extra read of every published file (inside the same region this is normally not billed as egress; check your own setup).

- **Azure**: the file is staged as blocks (16 MiB by default), each sent with a Content-MD5 the server verifies. Block ids are `<index>-<sha256 prefix of the block>`, so after a failed attempt the retry asks the server for its uncommitted blocks and sends only the missing ones; a changed file never reuses a stale block. Nothing is visible until the block list is committed, which replaces the blob atomically. The commit sets `Content-Type`, `Content-MD5` and `sha256` metadata. Uncommitted blocks expire server-side after 7 days.
- **GCS**: always a resumable session sent in `VOLT_GCS_CHUNK_MB` chunks; the library retries a failed chunk against the same session and checks CRC32C as it goes. The object appears when the session finalises. After it, size, MD5 and CRC32C are compared with the local file (either hash present is enough, none is refused as unverifiable), then the SHA-256 read-back. There is no resume across process restarts: a failed call starts a fresh session.

## Expiring URLs

Azure URLs are SAS tokens (read-only, signed with the account key from a connection string, or with a user delegation key under Entra ID; the 7 day cap is the delegation key's). GCS URLs are V4 signed URLs, signed with the service account key, or, for a credential that holds no key, through the IAM `signBlob` API (needs `roles/iam.serviceAccountTokenCreator` on itself). `refresh_expiring` reads their expiry from the URL too (Azure `se`, GCS `X-Goog-Date` + `X-Goog-Expires`).

Without `VOLT_S3_PUBLIC_URL_BASE` the stored `version.url` is a presigned URL and expires (default 7 days; with temporary role credentials it dies when the session does, whichever is first). `publish.refresh_url(store, version_id, expires=None)` makes a new one from the stored backend and key and writes it back. It refuses when the configured bucket and prefix differ from the ones the version was stored under. A presigned URL embeds the access key id (not the secret); treat it as a bearer link.

With `VOLT_S3_PUBLIC_URL_BASE` set (a CDN, or a bucket that allows public reads) `version.url` is `<base>/<prefix><key>`, percent-encoded, with no signature and no expiry: the same string every time, so it needs no refresh. `publish_files` records it as soon as the object is verified. The base must be an `http(s)` URL (refused otherwise). Volt does not create the CDN or change the bucket policy. An SSE-KMS object is not readable by anonymous public GET; with `VOLT_S3_SSE=aws:kms` the CDN must read it with its own origin access (for example CloudFront OAC with `kms:Decrypt`), or use `AES256` / no SSE for a public-read bucket.

`publish.refresh_expiring(store, backend=None, within_seconds=86400, expires=None, now=None)` is the opt-in pass that calls it for you. It re-signs every `done` version stored under the configured bucket and prefix (`version.storage_backend == backend.id`; versions under another bucket or prefix are not touched) whose URL expires within `within_seconds`, or already has. It does nothing for the local backend (those URLs do not expire). When `VOLT_S3_PUBLIC_URL_BASE` is set it re-signs nothing; it moves any `done` version still holding an older presigned URL onto its stable public URL, once (the switch-over case), and skips the rest. It returns a `RefreshReport` (`refreshed` ids, `skipped` count, `failed` as `(version id, reason)`); one version that fails to sign is recorded and the pass carries on.

The issue time is not stored (no schema change). The expiry is read from the URL: SigV4 `X-Amz-Date` + `X-Amz-Expires`, or SigV2 `Expires` (absolute epoch). A URL with neither is counted as skipped, never guessed at. Run it from cron: `python -m volt.publish refresh-urls <db> --within 86400 [--expires SECONDS]` (storage from the usual `VOLT_STORAGE` / `VOLT_S3_*` variables). It prints `refreshed= skipped= failed=` and exits 1 if any version failed. Pick `--within` larger than the cron interval plus the URL lifetime you can tolerate losing; it only runs when scheduled, by cron as above or by the job runner.

The runner can run the same pass itself: `VOLT_REFRESH_URLS_EVERY=<seconds>` (or `--refresh-urls-every`), off by default, with `VOLT_REFRESH_URLS_WITHIN` / `--refresh-urls-within` (default 86400) as the window; see `docs/specs/runner.md`. Several refreshers at once (two runners, or a runner and cron) are safe: the write is compare-and-set on the URL that was read (`UPDATE version SET url = new WHERE id = ? AND url = old`), so each version is changed by exactly one of them and the others count it as skipped. The cost of the race is one wasted signing (a local computation), not a double write. No schema change was needed.

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

Proven by tests that run in CI (no network, no boto3): the interface contract on the local backend and on `S3Backend` over a fake client that follows S3's checksum rules; key validation; config parsing; boto3 not imported until used; publish through a backend (URLs, columns, scratch cleanup, only-failed retry, corruption caught, refresh, runner handler), on SQLite and, with `VOLT_TEST_PG_DSN`, on Postgres.

Proven live only against a throwaway MinIO (`bitnamilegacy/minio`, release 2025-05-24) with boto3 1.43, when `VOLT_TEST_S3_ENDPOINT` is set (plus `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for that server): single and multipart uploads at 0, 5 MiB-1, 5 MiB and 12 MiB+7 with the composite checksum verified, URL read-back, expiry and tamper rejection, content type, bad credentials and unreachable endpoint failing with a reason and no leak, and real ffmpeg cuts published, fetched and probed. The Docker Hub `minio/minio` and quay images could not be pulled on the test box, so a Bitnami legacy build was used.

Proven on real AWS S3 (2026-10-11, bucket in us-east-2, boto3 1.43, an IAM user holding only the policy above, `bench/s3_live_check.py`, 12/12): single and multipart uploads at 0, 5 MiB-1, 5 MiB and 12 MiB+7 with checksum verified, read-back through the presigned URL, expiry (403) and tampered-signature rejection (403), `video/mp4` content type, `exists()` true and a clean `False` for a missing key (so `s3:ListBucket` on the prefix does what is said above), a write outside the policy's prefix denied, bad credentials refused with no secret in the message, and nothing left behind. This found a real bug the MinIO runs could not: for a bucket outside us-east-1 boto3 signed URLs for the region but pointed them at `s3.amazonaws.com`, which AWS rejects ("the region is wrong"), so every stored `version.url` would have been dead. AWS addressing is now virtual-hosted (`bucket.s3.<region>.amazonaws.com`); S3-compatible endpoints stay path style. If the machine's clock runs ahead of AWS, a short-lived URL outlives its expiry by the skew (seen: 3.9 s ahead, so a 1 s URL still worked).

Proven without AWS (fake client plus botocore 1.43 `Stubber`, no network; with boto3 not installed the Stubber and credential-chain tests skip): the SSE and KMS key id arguments boto3 accepts for a single PutObject and for CreateMultipartUpload (boto3 parameter validation, not a captured request); a server that does not report the configured encryption fails the upload and the object is deleted; public-URL mode never signs, is stable, and migrates old presigned URLs once; the compare-and-set race between two refreshers; the default credential chain resolving container-role credentials from a local port-0 HTTP endpoint (the URL is signed with the role's key and session token, and the client is built with no key arguments) and failing with a clean reason when no credential exists.

Not proven: SSE-KMS against a real KMS key and bucket (no object was written with it, so real header handling, the `kms:GenerateDataKey` / `kms:Decrypt` permissions and the SHA-256 checksum under KMS are unconfirmed), a real CDN or public-read bucket (the URL shape is tested, a fetch through a CDN is not), bucket policies, cross-region latency, a real ffmpeg cut published through `publish_files` to AWS (only the backend was exercised), and runs from a real EC2 instance profile (IMDS), EKS web-identity or ECS task role (only the container-credentials code path, against a local stand-in, was run). A scheduled refresh has been run only against fake clients, never against AWS. Corruption detection is proven against the fake client, not against a server that corrupts. Proxies and other essences are not routed through the seam.


## Azure Blob and GCS: what is proven and what is not

No real Azure or Google Cloud account exists for this project, so neither backend has touched a real service. Everything below is against fakes or local emulators.

Proven in CI with no SDK and no network (`tests/test_storage_clouds.py`): `BackendContract` on both backends over fakes that mimic the client surface each backend calls; block-boundary and empty-file uploads; Azure resume (a failed attempt leaves nothing visible, the retry re-sends only the missing blocks, a changed file reuses no stale block); corruption caught and the object deleted (Azure by SHA-256 read-back; GCS by the server's MD5/CRC32C, and by SHA-256 read-back when those lie); unverifiable GCS uploads refused; error text free of `sig=`, `AccountKey=` and token parameters; URL expiry bounds; configuration parsing; a missing SDK is a `StorageConfigError`; neither SDK is imported until used; expiry parsing for refresh. The fakes are written from the SDK documentation and from what the emulators did, so they prove the backend's logic, not the providers' behaviour.

Proven against emulators, when `VOLT_TEST_AZURITE_URL` / `VOLT_TEST_GCS_ENDPOINT` are set and the SDK is installed (azure-storage-blob 12.31.0, google-cloud-storage 3.17.0, Azurite 3.37.0 and fake-gcs-server 1.56.1 in Docker, 2026-10-12; CI does not run these):
- Azurite: the full contract; uploads at 0, 1 MiB-1, 1 MiB and 2 MiB+7 with 1 MiB blocks; content type, MD5 and `sha256` metadata on the stored blob; account-key SAS URLs read back, a tampered signature rejected, and a 2 s SAS refused after it expired; a real interrupted upload (the third `stage_block` dropped) resumed by sending only the missing blocks; a wrong account key failing with a reason and no key in the message.
- fake-gcs-server: the full contract; resumable uploads at 0, 1 MiB-1, 1 MiB and 2 MiB+7 with 1 MiB chunks; content type and the server's MD5 checked; V4 signed URL shape (algorithm, expiry, signature present) built with a throwaway RSA key.

Not proven:
- Anything against real Azure or GCS: real authentication, Entra ID (`DefaultAzureCredential`) and the user delegation key SAS path (Azurite was exercised with an account key only), signed URLs with IAM `signBlob`, real latency, throttling and retry behaviour, bucket or container policies, CDN or public access, customer-managed keys, soft delete or versioning on replace.
- Reading a GCS object through its signed URL, and GCS URL expiry or tamper rejection: fake-gcs-server neither serves the path-style signed URL for names containing `/` nor validates signatures, so the contract test reads GCS objects through the emulator's JSON API instead. Only the URL's shape is checked.
- GCS corruption and a failed chunk against a server that misbehaves (fakes only), and Azure corruption against a server that corrupts (fake only).
- The cost of the SHA-256 read-back on large files, and unreachable-endpoint behaviour (the SDKs' retry backoff took about 90 s to give up on a dead port, so that test was left out).
- A real ffmpeg cut published through `publish_files` to either cloud, and the Postgres CI job with these backends (the storage code does not touch the database; the Postgres suite was not run for this change).

## Next backends

None planned. A new backend subclasses `BackendContract` in `control/tests/test_storage.py`.
