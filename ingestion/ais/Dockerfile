FROM python:3.11-slim
WORKDIR /app
COPY ingestion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY ingestion/ais/ingestor.py .
CMD ["python", "ingestor.py"]