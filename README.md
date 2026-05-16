# Maritime AI Framework (MAF)

A scalable, multi-agent AI system designed to automatically detect and report suspicious maritime activity, flag hopping, and sanction violations using a Neo4j Knowledge Graph and LangGraph orchestration.

## Architecture

The system follows a strict 3-tier agentic workflow:

1. **Agent 1 (Data Retriever):** Fetches real-time vessel context via parallel scraping of maritime registries (MarineTraffic, Equasis).
2. **Agent 2 (Rule Evaluator):** Executes deterministic, graph-native anomaly queries (PROWL framework) against the Neo4j database to calculate a 0-100 Risk Score.
3. **Agent 3 (Output Generator):** Conditionally triggered for high-risk vessels (`score >= 50`). Uses Groq's Llama-3 model to synthesize a structured JSON Suspicious Activity Report (SAR) explaining the hypothesis and evidence.

## Getting Started

### Prerequisites
- Python 3.10+
- Docker & Docker Compose (for Neo4j infrastructure)
- A free API key from [Groq](https://console.groq.com/keys)

### Setup

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Create a `.env` file in the root directory and add your LLM API Key:
   ```env
   GROQ_API_KEY="your_groq_api_key_here"
   ```

3. **Database Infrastructure:**
   Ensure Docker is running, then spin up the Neo4j container:
   ```bash
   cd infra
   docker-compose up -d
   ```

### Running the System

**1. Run the Multi-Agent Pipeline (CLI Demo)**
Execute the main graph orchestrator to run the parallel scrapers and generate a report:
```bash
python agents/graph.py
```

**2. Start the API Server**
Start the FastAPI and GraphQL server to expose endpoints to the frontend dashboard:
```bash
python api/main.py
```
Navigate to `http://localhost:8000/graphql` to interact with the API interface.

## Current Progress (Week 1 & 2)
- ✅ Neo4j Knowledge Graph schema and constraints defined.
- ✅ Cypher anomaly rules (Flag Hopping, Spoofing, Sanction Proximity) implemented.
- ✅ Parallel Registry Cross-Referencing built with `ThreadPoolExecutor`.
- ✅ LangGraph Conditional Routing implemented to save token costs.
- ✅ Groq LLM integrated with strict Pydantic JSON Output formatting.

---
*Built as part of the MAF 4-Week Sprint.*
