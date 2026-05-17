FROM python:3.11-slim
WORKDIR /app
COPY ingestion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt netCDF4
COPY ingestion/bathymetry/bathymetry_loader.py .
CMD ["python", "bathymetry_loader.py"]