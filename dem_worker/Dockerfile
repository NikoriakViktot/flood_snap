FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
# System deps: Python 3.10, GDAL CLI (gdalwarp → COG), curl for the WBT download at build time
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    libgdal-dev gdal-bin unzip \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# WhiteboxTools binary is fetched at BUILD time (spec §2.2: no downloads at runtime)
RUN python3 -c "import whitebox; whitebox.download_wbt(verbose=False); from whitebox import WhiteboxTools; print(WhiteboxTools().version().splitlines()[0])"

# App code
WORKDIR /app
COPY wbt_pipeline.py basin_utils.py terrain.py basin_delineation.py tc_ingest.py s3_upload.py dem_worker.py /app/

CMD ["python3", "/app/dem_worker.py"]
