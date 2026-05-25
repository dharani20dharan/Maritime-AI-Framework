"""
MAF — Neo4j ETL (Engineer A, Stage 2 handoff)
Consumes ais.validated and ais.anomalies from Kafka.
Upserts Vessel nodes + Flag relationships into Neo4j.
Writes Anomaly events as Event nodes with Geospatial Points.
Strictly adheres to Engineer B's schema (init_db.py).
"""

import json
import logging
import os
import uuid
import time
from datetime import datetime, timezone

from confluent_kafka import Consumer, KafkaError
from neo4j import GraphDatabase

log = logging.getLogger("neo4j-etl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

KAFKA_BOOTSTRAP  = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
INPUT_TOPIC      = os.getenv("KAFKA_INPUT_TOPIC", "ais.validated")
ANOMALIES_TOPIC  = os.getenv("KAFKA_ANOMALIES_TOPIC", "ais.anomalies")
NEO4J_URI        = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER       = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD   = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
BATCH_SIZE       = int(os.getenv("BATCH_SIZE", "100"))


# ── CYPHER QUERIES (Strictly matching Engineer B's Schema) ────────────────────

UPSERT_VESSEL = """
MERGE (v:Vessel {mmsi: $mmsi})
ON CREATE SET
  v.imo         = $imo,
  v.name        = $name,
  v.vessel_type = $vessel_type,
  v.first_seen  = $timestamp,
  v.risk_score  = 0.0,
  v.sanctioned  = false
SET
  v.last_lat    = CASE WHEN $lat IS NOT NULL THEN $lat ELSE v.last_lat END,
  v.last_lon    = CASE WHEN $lon IS NOT NULL THEN $lon ELSE v.last_lon END,
  v.location    = CASE WHEN $lat IS NOT NULL AND $lon IS NOT NULL THEN point({latitude: $lat, longitude: $lon, crs: 'WGS-84'}) ELSE v.location END,
  v.speed_kts   = CASE WHEN $speed IS NOT NULL THEN $speed ELSE v.speed_kts END,
  v.heading     = CASE WHEN $heading IS NOT NULL THEN $heading ELSE v.heading END,
  v.nav_status  = CASE WHEN $nav_status IS NOT NULL THEN $nav_status ELSE v.nav_status END,
  v.draught_m   = CASE WHEN $draught IS NOT NULL THEN $draught ELSE v.draught_m END,
  v.last_seen   = $timestamp,
  v.last_updated= $ingested_at
WITH v
WHERE $flag IS NOT NULL
// Engineer B Contract: Label is 'Flag', property is 'country_code'
MERGE (f:Flag {country_code: $flag})
ON CREATE SET f.name = $flag
// Engineer B Contract & Backward Compat
MERGE (v)-[:REGISTERED_UNDER]->(f)
MERGE (v)-[:FLAGGED_UNDER]->(f)
"""

# Event Nodes for Anomalies
UPSERT_EVENT = """
MATCH (v:Vessel {mmsi: $mmsi})
MERGE (e:Event {event_id: $event_id})
ON CREATE SET 
  e.event_type = $technique,
  e.start_time = datetime($detected_at),
  e.end_time = datetime($detected_at),
  e.description = $detail,
  e.confidence = $confidence,
  e.location = CASE WHEN $lat IS NOT NULL AND $lon IS NOT NULL 
               THEN point({latitude: toFloat($lat), longitude: toFloat($lon), crs: 'WGS-84'}) 
               ELSE null END
// Engineer B Contract & Backward Compat
MERGE (v)-[:INVOLVED_IN]->(e)
MERGE (v)-[:TRIGGERED]->(e)
"""

SYNC_SANCTIONS = """
MATCH (v:Vessel), (s:Sanction)
WHERE v.imo IS NOT NULL AND v.imo = s.imo
MERGE (v)-[:SANCTIONED_BY]->(s)
SET v.sanctioned = true
"""

TECHNIQUE_MAP = {
    "M-DARK-EVENT": "AIS_GAP",
    "M-AIS-BEACON": "BEACON_PATTERN",
    "M-SPEED-ANOMALY": "SPEED_ANOMALY"
}

# ── BATCH WRITER ──────────────────────────────────────────────────────────────

class BatchWriter:
    def __init__(self, driver):
        self.driver = driver
        self.vessel_batch: list[dict] = []
        self.event_batch: list[dict] = []
        self.processed = 0

    def add_vessel(self, envelope: dict):
        self.vessel_batch.append(envelope)
        if len(self.vessel_batch) >= BATCH_SIZE:
            self.flush_vessels()

    def add_event(self, event_data: dict):
        self.event_batch.append(event_data)
        if len(self.event_batch) >= BATCH_SIZE:
            self.flush_events()

    def flush_vessels(self):
        if not self.vessel_batch:
            return
        with self.driver.session() as session:
            for env in self.vessel_batch:
                try:
                    session.run(UPSERT_VESSEL, {
                        "mmsi":       env.get("mmsi"),
                        "imo":        env.get("imo"),
                        "name":       env.get("name"),
                        "flag":       env.get("flag"),
                        "vessel_type":env.get("vessel_type"),
                        "lat":        env.get("lat"),
                        "lon":        env.get("lon"),
                        "speed":      env.get("speed_kts"),
                        "heading":    env.get("heading"),
                        "nav_status": env.get("nav_status"),
                        "draught":    env.get("draught_m"),
                        "timestamp":  env.get("timestamp"),
                        "ingested_at":datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    log.warning("ETL vessel write failed for mmsi=%s: %s", env.get("mmsi"), e)

        self.processed += len(self.vessel_batch)
        log.debug("Flushed %d vessels", len(self.vessel_batch))
        self.vessel_batch.clear()

    def flush_events(self):
        if not self.event_batch:
            return
        with self.driver.session() as session:
            for evt in self.event_batch:
                try:
                    # Map technique to Engineer B's expected event_type
                    raw_technique = evt.get("technique", "")
                    mapped_technique = TECHNIQUE_MAP.get(raw_technique, raw_technique)

                    session.run(UPSERT_EVENT, {
                        "mmsi":        evt.get("mmsi"),
                        "event_id":    str(uuid.uuid4()),
                        "technique":   mapped_technique,
                        "detected_at": evt.get("detected_at"),
                        "detail":      evt.get("detail"),
                        "confidence":  evt.get("confidence"),
                        "lat":         evt.get("lat"),
                        "lon":         evt.get("lon")
                    })
                except Exception as e:
                    log.warning("ETL event write failed for mmsi=%s: %s", evt.get("mmsi"), e)
        
        self.processed += len(self.event_batch)
        log.debug("Flushed %d anomaly events", len(self.event_batch))
        self.event_batch.clear()

    def flush_all(self):
        self.flush_vessels()
        self.flush_events()


def init_indexes(driver):
    """Create spatial indexes required for Engineer B's spatial queries."""
    log.info("Ensuring POINT INDEXES exist for Vessel and Event locations...")
    try:
        with driver.session() as session:
            session.run("CREATE POINT INDEX vessel_location IF NOT EXISTS FOR (v:Vessel) ON (v.location)")
            session.run("CREATE POINT INDEX event_location IF NOT EXISTS FOR (e:Event) ON (e.location)")
    except Exception as e:
        log.warning("Failed to initialize point indexes: %s", e)

def sync_sanctions(driver):
    """Periodically link existing vessels to sanctions records."""
    try:
        with driver.session() as session:
            res = session.run(SYNC_SANCTIONS)
            updates = res.consume().counters.relationships_created
            if updates > 0:
                log.info("Sanctions sync: created %d new SANCTIONED_BY relationships", updates)
    except Exception as e:
        log.warning("Sanctions sync failed: %s", e)

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    init_indexes(driver)
    writer = BatchWriter(driver)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "maf-neo4j-etl",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    
    # Subscribe to BOTH topics
    consumer.subscribe([INPUT_TOPIC, ANOMALIES_TOPIC])
    log.info("Neo4j ETL running — consuming %s and %s → neo4j at %s", INPUT_TOPIC, ANOMALIES_TOPIC, NEO4J_URI)

    last_sync = time.time()

    try:
        while True:
            msg = consumer.poll(timeout=1.0)

            # Run periodic Sanctions Sync (every 60s)
            if time.time() - last_sync > 60:
                sync_sanctions(driver)
                last_sync = time.time()

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    writer.flush_all()
                    continue
                log.error("Kafka error: %s", msg.error())
                continue
            
            try:
                payload = json.loads(msg.value().decode())
                topic = msg.topic()
                
                if topic == INPUT_TOPIC:
                    writer.add_vessel(payload)
                elif topic == ANOMALIES_TOPIC:
                    # Map anomaly to Engineer B's Event schema
                    writer.add_event(payload)
                
                consumer.commit(asynchronous=True)
            except Exception as e:
                log.error("ETL processing error: %s", e)
    except KeyboardInterrupt:
        writer.flush_all()
        log.info("ETL shutdown — %d total records processed", writer.processed)
    finally:
        consumer.close()
        driver.close()


if __name__ == "__main__":
    run()