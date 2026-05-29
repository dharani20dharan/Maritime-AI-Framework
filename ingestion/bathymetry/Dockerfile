FROM python:3.11-slim
WORKDIR /app
COPY ingestion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt netCDF4 pandas scipy pyarrow
COPY tools/ /app/tools/
COPY ingestion/bathymetry/bathymetry_loader.py .
ENV PYTHONPATH=/app
CMD ["python", "bathymetry_loader.py"]