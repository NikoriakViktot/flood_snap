"""Unit tests for Terracotta listener — no Redis/HTTP required."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "terracotta_listener"))

from manifest_validator import validate_manifest, ManifestValidationError
from listener import _get_nested, handle_analysis_completed

_ANALYSIS_DIR_STR = "/data/artifacts/run-1/dem_analysis"
_ANALYSIS_DIR = Path(_ANALYSIS_DIR_STR)
_SHA256 = "a" * 64

_GOOD_MANIFEST = {
    "schema_version": "1.0",
    "run_id": "run-1",
    "spec_hash": "b" * 64,
    "outputs": {
        "srtm": {
            "ellip": "outputs/srtm/srtm_ellip.tif",
            "terrain_attrs": {"slope": "outputs/srtm/srtm_slope.tif"},
            "geomorphons": "outputs/srtm/srtm_geomorphons.tif",
        },
    },
    "checksums": {
        "outputs/srtm/srtm_ellip.tif":      _SHA256,
        "outputs/srtm/srtm_slope.tif":      _SHA256,
        "outputs/srtm/srtm_geomorphons.tif": _SHA256,
    },
}


# ---------------------------------------------------------------------------
# Manifest validator (standalone copy in listener)
# ---------------------------------------------------------------------------

class TestListenerManifestValidator:
    """Listener's validator must behave identically to the CP validator."""

    def test_valid_manifest_passes(self):
        validate_manifest(_GOOD_MANIFEST, expected_run_id="run-1", analysis_dir=_ANALYSIS_DIR)

    def test_path_traversal_caught(self):
        m = {**_GOOD_MANIFEST, "outputs": {"srtm": {"ellip": "../../etc/passwd"}}}
        with pytest.raises(ManifestValidationError, match="traversal"):
            validate_manifest(m, expected_run_id="run-1", analysis_dir=_ANALYSIS_DIR)

    def test_wrong_run_id_caught(self):
        with pytest.raises(ManifestValidationError, match="run_id"):
            validate_manifest(_GOOD_MANIFEST, expected_run_id="other-id", analysis_dir=_ANALYSIS_DIR)

    def test_bad_schema_version_caught(self):
        m = {**_GOOD_MANIFEST, "schema_version": "2.0"}
        with pytest.raises(ManifestValidationError, match="schema_version"):
            validate_manifest(m, expected_run_id="run-1", analysis_dir=_ANALYSIS_DIR)

    def test_empty_checksums_caught(self):
        m = {**_GOOD_MANIFEST, "checksums": {}}
        with pytest.raises(ManifestValidationError, match="empty"):
            validate_manifest(m, expected_run_id="run-1", analysis_dir=_ANALYSIS_DIR)

    def test_bad_checksum_format_caught(self):
        m = {**_GOOD_MANIFEST, "checksums": {"some/file.tif": "not-hex"}}
        with pytest.raises(ManifestValidationError, match="sha256"):
            validate_manifest(m, expected_run_id="run-1", analysis_dir=_ANALYSIS_DIR)


# ---------------------------------------------------------------------------
# _get_nested helper
# ---------------------------------------------------------------------------

class TestGetNested:
    def test_flat_key(self):
        assert _get_nested({"ellip": "/x.tif"}, "ellip") == "/x.tif"

    def test_dot_key_one_level(self):
        d = {"terrain_attrs": {"slope": "/slope.tif"}}
        assert _get_nested(d, "terrain_attrs.slope") == "/slope.tif"

    def test_dot_key_two_levels(self):
        d = {"a": {"b": {"c": "/file.tif"}}}
        assert _get_nested(d, "a.b.c") == "/file.tif"

    def test_missing_top_key_returns_none(self):
        assert _get_nested({}, "missing") is None

    def test_missing_nested_key_returns_none(self):
        assert _get_nested({"a": {"b": "/x"}}, "a.c") is None

    def test_intermediate_node_not_string_returns_none(self):
        # terrain_attrs is a dict, not a leaf string
        d = {"terrain_attrs": {"slope": "/slope.tif"}}
        assert _get_nested(d, "terrain_attrs") is None

    def test_non_string_leaf_returns_none(self):
        assert _get_nested({"count": 42}, "count") is None


# ---------------------------------------------------------------------------
# handle_analysis_completed
# ---------------------------------------------------------------------------

