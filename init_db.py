"""
Maritime Intelligence Knowledge Graph - Database Initialization
This script connects to the Neo4j instance and creates all necessary
unique constraints and indexes required for the schema to ensure
data integrity and query performance.
"""
from neo4j import GraphDatabase

URI = "neo4j://localhost:7687"
AUTH = ("neo4j", "maritime123")

def init_constraints_and_indexes(driver):
    with driver.session() as session:
        # Constraints
        # Constraints ensure that properties like IMO and UNLOCODE remain unique across nodes.
        # This automatically creates a supporting index for fast lookups.
        print("Creating constraints...")
        session.run("CREATE CONSTRAINT vessel_imo IF NOT EXISTS FOR (v:Vessel) REQUIRE v.imo IS UNIQUE")
        session.run("CREATE CONSTRAINT company_imo IF NOT EXISTS FOR (c:Company) REQUIRE c.company_imo IS UNIQUE")
        session.run("CREATE CONSTRAINT port_unlocode IF NOT EXISTS FOR (p:Port) REQUIRE p.unlocode IS UNIQUE")
        session.run("CREATE CONSTRAINT voyage_id IF NOT EXISTS FOR (v:Voyage) REQUIRE v.voyage_id IS UNIQUE")
        session.run("CREATE CONSTRAINT sanction_id IF NOT EXISTS FOR (s:Sanction) REQUIRE s.sanction_id IS UNIQUE")
        session.run("CREATE CONSTRAINT flag_country_code IF NOT EXISTS FOR (f:Flag) REQUIRE f.country_code IS UNIQUE")
        session.run("CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.event_id IS UNIQUE")
        
        # Indexes
        # Indexes speed up queries that filter by non-unique properties like names and event types.
        print("Creating indexes...")
        session.run("CREATE INDEX vessel_mmsi IF NOT EXISTS FOR (v:Vessel) ON (v.mmsi)")
        session.run("CREATE INDEX vessel_name IF NOT EXISTS FOR (v:Vessel) ON (v.name)")
        session.run("CREATE INDEX company_name IF NOT EXISTS FOR (c:Company) ON (c.name)")
        session.run("CREATE INDEX port_name IF NOT EXISTS FOR (p:Port) ON (p.name)")
        session.run("CREATE INDEX event_type IF NOT EXISTS FOR (e:Event) ON (e.event_type)")
        session.run("CREATE INDEX sanction_program IF NOT EXISTS FOR (s:Sanction) ON (s.program)")

        # Geospatial Index
        session.run("CREATE POINT INDEX event_location IF NOT EXISTS FOR (e:Event) ON (e.location)")
        
        print("Database initialization complete.")

if __name__ == "__main__":
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        # Verify connection
        driver.verify_connectivity()
        init_constraints_and_indexes(driver)
