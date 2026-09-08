"""Tests for dem.analysis.v1 contract — validation, registry, end-to-end."""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

from control_plane.domain.contracts.dem_analysis_v1 import DemAnalysisPayloadV1
from control_plane.domain.contracts.registry import CONTRACT_REGISTRY
from control_plane.domain.contracts.validate import validate_contract

_MINIMAL_INPUTS = {
    "base_dir": "/data/dems",
    "geoid_tif": "/data/egg2015.tif",
    "dem_registry": {
        "srtm": {"vertical_datum": "EGM96"},
    },
}


class TestDemAnalysisContractMinimal:
    def test_minimal_valid(self):
        p = DemAnalysisPayloadV1(inputs=_MINIMAL_INPUTS)
        assert p.inputs.base_dir == "/data/dems"
        assert p.inputs.geoid_tif == "/data/egg2015.tif"
        assert p.inputs.aoi.pour_point is None
        assert p.plan.crs_conversion.enabled is True
        assert p.plan.terrain.enabled is False
        assert p.plan.geomorphons.enabled is False
        assert p.plan.hydro.enabled is False
        assert p.outputs.artifact_root == "/data/artifacts"

    def test_custom_artifact_root(self):
        p = DemAnalysisPayloadV1(inputs=_MINIMAL_INPUTS, outputs={"artifact_root": "/custom"})
        assert p.outputs.artifact_root == "/custom"

    def test_missing_base_dir_fails(self):
        with pytest.raises(ValidationError):
            DemAnalysisPayloadV1(inputs={"geoid_tif": "/x", "dem_registry": {}})

    def test_missing_geoid_tif_fails(self):
        with pytest.raises(ValidationError):
            DemAnalysisPayloadV1(inputs={"base_dir": "/x", "dem_registry": {}})

    def test_empty_dem_registry_allowed(self):
        p = DemAnalysisPayloadV1(inputs={**_MINIMAL_INPUTS, "dem_registry": {}})
        assert p.inputs.dem_registry == {}


class TestDemAnalysisContractVerticalDatum:
    @pytest.mark.parametrize("vcrs", ["EGM96", "EGM08", "Ellipsoid"])
    def test_all_supported_vcrs(self, vcrs):
        p = DemAnalysisPayloadV1(
            inputs={**_MINIMAL_INPUTS, "dem_registry": {"dem": {"vertical_datum": vcrs}}}
        )
        assert p.inputs.dem_registry["dem"].vertical_datum == vcrs

    def test_unsupported_vcrs_fails(self):
        with pytest.raises(ValidationError):
            DemAnalysisPayloadV1(
                inputs={**_MINIMAL_INPUTS, "dem_registry": {"dem": {"vertical_datum": "WGS84"}}}
            )

    def test_multiple_dems_different_vcrs(self):
        p = DemAnalysisPayloadV1(inputs={
            **_MINIMAL_INPUTS,
            "dem_registry": {
                "srtm": {"vertical_datum": "EGM96"},
                "alos": {"vertical_datum": "EGM96"},
                "cop30": {"vertical_datum": "EGM08"},
                "ellip_dem": {"vertical_datum": "Ellipsoid"},
            },
        })
        assert len(p.inputs.dem_registry) == 4


class TestDemAnalysisContractPourPoint:
    def test_valid_pour_point(self):
        p = DemAnalysisPayloadV1(
            inputs={**_MINIMAL_INPUTS, "aoi": {"pour_point": [25.05, 47.95]}}
        )
        assert p.inputs.aoi.pour_point == [25.05, 47.95]

    def test_null_pour_point_allowed(self):
        p = DemAnalysisPayloadV1(
            inputs={**_MINIMAL_INPUTS, "aoi": {"pour_point": None}}
        )
        assert p.inputs.aoi.pour_point is None

    def test_pour_point_wrong_length_fails(self):
        with pytest.raises(ValidationError, match="pour_point"):
            DemAnalysisPayloadV1(
                inputs={**_MINIMAL_INPUTS, "aoi": {"pour_point": [25.05]}}
            )

    def test_pour_point_out_of_range_lon_fails(self):
        with pytest.raises(ValidationError):
            DemAnalysisPayloadV1(
                inputs={**_MINIMAL_INPUTS, "aoi": {"pour_point": [200.0, 47.95]}}
            )

    def test_pour_point_out_of_range_lat_fails(self):
        with pytest.raises(ValidationError):
            DemAnalysisPayloadV1(
                inputs={**_MINIMAL_INPUTS, "aoi": {"pour_point": [25.05, 95.0]}}
            )

    def test_pour_point_boundary_values_allowed(self):
        p = DemAnalysisPayloadV1(
            inputs={**_MINIMAL_INPUTS, "aoi": {"pour_point": [-180.0, -90.0]}}
        )
        assert p.inputs.aoi.pour_point is not None


