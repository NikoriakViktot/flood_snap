# Terracotta XYZ tile server for the basin products (/tc/singleband/{category}/{layer}/{z}/{x}/{y}.png).
# The SQLite catalogue is written by dem_worker (tc_ingest.py) on the shared /data volume.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TC_DRIVER_PATH=/data/tc/terracotta.sqlite \
    TC_DRIVER_PROVIDER=sqlite \
    TC_RESAMPLING_METHOD=linear \
    TC_REPROJECTION_METHOD=linear \
    TC_ALLOWED_ORIGINS_TILES='["*"]' \
    TC_ALLOWED_ORIGINS_METADATA='["*"]'

# rasterio wheels need libexpat at runtime (missing from -slim)
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "terracotta>=0.8,<0.9" "marshmallow>=3.20,<4" "gunicorn>=22,<24"

EXPOSE 5000
# gunicorn instead of the Flask dev server. ~115 MB per worker; 2 workers fit an 8 GB / 2 CPU host.
ENV TC_WORKERS=2
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:5000 --workers ${TC_WORKERS} --timeout 60 terracotta.server.app:app"]
