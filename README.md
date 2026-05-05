# Maritime Intelligence Knowledge Graph

This project provides a robust, production-ready Neo4j database schema and initialization setup for modeling maritime intelligence data.

## Overview

The database models the complex relationships within the maritime domain, capturing:
- **Vessels**: Identification (IMO, MMSI) and technical details.
- **Companies**: Corporate ownership, commercial management, and registered addresses.
- **Ports**: Global port infrastructure with geospatial coordinates.
- **Voyages**: Vessel journeys between ports.
- **Events**: Tracked activities or anomalies, such as Port Calls, AIS Gaps, and Ship-to-Ship (STS) transfers.
- **Sanctions**: Regulatory designations (e.g., OFAC, EU) applied to entities or specific vessels.
- **Flags**: Jurisdictions of ship registration.

The schema employs Neo4j's native temporal and spatial data types to enable high-performance queries for dark fleet detection, sanction evasion (e.g., multi-hop corporate structures), and historical event tracking.

## Project Structure

This repository is structured around the Maritime AI Framework (MAF) to separate data pipelines (Engineer A) from reasoning and agents (Engineer B).

- `infra/`: Infrastructure configs (e.g., `docker-compose.yml` for Neo4j).
- `ingestion/`: Kafka and NiFi pipelines for AIS and satellite data (Engineer A).
- `database/`: Neo4j schema definitions, ETL jobs, and data loaders (`init_db.py`, `load_sample_data.py`) (Engineer A).
- `agents/`: LangGraph orchestration, Critic, and Verifier agents (Engineer B).
- `api/`: FastAPI and GraphQL output layer (Engineer B).
- `tools/`: Registry scrapers and MMSI cross-reference tools (Engineer B).
- `dashboard/`: Kepler.gl visualization and UI (Engineer B).

## Getting Started

### Prerequisites
- [Docker](https://www.docker.com/) and Docker Compose
- Python 3.8+

### 1. Start the Database
Run the following command to start the Neo4j container in the background:
```bash
cd infra
docker compose up -d
cd ..
```
*Wait a few seconds for Neo4j to fully initialize before proceeding to the next steps.*

### 2. Set Up the Environment
Create a virtual environment and install the required dependencies:
```bash
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On macOS/Linux:
# source venv/bin/activate

pip install -r requirements.txt
```

### 3. Initialize Schema & Load Data
First, run the initialization script to create the constraints and indexes:
```bash
python database/init_db.py
```

Then, load the dummy dataset into the database:
```bash
python database/load_sample_data.py
```

### 4. Explore the Graph
1. Open your web browser and navigate to the Neo4j Browser: **http://localhost:7474/**
2. Log in using the default credentials configured in `docker-compose.yml`:
   - **Username**: `neo4j`
   - **Password**: `maritime123`
3. Try running the following Cypher query to see everything:
   ```cypher
   MATCH (n) RETURN n
   ```

## Example Queries

### Find vessels connected to Sanctioned Entities
```cypher
MATCH (v:Vessel)-[:MANAGED_BY]->(mgr:Company)-[:SUBSIDIARY_OF*1..2]->(parent:Company)-[:SANCTIONED_BY]->(s:Sanction)
RETURN v.name, parent.name, s.program
```

### Detect "Dark Activity" (AIS Gap followed by a Port Visit)
```cypher
MATCH (p:Port {unlocode: 'IRBND'})
MATCH (v:Vessel)-[:INVOLVED_IN]->(e:Event {event_type: 'AIS_GAP'})
MATCH (v)-[:UNDERTOOK]->(:Voyage)-[:ARRIVED_AT]->(p)
RETURN v.name, e.description, p.name
```
