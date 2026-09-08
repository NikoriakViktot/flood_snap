"""Tests for manifest validator — all 4 checks + edge cases."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

from control_plane.use_cases.validate_manifest import validate_manifest, ManifestValidationError

_ANALYSIS_DIR = Path("/data/artifacts/abc-123/dem_analysis")
_GOOD_SHA256 = "a" * 64

_GOOD_MANIFEST = {
    "schema_version": "1.0",
    "run_id": "abc-123",
    "spec_hash": "b" * 64,
    "outputs": {
        "srtm": {
            "ellip": "outputs/srtm/srtm_ellip.tif",
            "orth":  "outputs/srtm/srtm_orth.tif",
        },
        "alos": {
            "terrain_attrs": {
                "slope": "outputs/alos/alos_slope.tif",
                "aspect": "outputs/alos/alos_aspect.tif",
            },
            "geomorphons": "outputs/alos/alos_geomorphons.tif",
        },
    },
    "checksums": {
        "outputs/srtm/srtm_ellip.tif":      _GOOD_SHA256,
        "outputs/srtm/srtm_orth.tif":       _GOOD_SHA256,
        "outputs/alos/alos_slope.tif":      _GOOD_SHA256,
        "outputs/alos/alos_aspect.tif":     _GOOD_SHA256,
        "outputs/alos/alos_geomorphons.tif": _GOOD_SHA256,
    },
}


def _validate(manifest, *, run_id="abc-123", analysis_dir=_ANALYSIS_DIR):
    validate_manifest(manifest, expected_run_id=run_id, analysis_dir=analysis_dir)


class TestValidManifest:
    def test_good_manifest_passes(self):
        _validate(_GOOD_MANIFEST)

    def test_minimal_manifest_passes(self):
        m = {
            "schema_version": "1.0",
            "run_id": "abc-123",
            "outputs": {},
            "checksums": {"some/file.tif": _GOOD_SHA256},
        }
        _validate(m)


class TestSchemaVersionCheck:
    def test_missing_schema_version_fails(self):
        m = {**_GOOD_MANIFEST, "schema_version": None}
        with pytest.raises(ManifestValidationError, match="schema_version"):
            _validate(m)

    def test_unknown_version_fails(self):
        m = {**_GOOD_MANIFEST, "schema_version": "9.9"}
        with pytest.raises(ManifestValidationError, match="9.9"):
            _validate(m)

    def test_empty_version_fails(self):
        m = {**_GOOD_MANIFEST, "schema_version": ""}
        with pytest.raises(ManifestValidationError, match="schema_version"):
            _validate(m)

    def test_version_1_0_accepted(self):
        m = {**_GOOD_MANIFEST, "schema_version": "1.0"}
        _validate(m)  # must not raise


class TestRunIdCheck:
    def test_run_id_mismatch_fails(self):
        with pytest.raises(ManifestValidationError, match="run_id"):
            _validate(_GOOD_MANIFEST, run_id="different-id")

    def test_missing_run_id_fails(self):
        m = {k: v for k, v in _GOOD_MANIFEST.items() if k != "run_id"}
        with pytest.raises(ManifestValidationError, match="run_id"):
            _validate(m)

    def test_empty_run_id_fails(self):
        m = {**_GOOD_MANIFEST, "run_id": ""}
        with pytest.raises(ManifestValidationError, match="run_id"):
            _validate(m)

    def test_correct_run_id_passes(self):
        _validate(_GOOD_MANIFEST, run_id="abc-123")


class TestPathTraversalCheck:
    def test_classic_dotdot_escape_fails(self):
        m = {**_GOOD_MANIFEST, "outputs": {"srtm": {"ellip": "../../../etc/passwd"}}}
        with pytest.raises(ManifestValidationError, match="traversal"):
            _validate(m)

    def test_nested_dotdot_escape_fails(self):
        m = {**_GOOD_MANIFEST, "outputs": {
            "srtm": {"terrain_attrs": {"slope": "../../secret/slope.tif"}}
        }}
        with pytest.raises(ManifestValidationError, match="traversal"):
            _validate(m)

    def test_absolute_path_outside_dir_fails(self):
        # Relative to analysis_dir, this resolves outside
        m = {**_GOOD_MANIFEST, "outputs": {"srtm": {"ellip": "../../other_run/file.tif"}}}
        with pytest.raises(ManifestValidationError, match="traversal"):
            _validate(m)

    def test_normal_relative_path_allowed(self):
        m = {**_GOOD_MANIFEST, "outputs": {"srtm": {"ellip": "outputs/srtm/srtm_ellip.tif"}}}
        _validate(m)  # must not raise

    def test_nested_normal_path_allowed(self):
        m = {**_GOOD_MANIFEST, "outputs": {
            "srtm": {"terrain_attrs": {"slope": "outputs/srtm/srtm_slope.tif"}}
        }}
        _validate(m)  # must not raise

    def test_empty_outputs_passes(self):
        m = {**_GOOD_MANIFEST, "outputs": {}}
        _validate(m)


class TestChecksumCheck:
    def test_empty_checksums_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {}}
        with pytest.raises(ManifestValidationError, match="empty"):
            _validate(m)

    def test_missing_checksums_key_fails(self):
        m = {k: v for k, v in _GOOD_MANIFEST.items() if k != "checksums"}
        with pytest.raises(ManifestValidationError, match="empty"):
            _validate(m)

    def test_invalid_checksum_format_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "not-a-hash"}}
        with pytest.raises(ManifestValidationError, match="sha256"):
            _validate(m)

    def test_uppercase_checksum_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "A" * 64}}
        with pytest.raises(ManifestValidationError, match="sha256"):
            _validate(m)

    def test_short_checksum_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "abc123"}}
        with pytest.raises(ManifestValidationError, match="sha256"):
            _validate(m)

    def test_63_char_checksum_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "a" * 63}}
        with pytest.raises(ManifestValidationError, match="sha256"):
            _validate(m)

    def test_65_char_checksum_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "a" * 65}}
        with pytest.raises(ManifestValidationError, match="sha256"):
            _validate(m)

    def test_valid_64_char_hex_passes(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "f" * 64}}
        _validate(m)

    def test_multiple_checksums_all_valid(self):
        m = {**_GOOD_MANIFEST, "checksums": {
            "file1.tif": _GOOD_SHA256,
            "file2.tif": "b" * 64,
            "file3.tif": "c" * 64,
        }}
        _validate(m)

    def test_one_bad_checksum_in_many_fails(self):
        m = {**_GOOD_MANIFEST, "checksums": {
            "file1.tif": _GOOD_SHA256,
            "file2.tif": "bad",
        }}
        with pytest.raises(ManifestValidationError, match="sha256"):
            _validate(m)
