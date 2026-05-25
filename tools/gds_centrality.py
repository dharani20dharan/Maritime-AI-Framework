"""
Neo4j Graph Data Science (GDS) Betweenness Centrality Service.
Projects the Vessel-Company ownership graph and computes Betweenness Centrality
to identify key fleet brokers and shell company hubs in the shadow fleet network.
"""
from neo4j import GraphDatabase
import logging
import os

log = logging.getLogger("gds-centrality")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USER = os.getenv("NEO4J_USER", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")

class GDSCentralityJob:
    def __init__(self, uri=URI, auth=(USER, PASSWORD)):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def close(self):
        self.driver.close()

    def run_betweenness_centrality(self) -> dict:
        """
        Executes the in-memory Betweenness Centrality GDS algorithm.
        Creates graph projection, computes scores, and writes results back to company nodes.
        """
        log.info("Starting GDS Betweenness Centrality job...")
        results = {}

        with self.driver.session() as session:
            # 1. Clean up old projections if they exist
            try:
                session.run("CALL gds.graph.drop('corporate-fleet', false)")
                log.info("Dropped existing 'corporate-fleet' projection.")
            except Exception:
                pass

            # Ensure 'Company' label and relationship types exist to prevent GDS graph projection errors when empty
            session.run("MERGE (v:Vessel {imo: 'DUMMY_IMO_FOR_PROJECTION'})")
            session.run("MERGE (c:Company {name: 'DUMMY_HOLDER_FOR_PROJECTION'})")
            session.run("MATCH (v:Vessel {imo: 'DUMMY_IMO_FOR_PROJECTION'}), (c:Company {name: 'DUMMY_HOLDER_FOR_PROJECTION'}) "
                        "MERGE (v)-[:OWNED_BY]->(c) MERGE (v)-[:MANAGED_BY]->(c)")

            # 2. Create GDS projection of Vessels, Companies and ownership relationships
            projection_query = """
            CALL gds.graph.project(
              'corporate-fleet',
              ['Vessel', 'Company'],
              {
                OWNED_BY: { type: 'OWNED_BY', orientation: 'UNDIRECTED' },
                MANAGED_BY: { type: 'MANAGED_BY', orientation: 'UNDIRECTED' }
              }
            )
            """
            try:
                proj_res = session.run(projection_query).single()
                node_count = proj_res["nodeCount"] if proj_res else 0
                rel_count = proj_res["relationshipCount"] if proj_res else 0
                log.info(f"Projected corporate-fleet in-memory graph: {node_count} nodes, {rel_count} relationships.")
                results["nodes_projected"] = node_count
                results["relationships_projected"] = rel_count
            except Exception as e:
                log.error(f"Failed to create GDS projection: {e}")
                results["error"] = f"Projection failed: {str(e)}"
                return results

            # 3. Execute Betweenness Centrality write job
            # Computes centrality and writes it as property 'centrality_score' on Company nodes
            write_query = """
            CALL gds.betweenness.write(
              'corporate-fleet',
              {
                nodeLabels: ['Company'],
                writeProperty: 'betweenness_score'
              }
            )
            YIELD nodePropertiesWritten, computeMillis, postProcessingMillis
            """
            try:
                write_res = session.run(write_query).single()
                written = write_res["nodePropertiesWritten"] if write_res else 0
                log.info(f"Betweenness Centrality calculated and written for {written} company nodes.")
                results["nodes_calculated"] = written
            except Exception as e:
                log.error(f"Failed to execute GDS centrality calculation: {e}")
                results["error"] = f"GDS calculation failed: {str(e)}"
                return results

            # 4. Tag high-centrality parent nodes (Fleet Brokers)
            # Find the average score and mark companies above the average as high_betweenness_parent
            tag_companies_query = """
            MATCH (c:Company)
            WITH c, c.betweenness_score AS score
            WHERE score IS NOT NULL
            WITH collect(score) AS scores, collect(c) AS companies
            WITH apoc.coll.avg(scores) AS avg_score, companies
            UNWIND companies AS comp
            SET comp.high_betweenness_parent = (comp.betweenness_score > avg_score AND comp.betweenness_score > 0)
            RETURN count(comp) as tagged
            """
            try:
                tag_res = session.run(tag_companies_query).single()
                tagged = tag_res["tagged"] if tag_res else 0
                log.info(f"Successfully calculated fleet broker metrics and tagged {tagged} companies.")
                results["companies_tagged"] = tagged
            except Exception as e:
                log.warning(f"Failed to tag high betweenness parents: {e}")
                
            # 5. Propagate high_betweenness_parent flag to associated Vessels for agent quick-lookup
            propagate_query = """
            MATCH (v:Vessel)-[:OWNED_BY|MANAGED_BY]->(c:Company)
            WHERE c.high_betweenness_parent = true
            SET v.high_betweenness_parent = true
            RETURN count(v) as vessels_updated
            """
            try:
                prop_res = session.run(propagate_query).single()
                vessels_updated = prop_res["vessels_updated"] if prop_res else 0
                log.info(f"Propagated high_betweenness_parent status to {vessels_updated} vessel nodes.")
                results["vessels_updated"] = vessels_updated
            except Exception as e:
                log.warning(f"Failed to propagate centrality status to vessels: {e}")

            # 6. Clean up graph projection
            session.run("CALL gds.graph.drop('corporate-fleet', false)")
            # Delete dummy nodes
            session.run("MATCH (v:Vessel {imo: 'DUMMY_IMO_FOR_PROJECTION'}) DETACH DELETE v")
            session.run("MATCH (c:Company {name: 'DUMMY_HOLDER_FOR_PROJECTION'}) DETACH DELETE c")
            log.info("Cleaned up GDS graph projection and dummy nodes.")

        results["status"] = "success"
        return results

if __name__ == "__main__":
    job = GDSCentralityJob()
    res = job.run_betweenness_centrality()
    print("GDS Job Result:", res)
    job.close()
