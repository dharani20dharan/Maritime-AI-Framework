"""
Sanction Evasion Risk Scorer
Connects to the Neo4j Knowledge Graph to calculate an evasion risk score
for a given vessel based on identity, behavioral, and ownership anomalies.
"""
from neo4j import GraphDatabase
import json

URI = "neo4j://localhost:7687"
AUTH = ("neo4j", "maritime123")

class SanctionScorer:
    def __init__(self, uri=URI, auth=AUTH):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def close(self):
        self.driver.close()

    def calculate_risk(self, vessel_imo):
        """
        Calculates the evasion risk score (0-100) for a vessel.
        Queries the Neo4j graph for complex evasion patterns.
        Returns the score and a list of triggered risk flags.
        """
        risk_score = 0
        flags = []

        with self.driver.session() as session:
            # ---------------------------------------------------------
            # 1. Ownership & Direct Sanction Risk
            # ---------------------------------------------------------
            
            # Check for direct sanction on the vessel itself
            direct_sanction_query = """
            MATCH (v:Vessel {imo: $imo})-[:SANCTIONED_BY]->(s:Sanction)
            RETURN s.program AS Program LIMIT 1
            """
            direct_result = session.run(direct_sanction_query, imo=vessel_imo).single()
            if direct_result:
                risk_score += 100
                flags.append(f"Vessel is directly sanctioned under: {direct_result['Program']}")

            # Find shortest path to a sanctioned company (up to 4 layers deep)
            ownership_query = """
            MATCH path = (v:Vessel {imo: $imo})-[:OWNED_BY|MANAGED_BY]->(c:Company)-[:SUBSIDIARY_OF*0..4]->(parent:Company)-[:SANCTIONED_BY]->(s:Sanction)
            RETURN length(path) - 2 AS Layers, parent.name AS SanctionedEntity
            ORDER BY Layers ASC LIMIT 1
            """
            owner_result = session.run(ownership_query, imo=vessel_imo).single()
            if owner_result:
                layers = owner_result["Layers"]
                entity = owner_result["SanctionedEntity"]
                if layers <= 0:
                    risk_score += 100
                    if not direct_result: # Prevent duplicate 100 score flags
                        flags.append(f"Directly owned/managed by sanctioned entity: {entity}")
                else:
                    penalty = 80 - (10 * layers)
                    risk_score += max(penalty, 0)
                    flags.append(f"Obfuscated ownership: {layers} layers removed from sanctioned entity ({entity})")

            # ---------------------------------------------------------
            # 2. Behavioral Risk (STS / Gaps)
            # ---------------------------------------------------------
            sts_query = """
            MATCH (v1:Vessel {imo: $imo})-[:INVOLVED_IN]->(e1:Event)
            MATCH (v2:Vessel)-[:INVOLVED_IN]->(e2:Event)
            WHERE v1 <> v2 
              AND e1.event_type IN ['AIS_GAP', 'LOITERING']
              AND e2.event_type IN ['AIS_GAP', 'LOITERING']
              // Temporal overlap
              AND e1.start_time <= e2.end_time AND e1.end_time >= e2.start_time
              // Spatial proximity (omitted for simplistic dummy data, but required for prod)
              // AND point.distance(e1.location, e2.location) < 10000 
            RETURN count(v2) AS Intersections
            """
            sts_result = session.run(sts_query, imo=vessel_imo).single()
            intersections = sts_result["Intersections"] if sts_result else 0
            if intersections > 0:
                risk_score += 40 * intersections
                flags.append(f"Potential STS Transfer: Intersecting dark/loitering events with {intersections} other vessels")

            # ---------------------------------------------------------
            # 3. Identity Risk (Flag Hopping)
            # ---------------------------------------------------------
            flag_query = """
            MATCH (v:Vessel {imo: $imo})-[r:REGISTERED_UNDER]->(f:Flag)
            RETURN count(r) AS FlagChanges
            """
            flag_result = session.run(flag_query, imo=vessel_imo).single()
            flag_changes = flag_result["FlagChanges"] if flag_result else 0
            # Penalize if they have had more than 1 flag registration in the graph
            if flag_changes > 1:
                risk_score += 15 * (flag_changes - 1)
                flags.append(f"Flag Hopping Risk: {flag_changes} flag registrations found")

        # Cap score at 100
        final_score = min(risk_score, 100)
        return final_score, flags


if __name__ == "__main__":
    scorer = SanctionScorer()
    
    # Test with normal vessel (Ocean Voyager)
    print("--- Evaluating OCEAN VOYAGER (IMO: 9123456) ---")
    score, flags = scorer.calculate_risk("9123456")
    print(json.dumps({"Risk Score": score, "Flags": flags}, indent=2))
    
    # Test with sanctioned/evasive vessel (Sea Shadow)
    print("\n--- Evaluating SEA SHADOW (IMO: 9988776) ---")
    score, flags = scorer.calculate_risk("9988776")
    print(json.dumps({"Risk Score": score, "Flags": flags}, indent=2))
    
    scorer.close()
