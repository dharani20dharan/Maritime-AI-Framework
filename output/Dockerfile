FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn "strawberry-graphql[fastapi]" neo4j beautifulsoup4 requests
COPY api/ /app/api/
COPY tools/ /app/tools/
ENV PYTHONPATH=/app
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]