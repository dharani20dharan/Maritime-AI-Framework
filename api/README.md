# API Output Layer (FastAPI + GraphQL)

This directory contains the production-ready API layer for the Maritime AI Framework. It bridges the gap between the Neo4j Knowledge Graph, our autonomous agents, and external dashboards (e.g., Kepler.gl).

## Architecture

We use **FastAPI** for high-performance async routing and **Strawberry** to provide a strictly-typed GraphQL endpoint. 
*   `api/main.py`: The FastAPI application entry point. Handles Neo4j connection pooling and routes.
*   `api/schema.py`: The Strawberry GraphQL type definitions and resolvers.

## Getting Started

1. Ensure the Neo4j database is running (`docker compose up -d` in the `infra/` dir).
2. Install dependencies: `pip install -r requirements.txt`.
3. Start the server:
```bash
uvicorn api.main:app --reload
```

## Interactive Playground (GraphiQL)

Once the server is running, navigate to [http://localhost:8000/graphql](http://localhost:8000/graphql) in your browser. This provides an interactive IDE to explore the schema and test queries.

## Sample Queries

### 1. Retrieve a Vessel and its Events (Query)
Extract a vessel's details and all associated dark/loitering events from the Neo4j graph.

**GraphQL Query:**
```graphql
query GetVesselData {
  getVessel(imo: "9988776") {
    imo
    name
    vesselType
    events {
      eventType
      location
      startTime
      endTime
    }
  }
}
```

**Expected JSON Response:**
```json
{
  "data": {
    "getVessel": {
      "imo": "9988776",
      "name": "SEA SHADOW",
      "vesselType": "Tanker",
      "events": [
        {
          "eventType": "AIS_GAP",
          "location": "SRID=4326;POINT(55.5 24.5)",
          "startTime": "2023-10-01T08:00:00.000000000+00:00",
          "endTime": "2023-10-01T18:00:00.000000000+00:00"
        }
      ]
    }
  }
}
```

### 2. Trigger Anomaly Detection (Mutation)
Run the Python `SanctionScorer` tool against a specific vessel to generate an immediate Evasion Risk Score.

**GraphQL Mutation:**
```graphql
mutation TriggerRiskAssessment {
  evaluateVesselRisk(imo: "9988776") {
    imo
    riskScore
    flags
  }
}
```

**Expected JSON Response:**
```json
{
  "data": {
    "evaluateVesselRisk": {
      "imo": "9988776",
      "riskScore": 100,
      "flags": [
        "Vessel is directly sanctioned under: IRAN"
      ]
    }
  }
}
```
