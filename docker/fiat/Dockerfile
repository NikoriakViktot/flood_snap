# Delft-FIAT (Deltares) for the Recovery Lab damage runs: GDAL's own image, so FIAT's GDAL bindings need no build.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.3
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --break-system-packages "delft_fiat==0.2.1" pandas
WORKDIR /work
ENTRYPOINT ["fiat"]
