"""Tests for contract validation and registry."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

from pydantic import ValidationError

from control_plane.domain.contracts.base import ContractEnvelopeV1
from control_plane.domain.contracts.registry import CONTRACT_REGISTRY
from control_plane.domain.contracts.validate import validate_contract
from control_plane.domain.contracts.flood_pipeline_v1 import FloodPipelinePayloadV1
from control_plane.domain.contracts.dem_hand_v1 import DemHandPayloadV1
from control_plane.domain.contracts.flood_mask_v1 import FloodMaskPayloadV1


class TestContractEnvelope:
    """Test envelope validation."""

    def test_valid_envelope(self):
        env = ContractEnvelopeV1(
            type="flood.pipeline.v1",
            spec_version="v1",
            payload={"scene": "test.SAFE"},
        )
        assert env.type == "flood.pipeline.v1"
        assert env.spec_version == "v1"

    def test_envelope_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            ContractEnvelopeV1(
                type="flood.pipeline.v1",
                spec_version="v1",
                payload={},
                unknown_field="bad",
            )

    def test_envelope_requires_type_and_payload(self):
        with pytest.raises(ValidationError):
            ContractEnvelopeV1(spec_version="v1", payload={})
        with pytest.raises(ValidationError):
            ContractEnvelopeV1(type="flood.pipeline.v1", spec_version="v1")

    def test_envelope_only_accepts_v1(self):
        with pytest.raises(ValidationError):
            ContractEnvelopeV1(
                type="flood.pipeline.v1",
                spec_version="v2",
                payload={},
            )


class TestContractRegistry:
    """Test contract type registry."""

    def test_all_types_registered(self):
        assert "flood.pipeline.v1" in CONTRACT_REGISTRY
        assert "dem.hand.v1" in CONTRACT_REGISTRY
        assert "flood.mask.v1" in CONTRACT_REGISTRY

    def test_unknown_type_not_registered(self):
        assert "train.qlora.v1" not in CONTRACT_REGISTRY
        assert "nonexistent.v1" not in CONTRACT_REGISTRY

    def test_registry_maps_to_correct_classes(self):
        assert CONTRACT_REGISTRY["flood.pipeline.v1"] is FloodPipelinePayloadV1
        assert CONTRACT_REGISTRY["dem.hand.v1"] is DemHandPayloadV1
        assert CONTRACT_REGISTRY["flood.mask.v1"] is FloodMaskPayloadV1


class TestFloodPipelinePayload:
    """Test flood.pipeline.v1 payload validation."""

    def test_minimal_valid(self):
        p = FloodPipelinePayloadV1(scene="S1B_IW_GRDH.SAFE")
        assert p.scene == "S1B_IW_GRDH.SAFE"
        assert p.basin_geojson == "/work/basin_bc.geojson"
        assert p.tc_epsg == "EPSG:32635"
        assert p.stream_threshold == 1000
        assert p.max_slope_deg == 8.0
        assert p.max_hand_m == 25.0

    def test_full_valid(self):
        p = FloodPipelinePayloadV1(
            scene="S1B.SAFE",
            basin_geojson="/custom/basin.geojson",
            subset_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            tc_epsg="EPSG:32636",
            stream_threshold=500,
            max_slope_deg=10.0,
            max_hand_m=30.0,
        )
        assert p.tc_epsg == "EPSG:32636"
        assert p.stream_threshold == 500

    def test_scene_required(self):
        with pytest.raises(ValidationError):
            FloodPipelinePayloadV1()

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            FloodPipelinePayloadV1(scene="x.SAFE", unknown="bad")


class TestDemHandPayload:
    """Test dem.hand.v1 payload validation."""

    def test_minimal_valid(self):
        p = DemHandPayloadV1(basin_geojson="/work/basin.geojson")
        assert p.basin_geojson == "/work/basin.geojson"
        assert p.tc_tif is None  # GLO-30 primary, tc_tif optional
        assert p.stream_threshold == 1000
        assert p.target_epsg == "EPSG:32635"
        assert p.pixel_size_m == 30.0

    def test_with_tc_fallback(self):
        p = DemHandPayloadV1(
            basin_geojson="/work/basin.geojson",
            tc_tif="/data/tc_output.tif",
        )
        assert p.tc_tif == "/data/tc_output.tif"

    def test_basin_required(self):
        with pytest.raises(ValidationError):
            DemHandPayloadV1()


class TestFloodMaskPayload:
    """Test flood.mask.v1 payload validation."""

    def test_valid(self):
        p = FloodMaskPayloadV1(
            tc_tif="/data/tc.tif",
            hand_tif="/data/hand.tif",
            slope_tif="/data/slope.tif",
            basin_geojson="/work/basin.geojson",
            output_tif="/data/mask.tif",
        )
        assert p.max_slope_deg == 8.0
        assert p.max_hand_m == 25.0

    def test_all_paths_required(self):
        with pytest.raises(ValidationError):
            FloodMaskPayloadV1(tc_tif="/data/tc.tif")


class TestValidateContract:
    """Test end-to-end contract validation."""

    def test_valid_flood_pipeline(self):
        c_type, c_ver, model = validate_contract({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "S1B.SAFE"},
        })
        assert c_type == "flood.pipeline.v1"
        assert c_ver == "v1"
        assert isinstance(model, FloodPipelinePayloadV1)

    def test_valid_dem_hand(self):
        c_type, _, model = validate_contract({
            "type": "dem.hand.v1",
            "spec_version": "v1",
            "payload": {"basin_geojson": "/work/basin.geojson"},
        })
        assert c_type == "dem.hand.v1"
        assert isinstance(model, DemHandPayloadV1)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown contract type"):
            validate_contract({
                "type": "train.qlora.v1",
                "spec_version": "v1",
                "payload": {},
            })

    def test_invalid_envelope_raises(self):
        with pytest.raises(ValueError, match="Invalid Contract Envelope"):
            validate_contract({"bad": "data"})

    def test_invalid_payload_raises(self):
        with pytest.raises(ValueError, match="Invalid Payload"):
            validate_contract({
                "type": "flood.pipeline.v1",
                "spec_version": "v1",
                "payload": {"unknown_field": "bad"},
            })
