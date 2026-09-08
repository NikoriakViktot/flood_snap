"""Unit tests for dem_analysis_worker — no xDEM/WBT/rasterio required."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dem_analysis_worker"))

from dem_analysis_worker import _spec_hash, _Spec, _build_manifest, _flatten_artifacts


class TestSpecHash:
    def test_deterministic(self):
        spec = {"inputs": {"base_dir": "/x", "geoid_tif": "/y", "dem_registry": {}}}
        assert _spec_hash(spec) == _spec_hash(spec)

    def test_changes_on_field_change(self):
        s1 = {"inputs": {"base_dir": "/x", "geoid_tif": "/y", "dem_registry": {}}}
        s2 = {"inputs": {"base_dir": "/z", "geoid_tif": "/y", "dem_registry": {}}}
        assert _spec_hash(s1) != _spec_hash(s2)

    def test_canonical_key_order_independent(self):
        s1 = {"a": 1, "b": 2, "c": 3}
        s2 = {"c": 3, "b": 2, "a": 1}
        assert _spec_hash(s1) == _spec_hash(s2)

    def test_nested_key_order_independent(self):
        s1 = {"inputs": {"b": 2, "a": 1}}
        s2 = {"inputs": {"a": 1, "b": 2}}
        assert _spec_hash(s1) == _spec_hash(s2)

    def test_returns_64_char_lowercase_hex(self):
        h = _spec_hash({"x": "y"})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_matches_manual_sha256(self):
        spec = {"inputs": {"base_dir": "/x"}}
        canonical = json.dumps(spec, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        assert _spec_hash(spec) == expected


class TestSpecAccessor:
    _RAW = {
        "inputs": {
            "base_dir": "/data/dems",
            "geoid_tif": "/data/egg.tif",
            "dem_registry": {
                "srtm": {"vertical_datum": "EGM96"},   # dict format
                "alos": "EGM08",                       # old-style string
            },
            "aoi": {"pour_point": [25.05, 47.95]},
        },
        "plan": {
            "crs_conversion": {"enabled": True},
            "terrain": {"enabled": False},
            "hydro": {"enabled": True, "stream_threshold": 300},
        },
        "outputs": {"artifact_root": "/custom/artifacts"},
    }

    def test_base_dir(self):
        assert _Spec(self._RAW).base_dir == Path("/data/dems")

    def test_geoid_tif(self):
        assert _Spec(self._RAW).geoid_tif == "/data/egg.tif"

    def test_dem_registry_normalizes_dict_entry_to_string(self):
        assert _Spec(self._RAW).dem_registry["srtm"] == "EGM96"

    def test_dem_registry_preserves_string_entry(self):
        assert _Spec(self._RAW).dem_registry["alos"] == "EGM08"

    def test_pour_point_returns_tuple(self):
        assert _Spec(self._RAW).pour_point == (25.05, 47.95)

    def test_pour_point_none_when_aoi_empty(self):
        raw = {**self._RAW, "inputs": {**self._RAW["inputs"], "aoi": {}}}
        assert _Spec(raw).pour_point is None

    def test_pour_point_none_when_no_aoi_key(self):
        inputs = {k: v for k, v in self._RAW["inputs"].items() if k != "aoi"}
        raw = {**self._RAW, "inputs": inputs}
        assert _Spec(raw).pour_point is None

    def test_artifact_root(self):
        assert _Spec(self._RAW).artifact_root == Path("/custom/artifacts")

    def test_artifact_root_default(self):
        raw = {**self._RAW, "outputs": {}}
        assert _Spec(raw).artifact_root == Path("/data/artifacts")

    def test_plan_enabled_true(self):
        assert _Spec(self._RAW).plan_enabled("hydro") is True

    def test_plan_enabled_false(self):
        assert _Spec(self._RAW).plan_enabled("terrain") is False

    def test_plan_enabled_missing_key(self):
        assert _Spec(self._RAW).plan_enabled("geomorphons") is False

    def test_plan_get_present_value(self):
        assert _Spec(self._RAW).plan_get("hydro", "stream_threshold", 999) == 300

    def test_plan_get_default_for_missing_key(self):
        assert _Spec(self._RAW).plan_get("terrain", "methods", ["horn"]) == ["horn"]

    def test_plan_get_default_for_missing_section(self):
        assert _Spec(self._RAW).plan_get("geomorphons", "search", 50) == 50


class TestFlattenArtifacts:
    def test_flat_dict(self):
        d = {"ellip": "/a.tif", "orth": "/b.tif"}
        result = dict(_flatten_artifacts(d))
        assert result == {"ellip": "/a.tif", "orth": "/b.tif"}

    def test_nested_one_level(self):
        d = {"terrain_attrs": {"slope": "/slope.tif", "aspect": "/aspect.tif"}}
        result = dict(_flatten_artifacts(d))
        assert result["terrain_attrs.slope"] == "/slope.tif"
        assert result["terrain_attrs.aspect"] == "/aspect.tif"

    def test_nested_two_levels(self):
        d = {"hydro": {"metrics": {"hand": "/hand.tif"}}}
        result = dict(_flatten_artifacts(d))
        assert result["hydro.metrics.hand"] == "/hand.tif"

    def test_empty_dict(self):
        assert list(_flatten_artifacts({})) == []

    def test_mixed_flat_and_nested(self):
        d = {
            "ellip": "/ellip.tif",
            "hydro": {"hand": "/hand.tif", "twi": "/twi.tif"},
        }
        result = dict(_flatten_artifacts(d))
        assert "ellip" in result
        assert "hydro.hand" in result
        assert "hydro.twi" in result
        assert len(result) == 3


class TestBuildManifest:
    def _make_dummy_files(self, tmp_path, names):
        for name in names:
            (tmp_path / name).write_bytes(b"dummy content for " + name.encode())

    def test_schema_version(self, tmp_path):
        self._make_dummy_files(tmp_path, ["a.tif"])
        m = _build_manifest("r1", "h" * 64, {}, {"s": {"ellip": str(tmp_path / "a.tif")}}, tmp_path)
        assert m["schema_version"] == "1.0"

    def test_run_id(self, tmp_path):
        self._make_dummy_files(tmp_path, ["a.tif"])
        m = _build_manifest("my-run-id", "h" * 64, {}, {"s": {"ellip": str(tmp_path / "a.tif")}}, tmp_path)
        assert m["run_id"] == "my-run-id"

    def test_spec_hash_stored(self, tmp_path):
        self._make_dummy_files(tmp_path, ["a.tif"])
        shash = "f" * 64
        m = _build_manifest("r1", shash, {}, {"s": {"ellip": str(tmp_path / "a.tif")}}, tmp_path)
        assert m["spec_hash"] == shash

    def test_outputs_use_relative_paths(self, tmp_path):
        self._make_dummy_files(tmp_path, ["srtm_ellip.tif"])
        m = _build_manifest(
            "r1", "h" * 64, {},
            {"srtm": {"ellip": str(tmp_path / "srtm_ellip.tif")}},
            tmp_path,
        )
        rel = m["outputs"]["srtm"]["ellip"]
        assert not Path(rel).is_absolute(), f"Expected relative path, got: {rel}"

    def test_checksums_present_for_all_files(self, tmp_path):
        names = ["srtm_ellip.tif", "srtm_orth.tif"]
        self._make_dummy_files(tmp_path, names)
        m = _build_manifest(
            "r1", "h" * 64, {},
            {"srtm": {"ellip": str(tmp_path / "srtm_ellip.tif"),
                      "orth":  str(tmp_path / "srtm_orth.tif")}},
            tmp_path,
        )
        assert len(m["checksums"]) == 2

    def test_checksums_are_valid_sha256(self, tmp_path):
        self._make_dummy_files(tmp_path, ["a.tif"])
        m = _build_manifest("r1", "h" * 64, {}, {"s": {"ellip": str(tmp_path / "a.tif")}}, tmp_path)
        for checksum in m["checksums"].values():
            assert len(checksum) == 64
            assert all(c in "0123456789abcdef" for c in checksum)

    def test_checksums_change_with_content(self, tmp_path):
        f1 = tmp_path / "file1.tif"
        f2 = tmp_path / "file2.tif"
        f1.write_bytes(b"content A")
        f2.write_bytes(b"content B")
        m1 = _build_manifest("r", "h"*64, {}, {"s": {"f": str(f1)}}, tmp_path)
        m2 = _build_manifest("r", "h"*64, {}, {"s": {"f": str(f2)}}, tmp_path)
        checksums1 = list(m1["checksums"].values())
        checksums2 = list(m2["checksums"].values())
        assert checksums1 != checksums2

    def test_nested_artifacts_flattened_in_outputs(self, tmp_path):
        self._make_dummy_files(tmp_path, ["slope.tif"])
        m = _build_manifest(
            "r1", "h"*64, {},
            {"srtm": {"terrain_attrs": {"slope": str(tmp_path / "slope.tif")}}},
            tmp_path,
        )
        assert "terrain_attrs.slope" in m["outputs"]["srtm"]

    def test_created_at_present(self, tmp_path):
        self._make_dummy_files(tmp_path, ["a.tif"])
        m = _build_manifest("r1", "h"*64, {}, {"s": {"e": str(tmp_path / "a.tif")}}, tmp_path)
        assert "created_at" in m
        assert "T" in m["created_at"]  # ISO 8601 format

    def test_spec_dict_stored(self, tmp_path):
        self._make_dummy_files(tmp_path, ["a.tif"])
        spec = {"inputs": {"base_dir": "/x"}}
        m = _build_manifest("r1", "h"*64, spec, {"s": {"e": str(tmp_path / "a.tif")}}, tmp_path)
        assert m["spec"] == spec
