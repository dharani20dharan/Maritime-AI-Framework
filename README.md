# Maritime AI Framework (MAF) & OSINT Intelligence Platform

## Overview
The **Maritime AI Framework (MAF)** is an open-source, enterprise-grade real-time maritime domain awareness, OSINT intelligence, and sanction evasion detection platform. It ingests, validates, analyzes, and visualizes AIS vessel telemetry alongside international sanction watchlists, geopolitical boundaries (EEZ), and bathymetric data.

GitHub Repository: [https://github.com/dharani20dharan/Maritime-AI-Framework](https://github.com/dharani20dharan/Maritime-AI-Framework)

---

## 🚀 Quick Start Guide for New Developers

Anyone cloning this repository can run and test the complete system locally by following these simple steps:

### Step 1: Clone Repository & Create Environment Configuration
```bash
git clone https://github.com/dharani20dharan/Maritime-AI-Framework.git
cd Maritime-AI-Framework
cp .env.example .env
```

### Step 2: Start All Infrastructure Microservices (Docker Compose)
```bash
docker-compose up -d
```
*(Starts all 13 microservices: Kafka, ZooKeeper, Cassandra, Neo4j, NiFi, Kafka-UI, API, Ingestors, ETL, and Agent Zero container).*

### Step 3: Install Python Dependencies
```bash
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### Step 4: Launch Web Applications & API Servers

1. **Launch OSINT Map Dashboard Server**:
   ```bash
   python dashboard_server.py
   ```
   👉 Access OSINT Dashboard at: **[http://localhost:5000/](http://localhost:5000/)**

2. **Launch FastMCP Tool Server for Agent Zero**:
   ```bash
   python agents/mcp_http_server.py
   ```
   👉 Access Agent Zero AI Interface at: **[http://localhost:5080/](http://localhost:5080/)**

---

## 🌐 Active Port Map & Services

| Service | Port | Endpoint / URL | Description |
| :--- | :---: | :--- | :--- |
| **OSINT Map Dashboard** | `5000` | [http://localhost:5000/](http://localhost:5000/) | Geospatial interactive Leaflet map dashboard |
| **Agent Zero AI Interface** | `5080` | [http://localhost:5080/](http://localhost:5080/) | Autonomous AI Agent chat interface |
| **FastMCP Tool Server** | `7331` | [http://localhost:7331/sse](http://localhost:7331/sse) | FastMCP SSE tool server for LLM integration |
| **FastAPI REST Engine** | `8000` | [http://localhost:8000/docs](http://localhost:8000/docs) | OpenAPI interactive endpoints |
| **Neo4j Browser** | `7474` | [http://localhost:7474/](http://localhost:7474/) | Cypher graph visualization browser |
| **Kafka UI** | `8080` | [http://localhost:8080/](http://localhost:8080/) | Kafka topic monitor (`ais.raw`, `ais.validated`) |

---

## 🛠️ Architecture & Core Subsystems

* **Kafka Streaming**: Real-time topic validation and event routing (`ais.raw`, `ais.validated`, `ais.anomalies`).
* **Cassandra Telemetry Store**: Partitioned time-series trajectory database (`maf_ais.ais_positions`) indexed by `(mmsi, date_bucket)`.
* **Neo4j Knowledge Graph**: Indexed graph network containing 69,371 Vessels, 286,000+ Anomaly Events, 3,528 Sanctioned Entities, and 572 EEZ Zones.
* **Sanction Risk Scorer**: Calculates 0-100 evasion risk ratings based on OFAC SDN / EU / UN watchlist matches, high-risk flag jurisdictions, and sister-vessel links.
* **Agent Zero AI Agent**: Integrated via FastMCP for natural language Cypher queries, threat scoring, and executive reporting.

