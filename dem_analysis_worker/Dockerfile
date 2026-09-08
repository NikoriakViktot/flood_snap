FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# System deps: Python, GDAL, PROJ, spatialite
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    libgdal-dev gdal-bin \
    libproj-dev proj-bin \
    libgeos-dev \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY xdem_pipeline.py dem_analysis_worker.py /app/

CMD ["python3", "/app/dem_analysis_worker.py"]
