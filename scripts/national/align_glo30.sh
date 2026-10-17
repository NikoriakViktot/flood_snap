#!/bin/sh
# Re-grid every GLO-30 tile onto the exact 1" grid (FABDEM's): the originals sit at fractional VRT offsets and the tiles
# north of 50°N have 1.5" columns, which makes every read through the national VRT resample (very slow).
# Each tile is warped FROM THE MOSAIC of the original tiles (glo30_4326.vrt), not from its own file: the originals are
# shifted half a pixel, so a tile warped alone has no source for its east column and south row (bilinear) and came out
# with a nodata line along every whole degree — a barrier for D8 that cut basins along straight lines (fixed 2026-10-17).
# Writes into aligned.new/ and swaps it in only when every tile is complete; ALIGN_FORCE=1 rebuilds existing tiles.
set -e
D=/data/dem/glo30
mkdir -p $D/aligned.new
[ -s $D/glo30_4326.vrt ] || gdalbuildvrt -q -overwrite $D/glo30_4326.vrt $D/tiles/*.tif
ls $D/tiles/*.tif | xargs -P ${ALIGN_JOBS:-12} -I{} sh -c '
  f={}; b=$(basename $f); o='"$D"'/aligned.new/$b
  [ -s $o ] && [ -z "$ALIGN_FORCE" ] && exit 0
  lat=$(echo $b | sed -E "s/.*_([NS])([0-9]{2})_00_([EW])([0-9]{3})_00_DEM.tif/\2/"); lon=$(echo $b | sed -E "s/.*_([NS])([0-9]{2})_00_([EW])([0-9]{3})_00_DEM.tif/\4/")
  lat=$(expr $lat + 0); lon=$(expr $lon + 0)
  gdalwarp -q -overwrite -te $lon $lat $((lon+1)) $((lat+1)) -tr 0.000277777777777778 0.000277777777777778 -r bilinear \
    -srcnodata -9999 -dstnodata -9999 -co TILED=YES -co COMPRESS=DEFLATE -co PREDICTOR=3 '"$D"'/glo30_4326.vrt $o.tmp.tif && mv $o.tmp.tif $o'
[ "$(ls $D/aligned.new/*.tif | wc -l)" -eq "$(ls $D/tiles/*.tif | wc -l)" ] || { echo "aligned.new incomplete"; exit 1; }
rm -rf $D/aligned.old; [ -d $D/aligned ] && mv $D/aligned $D/aligned.old; mv $D/aligned.new $D/aligned
gdalbuildvrt -q -overwrite -srcnodata -9999 -vrtnodata -9999 $D/glo30_4326_aligned.vrt $D/aligned/*.tif
gdalinfo $D/glo30_4326_aligned.vrt | grep -E "Size is|Pixel Size"
grep -c "<SourceFilename" $D/glo30_4326_aligned.vrt
