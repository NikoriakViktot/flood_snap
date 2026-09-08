FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# System deps: Python, GDAL
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    libgdal-dev gdal-bin \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# App code
WORKDIR /app
COPY wbt_pipeline.py basin_utils.py dem_worker.py /app/

CMD ["python3", "/app/dem_worker.py"]
