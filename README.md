# MAF — Maritime AI Framework
## Setup & Data Sources

---

### Quick start

```bash
cp .env.example .env
# add your AISSTREAM_API_KEY to .env
docker compose up -d
```

### 🗺️ GFW GIS Live Operations Dashboard
We have built an interactive, dark-themed operations dashboard leveraging Leaflet.js and a Python Flask proxy server to visualize live real-world vessel telemetry, pulsing Ship-to-Ship rendezvous event rings, and dynamic risk scoring overlays.

#### 🏛️ Interactive Telemetry Actions
1. **AIS Tracks (Cassandra)**: Plots glowing trailing trajectories (`L.polyline`) directly from Cassandra timeseries data, displaying waypoint index, speeds, and timestamps on hover tooltips.
2. **Graph Path (Neo4j)**: Spawns a custom glassmorphic modal displaying vessel ownership corporate structures, registered flags, OFAC sanctions, and LLM-Agent verdicts from Neo4j.

#### 🛠️ Dashboard Setup & Startup
1. **Update Vessel Risk Scores**:
   Calculate and populate risk scores for all 32,800+ vessels in Neo4j in under a second (using our bulk batch transaction optimizer):
   ```bash
   .\venv\Scripts\python tools/update_risk_scores.py
   ```
2. **Launch the Proxy Server**:
   Start the proxy server locally on port 5000:
   ```bash
   .\venv\Scripts\python dashboard_server.py
   ```
3. **Open the Dashboard**:
   Simply open `dashboard.html` in any web browser to view the interactive map and operational telemetry!

---

### Services will be available at:
| Service | URL |
|---------|-----|
| Kafka UI | http://localhost:8080 |
| Neo4j Browser | http://localhost:7474 |
| NiFi | https://localhost:8443 |
| FastAPI | http://localhost:8000/docs |

---

### Data sources (all open / free)

#### AIS — vessel position feed
- **AISStream.io** — free websocket API, global AIS coverage
  - Register: https://aisstream.io → get API key → set `AISSTREAM_API_KEY` in `.env`
  - Without a key: set `REPLAY_MODE=true` — the ingestor generates synthetic data
- **AISHub.net** — alternative; requires sharing your own AIS feed in return
  - https://www.aishub.net/join-us

#### Bathymetry
- **GEBCO 2026** — authoritative global ocean depth grid, 15 arc-second resolution
  - Download: https://download.gebco.net/ → select region → NetCDF format
  - License: public domain
  - Place downloaded file at: `ingestion/bathymetry/data/gebco_2026.nc`
  - Run: `docker compose run bathymetry-loader` (one-shot preprocessing)

#### EEZ boundaries
- **Marine Regions / Flanders** — MRGID EEZ GeoJSON, authoritative
  - Download: https://www.marineregions.org/downloads.php → World EEZ v12 GeoJSON
  - License: CC BY 4.0
  - Place at: `ingestion/eez/data/eez_boundaries.geojson`
  - Run: `docker compose run eez-loader` (one-shot)

#### Sanctions lists
- **OFAC SDN** — official US Treasury XML, no key required
  - Auto-fetched by `sanctions-ingestor` on startup and every hour
  - URL: https://www.treasury.gov/ofac/downloads/sdn.xml
- **OpenSanctions vessels** — aggregates OFAC + UN + EU + UK OFSI + others
  - Free for non-commercial / OSINT use
  - Auto-fetched by `sanctions-ingestor`
  - URL: https://data.opensanctions.org/datasets/latest/vessels/targets.nested.json
- **UN Security Council** — included via OpenSanctions
- **EU FSF** — included via OpenSanctions

---

### Kafka topics

| Topic | Producer | Consumers | Contents |
|-------|----------|-----------|----------|
| `ais.raw` | ais-ingestor | — | All raw AIS messages, unvalidated |
| `ais.validated` | ais-ingestor | signal-analyser, neo4j-etl | Valid Contract A1 envelopes |
| `ais.anomalies` | signal-analyser | (critic layer) | M-AIS-BEACON, M-SPEED-ANOMALY events |


4. **Contract C1** — debate log output schema (your responsibility to define)
   - Required fields: `hypothesis`, `evidence_for`, `evidence_against`,
     `verdict` (CONFIRMED|DISMISSED|ESCALATE), `confidence` (0.0–1.0)

