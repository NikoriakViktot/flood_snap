FROM flood-snap:latest

RUN pip install redis geopandas rasterio

COPY worker.py /worker.py
COPY engines/ /engines/
COPY basin_bc.geojson /work/basin_bc.geojson

CMD ["python3", "/worker.py"]
