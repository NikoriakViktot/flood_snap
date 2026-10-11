"""
Upload a staged data-lake tree to S3, mirroring paths: <staging>/<key> → s3://$S3_BUCKET/<key>.

Same env convention as dem_worker/s3_upload.py: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_DEFAULT_REGION / AWS_ENDPOINT_URL (empty for AWS) / S3_BUCKET / S3_WRITABLE_PREFIXES.
- Only keys under a writable prefix are sent (the IAM policy has PutObject, no Delete).
- Skips objects that already exist with the same size (resumable, no re-upload).
- manifest.json files go last in each directory, so a manifest means "this folder is complete".
- Large files use multipart (8 MiB parts), which PutObject permission covers.

  docker run --rm --env-file .env -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/upload_lake.py --staging /data/s3_staging --prefix hydro/lake/dem [--dry-run]
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TYPES = {".tif": "image/tiff", ".vrt": "application/xml", ".geojson": "application/geo+json",
         ".json": "application/json", ".csv": "text/csv"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", default="/data/s3_staging")
    ap.add_argument("--prefix", default="hydro/lake", help="only upload keys under this prefix")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not bucket:
        sys.exit("S3_BUCKET is not set")
    writable = [p.strip().strip("/") for p in os.environ.get(
        "S3_WRITABLE_PREFIXES", "staging,serving,curated,hydro/lake").split(",") if p.strip()]
    prefix = a.prefix.strip("/")
    if not any(prefix == w or prefix.startswith(w + "/") for w in writable):
        sys.exit(f"prefix {prefix!r} is outside the writable prefixes {writable}")

    root = Path(a.staging)
    files = [p for p in (root / prefix).rglob("*") if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: (p.name == "manifest.json", str(p)))   # manifests last
    total = sum(p.stat().st_size for p in files)
    print(f"{len(files)} files, {total / 2**30:.2f} GiB → s3://{bucket}/{prefix}/", flush=True)
    if a.dry_run:
        for p in files[:10]:
            print("  ", p.relative_to(root).as_posix())
        return

    import boto3
    from boto3.s3.transfer import TransferConfig
    s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None)
    cfg = TransferConfig(multipart_threshold=64 * 2**20, multipart_chunksize=8 * 2**20, max_concurrency=4)

    def put(p: Path):
        key = p.relative_to(root).as_posix()
        size = p.stat().st_size
        try:
            if s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == size:
                return "skip", size
        except s3.exceptions.ClientError:
            pass
        ctype = TYPES.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        s3.upload_file(str(p), bucket, key, ExtraArgs={"ContentType": ctype}, Config=cfg)
        return "put", size

    data = [p for p in files if p.name != "manifest.json"]
    manifests = [p for p in files if p.name == "manifest.json"]
    sent = skipped = done_bytes = 0
    for batch in (data, manifests):
        with ThreadPoolExecutor(a.workers) as pool:
            futs = {pool.submit(put, p): p for p in batch}
            for i, f in enumerate(as_completed(futs), 1):
                status, size = f.result()
                done_bytes += size
                sent += status == "put"
                skipped += status == "skip"
                if i % 25 == 0:
                    print(f"  {done_bytes / 2**30:.2f}/{total / 2**30:.2f} GiB", flush=True)
    print(f"DONE put={sent} skipped={skipped} → s3://{bucket}/{prefix}/", flush=True)


if __name__ == "__main__":
    main()
