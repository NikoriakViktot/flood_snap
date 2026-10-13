# Third-party notices

flood_snap is MIT-licensed (LICENSE). Method references and code adapted from other projects:

## NOAA OWP inundation-mapping (FIM v4) — Apache License 2.0

https://github.com/NOAA-OWP/inundation-mapping (fork: https://github.com/NikoriakViktot/inundation-mapping, branch `dev`).
The HAND model pipeline (`lab_service/hand/`) follows FIM's method (relative elevation per reach catchment, stage →
extent / depth per reach, synthetic rating curves, agreement-raster metrics) with WhiteboxTools instead of TauDEM.
**No FIM source code is copied in this repository yet.** When a file is adapted (planned: `src/agreedem.py`,
`src/adjust_thalweg_lateral.py`, `src/make_rem.py`, `tools/inundation.py`, `src/add_crosswalk.py`,
`tools/tools_shared_functions.py`), it keeps the Apache License 2.0 header, states the changes, and is listed here.
