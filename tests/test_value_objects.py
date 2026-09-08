"""Tests for domain value objects."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

try:
    from control_plane.domain.value_objects import (
        RunId, SceneId, BasinHash, ArtifactURI,
        ContractHash, StepName, ArtifactType,
    )
    HAS_CP = True
except ImportError:
    HAS_CP = False

pytestmark = pytest.mark.skipif(not HAS_CP, reason="control_plane not available")


class TestRunId:
    def test_valid(self):
        uid = uuid4()
        r = RunId(uid)
        assert str(r) == str(uid)

    def test_frozen(self):
        r = RunId(uuid4())
        with pytest.raises(AttributeError):
            r.value = uuid4()


class TestSceneId:
    def test_valid(self):
        s = SceneId("S1B_IW_GRDH_test.SAFE")
        assert s.value.endswith(".SAFE")

    def test_invalid(self):
        with pytest.raises(ValueError, match=".SAFE"):
            SceneId("not_a_scene.zip")


class TestBasinHash:
    def test_valid(self):
        bh = BasinHash("abcdef012345")
        assert len(bh.value) == 12

    def test_invalid_length(self):
        with pytest.raises(ValueError):
            BasinHash("abc")

    def test_invalid_chars(self):
        with pytest.raises(ValueError):
            BasinHash("ABCDEF012345")  # uppercase not allowed


class TestArtifactURI:
    def test_valid(self):
        a = ArtifactURI("/data/mask.tif")
        assert a.value.startswith("/")

    def test_invalid(self):
        with pytest.raises(ValueError, match="absolute"):
            ArtifactURI("relative/path.tif")


class TestContractHash:
    def test_valid(self):
        h = "a" * 64
        ch = ContractHash(h)
        assert ch.value == h

    def test_invalid(self):
        with pytest.raises(ValueError):
            ContractHash("short")


class TestStepName:
    def test_valid_steps(self):
        for name in ("SAR", "DEM", "MASK"):
            assert StepName(name).value == name

    def test_invalid(self):
        with pytest.raises(ValueError):
            StepName("UNKNOWN")


class TestArtifactType:
    def test_valid_types(self):
        for name in ("sigma0", "hand", "slope", "mask", "tc", "elevation"):
            assert ArtifactType(name).value == name

    def test_invalid(self):
        with pytest.raises(ValueError):
            ArtifactType("invalid")
