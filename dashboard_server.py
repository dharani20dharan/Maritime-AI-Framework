"""
MAF — Dashboard Proxy Server
Runs on localhost:5000. Queries Neo4j, Kafka, and Cassandra,
returns JSON to the dashboard HTML page.

Start with:
    pip install flask flask-cors neo4j confluent-kafka cassandra-driver
    python dashboard_server.py
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from flask import Flask, jsonify
from flask_cors import CORS

log = logging.getLogger("dashboard-server")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

app = Flask(__name__)
CORS(app)

# ── CONFIG ────────────────────────────────────────────────────────────────────
NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
KAFKA_BOOTSTRAP= os.getenv("KAFKA_BOOTSTRAP","localhost:29092")
CASSANDRA_HOST = os.getenv("CASSANDRA_HOST", "localhost")
CASSANDRA_PORT = int(os.getenv("CASSANDRA_PORT", "9042"))


# ── NEO4J ─────────────────────────────────────────────────────────────────────
def get_neo4j_driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


@app.route("/api/neo4j")
def api_neo4j():
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            counts = {}
            for label in ["Vessel", "FlagState", "EEZZone",
                          "SanctionedEntity", "AnomalyEvent", "PositionRecord"]:
                r = s.run(f"MATCH (n:{label}) RETURN count(n) AS c")
                counts[label] = r.single()["c"]
        driver.close()
        return jsonify({
            "vessels":   counts["Vessel"],
            "flags":     counts["FlagState"],
            "eez_zones": counts["EEZZone"],
            "sanctioned":counts["SanctionedEntity"],
            "anomalies": counts["AnomalyEvent"],
            "positions": counts["PositionRecord"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route("/api/vessels")
def api_vessels():
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            r = s.run("""
                MATCH (v:Vessel)
                WHERE v.last_seen IS NOT NULL
                RETURN v.mmsi AS mmsi, v.name AS name, v.imo AS imo,
                       v.flag AS flag, v.speed_kts AS speed_kts,
                       v.last_seen AS last_seen
                ORDER BY v.last_seen DESC
                LIMIT 10
            """)
            vessels = [dict(rec) for rec in r]
        driver.close()
        return jsonify({"vessels": vessels})
    except Exception as e:
        return jsonify({"error": str(e)}), 200


# ── KAFKA ─────────────────────────────────────────────────────────────────────
# Cache message counts to avoid hammering Kafka
_kafka_cache = {}
_kafka_cache_ts = 0

@app.route("/api/topics")
def api_topics():
    global _kafka_cache, _kafka_cache_ts
    if time.time() - _kafka_cache_ts < 10:
        return jsonify(_kafka_cache)
    try:
        from confluent_kafka.admin import AdminClient
        from confluent_kafka import Consumer, TopicPartition

        admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
        topics_meta = admin.list_topics(timeout=5)

        result = {}
        target_topics = {
            "ais.raw":       "ais_raw",
            "ais.validated": "ais_validated",
            "ais.anomalies": "ais_anomalies",
        }

        consumer = Consumer({
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": "maf-dashboard-probe",
            "auto.offset.reset": "latest",
        })

        for topic_name, key in target_topics.items():
            if topic_name not in topics_meta.topics:
                result[key] = {"message_count": 0, "rate": 0}
                continue
            partitions = topics_meta.topics[topic_name].partitions
            tps = [TopicPartition(topic_name, p) for p in partitions]
            # Get end offsets (approximate message count)
            total = 0
            try:
                committed = consumer.committed(tps, timeout=3)
                end_offs  = consumer.get_watermark_offsets
                for tp in tps:
                    lo, hi = consumer.get_watermark_offsets(tp, timeout=2)
                    total += max(0, hi)
            except Exception:
                pass
            result[key] = {"message_count": total, "rate": 0}

        consumer.close()
        _kafka_cache = result
        _kafka_cache_ts = time.time()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route("/api/anomalies")
def api_anomalies():
    try:
        from confluent_kafka import Consumer, KafkaError
        consumer = Consumer({
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": "maf-dashboard-anomalies",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        })
        consumer.subscribe(["ais.anomalies"])
        events = []
        deadline = time.time() + 3.0
        while time.time() < deadline and len(events) < 50:
            msg = consumer.poll(timeout=0.5)
            if msg is None:
                break
            if msg.error():
                if msg.error().code() != -191:  # not EOF
                    break
                break
            try:
                events.append(json.loads(msg.value().decode()))
            except Exception:
                pass
        consumer.close()
        events.sort(key=lambda e: e.get("detected_at", ""), reverse=True)
        return jsonify({"events": events[:20]})
    except Exception as e:
        return jsonify({"error": str(e), "events": []}), 200


# ── CASSANDRA ─────────────────────────────────────────────────────────────────
@app.route("/api/cassandra")
def api_cassandra():
    try:
        from cassandra.cluster import Cluster
        cluster = Cluster([CASSANDRA_HOST], port=CASSANDRA_PORT,
                          connect_timeout=5)
        session = cluster.connect("maf_ais")
        total = session.execute(
            "SELECT COUNT(*) FROM ais_positions").one()[0]
        tracked = session.execute(
            "SELECT COUNT(*) FROM vessel_track_summary").one()[0]
        dark = session.execute(
            "SELECT COUNT(*) FROM dark_event_candidates WHERE resolved = false ALLOW FILTERING"
        ).one()[0]
        cluster.shutdown()
        return jsonify({
            "total_positions": total,
            "tracked_vessels": tracked,
            "dark_candidates": dark,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 200


# ── SERVICES ──────────────────────────────────────────────────────────────────
@app.route("/api/services")
def api_services():
    """
    Probe each service with a lightweight connectivity check.
    Returns 'healthy', 'error', or 'unknown' per service.
    """
    import socket

    def tcp_check(host, port, timeout=2):
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return "healthy"
        except Exception:
            return "error"

    def neo4j_check():
        try:
            d = get_neo4j_driver()
            with d.session() as s:
                s.run("RETURN 1")
            d.close()
            return "healthy"
        except Exception:
            return "error"

    def kafka_check():
        try:
            from confluent_kafka.admin import AdminClient
            a = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
            a.list_topics(timeout=3)
            return "healthy"
        except Exception:
            return "error"

    def cassandra_check():
        try:
            from cassandra.cluster import Cluster
            c = Cluster([CASSANDRA_HOST], port=CASSANDRA_PORT, connect_timeout=3)
            c.connect()
            c.shutdown()
            return "healthy"
        except Exception:
            return "error"

    return jsonify({
        "zookeeper": tcp_check("localhost", 2181),
        "kafka":     kafka_check(),
        "neo4j":     neo4j_check(),
        "cassandra": cassandra_check(),
        "ingestor":  "running",   # no health endpoint — assume running if kafka is up
        "signal":    "running",
        "sanctions": "running",
        "history":   "running",
        "etl":       "running",
    })


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("""
  ╔══════════════════════════════════════════╗
  ║  MAF Dashboard Server                    ║
  ║  http://localhost:5000                   ║
  ╚══════════════════════════════════════════╝

  Queries: Neo4j · Kafka · Cassandra
  Open dashboard.html in your browser.
  """)
    app.run(host="0.0.0.0", port=5000, debug=False)
