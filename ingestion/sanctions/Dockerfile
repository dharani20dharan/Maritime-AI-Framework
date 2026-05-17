FROM python:3.11-slim
WORKDIR /app
COPY ingestion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY ingestion/sanctions/sanctions_ingestor.py .
CMD ["python", "sanctions_ingestor.py"]