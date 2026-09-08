"""Tests for DemAnalysisSpec — value object, serialization, command integration."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

from control_plane.domain.specs import (
    DemAnalysisSpec, InputsSpec, AoiSpec, PlanSpec,
    CrsConversionSpec, TerrainSpec, GeomorphonsSpec, HydroSpec, OutputsSpec,
    dem_analysis_spec_from_dict,
)
from control_plane.domain.commands import StartAnalysis

_FULL_PAYLOAD = {
    "inputs": {
        "base_dir": "/data/dems",
        "geoid_tif": "/data/egg2015.tif",
        "dem_registry": {"srtm": {"vertical_datum": "EGM96"}, "alos": {"vertical_datum": "EGM96"}},
        "aoi": {"pour_point": [25.05, 47.95]},
    },
    "plan": {
        "crs_conversion": {"enabled": True},
        "terrain": {"enabled": True, "methods": ["horn"]},
        "geomorphons": {"enabled": True, "search": 50, "threshold": 1.0},
        "hydro": {"enabled": True, "stream_threshold": 300},
    },
    "outputs": {"artifact_root": "/data/artifacts"},
}


def _make_full_spec() -> DemAnalysisSpec:
    return DemAnalysisSpec(
        inputs=InputsSpec(
            base_dir="/data/dems",
            geoid_tif="/data/egg2015.tif",
            dem_registry={"srtm": {"vertical_datum": "EGM96"}},
            aoi=AoiSpec(pour_point=(25.05, 47.95)),
        ),
        plan=PlanSpec(
            crs_conversion=CrsConversionSpec(enabled=True),
            terrain=TerrainSpec(enabled=True, methods=("horn",)),
            geomorphons=GeomorphonsSpec(enabled=True, search=50, threshold=1.0),
            hydro=HydroSpec(enabled=True, stream_threshold=300),
        ),
        outputs=OutputsSpec(artifact_root="/data/artifacts"),
    )


class TestDemAnalysisSpecCreation:
    def test_defaults(self):
        spec = DemAnalysisSpec(
            inputs=InputsSpec(base_dir="/x", geoid_tif="/y", dem_registry={}),
            plan=PlanSpec(),
            outputs=OutputsSpec(),
        )
        assert spec.plan.crs_conversion.enabled is True
        assert spec.plan.terrain.enabled is False
        assert spec.plan.hydro.enabled is False
        assert spec.plan.geomorphons.enabled is False
        assert spec.inputs.aoi.pour_point is None
        assert spec.outputs.artifact_root == "/data/artifacts"

    def test_frozen_immutable(self):
        spec = _make_full_spec()
        with pytest.raises(Exception):  # FrozenInstanceError
            spec.inputs = None  # type: ignore

    def test_aoi_defaults_to_no_pour_point(self):
        spec = DemAnalysisSpec(
            inputs=InputsSpec(base_dir="/x", geoid_tif="/y", dem_registry={}),
            plan=PlanSpec(),
            outputs=OutputsSpec(),
        )
        assert spec.inputs.aoi.pour_point is None


class TestDemAnalysisSpecSerialization:
    def test_to_dict_contains_all_sections(self):
        spec = _make_full_spec()
        d = spec.to_dict()
        assert "inputs" in d
        assert "plan" in d
        assert "outputs" in d

    def test_to_dict_values(self):
        spec = _make_full_spec()
        d = spec.to_dict()
        assert d["inputs"]["base_dir"] == "/data/dems"
        assert d["plan"]["hydro"]["stream_threshold"] == 300
        assert d["plan"]["geomorphons"]["search"] == 50
        assert d["outputs"]["artifact_root"] == "/data/artifacts"

    def test_pour_point_preserved_as_tuple_in_asdict(self):
        spec = _make_full_spec()
        d = asdict(spec)
        pp = d["inputs"]["aoi"]["pour_point"]
        assert pp == (25.05, 47.95)

    def test_asdict_is_json_serializable(self):
        spec = _make_full_spec()
        d = asdict(spec)
        json.dumps(d)  # must not raise

    def test_methods_tuple_survives_asdict(self):
        spec = _make_full_spec()
        d = asdict(spec)
        # tuples become tuples in asdict
        assert d["plan"]["terrain"]["methods"] == ("horn",)


class TestDemAnalysisSpecDeserialization:
    def test_full_payload_roundtrip(self):
        spec = dem_analysis_spec_from_dict(_FULL_PAYLOAD)
        assert spec.inputs.base_dir == "/data/dems"
        assert spec.inputs.aoi.pour_point == (25.05, 47.95)
        assert spec.plan.hydro.enabled is True
        assert spec.plan.hydro.stream_threshold == 300
        assert spec.plan.geomorphons.search == 50
        assert spec.plan.terrain.methods == ("horn",)
        assert spec.outputs.artifact_root == "/data/artifacts"

    def test_minimal_payload(self):
        spec = dem_analysis_spec_from_dict({
            "inputs": {
                "base_dir": "/x",
                "geoid_tif": "/y",
                "dem_registry": {"srtm": {"vertical_datum": "EGM96"}},
            }
        })
        assert spec.plan.crs_conversion.enabled is True
        assert spec.plan.terrain.enabled is False
        assert spec.inputs.aoi.pour_point is None
        assert spec.outputs.artifact_root == "/data/artifacts"

    def test_no_aoi_section(self):
        payload = {k: v for k, v in _FULL_PAYLOAD.items()}
        payload = {**payload, "inputs": {**payload["inputs"]}}
        del payload["inputs"]["aoi"]
        spec = dem_analysis_spec_from_dict(payload)
        assert spec.inputs.aoi.pour_point is None

    def test_null_pour_point(self):
        payload = {**_FULL_PAYLOAD, "inputs": {**_FULL_PAYLOAD["inputs"], "aoi": {"pour_point": None}}}
        spec = dem_analysis_spec_from_dict(payload)
        assert spec.inputs.aoi.pour_point is None

    def test_dem_registry_preserved(self):
        spec = dem_analysis_spec_from_dict(_FULL_PAYLOAD)
        assert "srtm" in spec.inputs.dem_registry
        assert "alos" in spec.inputs.dem_registry


class TestStartAnalysisCommand:
    def test_carries_spec(self):
        spec = _make_full_spec()
        cmd = StartAnalysis(run_id="abc-123", spec=spec)
        assert cmd.spec is spec
        assert cmd.run_id == "abc-123"

    def test_asdict_has_run_id_and_spec(self):
        spec = _make_full_spec()
        cmd = StartAnalysis(run_id="abc-123", spec=spec)
        d = asdict(cmd)
        assert set(d.keys()) == {"run_id", "spec"}
        assert d["run_id"] == "abc-123"

    def test_asdict_is_json_serializable(self):
        cmd = StartAnalysis(run_id="abc-123", spec=_make_full_spec())
        json.dumps(asdict(cmd))  # must not raise

    def test_spec_hash_deterministic_through_asdict(self):
        import hashlib
        cmd = StartAnalysis(run_id="abc-123", spec=_make_full_spec())
        d1 = asdict(cmd)
        d2 = asdict(cmd)
        s1 = json.dumps(d1["spec"], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        s2 = json.dumps(d2["spec"], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        assert hashlib.sha256(s1.encode()).hexdigest() == hashlib.sha256(s2.encode()).hexdigest()
