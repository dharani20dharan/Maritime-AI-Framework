from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter
from neo4j import GraphDatabase
import os
import sys

# Add root directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.schema import schema

# ---------------------------------------------------------
# Database Configuration
# ---------------------------------------------------------
NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "maritime123")

driver = None

# ---------------------------------------------------------
# FastAPI Application & Lifespan
# ---------------------------------------------------------
app = FastAPI(
    title="Maritime AI Framework API",
    description="GraphQL and REST API exposing the Neo4j Knowledge Graph and Anomaly Scorer.",
    version="1.0.0"
)

@app.on_event("startup")
async def startup_event():
    global driver
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    print("Connected to Neo4j database.")

@app.on_event("shutdown")
async def shutdown_event():
    global driver
    if driver:
        driver.close()
        print("Closed Neo4j database connection.")

# ---------------------------------------------------------
# GraphQL Context Injection
# ---------------------------------------------------------
def get_context():
    """Injects the Neo4j driver into the Strawberry GraphQL context."""
    return {"db": driver}

# ---------------------------------------------------------
# Routers
# ---------------------------------------------------------
# 1. REST Endpoint (Health check)
@app.get("/api/v1/health")
async def health_check():
    return {"status": "healthy", "database": "connected" if driver else "disconnected"}

# 2. GraphQL Endpoint
graphql_app = GraphQLRouter(schema, context_getter=get_context)
app.include_router(graphql_app, prefix="/graphql")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
