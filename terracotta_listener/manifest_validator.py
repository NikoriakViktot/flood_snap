"""
Manifest validator — standalone copy for Terracotta listener.

Validates a dem_analysis manifest.json before registering COGs:
  1. schema_version is supported
  2. run_id matches expected
  3. Output paths do not escape the analysis directory (path traversal guard)
  4. Checksums are present and valid sha256 hex strings
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestValidationError(Exception):
    pass


def validate_manifest(
    manifest: dict,
    *,
    expected_run_id: str,
    analysis_dir: Path,
) -> None:
    """Validate manifest. Raises ManifestValidationError on any failure."""
    version = manifest.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestValidationError(
            f"Unsupported schema_version: {version!r}. Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    actual_run_id = manifest.get("run_id", "")
    if actual_run_id != expected_run_id:
        raise ManifestValidationError(
            f"run_id mismatch: manifest={actual_run_id!r}, expected={expected_run_id!r}"
        )

    resolved_base = analysis_dir.resolve()
    for stem, artifacts in manifest.get("outputs", {}).items():
        for rel_path in _iter_rel_paths(artifacts):
            abs_path = (analysis_dir / rel_path).resolve()
            try:
                abs_path.relative_to(resolved_base)
            except ValueError:
                raise ManifestValidationError(
                    f"Path traversal for stem '{stem}': {rel_path!r} escapes {analysis_dir}"
                )

    checksums = manifest.get("checksums", {})
    if not checksums:
        raise ManifestValidationError("manifest.checksums is empty")
    for rel_path, checksum in checksums.items():
        if not _SHA256_RE.match(str(checksum)):
            raise ManifestValidationError(
                f"Invalid sha256 for '{rel_path}': {checksum!r}"
            )


def _iter_rel_paths(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_rel_paths(v)
