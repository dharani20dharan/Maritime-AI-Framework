"""
MAF — Neo4j ETL (Engineer A, Stage 2 handoff)
Consumes ais.validated from Kafka.
Upserts Vessel nodes + FlagState relationships into Neo4j.
This is the seam between Engineer A's pipeline and Engineer B's graph layer.
Contract: A1_ais_envelope.py
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from confluent_kafka import Consumer, KafkaError
from neo4j import GraphDatabase

log = logging.getLogger("neo4j-etl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
INPUT_TOPIC     = os.getenv("KAFKA_INPUT_TOPIC", "ais.validated")
NEO4J_URI       = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER      = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD  = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
BATCH_SIZE      = int(os.getenv("BATCH_SIZE", "100"))


# ── CYPHER QUERIES ────────────────────────────────────────────────────────────

UPSERT_VESSEL = """
MERGE (v:Vessel {mmsi: $mmsi})
ON CREATE SET
  v.imo         = $imo,
  v.name        = $name,
  v.flag        = $flag,
  v.vessel_type = $vessel_type,
  v.first_seen  = $timestamp,
  v.risk_score  = 0.0,
  v.sanctioned  = false
SET
  v.last_lat    = CASE WHEN $lat IS NOT NULL THEN $lat ELSE v.last_lat END,
  v.last_lon    = CASE WHEN $lon IS NOT NULL THEN $lon ELSE v.last_lon END,
  v.speed_kts   = CASE WHEN $speed IS NOT NULL THEN $speed ELSE v.speed_kts END,
  v.heading     = CASE WHEN $heading IS NOT NULL THEN $heading ELSE v.heading END,
  v.nav_status  = CASE WHEN $nav_status IS NOT NULL THEN $nav_status ELSE v.nav_status END,
  v.draught_m   = CASE WHEN $draught IS NOT NULL THEN $draught ELSE v.draught_m END,
  v.last_seen   = $timestamp,
  v.last_updated= $ingested_at
WITH v
WHERE $flag IS NOT NULL
MERGE (f:FlagState {iso_code: $flag})
ON CREATE SET f.name = $flag
MERGE (v)-[:FLAGGED_UNDER]->(f)
"""

UPSERT_POSITION_HISTORY = """
MATCH (v:Vessel {mmsi: $mmsi})
CREATE (p:PositionRecord {
  record_id:  $record_id,
  mmsi:       $mmsi,
  lat:        $lat,
  lon:        $lon,
  speed_kts:  $speed,
  heading:    $heading,
  timestamp:  $timestamp,
  source:     $source
})
CREATE (v)-[:HAS_POSITION]->(p)
"""


# ── BATCH WRITER ──────────────────────────────────────────────────────────────

class BatchWriter:
    def __init__(self, driver):
        self.driver = driver
        self.batch: list[dict] = []
        self.processed = 0

    def add(self, envelope: dict):
        self.batch.append(envelope)
        if len(self.batch) >= BATCH_SIZE:
            self.flush()

    def flush(self):
        if not self.batch:
            return
        with self.driver.session() as session:
            for env in self.batch:
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
                    # Only log position history for geo-tagged messages
                    if env.get("lat") is not None:
                        session.run(UPSERT_POSITION_HISTORY, {
                            "record_id": str(uuid.uuid4()),
                            "mmsi":      env.get("mmsi"),
                            "lat":       env.get("lat"),
                            "lon":       env.get("lon"),
                            "speed":     env.get("speed_kts"),
                            "heading":   env.get("heading"),
                            "timestamp": env.get("timestamp"),
                            "source":    env.get("source"),
                        })
                except Exception as e:
                    log.warning("ETL write failed for mmsi=%s: %s", env.get("mmsi"), e)

        self.processed += len(self.batch)
        log.debug("Flushed %d records — total %d", len(self.batch), self.processed)
        self.batch.clear()


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    writer = BatchWriter(driver)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "maf-neo4j-etl",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([INPUT_TOPIC])
    log.info("Neo4j ETL running — consuming %s → neo4j at %s", INPUT_TOPIC, NEO4J_URI)

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    writer.flush()
                    continue
                log.error("Kafka error: %s", msg.error())
                continue
            try:
                envelope = json.loads(msg.value().decode())
                writer.add(envelope)
                consumer.commit(asynchronous=True)
            except Exception as e:
                log.error("ETL processing error: %s", e)
    except KeyboardInterrupt:
        writer.flush()
        log.info("ETL shutdown — %d total records processed", writer.processed)
    finally:
        consumer.close()
        driver.close()


if __name__ == "__main__":
    run()