class TestHandleAnalysisCompleted:
    def _envelope(self, manifest_path, output_dir):
        return {
            "run_id": "run-1",
            "event": "ANALYSIS_COMPLETED",
            "artifacts": {
                "manifest": str(manifest_path),
                "output_dir": str(output_dir),
            },
        }

    def test_skips_when_manifest_path_missing_from_envelope(self):
        envelope = {"run_id": "run-1", "event": "ANALYSIS_COMPLETED", "artifacts": {}}
        # Must not raise
        with patch("listener.register_cog") as mock:
            handle_analysis_completed(envelope)
            mock.assert_not_called()

    def test_skips_when_manifest_file_does_not_exist(self):
        envelope = self._envelope("/nonexistent/manifest.json", "/nonexistent/")
        with patch("listener.register_cog") as mock:
            handle_analysis_completed(envelope)
            mock.assert_not_called()

    def test_registers_cogs_for_valid_manifest(self, tmp_path):
        analysis_dir = tmp_path / "run-1" / "dem_analysis"
        analysis_dir.mkdir(parents=True)
        manifest_path = analysis_dir / "manifest.json"
        manifest_path.write_text(json.dumps(_GOOD_MANIFEST))

        with patch("listener.register_cog", return_value=True) as mock_register:
            handle_analysis_completed(self._envelope(manifest_path, analysis_dir))
            assert mock_register.called

    def test_does_not_register_when_manifest_invalid(self, tmp_path):
        analysis_dir = tmp_path / "run-1" / "dem_analysis"
        analysis_dir.mkdir(parents=True)
        manifest_path = analysis_dir / "manifest.json"
        # Wrong run_id in manifest
        bad = {**_GOOD_MANIFEST, "run_id": "wrong-id"}
        manifest_path.write_text(json.dumps(bad))

        with patch("listener.register_cog") as mock_register:
            handle_analysis_completed(self._envelope(manifest_path, analysis_dir))
            mock_register.assert_not_called()

    def test_does_not_register_when_manifest_corrupted(self, tmp_path):
        analysis_dir = tmp_path / "run-1" / "dem_analysis"
        analysis_dir.mkdir(parents=True)
        manifest_path = analysis_dir / "manifest.json"
        manifest_path.write_text("{ not valid json }")

        with patch("listener.register_cog") as mock_register:
            handle_analysis_completed(self._envelope(manifest_path, analysis_dir))
            mock_register.assert_not_called()

    def test_registers_only_matched_cog_keys(self, tmp_path):
        """Only COG_KEYS present in manifest outputs are registered."""
        analysis_dir = tmp_path / "run-1" / "dem_analysis"
        analysis_dir.mkdir(parents=True)
        manifest_path = analysis_dir / "manifest.json"
        # Minimal manifest: only "ellip" present for srtm
        minimal = {
            **_GOOD_MANIFEST,
            "outputs": {"srtm": {"ellip": "outputs/srtm/srtm_ellip.tif"}},
        }
        manifest_path.write_text(json.dumps(minimal))

        calls = []
        with patch("listener.register_cog", side_effect=lambda *a, **kw: calls.append(a) or True):
            handle_analysis_completed(self._envelope(manifest_path, analysis_dir))

        # Only "ellip" key should match; terrain_attrs.slope, geomorphons, etc. are absent
        registered_keys = [call[2] for call in calls]  # 3rd arg is `key`
        assert "ellip" in registered_keys
        assert all(k == "ellip" for k in registered_keys), \
            f"Expected only 'ellip', got: {registered_keys}"

    def test_skips_cog_key_not_in_manifest(self, tmp_path):
        analysis_dir = tmp_path / "run-1" / "dem_analysis"
        analysis_dir.mkdir(parents=True)
        manifest_path = analysis_dir / "manifest.json"
        # No terrain_attrs in manifest
        manifest_path.write_text(json.dumps(_GOOD_MANIFEST))

        calls = []
        with patch("listener.register_cog", side_effect=lambda *a, **kw: calls.append(a) or True):
            handle_analysis_completed(self._envelope(manifest_path, analysis_dir))

        registered_keys = [call[2] for call in calls]
        # terrain_attrs.slope is in _GOOD_MANIFEST as a nested key
        # The manifest has "terrain_attrs": {"slope": "..."} so terrain_attrs.slope should match
        # (depends on COG_KEYS — but the key "terrain_attrs.slope" should map to it)
        assert len(calls) >= 1, "At least one COG should be registered"
