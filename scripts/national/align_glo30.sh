#!/bin/sh
# Re-grid every GLO-30 tile onto the exact 1" grid (FABDEM's): the originals sit at fractional VRT offsets and the tiles
# north of 50°N have 1.5" columns, which makes every read through the national VRT resample (very slow).
set -e
mkdir -p /data/dem/glo30/aligned
ls /data/dem/glo30/tiles/*.tif | xargs -P 12 -I{} sh -c '
  f={}; b=$(basename $f); o=/data/dem/glo30/aligned/$b
  [ -s $o ] && exit 0
  lat=$(echo $b | sed -E "s/.*_([NS])([0-9]{2})_00_([EW])([0-9]{3})_00_DEM.tif/\2/"); lon=$(echo $b | sed -E "s/.*_([NS])([0-9]{2})_00_([EW])([0-9]{3})_00_DEM.tif/\4/")
  lat=$(expr $lat + 0); lon=$(expr $lon + 0)
  gdalwarp -q -overwrite -te $lon $lat $((lon+1)) $((lat+1)) -tr 0.000277777777777778 0.000277777777777778 -r bilinear \
    -srcnodata -9999 -dstnodata -9999 -co TILED=YES -co COMPRESS=DEFLATE -co PREDICTOR=3 $f $o.tmp.tif && mv $o.tmp.tif $o'
gdalbuildvrt -q -overwrite -srcnodata -9999 -vrtnodata -9999 /data/dem/glo30/glo30_4326_aligned.vrt /data/dem/glo30/aligned/*.tif
gdalinfo /data/dem/glo30/glo30_4326_aligned.vrt | grep -E "Size is|Pixel Size"
grep -c "<SourceFilename" /data/dem/glo30/glo30_4326_aligned.vrt
grep -m1 "DstRect" /data/dem/glo30/glo30_4326_aligned.vrt
