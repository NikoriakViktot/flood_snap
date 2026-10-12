#!/bin/sh
# National DEM display layers: EPSG:3857 60 m Int16 COG (metres, nodata -32768) + multidirectional hillshade (Byte COG).
set -e
D=$1; SRC=$2
OUT=/data/dem_display
gdalwarp -q -overwrite -t_srs EPSG:3857 -tr 60 60 -r average -ot Int16 -srcnodata -9999 -dstnodata -32768 \
  -multi -wo NUM_THREADS=8 -wm 4096 -co TILED=YES -co COMPRESS=DEFLATE -co PREDICTOR=2 -co BIGTIFF=YES \
  $SRC $OUT/${D}_3857_tmp.tif
gdal_translate -q -of COG -co COMPRESS=DEFLATE -co PREDICTOR=2 -co BLOCKSIZE=512 -co OVERVIEW_RESAMPLING=AVERAGE -co BIGTIFF=YES -co NUM_THREADS=8 \
  $OUT/${D}_3857_tmp.tif $OUT/${D}_ua_60m.tif
# 3857 metres are ~1/cos(49°) = 1.52 × ground metres here: compensate the vertical scale
gdaldem hillshade -q -multidirectional -z 1.52 -compute_edges -of GTiff -co TILED=YES -co COMPRESS=DEFLATE -co BIGTIFF=YES \
  $OUT/${D}_3857_tmp.tif $OUT/${D}_hs_tmp.tif
gdal_translate -q -of COG -co COMPRESS=DEFLATE -co BLOCKSIZE=512 -co OVERVIEW_RESAMPLING=AVERAGE -a_nodata 0 -co NUM_THREADS=8 \
  $OUT/${D}_hs_tmp.tif $OUT/${D}_hillshade_ua_60m.tif
rm -f $OUT/${D}_3857_tmp.tif $OUT/${D}_hs_tmp.tif
ls -la $OUT/${D}_*
