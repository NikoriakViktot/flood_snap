"""
Upload a basin's products to S3 (AWS S3 or any S3-compatible store, e.g. SeaweedFS).

Configuration follows the standard AWS env vars (same convention as icesat2-ingest):
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
    AWS_ENDPOINT_URL                       custom endpoint; unset/empty for AWS S3
    AWS_REQUEST_CHECKSUM_CALCULATION=when_required   } keep for non-AWS S3, otherwise
    AWS_RESPONSE_CHECKSUM_VALIDATION=when_required   } objects are stored aws-chunked
    S3_BUCKET   bucket; when unset, uploading is disabled
    S3_PREFIX   lake root (default "hydro/lake")
    S3_WRITABLE_PREFIXES  comma list the IAM policy allows PutObject on; S3_PREFIX must be inside one

Layout (hive-style partitions, one immutable folder per basin id):
    s3://{bucket}/{prefix}/basin_delineation/v1/dem_source={dem}/basin_id={id}/
        manifest.json  analysis/*.tif  tiles/*.tif  vectors/*.geojson
manifest.json is uploaded last, so its presence means the upload is complete.
The IAM policy grants PutObject only (no DeleteObject): re-running a basin overwrites the
same keys; the basin id fixes the parameter set, so the file list never shrinks.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

log = logging.getLogger("s3_upload")

S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_PREFIX = os.environ.get("S3_PREFIX", "hydro/lake").strip("/")
S3_WRITABLE_PREFIXES = [p.strip().strip("/") for p in os.environ.get(
    "S3_WRITABLE_PREFIXES", "staging,serving,curated,hydro/lake").split(",") if p.strip()]
DATASET = "basin_delineation/v1"

_CONTENT_TYPES = {".tif": "image/tiff", ".geojson": "application/geo+json", ".json": "application/json"}


def enabled() -> bool:
    return bool(S3_BUCKET)


def check_config() -> None:
    """Fail fast when S3_PREFIX is outside the folders the IAM policy lets us write."""
    if not enabled():
        log.info("S3 upload disabled (S3_BUCKET not set)")
        return
    if not any(S3_PREFIX == p or S3_PREFIX.startswith(p + "/") for p in S3_WRITABLE_PREFIXES):
        raise RuntimeError(
            f"S3_PREFIX={S3_PREFIX!r} is not under a writable prefix {S3_WRITABLE_PREFIXES} — "
            "PutObject would be denied")
    log.info("S3 upload enabled → s3://%s/%s/%s/", S3_BUCKET, S3_PREFIX, DATASET)


def basin_key_root(dem_source: str, basin_id: str) -> str:
    return f"{S3_PREFIX}/{DATASET}/dem_source={dem_source}/basin_id={basin_id}"


def basin_root(dem_source: str, basin_id: str) -> str:
    return f"s3://{S3_BUCKET}/{basin_key_root(dem_source, basin_id)}"


def _client():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None)


def _put(s3, path: Path, key: str) -> None:
    ctype = _CONTENT_TYPES.get(path.suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    s3.upload_file(str(path), S3_BUCKET, key, ExtraArgs={"ContentType": ctype})


def upload_basin(dem_source: str, basin_id: str, out_dir: Path) -> dict:
    """Upload every file of a basin product dir, manifest last. Returns {"root", "objects"}."""
    s3 = _client()
    root = basin_key_root(dem_source, basin_id)
    files = sorted(p for p in out_dir.rglob("*") if p.is_file() and "_tmp" not in p.parts)
    manifest = out_dir / "manifest.json"
    ordered = [p for p in files if p != manifest] + ([manifest] if manifest.exists() else [])
    for p in ordered:
        _put(s3, p, f"{root}/{p.relative_to(out_dir).as_posix()}")
    log.info("S3: uploaded %d objects → %s", len(ordered), basin_root(dem_source, basin_id))
    return {"root": basin_root(dem_source, basin_id), "objects": len(ordered)}


def upload_manifest(dem_source: str, basin_id: str, out_dir: Path) -> None:
    _put(_client(), out_dir / "manifest.json", f"{basin_key_root(dem_source, basin_id)}/manifest.json")