class TestDemAnalysisContractPlan:
    def test_all_steps_enabled(self):
        p = DemAnalysisPayloadV1(
            inputs=_MINIMAL_INPUTS,
            plan={
                "crs_conversion": {"enabled": True},
                "terrain": {"enabled": True, "methods": ["horn"]},
                "geomorphons": {"enabled": True, "search": 100, "threshold": 2.0},
                "hydro": {"enabled": True, "stream_threshold": 300},
            },
        )
        assert p.plan.terrain.enabled
        assert p.plan.terrain.methods == ["horn"]
        assert p.plan.geomorphons.search == 100
        assert p.plan.geomorphons.threshold == 2.0
        assert p.plan.hydro.stream_threshold == 300

    def test_stream_threshold_must_be_positive(self):
        with pytest.raises(ValidationError, match="stream_threshold"):
            DemAnalysisPayloadV1(
                inputs=_MINIMAL_INPUTS,
                plan={"hydro": {"enabled": True, "stream_threshold": 0}},
            )

    def test_negative_stream_threshold_fails(self):
        with pytest.raises(ValidationError):
            DemAnalysisPayloadV1(
                inputs=_MINIMAL_INPUTS,
                plan={"hydro": {"enabled": True, "stream_threshold": -1}},
            )

    def test_terrain_enabled_requires_crs_conversion(self):
        with pytest.raises(ValidationError, match="crs_conversion"):
            DemAnalysisPayloadV1(
                inputs=_MINIMAL_INPUTS,
                plan={
                    "crs_conversion": {"enabled": False},
                    "terrain": {"enabled": True},
                },
            )

    def test_hydro_enabled_requires_crs_conversion(self):
        with pytest.raises(ValidationError, match="crs_conversion"):
            DemAnalysisPayloadV1(
                inputs=_MINIMAL_INPUTS,
                plan={
                    "crs_conversion": {"enabled": False},
                    "hydro": {"enabled": True},
                },
            )

    def test_crs_conversion_alone_is_valid(self):
        p = DemAnalysisPayloadV1(
            inputs=_MINIMAL_INPUTS,
            plan={"crs_conversion": {"enabled": True}},
        )
        assert p.plan.crs_conversion.enabled


class TestDemAnalysisRegistry:
    def test_registered(self):
        assert "dem.analysis.v1" in CONTRACT_REGISTRY

    def test_maps_to_correct_class(self):
        assert CONTRACT_REGISTRY["dem.analysis.v1"] is DemAnalysisPayloadV1

    def test_validate_contract_end_to_end(self):
        c_type, c_ver, model = validate_contract({
            "type": "dem.analysis.v1",
            "spec_version": "v1",
            "payload": {"inputs": _MINIMAL_INPUTS},
        })
        assert c_type == "dem.analysis.v1"
        assert c_ver == "v1"
        assert isinstance(model, DemAnalysisPayloadV1)

    def test_validate_contract_full_payload(self):
        _, _, model = validate_contract({
            "type": "dem.analysis.v1",
            "spec_version": "v1",
            "payload": {
                "inputs": {
                    **_MINIMAL_INPUTS,
                    "aoi": {"pour_point": [25.05, 47.95]},
                },
                "plan": {
                    "crs_conversion": {"enabled": True},
                    "terrain": {"enabled": True},
                    "hydro": {"enabled": True, "stream_threshold": 200},
                },
                "outputs": {"artifact_root": "/data/artifacts"},
            },
        })
        assert model.inputs.aoi.pour_point == [25.05, 47.95]
        assert model.plan.hydro.stream_threshold == 200

    def test_validate_contract_invalid_vcrs_raises(self):
        with pytest.raises(ValueError, match="Invalid Payload"):
            validate_contract({
                "type": "dem.analysis.v1",
                "spec_version": "v1",
                "payload": {
                    "inputs": {
                        **_MINIMAL_INPUTS,
                        "dem_registry": {"dem": {"vertical_datum": "INVALID"}},
                    }
                },
            })
