"""
Sanction Evasion Risk Scorer
Connects to the Neo4j Knowledge Graph to calculate an evasion risk score
for a given vessel based on identity, behavioral, and ownership anomalies.
"""
from neo4j import GraphDatabase
import json

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "maf_neo4j_2024")

class SanctionScorer:
    def __init__(self, uri=URI, auth=AUTH):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def close(self):
        self.driver.close()

    def calculate_risk(self, vessel_imo):
        """
        Calculates the evasion risk score (0-100) for a vessel.
        Queries the Neo4j graph for complex evasion patterns (16 distinct techniques).
        Returns the score and a list of triggered risk flags.
        """
        risk_score = 0
        flags = []

        with self.driver.session() as session:
            # ---------------------------------------------------------
            # 1. Direct Sanctions Check
            # ---------------------------------------------------------
            direct_sanction_query = """
            MATCH (v:Vessel {imo: $imo})-[:SANCTIONED_BY]->(s:Sanction)
            RETURN s.program AS Program LIMIT 1
            """
            direct_result = session.run(direct_sanction_query, imo=vessel_imo).single()
            if direct_result:
                risk_score += 100
                flags.append(f"Vessel is directly sanctioned under: {direct_result['Program']}")

            # ---------------------------------------------------------
            # 2. M-SHELL-CHAIN (Shell Company Chain & Obfuscated Ownership)
            # ---------------------------------------------------------
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
                    if not direct_result:
                        flags.append(f"Directly owned/managed by sanctioned entity: {entity}")
                else:
                    penalty = 80 - (10 * layers)
                    risk_score += max(penalty, 0)
                    flags.append(f"Obfuscated ownership: {layers} layers removed from sanctioned entity ({entity})")

            # Check corporate chain depth for general M-SHELL-CHAIN
            depth_query = """
            MATCH path = (v:Vessel {imo: $imo})-[:OWNED_BY|MANAGED_BY]->(c:Company)-[:SUBSIDIARY_OF*0..5]->(parent:Company)
            RETURN length(path) AS Depth ORDER BY Depth DESC LIMIT 1
            """
            depth_result = session.run(depth_query, imo=vessel_imo).single()
            if depth_result and depth_result["Depth"] > 3:
                risk_score += 30
                flags.append(f"M-SHELL-CHAIN: Deeply layered corporate ownership chain (depth of {depth_result['Depth']} levels)")

            # ---------------------------------------------------------
            # 3. M-LOITERING & M-DARK-EVENT & M-AIS-BEACON & M-SPEED-ANOMALY
            # ---------------------------------------------------------
            event_counts_query = """
            MATCH (v:Vessel {imo: $imo})-[:INVOLVED_IN]->(e:Event)
            RETURN e.event_type AS event_type, count(e) AS occurrences
            """
            event_results = session.run(event_counts_query, imo=vessel_imo)
            for record in event_results:
                etype = record["event_type"]
                count = record["occurrences"]
                if etype == 'LOITERING':
                    risk_score += 15 * count
                    flags.append(f"M-LOITERING: {count} suspicious loiter events in open water")
                elif etype == 'AIS_GAP':
                    risk_score += 20 * count
                    flags.append(f"M-DARK-EVENT: {count} transponder gaps (AIS Gap) anomalies")
                elif etype == 'BEACON_PATTERN':
                    risk_score += 25 * count
                    flags.append(f"M-AIS-BEACON: {count} scripted AIS beacon periodicities detected")
                elif etype == 'SPEED_ANOMALY':
                    risk_score += 20 * count
                    flags.append(f"M-SPEED-ANOMALY: {count} impossible speed events detected")
                elif etype == 'TRACK_PLAUSIBILITY':
                    risk_score += 15 * count
                    flags.append(f"M-TRACK-PLAUSIBILITY: {count} track plausibility/bathymetric anomalies")
                elif etype == 'PORT_CALL_ANOMALY':
                    risk_score += 20 * count
                    flags.append(f"M-PORT-CALL-ANOMALY: {count} irregular port call events detected")
                elif etype == 'EEZ_VIOLATION':
                    risk_score += 30 * count
                    flags.append(f"M-EEZ-VIOLATION: {count} unauthorized Exclusive Economic Zone entries")
                elif etype == 'SANCTIONED_ZONE':
                    risk_score += 50 * count
                    flags.append(f"M-SANCTIONED-ZONE: {count} entries into restricted sanctioned zones")

            # ---------------------------------------------------------
            # 4. M-FLAG-HOP (Flag Hopping)
            # ---------------------------------------------------------
            flag_query = """
            MATCH (v:Vessel {imo: $imo})-[r:REGISTERED_UNDER]->(f:Flag)
            RETURN count(DISTINCT f) AS FlagChanges
            """
            flag_result = session.run(flag_query, imo=vessel_imo).single()
            flag_changes = flag_result["FlagChanges"] if flag_result else 0
            if flag_changes > 1:
                risk_score += 25 * (flag_changes - 1)
                flags.append(f"M-FLAG-HOP: {flag_changes} flag registrations found in short period")

            # ---------------------------------------------------------
            # 5. M-MMSI-SPOOF (MMSI Spoofing)
            # ---------------------------------------------------------
            mmsi_query = """
            MATCH (v:Vessel {imo: $imo})
            WITH v.mmsi AS mmsi, v.name AS curr_name
            MATCH (other:Vessel {mmsi: mmsi})
            WHERE other.name <> curr_name
            RETURN count(DISTINCT other.name) AS OtherNames, collect(DISTINCT other.name) AS NamesList
            """
            mmsi_result = session.run(mmsi_query, imo=vessel_imo).single()
            if mmsi_result and mmsi_result["OtherNames"] > 0:
                risk_score += 50
                flags.append(f"M-MMSI-SPOOF: Multiple distinct vessel names {mmsi_result['NamesList']} broadcast same MMSI")

            # ---------------------------------------------------------
            # 6. M-IDENTITY-LAUNDER (Identity Laundering)
            # ---------------------------------------------------------
            launder_query = """
            MATCH (v1:Vessel {imo: $imo}), (v2:Vessel {imo: $imo})
            WHERE v1.mmsi <> v2.mmsi OR v1.name <> v2.name
            RETURN count(DISTINCT v2.name) AS NameChanges
            """
            launder_result = session.run(launder_query, imo=vessel_imo).single()
            if launder_result and launder_result["NameChanges"] > 1:
                risk_score += 60
                flags.append(f"M-IDENTITY-LAUNDER: Persistent IMO associated with simultaneous name/MMSI change")

            # ---------------------------------------------------------
            # 7. M-IMO-CLONE (IMO Cloning)
            # ---------------------------------------------------------
            clone_query = """
            MATCH (v:Vessel {imo: $imo})
            MATCH (other:Vessel {imo: $imo})
            WHERE other.mmsi <> v.mmsi
            RETURN count(DISTINCT other.mmsi) AS Clones
            """
            clone_result = session.run(clone_query, imo=vessel_imo).single()
            if clone_result and clone_result["Clones"] > 0:
                risk_score += 60
                flags.append(f"M-IMO-CLONE: {clone_result['Clones']} distinct vessels broadcast identical IMO number")

            # ---------------------------------------------------------
            # 8. M-SANCTIONED-ZONE flag check
            # ---------------------------------------------------------
            zone_query = """
            MATCH (v:Vessel {imo: $imo})-[r:REGISTERED_UNDER]->(f:Flag)
            WHERE f.country_code IN ['IR', 'KP', 'RU']
            RETURN f.name AS FlagName
            """
            zone_result = session.run(zone_query, imo=vessel_imo).single()
            if zone_result:
                risk_score += 50
                flags.append(f"M-SANCTIONED-ZONE: Registered under sanctioned jurisdiction flag: {zone_result['FlagName']}")

            # ---------------------------------------------------------
            # 9. M-STS-TRANSFER (Ship-to-Ship Transfer co-loitering)
            # ---------------------------------------------------------
            sts_query = """
            MATCH (v1:Vessel {imo: $imo})-[:INVOLVED_IN]->(e1:Event)
            MATCH (v2:Vessel)-[:INVOLVED_IN]->(e2:Event)
            WHERE v1 <> v2 
              AND e1.event_type IN ['AIS_GAP', 'LOITERING']
              AND e2.event_type IN ['AIS_GAP', 'LOITERING']
              AND e1.start_time <= e2.end_time AND e1.end_time >= e2.start_time
            RETURN count(DISTINCT v2) AS Intersections
            """
            sts_result = session.run(sts_query, imo=vessel_imo).single()
            intersections = sts_result["Intersections"] if sts_result else 0
            if intersections > 0:
                risk_score += 40 * intersections
                flags.append(f"M-STS-TRANSFER: Suspected mid-ocean rendezvous with {intersections} other vessels")

            # ---------------------------------------------------------
            # 10. M-FLEET-BROKER (Fleet Broker shell connections)
            # ---------------------------------------------------------
            broker_query = """
            MATCH (v:Vessel {imo: $imo})-[:OWNED_BY|MANAGED_BY]->(c:Company)
            MATCH (other:Vessel)-[:OWNED_BY|MANAGED_BY]->(c)
            WHERE other.imo <> $imo
            WITH c, count(DISTINCT other) AS FleetSize
            WHERE FleetSize > 5
            RETURN c.name AS CompanyName, FleetSize
            """
            broker_result = session.run(broker_query, imo=vessel_imo).single()
            if broker_result:
                risk_score += 40
                flags.append(f"M-FLEET-BROKER: Connected to central fleet broker '{broker_result['CompanyName']}' managing {broker_result['FleetSize']} vessels")

            # ---------------------------------------------------------
            # 11. M-SISTER-RISK (Sister Ship Risk Propagation)
            # ---------------------------------------------------------
            sister_query = """
            MATCH (v:Vessel {imo: $imo})
            MATCH (sister:Vessel)-[:SANCTIONED_BY]->(s:Sanction)
            WHERE sister.imo <> $imo 
              AND (sister.vessel_type = v.vessel_type OR sister.built_year = v.built_year)
            RETURN count(DISTINCT sister) AS SisterShips, collect(DISTINCT sister.name) AS SisterNames
            """
            sister_result = session.run(sister_query, imo=vessel_imo).single()
            if sister_result and sister_result["SisterShips"] > 0:
                risk_score += 25
                flags.append(f"M-SISTER-RISK: Structural identical sister ship is sanctioned: {sister_result['SisterNames']}")

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
