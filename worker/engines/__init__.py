"""Worker engines — pure computation modules extracted from worker.py."""

from .otsu import auto_detect_threshold
from .compound_mask import build_compound_mask

__all__ = ["auto_detect_threshold", "build_compound_mask"]
