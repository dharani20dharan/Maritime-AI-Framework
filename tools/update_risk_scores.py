from neo4j import GraphDatabase
import logging

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "maf_neo4j_2024")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def update_all_vessel_risk_scores():
    driver = GraphDatabase.driver(URI, auth=AUTH, encrypted=False)
    with driver.session() as s:
        logging.info("Fetching vessels and their anomalies from Neo4j...")
        
        query = """
        MATCH (v:Vessel)
        OPTIONAL MATCH (v)-[:INVOLVED_IN]->(e:Event)
        OPTIONAL MATCH (v)-[:SANCTIONED_BY]->(s)
        OPTIONAL MATCH (v)-[:OWNED_BY|MANAGED_BY]->(c:Company)-[:SANCTIONED_BY]->(cs)
        RETURN v.mmsi AS mmsi, v.name AS name,
               collect(DISTINCT e.event_type) AS event_types,
               count(DISTINCT e) AS event_count,
               s IS NOT NULL AS directly_sanctioned,
               cs IS NOT NULL AS owner_sanctioned
        """
        
        vessels = s.run(query)
        vessels_list = [dict(rec) for rec in vessels]
        logging.info(f"Loaded {len(vessels_list)} vessels. Processing risk scores...")

        # Pre-load all event counts to avoid N+1 queries
        logging.info("Loading all detailed event counts for processing...")
        event_counts_query = """
        MATCH (v:Vessel)-[:INVOLVED_IN]->(e:Event)
        RETURN v.mmsi AS mmsi, e.event_type AS et, count(e) AS c
        """
        event_counts = {}
        for rec in s.run(event_counts_query):
            mmsi = rec["mmsi"]
            if mmsi not in event_counts:
                event_counts[mmsi] = {}
            event_counts[mmsi][rec["et"]] = rec["c"]

        batch_updates = []
        updated_count = 0
        
        for v in vessels_list:
            mmsi = v["mmsi"]
            name = v["name"] or "UNKNOWN"
            score = 0
            
            # Direct/Indirect Sanctions
            if v["directly_sanctioned"] or v["owner_sanctioned"]:
                score = 100
            else:
                # Calculate based on event types and frequency from preloaded cache
                mmsi_events = event_counts.get(mmsi, {})
                for etype, count in mmsi_events.items():
                    if etype == 'LOITERING':
                        score += 15 * count
                    elif etype == 'AIS_GAP':
                        score += 20 * count
                    elif etype == 'BEACON_PATTERN':
                        score += 25 * count
                    elif etype == 'SPEED_ANOMALY':
                        score += 20 * count
                    elif etype == 'TRACK_PLAUSIBILITY':
                        score += 15 * count
                    elif etype == 'PORT_CALL_ANOMALY':
                        score += 20 * count
                    elif etype == 'EEZ_VIOLATION':
                        score += 30 * count
                    elif etype == 'SANCTIONED_ZONE':
                        score += 50 * count
                    elif etype == 'STS_TRANSFER':
                        score += 40 * count

            # Cap the score between 0 and 100
            score = min(max(score, 0), 100)
            
            # Always update, but track non-zero ones for logs
            batch_updates.append({"mmsi": mmsi, "score": score})
            if score > 0:
                logging.info(f"Vessel {name} (MMSI: {mmsi}) scored {score}% based on anomalies.")
                updated_count += 1

        logging.info(f"Writing {len(batch_updates)} vessel scores in a single Neo4j batch transaction...")
        s.run("""
            UNWIND $batch AS item
            MATCH (v:Vessel {mmsi: item.mmsi})
            SET v.risk_score = item.score
        """, batch=batch_updates)
        
        logging.info(f"Successfully calculated and batch-updated risk scores for {updated_count} suspicious vessels!")
        
    driver.close()

if __name__ == "__main__":
    update_all_vessel_risk_scores()
