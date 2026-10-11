"""basin.delineation.v1: validation, deterministic ids, state machine wiring."""

import pytest

pytest.importorskip("pydantic")

from control_plane.domain.contracts.basin_delineation_v1 import BasinDelineationPayloadV1, basin_id_for
from control_plane.domain.contracts.validate import validate_contract
from control_plane.domain.commands import StartBasin
from control_plane.domain.events import EventType
from control_plane.domain.ports.run_repository import RunRecord
from control_plane.domain.policies.run_state_machine import build_command, transition
from control_plane.domain.states import RunState


def _payload(**kw):
    p = {"outlet": {"lon": 25.03, "lat": 48.02}}
    p.update(kw)
    return p


def test_defaults():
    m = BasinDelineationPayloadV1.model_validate(_payload())
    assert m.dem_source == "copernicus_dem"
    assert m.flood_levels_m == list(range(1, 11))
    assert m.target_epsg == "EPSG:32635"


@pytest.mark.parametrize("bad", [
    {"target_epsg": "EPSG:4326"},
    {"flood_levels_m": []},
    {"flood_levels_m": [1, 1]},
    {"dem_bbox": [25, 48, 24, 47]},
    {"dem_source": "srtm"},
    {"outlet": {"lon": 200, "lat": 48}},
])
def test_rejects(bad):
    with pytest.raises(ValueError):
        validate_contract({"type": "basin.delineation.v1", "spec_version": "v1", "payload": _payload(**bad)})


def test_basin_id_deterministic_and_letters_only():
    a = basin_id_for(BasinDelineationPayloadV1.model_validate(_payload()))
    b = basin_id_for(BasinDelineationPayloadV1.model_validate(_payload()))
    c = basin_id_for(BasinDelineationPayloadV1.model_validate(_payload(stream_threshold=1000)))
    assert a == b != c
    assert a.isalpha() and a.islower()


def test_state_machine():
    assert transition(RunState.BASIN_RUNNING, EventType.BASIN_COMPLETED) == RunState.DONE
    assert transition(RunState.BASIN_RUNNING, EventType.FAILED) == RunState.FAILED
    rec = RunRecord(id="00000000-0000-0000-0000-000000000001", state=RunState.BASIN_RUNNING,
                    contract={"type": "basin.delineation.v1", "payload": BasinDelineationPayloadV1.model_validate(_payload()).model_dump(mode="json")})
    cmd = build_command(RunState.BASIN_RUNNING, EventType.CONTRACT_ACCEPTED, rec)
    assert isinstance(cmd, StartBasin)
    assert cmd.outlet_lon == 25.03 and cmd.stream_threshold == 2000
