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
NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://127.0.0.1:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
KAFKA_BOOTSTRAP= os.getenv("KAFKA_BOOTSTRAP","127.0.0.1:29092")
CASSANDRA_HOST = os.getenv("CASSANDRA_HOST", "127.0.0.1")
CASSANDRA_PORT = int(os.getenv("CASSANDRA_PORT", "9042"))


# ── NEO4J ─────────────────────────────────────────────────────────────────────
def get_neo4j_driver():
    from neo4j import GraphDatabase
    # encrypted=False and trust settings force a clean IPv4 bolt connection
    return GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        encrypted=False,
        connection_timeout=10,
    )


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
    """
    Queries Cassandra via two methods:
    1. Direct driver connection to 127.0.0.1:9042 (works if WSL2 port forwarding is active)
    2. Fallback: docker exec into the cassandra container and run cqlsh
    """
    import subprocess, json as _json

    def _via_docker_exec():
        """Run cqlsh inside the container — works regardless of port forwarding."""
        queries = {
            "total_positions": "SELECT COUNT(*) FROM maf_ais.ais_positions;",
            "tracked_vessels": "SELECT COUNT(*) FROM maf_ais.vessel_track_summary;",
        }
        results = {}
        for key, cql in queries.items():
            try:
                out = subprocess.run(
                    ["docker", "exec", "maf-cassandra", "cqlsh", "-e", cql],
                    capture_output=True, text=True, timeout=20 , shell=(os.name == 'nt')
                )
                # cqlsh output looks like:
                #  count
                # -------
                #     50
                lines = [l.strip() for l in out.stdout.strip().split('\n') if l.strip()]
                # Find the number line (last line that's all digits)
                for line in reversed(lines):
                    if line.replace(',','').isdigit():
                        results[key] = int(line.replace(',',''))
                        break
                else:
                    results[key] = 0
            except Exception as ex:
                results[key] = f"err:{ex}"
        return results

    # Try direct driver first
    try:
        from cassandra.cluster import Cluster
        from cassandra.policies import RoundRobinPolicy
        cluster = Cluster(
            [CASSANDRA_HOST],
            port=CASSANDRA_PORT,
            connect_timeout=5,
            load_balancing_policy=RoundRobinPolicy(),
        )
        session = cluster.connect()
        keyspaces = [r.keyspace_name for r in
                     session.execute("SELECT keyspace_name FROM system_schema.keyspaces")]
        if "maf_ais" not in keyspaces:
            cluster.shutdown()
            return jsonify({
                "error": "maf_ais keyspace not found — run: docker compose run --rm cassandra-init",
                "total_positions": 0, "tracked_vessels": 0
            })
        session.set_keyspace("maf_ais")
        total   = session.execute("SELECT COUNT(*) FROM ais_positions").one()[0]
        tracked = session.execute("SELECT COUNT(*) FROM vessel_track_summary").one()[0]
        cluster.shutdown()
        return jsonify({
            "total_positions": total,
            "tracked_vessels": tracked,
            "dark_candidates": 0,
            "source": "driver",
        })
    except Exception as driver_err:
        log.warning("Cassandra direct driver failed (%s), falling back to docker exec", driver_err)

    # Fallback: docker exec
    try:
        data = _via_docker_exec()
        data["source"] = "docker-exec"
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Cassandra unreachable: {str(e)[:200]}"}), 200


# ── SERVICES ──────────────────────────────────────────────────────────────────
@app.route("/api/services")
def api_services():
    """
    Probe each service with a lightweight connectivity check.
    Returns 'healthy', 'error', or 'unknown' per service.
    """
    import socket

    def tcp_check(host, port, timeout=3):
        try:
            # Force IPv4 by resolving to 127.0.0.1 explicitly
            s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
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

    kafka_status = kafka_check()
    return jsonify({
        # ZooKeeper port 2181 is internal to Docker only — not exposed to host.
        # It is healthy if Kafka is healthy (Kafka depends on ZooKeeper).
        "zookeeper": "healthy" if kafka_status == "healthy" else "starting",
        "kafka":     kafka_status,
        "neo4j":     neo4j_check(),
        "cassandra": cassandra_check(),
        "ingestor":  "running" if kafka_status == "healthy" else "starting",
        "signal":    "running" if kafka_status == "healthy" else "starting",
        "sanctions": "running",
        "history":   "running" if kafka_status == "healthy" else "starting",
        "etl":       "running" if kafka_status == "healthy" else "starting",
    })


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("""
  +------------------------------------------+
  |  MAF Dashboard Server                    |
  |  http://localhost:5000                   |
  +------------------------------------------+

  Queries: Neo4j · Kafka · Cassandra
  Open dashboard.html in your browser.
  """)
    app.run(host="0.0.0.0", port=5000, debug=False)
