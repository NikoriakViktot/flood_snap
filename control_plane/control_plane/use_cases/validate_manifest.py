"""
Manifest validator — application layer.

Validates a dem_analysis manifest.json before any agent consumes its artifacts.

Checks:
  1. schema_version is a supported version
  2. run_id matches the expected run
  3. All output paths stay within the analysis directory (path traversal guard)
  4. All checksums are present and are valid sha256 hex strings (64 lowercase chars)

Usage:
    from control_plane.use_cases.validate_manifest import validate_manifest, ManifestValidationError

    try:
        validate_manifest(manifest_dict, expected_run_id="abc-123", analysis_dir=Path("/data/artifacts/abc-123/dem_analysis"))
    except ManifestValidationError as e:
        log.error("Manifest invalid: %s", e)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestValidationError(Exception):
    """Raised when a manifest fails one or more validation checks."""


def validate_manifest(
    manifest: dict,
    *,
    expected_run_id: str,
    analysis_dir: Path,
) -> None:
    """Validate a manifest dict in full.

    Args:
        manifest:         Parsed manifest.json as a plain dict.
        expected_run_id:  The run_id this manifest must belong to.
        analysis_dir:     The root directory under which all outputs must reside.
                          Typically {artifact_root}/{run_id}/dem_analysis/

    Raises:
        ManifestValidationError: on any check failure (first failure only).
    """
    _check_schema_version(manifest)
    _check_run_id(manifest, expected_run_id)
    _check_no_path_traversal(manifest, analysis_dir)
    _check_checksums(manifest)


# ---------------------------------------------------------------------------
# Individual checks (each raises ManifestValidationError on failure)
# ---------------------------------------------------------------------------

def _check_schema_version(manifest: dict) -> None:
    version = manifest.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestValidationError(
            f"Unsupported schema_version: {version!r}. "
            f"Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )


def _check_run_id(manifest: dict, expected: str) -> None:
    actual = manifest.get("run_id", "")
    if actual != expected:
        raise ManifestValidationError(
            f"run_id mismatch: manifest has {actual!r}, expected {expected!r}"
        )


def _check_no_path_traversal(manifest: dict, analysis_dir: Path) -> None:
    resolved_base = analysis_dir.resolve()
    outputs: dict = manifest.get("outputs", {})

    for stem, stem_artifacts in outputs.items():
        for rel_path in _iter_rel_paths(stem_artifacts):
            abs_path = (analysis_dir / rel_path).resolve()
            try:
                abs_path.relative_to(resolved_base)
            except ValueError:
                raise ManifestValidationError(
                    f"Path traversal detected for stem '{stem}': "
                    f"{rel_path!r} escapes {analysis_dir}"
                )


def _check_checksums(manifest: dict) -> None:
    checksums: dict = manifest.get("checksums", {})
    if not checksums:
        raise ManifestValidationError("manifest.checksums is empty — no files were verified")

    for rel_path, checksum in checksums.items():
        if not _SHA256_RE.match(str(checksum)):
            raise ManifestValidationError(
                f"Invalid sha256 for '{rel_path}': {checksum!r} "
                f"(must be exactly 64 lowercase hex chars)"
            )


def _iter_rel_paths(node: object) -> Iterator[str]:
    """Yield relative path strings from a nested artifact structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_rel_paths(v)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_rel_paths(item)
