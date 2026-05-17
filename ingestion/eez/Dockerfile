FROM python:3.11-slim
WORKDIR /app
COPY ingestion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt pyshp shapely
COPY ingestion/eez/eez_loader.py .
CMD ["python", "eez_loader.py"]