"""
Contract registry: maps type strings to Pydantic payload classes.
"""

from typing import Type
from pydantic import BaseModel

from .flood_pipeline_v1 import FloodPipelinePayloadV1
from .dem_hand_v1 import DemHandPayloadV1
from .flood_mask_v1 import FloodMaskPayloadV1
from .dem_analysis_v1 import DemAnalysisPayloadV1
from .basin_delineation_v1 import BasinDelineationPayloadV1

CONTRACT_REGISTRY: dict[str, Type[BaseModel]] = {
    "flood.pipeline.v1": FloodPipelinePayloadV1,
    "dem.hand.v1": DemHandPayloadV1,
    "flood.mask.v1": FloodMaskPayloadV1,
    "dem.analysis.v1": DemAnalysisPayloadV1,
    "basin.delineation.v1": BasinDelineationPayloadV1,
}
