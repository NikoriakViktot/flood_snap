"""Flood Lab service. Thread pools of BLAS / GDAL are capped before numpy is imported anywhere: on a many-core host the
pools alone reach ~2.9 GB of virtual memory, above the pipeline child's RLIMIT_AS (found 2026-10-13)."""
import os

for _k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "GDAL_NUM_THREADS"):
    os.environ.setdefault(_k, "1")
