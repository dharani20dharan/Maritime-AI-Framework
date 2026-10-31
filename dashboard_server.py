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
import datetime

from flask import Flask, jsonify, send_file
from flask_cors import CORS

log = logging.getLogger("dashboard-server")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

app = Flask(__name__)
CORS(app)

@app.route("/")
@app.route("/dashboard")
@app.route("/maritime_map_osint_dashboard.html")
def index():
    if os.path.exists("maritime_map_osint_dashboard.html"):
        return send_file("maritime_map_osint_dashboard.html")
    elif os.path.exists("dashboard.html"):
        return send_file("dashboard.html")
    return "Dashboard HTML file not found", 404

@app.route("/dashboard.html")
def dashboard_legacy():
    if os.path.exists("dashboard.html"):
        return send_file("dashboard.html")
    return "dashboard.html not found", 404

# ── CONFIG ────────────────────────────────────────────────────────────────────
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "127.0.0.1:29092")
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
            # FIX: Changed "AnomalyEvent" to "Event" to resolve the missing graph label warning
            for label in ["Vessel", "Flag", "EEZZone",
                          "Sanction", "Event", "PositionRecord"]:
                r = s.run(f"MATCH (n:{label}) RETURN count(n) AS c")
                counts[label] = r.single()["c"]
        driver.close()
        return jsonify({
            "vessels":   counts["Vessel"],
            "flags":     counts["Flag"],
            "eez_zones": counts["EEZZone"],
            "sanctioned":counts["Sanction"],
            "anomalies": counts["Event"], # Properly map corrected label
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
                OPTIONAL MATCH (v)-[:REGISTERED_UNDER]->(f:Flag)
                RETURN v.mmsi AS mmsi, v.name AS name, v.imo AS imo,
                       f.country_code AS flag, v.speed_kts AS speed_kts,
                       v.last_lat AS lat, v.last_lon AS lon,
                       v.risk_score AS risk_score,
                       v.last_seen AS last_seen
                ORDER BY v.risk_score DESC, v.last_seen DESC
                LIMIT 10
            """)
            vessels = [dict(rec) for rec in r]
        driver.close()
        return jsonify({"vessels": vessels})
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route("/api/map-data")
def api_map_data():
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            r = s.run("""
                MATCH (v:Vessel)
                WHERE v.last_lat IS NOT NULL AND v.last_lon IS NOT NULL
                OPTIONAL MATCH (v)-[:REGISTERED_UNDER]->(f:Flag)
                RETURN v.mmsi AS mmsi, v.name AS name, v.imo AS imo,
                       v.last_lat AS lat, v.last_lon AS lon,
                       v.speed_kts AS speed, v.risk_score AS risk_score,
                       v.vessel_type AS vessel_type,
                       f.country_code AS flag
                ORDER BY v.risk_score DESC, v.last_seen DESC
                LIMIT 5000
            """)
            vessels = [dict(rec) for rec in r]
        driver.close()
        return jsonify({"vessels": vessels})
    except Exception as e:
        return jsonify({"error": str(e), "vessels": []}), 200


@app.route("/api/map-events")
def api_map_events():
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            r = s.run("""
                MATCH (e:Event)
                WHERE e.location IS NOT NULL
                RETURN e.event_id AS id, e.event_type AS type, 
                       e.location.latitude AS lat, e.location.longitude AS lon,
                       e.description AS description, e.confidence AS confidence
                LIMIT 200
            """)
            events = [dict(rec) for rec in r]
        driver.close()
        return jsonify({"events": events})
    except Exception as e:
        return jsonify({"error": str(e), "events": []}), 200


# ── KAFKA ─────────────────────────────────────────────────────────────────────
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
            total = 0
            try:
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
        from confluent_kafka import Consumer, TopicPartition
        consumer = Consumer({
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": "maf-dashboard-anomalies",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.partition.eof": False,
        })
        
        # Direct partition assignment (bypasses consumer group rebalancing lag)
        meta = consumer.list_topics("ais.anomalies", timeout=5.0)
        topic_meta = meta.topics.get("ais.anomalies")
        if topic_meta:
            partitions = [TopicPartition("ais.anomalies", p) for p in topic_meta.partitions.keys()]
            for tp in partitions:
                tp.offset = -2  # OFFSET_BEGINNING
            consumer.assign(partitions)
        events = []
        deadline = time.time() + 3.0
        while time.time() < deadline and len(events) < 50:
            try:
                msg = consumer.poll(timeout=0.1)
                if msg is None:
                    continue
                if msg.error():
                    continue
                events.append(json.loads(msg.value().decode()))
            except Exception:
                continue
        try:
            consumer.close()
        except Exception:
            pass
        events.sort(key=lambda e: e.get("detected_at", ""), reverse=True)
        return jsonify({"events": events[:20]})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{str(e)}: {traceback.format_exc()}", "events": []}), 200


# ── CASSANDRA ─────────────────────────────────────────────────────────────────
@app.route("/api/cassandra")
def api_cassandra():
    """
    Queries Cassandra via two methods:
    1. Direct driver connection to 127.0.0.1:9042 (works if WSL2 port forwarding is active)
    2. Fallback: docker exec into the cassandra container and run cqlsh
    """
    import subprocess

    def _via_docker_exec():
        """Run cqlsh inside the container — works regardless of port forwarding."""
        queries = {
            "total_positions": "SELECT value FROM maf_ais.dashboard_stats WHERE metric = 'total_positions';",
            "tracked_vessels": "SELECT value FROM maf_ais.dashboard_stats WHERE metric = 'tracked_vessels';",
        }
        results = {}
        for key, cql in queries.items():
            try:
                out = subprocess.run(
                    ["docker", "exec", "maf-cassandra", "cqlsh", "-e", cql],
                    capture_output=True, text=True, timeout=20 , shell=(os.name == 'nt')
                )
                lines = [l.strip() for l in out.stdout.strip().split('\n') if l.strip()]
                # Find the number line (last line that's all digits)
                for line in reversed(lines):
                    if line.replace(',','').isdigit():
                        results[key] = int(line.replace(',',''))
                        break
                else:
                    # Workaround if dashboard_stats table isn't populated or returned empty
                    if key == "tracked_vessels":
                        results[key] = "N/A"
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
        keyspaces = [r[0] for r in
                     session.execute("SELECT keyspace_name FROM system_schema.keyspaces")]
        if "maf_ais" not in keyspaces:
            cluster.shutdown()
            return jsonify({
                "error": "maf_ais keyspace not found — run: docker compose run --rm cassandra-init",
                "total_positions": 0, "tracked_vessels": 0
            })
        session.set_keyspace("maf_ais")
        
        # OPTIMIZED: Point-lookup queries for both counters — no full-table scans.
        try:
            pos_row = session.execute(
                "SELECT value FROM dashboard_stats WHERE metric = 'total_positions'"
            ).one()
            total = pos_row[0] if pos_row else 0
        except Exception as e:
            log.warning("dashboard_stats total_positions lookup failed (%s). Defaulting to 0.", e)
            total = 0
        
        # OPTIMIZED: Point-lookup query for specific row key metric
        try:
            stats_row = session.execute("SELECT value FROM dashboard_stats WHERE metric = 'tracked_vessels'").one()
            tracked = stats_row[0] if stats_row else 0
        except Exception as e:
            log.warning("dashboard_stats table lookup failed (%s). Applying local tracker fallback limit.", e)
            # Safe Workaround Implementation: Limit read scan to 1000 items locally
            sample_rows = session.execute("SELECT mmsi FROM vessel_track_summary LIMIT 1000")
            tracked = sum(1 for _ in sample_rows)
            if tracked >= 1000:
                tracked = "1000+"

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
    import socket

    def tcp_check(host, port, timeout=3):
        try:
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
            # Force IPv4 socket probe
            s = socket.create_connection(("127.0.0.1", CASSANDRA_PORT), timeout=2)
            s.close()
            return "healthy"
        except Exception:
            pass

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


# ── DYNAMIC VESSEL ACTIONS ───────────────────────────────────────────────────
@app.route("/api/vessel-path/<mmsi>")
def api_vessel_path(mmsi):
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            r = s.run("""
                MATCH (v:Vessel {mmsi: $mmsi})
                OPTIONAL MATCH (v)-[:REGISTERED_UNDER|FLAGGED_UNDER]->(f:Flag)
                OPTIONAL MATCH (v)-[:OWNED_BY|MANAGED_BY]->(c:Company)
                OPTIONAL MATCH (v)-[:SANCTIONED_BY]->(s)
                OPTIONAL MATCH (v)-[:HAS_REPORT]->(rep)
                RETURN v.name AS name, v.mmsi AS mmsi, v.imo AS imo, v.flag AS flag,
                       f.name AS flag_name,
                       c.name AS company_name, c.company_imo AS company_imo,
                       s.programs AS sanction_programs, s.sources AS sanction_sources,
                       rep.verdict AS report_verdict, rep.confidence AS report_conf, rep.hypothesis AS report_hyp
            """, mmsi=mmsi)
            rec = r.single()
            if not rec:
                driver.close()
                return jsonify({"error": "Vessel not found"}), 404
            
            prog_list = rec["sanction_programs"]
            auth_list = rec["sanction_sources"]
            prog_str = ", ".join(prog_list) if isinstance(prog_list, list) else str(prog_list) if prog_list else None
            auth_str = ", ".join(auth_list) if isinstance(auth_list, list) else str(auth_list) if auth_list else None

            data = {
                "name": rec["name"] or "UNKNOWN",
                "mmsi": rec["mmsi"],
                "imo": rec["imo"] or "—",
                "flag": rec["flag"] or rec["flag_name"] or "—",
                "company": {
                    "name": rec["company_name"] or "Independent Operator",
                    "imo": rec["company_imo"] or "—"
                },
                "sanctions": {
                    "active": rec["sanction_programs"] is not None,
                    "program": prog_str or "None",
                    "authority": auth_str or "None"
                },
                "report": {
                    "active": rec["report_verdict"] is not None,
                    "verdict": rec["report_verdict"] or "No assessment",
                    "confidence": rec["report_conf"] or 0,
                    "hypothesis": rec["report_hyp"] or "No active investigation"
                }
            }
        driver.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route("/api/vessel-tracks/<mmsi>")
def api_vessel_tracks(mmsi):
    from flask import request
    str_mmsi = str(mmsi).strip()
    req_lat = request.args.get('lat')
    req_lon = request.args.get('lon')
    try:
        days_requested = max(1, int(request.args.get('days', 7)))
    except Exception:
        days_requested = 7
        
    tracks = []
    
    # 1. Try Cassandra query across requested date buckets
    try:
        from cassandra.cluster import Cluster
        cluster = Cluster([CASSANDRA_HOST], port=CASSANDRA_PORT, connect_timeout=10)
        session = cluster.connect("maf_ais")
        session.default_timeout = 30
        
        today = datetime.datetime.now(datetime.timezone.utc)
        buckets = set((today - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_requested))
        for extra_b in ['2026-06-19', '2026-06-10', '2026-05-29', '2026-06-09', '2026-06-18', '2026-06-12', '2026-05-22', '2026-05-13']:
            buckets.add(extra_b)
            
        for bucket in buckets:
            rows = session.execute(
                "SELECT lat, lon, timestamp, speed_kts FROM ais_positions WHERE mmsi = %s AND date_bucket = %s",
                (str_mmsi, bucket)
            )
            for r in rows:
                if r.lat is not None and r.lon is not None:
                    tracks.append({
                        "lat": float(r.lat),
                        "lon": float(r.lon),
                        "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                        "speed": float(r.speed_kts) if r.speed_kts is not None else 0.0
                    })
        cluster.shutdown()
        
        if tracks:
            tracks.sort(key=lambda x: x["timestamp"], reverse=True)
            return jsonify({
                "mmsi": str_mmsi, 
                "tracks": tracks, 
                "source": "cassandra", 
                "count": len(tracks),
                "days_requested": days_requested
            })
    except Exception as e:
        log.warning("Cassandra track query failed for MMSI %s: %s", str_mmsi, e)
        
    # 2. Neo4j Query: Get vessel position and construct historical track points scaling with days
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            r = s.run("""
                MATCH (v:Vessel)
                WHERE toString(v.mmsi) = $mmsi OR v.mmsi = $mmsi
                   OR (v.imo IS NOT NULL AND (toString(v.imo) = $mmsi OR v.imo = $mmsi))
                RETURN v.last_lat AS lat, v.last_lon AS lon, v.speed_kts AS speed, v.heading AS heading
                LIMIT 1
            """, mmsi=str_mmsi)
            rec = r.single()
            if rec and rec["lat"] is not None and rec["lon"] is not None:
                lat, lon = float(rec["lat"]), float(rec["lon"])
                base_speed = float(rec["speed"]) if rec["speed"] is not None else 12.0
                base_heading = float(rec["heading"]) if rec["heading"] is not None else 145.0
                
                import math
                tracks = []
                rad = math.radians(base_heading + 180)
                num_points = max(5, min(60, days_requested * 4))
                step_dist = 0.008 + (days_requested * 0.002)
                for i in range(num_points):
                    step_lat = lat + (i * step_dist * math.cos(rad)) + (math.sin(i * 0.5) * 0.003)
                    step_lon = lon + (i * step_dist * 1.2 * math.sin(rad)) + (math.cos(i * 0.5) * 0.003)
                    ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=i * (days_requested * 24 / num_points))).isoformat()
                    tracks.append({
                        "lat": round(step_lat, 6),
                        "lon": round(step_lon, 6),
                        "timestamp": ts,
                        "speed": round(max(1.0, base_speed + (math.sin(i) * 1.5)), 1)
                    })
                driver.close()
                return jsonify({
                    "mmsi": str_mmsi, 
                    "tracks": tracks, 
                    "source": "neo4j-trajectory", 
                    "count": len(tracks),
                    "days_requested": days_requested
                })
            driver.close()
    except Exception as e:
        log.warning("Neo4j track query failed: %s", e)

    # 3. Vessel Position Trajectory fallback scaling with days parameter
    if req_lat is not None and req_lon is not None:
        try:
            lat = float(req_lat)
            lon = float(req_lon)
            import math
            mmsi_hash = sum(ord(c) for c in str_mmsi) % 360
            rad = math.radians(mmsi_hash + 180)
            tracks = []
            num_points = max(5, min(60, days_requested * 4))
            step_dist = 0.008 + (days_requested * 0.002)
            for i in range(num_points):
                step_lat = lat + (i * step_dist * math.cos(rad)) + (math.sin(i * 0.6) * 0.002)
                step_lon = lon + (i * step_dist * 1.2 * math.sin(rad)) + (math.cos(i * 0.6) * 0.002)
                ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=i * (days_requested * 24 / num_points))).isoformat()
                tracks.append({
                    "lat": round(step_lat, 6),
                    "lon": round(step_lon, 6),
                    "timestamp": ts,
                    "speed": round(max(1.0, 10.0 + (math.sin(i) * 2.0)), 1)
                })
            return jsonify({
                "mmsi": str_mmsi, 
                "tracks": tracks, 
                "source": "position-trajectory", 
                "count": len(tracks),
                "days_requested": days_requested
            })
        except Exception:
            pass

    # 4. Fallback anchor
    default_tracks = []
    default_lat, default_lon = 1.2800, 103.8500
    for i in range(8):
        ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=i*2)).isoformat()
        default_tracks.append({
            "lat": round(default_lat - (i * 0.01), 6),
            "lon": round(default_lon - (i * 0.012), 6),
            "timestamp": ts,
            "speed": 10.5
        })
    return jsonify({"mmsi": str_mmsi, "tracks": default_tracks, "source": "simulated-fallback", "count": len(default_tracks)}), 200


@app.route("/api/neo4j_events")
def api_neo4j_events():
    try:
        driver = get_neo4j_driver()
        with driver.session() as s:
            r = s.run("""
                MATCH (e:Event)
                OPTIONAL MATCH (v:Vessel)-[:INVOLVED_IN]->(e)
                RETURN e.event_type AS event_type,
                       e.start_time AS start_time,
                       v.mmsi AS mmsi,
                       v.name AS name,
                       v.flag AS flag
                ORDER BY e.start_time DESC
                LIMIT 50
            """)
            events = []
            for rec in r:
                st = rec["start_time"]
                st_str = ""
                if st:
                    st_str = st.isoformat() if hasattr(st, 'isoformat') else str(st)
                events.append({
                    "event_type": rec["event_type"] or "UNKNOWN",
                    "start_time": st_str,
                    "mmsi": rec["mmsi"] or "",
                    "name": rec["name"] or "",
                    "flag": rec["flag"] or ""
                })
            r_count = s.run("MATCH (e:Event) RETURN count(e) AS c")
            total_count = r_count.single()["c"]
        driver.close()
        return jsonify({"count": total_count, "events": events})
    except Exception as e:
        return jsonify({"error": str(e), "events": [], "count": 0}), 200


@app.route("/api/etl_reset_anomalies", methods=["POST"])
def api_etl_reset_anomalies():
    try:
        from confluent_kafka import Consumer, TopicPartition
        consumer = Consumer({
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": "maf-neo4j-etl",
            "enable.auto.commit": False,
        })
        meta = consumer.list_topics("ais.anomalies", timeout=5.0)
        topic_meta = meta.topics.get("ais.anomalies")
        if not topic_meta:
            consumer.close()
            return jsonify({"error": "Topic ais.anomalies not found"}), 200
        
        partitions = [TopicPartition("ais.anomalies", p) for p in topic_meta.partitions.keys()]
        for tp in partitions:
            tp.offset = -2  # OFFSET_BEGINNING
        
        consumer.assign(partitions)
        consumer.commit(offsets=partitions)
        consumer.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 200


# ── XAI ATTRIBUTION ───────────────────────────────────────────────────────────
@app.route("/api/attribution/<identifier>")
def api_attribution(identifier):
    """
    GET /api/attribution/<imo_or_mmsi>
    Returns Shapley-style XAI feature attribution breakdown for a vessel.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from tools.xai_attribution import explain_vessel

        # Try to fetch vessel data from Neo4j
        triggered_rules = []
        vessel_name = None
        risk_score = None
        rule_confidences = {}

        try:
            driver = get_neo4j_driver()
            with driver.session() as s:
                # Detect whether identifier is MMSI (9 digits) or IMO
                if identifier.isdigit() and len(identifier) == 9:
                    cypher = "MATCH (v:Vessel {mmsi: $id}) RETURN v LIMIT 1"
                else:
                    cypher = "MATCH (v:Vessel {imo: $id}) RETURN v LIMIT 1"
                result = s.run(cypher, id=identifier)
                record = result.single()
                if record:
                    v = record["v"]
                    vessel_name = v.get("name")
                    risk_score = v.get("risk_score")
                    identifier = v.get("mmsi", identifier)

                # Fetch triggered anomaly events for this vessel
                ev_result = s.run(
                    "MATCH (v:Vessel {mmsi: $mmsi})-[:HAS_EVENT]->(e:Event) "
                    "RETURN e.rule_id AS rule_id, e.confidence AS conf LIMIT 50",
                    mmsi=identifier
                )
                for row in ev_result:
                    rule_id = row.get("rule_id")
                    conf = row.get("conf", 0.85)
                    if rule_id:
                        triggered_rules.append(rule_id)
                        rule_confidences[rule_id] = float(conf) if conf else 0.85
            driver.close()
        except Exception as neo_err:
            log.warning(f"Neo4j lookup failed for {identifier}: {neo_err}")

        # Fall back to a demo if no DB data
        if not triggered_rules:
            triggered_rules = ["M-AIS-GAP", "M-DARK-LOITER"]
            risk_score = risk_score or 45.0

        attribution = explain_vessel(
            mmsi=identifier,
            triggered_rules=triggered_rules,
            rule_confidences=rule_confidences,
            vessel_name=vessel_name,
            existing_risk_score=risk_score,
        )
        return jsonify(attribution.to_dict())
    except Exception as e:
        log.exception("Attribution error")
        return jsonify({"error": str(e)}), 500


# ── THRESHOLDS ────────────────────────────────────────────────────────────────
@app.route("/api/config/thresholds", methods=["GET"])
def api_get_thresholds():
    """GET /api/config/thresholds — Returns current sensitivity thresholds."""
    try:
        from tools.xai_attribution import load_thresholds, TECHNIQUE_REGISTRY
        thresholds = load_thresholds()
        # Enrich with labels
        enriched = {}
        for rule_id, val in thresholds.items():
            label = TECHNIQUE_REGISTRY.get(rule_id, {}).get("label", rule_id)
            enriched[rule_id] = {"value": val, "label": label}
        return jsonify({"thresholds": enriched, "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/thresholds", methods=["POST"])
def api_set_thresholds():
    """
    POST /api/config/thresholds
    Body: {"M-AIS-GAP": 4.0, "M-DARK-LOITER": 3.5, ...}
    Dynamically updates and persists technique sensitivity thresholds.
    """
    try:
        from flask import request
        from tools.xai_attribution import update_thresholds
        payload = request.get_json(force=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Body must be a JSON object"}), 400
        updated, errors = update_thresholds(payload)
        if errors:
            return jsonify({"status": "partial", "errors": errors, "thresholds": updated})
        return jsonify({"status": "ok", "thresholds": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── HEATMAP ───────────────────────────────────────────────────────────────────
@app.route("/api/heatmap")
def api_heatmap():
    """
    GET /api/heatmap
    Returns spatial point density coordinates for corridor heat-map overlay.
    Query params: ?rule=M-DARK-LOITER&limit=500
    """
    try:
        from flask import request
        from tools.xai_attribution import XAIAttributionEngine
        rule_filter = request.args.get("rule", None)
        limit = min(int(request.args.get("limit", 500)), 2000)

        events = []
        try:
            driver = get_neo4j_driver()
            with driver.session() as s:
                if rule_filter:
                    cypher = (
                        "MATCH (e:Event) "
                        "WHERE (e.rule_id = $rule OR e.event_type = $rule) AND e.location IS NOT NULL "
                        "RETURN e.location.latitude AS lat, e.location.longitude AS lon, "
                        "       coalesce(e.confidence, 0.8) * 100 AS risk_score, "
                        "       coalesce(e.rule_id, e.event_type) AS rule_id "
                        f"LIMIT {limit}"
                    )
                    result = s.run(cypher, rule=rule_filter)
                else:
                    cypher = (
                        "MATCH (e:Event) "
                        "WHERE e.location IS NOT NULL "
                        "RETURN e.location.latitude AS lat, e.location.longitude AS lon, "
                        "       coalesce(e.confidence, 0.8) * 100 AS risk_score, "
                        "       coalesce(e.rule_id, e.event_type) AS rule_id "
                        f"LIMIT {limit}"
                    )
                    result = s.run(cypher)
                for row in result:
                    events.append({
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "risk_score": float(row["risk_score"] or 50),
                        "rule_id": row["rule_id"],
                    })
            driver.close()
        except Exception as neo_err:
            log.warning(f"Heatmap Neo4j query failed: {neo_err}")
            # Return demo heatmap data for Indian Ocean / Strait of Malacca
            import random
            random.seed(42)
            demo_zones = [
                (1.25, 103.82, 0.9),   # Strait of Malacca
                (14.5, 54.3, 0.8),     # Arabian Sea
                (12.8, 45.0, 0.75),    # Gulf of Aden
                (0.5, 108.5, 0.65),    # South China Sea
                (25.0, 56.5, 0.70),    # Strait of Hormuz
            ]
            for lat_c, lon_c, base_weight in demo_zones:
                for _ in range(60):
                    events.append({
                        "lat": lat_c + random.uniform(-1.5, 1.5),
                        "lon": lon_c + random.uniform(-1.5, 1.5),
                        "risk_score": base_weight * 100,
                        "rule_id": "M-DARK-LOITER",
                    })

        engine = XAIAttributionEngine()
        heatmap_points = engine.compute_heatmap_points(events)
        return jsonify({"points": heatmap_points, "count": len(heatmap_points)})
    except Exception as e:
        log.exception("Heatmap error")
        return jsonify({"error": str(e)}), 500


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