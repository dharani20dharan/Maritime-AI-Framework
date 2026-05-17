FROM python:3.11-slim
WORKDIR /app
COPY ingestion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt cassandra-driver
COPY ingestion/cassandra/ais_history_writer.py .
CMD ["python", "ais_history_writer.py"]
