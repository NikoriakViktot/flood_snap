# Flood Lab geoprocessing service (API + worker) on top of the DEM worker image (GDAL, WhiteboxTools, rasterio, shapely).
FROM projects-dem_worker
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*" pyjwt==2.9.* boto3 redis httpx
WORKDIR /app
COPY lab_service/ /app/lab_service/
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "lab_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
