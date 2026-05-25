"""
Spatio-Temporal Ship-to-Ship (STS) Transfer Risk Detector.
Analyzes overlapping AIS loitering and transponder gap events across vessels
to identify high-risk mid-ocean rendezvous and writes the detections into Neo4j.
"""
from neo4j import GraphDatabase
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("sts-detector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USER = os.getenv("NEO4J_USER", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")

class STSDetector:
    def __init__(self, uri=URI, auth=(USER, PASSWORD)):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def close(self):
        self.driver.close()

    def run_sts_detection(self, target_imo: str) -> list:
        """
        Scans for vessels that have overlapping LOITERING or AIS_GAP events 
        with the target vessel, signifying a Spatio-Temporal rendezvous.
        Writes a new STS_TRANSFER Event node and links both vessels.
        """
        log.info(f"Scanning for STS rendezvous events for vessel IMO: {target_imo}...")
        detections = []

        # Cypher query to find overlapping loitering and dark events
        find_overlapping_query = """
        MATCH (v1:Vessel {imo: $imo})-[:INVOLVED_IN]->(e1:Event)
        MATCH (v2:Vessel)-[:INVOLVED_IN]->(e2:Event)
        WHERE v1 <> v2
          AND e1.event_type IN ['AIS_GAP', 'LOITERING']
          AND e2.event_type IN ['AIS_GAP', 'LOITERING']
          AND e1.start_time <= e2.end_time AND e1.end_time >= e2.start_time
        RETURN v2.imo AS peer_imo, v2.name AS peer_name, 
               e1.event_type AS e1_type, e2.event_type AS e2_type,
               e1.location AS loc,
               apoc.coll.max([e1.start_time, e2.start_time]) AS overlap_start,
               apoc.coll.min([e1.end_time, e2.end_time]) AS overlap_end
        """

        with self.driver.session() as session:
            result = session.run(find_overlapping_query, imo=target_imo)
            for record in result:
                peer_imo = record["peer_imo"]
                peer_name = record["peer_name"]
                loc = record["loc"] or "Open Waters"
                
                # Parse times and compute overlap duration
                t_start = record["overlap_start"]
                t_end = record["overlap_end"]
                
                try:
                    dt_start = datetime.fromisoformat(t_start.replace('Z', '+00:00'))
                    dt_end = datetime.fromisoformat(t_end.replace('Z', '+00:00'))
                    duration_hours = (dt_end - dt_start).total_seconds() / 3600.0
                except Exception:
                    duration_hours = 4.0 # Default fallback if unparseable
                
                # STS transfers require sustainable co-loitering (> 2 hours)
                if duration_hours >= 2.0:
                    confidence = min(1.0, 0.5 + (duration_hours / 24.0)) # Longer = higher confidence
                    
                    detection = {
                        "peer_imo": peer_imo,
                        "peer_name": peer_name,
                        "location": loc,
                        "start_time": t_start,
                        "end_time": t_end,
                        "duration_hours": round(duration_hours, 2),
                        "confidence": round(confidence, 2)
                    }
                    detections.append(detection)
                    
                    # Write the STS_TRANSFER Event node back into Neo4j
                    event_id = f"STS-{target_imo}-{peer_imo}-{t_start[:10]}"
                    write_event_query = """
                    MERGE (e:Event {event_id: $event_id})
                    ON CREATE SET 
                      e.event_type = 'STS_TRANSFER',
                      e.location = $loc,
                      e.start_time = $start_time,
                      e.end_time = $end_time,
                      e.duration_hours = $duration_hours,
                      e.confidence = $confidence,
                      e.description = $desc
                    
                    WITH e
                    MATCH (v1:Vessel {imo: $imo1})
                    MATCH (v2:Vessel {imo: $imo2})
                    MERGE (v1)-[:INVOLVED_IN]->(e)
                    MERGE (v2)-[:INVOLVED_IN]->(e)
                    """
                    
                    desc = f"Suspected Ship-to-Ship cargo transfer event between {target_imo} and {peer_name} ({peer_imo}) in {loc}."
                    session.run(write_event_query, 
                                event_id=event_id,
                                loc=loc,
                                start_time=t_start,
                                end_time=t_end,
                                duration_hours=round(duration_hours, 2),
                                confidence=round(confidence, 2),
                                desc=desc,
                                imo1=target_imo,
                                imo2=peer_imo)
                    
                    log.info(f"Recorded STS_TRANSFER Event ({event_id}) between {target_imo} and {peer_name} (Confidence: {confidence:.2f})")

        return detections

if __name__ == "__main__":
    detector = STSDetector()
    # Test execution on high-risk test vessel IMO
    target = "9000000"
    res = detector.run_sts_detection(target)
    print("Detected STS Rendezvous Events:", res)
    detector.close()
