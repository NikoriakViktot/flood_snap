# Flood Lab geoprocessing service (API + worker) on top of the DEM worker image (GDAL, WhiteboxTools, rasterio, shapely).
# BASE: the local tag on a workstation / the server, or ghcr.io/<owner>/projects-dem_worker:<sha> in CI (deploy-lab.yml)
ARG BASE=projects-dem_worker
FROM ${BASE}
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*" pyjwt==2.9.* boto3 redis httpx "celery>=5.4,<6"
# GR4H runs (lab_service/gr4h): R + airGR (INRAE, GPL-2) called as a short-lived Rscript child of a job — no R service.
# Ubuntu 22.04 ships R 4.1; airGR 1.7.9 compiles its Fortran core at install (r-base-dev brings gfortran).
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends r-base-core r-base-dev r-cran-jsonlite \
 && rm -rf /var/lib/apt/lists/* \
 && Rscript -e 'install.packages("airGR", repos="https://cloud.r-project.org", Ncpus=2); stopifnot(packageVersion("airGR") >= "1.7.9")'
WORKDIR /app
COPY lab_service/ /app/lab_service/
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "lab_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
