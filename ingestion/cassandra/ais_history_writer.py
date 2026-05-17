"""
MAF — AIS History Writer (Engineer A, Cassandra)

Consumes ais.validated from Kafka.
Writes every position report into Cassandra with a 90-day TTL.
Also maintains the vessel_track_summary table with the latest position.

Design notes:
  - Partition key (mmsi, date_bucket) keeps per-day partitions small (~144 rows
    at 6 pings/hr). Cassandra performs best with partitions under 100MB.
  - TTL of 7,776,000 seconds (90 days) is applied at the row level so Cassandra
    handles expiry automatically via TimeWindowCompactionStrategy.
  - Batching: 200 statements per batch, flushed every 5 seconds at most.
    Cassandra batches are for atomicity, not performance — keep them small.
  - The writer is idempotent: re-inserting the same (mmsi, date_bucket, timestamp)
    is a no-op at the storage level (Cassandra LWT not needed here).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import DCAwareRoundRobinPolicy, RetryPolicy
from cassandra.query import BatchStatement, PreparedStatement, ConsistencyLevel
from confluent_kafka import Consumer, KafkaError

log = logging.getLogger("ais-history-writer")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

# ── CONFIG ────────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP  = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
KAFKA_TOPIC      = os.getenv("KAFKA_INPUT_TOPIC", "ais.validated")
KAFKA_GROUP_ID   = os.getenv("KAFKA_GROUP_ID", "maf-cassandra-writer")
CASSANDRA_HOST   = os.getenv("CASSANDRA_HOST", "cassandra")
CASSANDRA_PORT   = int(os.getenv("CASSANDRA_PORT", "9042"))
KEYSPACE         = os.getenv("CASSANDRA_KEYSPACE", "maf_ais")
TTL_DAYS         = int(os.getenv("HISTORY_TTL_DAYS", "90"))
TTL_SECONDS      = TTL_DAYS * 86400
BATCH_SIZE       = int(os.getenv("BATCH_SIZE", "200"))
FLUSH_INTERVAL_S = 5.0   # max seconds between flushes regardless of batch size


# ── CASSANDRA SESSION ─────────────────────────────────────────────────────────

def connect_cassandra():
    """Connect with retry — Cassandra takes ~60s to start."""
    profile = ExecutionProfile(
        load_balancing_policy=DCAwareRoundRobinPolicy(local_dc="dc1"),
        retry_policy=RetryPolicy(),
        consistency_level=ConsistencyLevel.LOCAL_ONE,
    )
    for attempt in range(20):
        try:
            cluster = Cluster(
                [CASSANDRA_HOST],
                port=CASSANDRA_PORT,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                connect_timeout=15,
            )
            session = cluster.connect(KEYSPACE)
            log.info("Connected to Cassandra at %s:%d / keyspace=%s",
                     CASSANDRA_HOST, CASSANDRA_PORT, KEYSPACE)
            return session
        except Exception as e:
            log.warning("Cassandra not ready (attempt %d/20): %s", attempt + 1, e)
            time.sleep(10)
    raise RuntimeError("Could not connect to Cassandra after 20 attempts")


# ── PREPARED STATEMENTS ───────────────────────────────────────────────────────

INSERT_POSITION = """
INSERT INTO ais_positions (
  mmsi, date_bucket, timestamp, imo, vessel_name,
  lat, lon, speed_kts, heading, nav_status,
  flag, draught_m, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
USING TTL {ttl}
""".format(ttl=TTL_SECONDS)

UPSERT_SUMMARY = """
INSERT INTO vessel_track_summary (
  mmsi, imo, vessel_name, flag,
  last_lat, last_lon, last_speed_kts, last_heading,
  last_seen, first_seen
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_DARK_CANDIDATE = """
INSERT INTO dark_event_candidates (
  mmsi, event_start, last_known_lat, last_known_lon,
  last_known_speed, silence_hours, eez_zone_id, resolved
) VALUES (?, ?, ?, ?, ?, ?, ?, false)
USING TTL {ttl}
""".format(ttl=TTL_SECONDS)


# ── DARK EVENT TRACKER ────────────────────────────────────────────────────────

class DarkEventTracker:
    """
    Tracks per-vessel silence. When a vessel hasn't transmitted for more than
    DARK_THRESHOLD_HOURS, writes a dark_event_candidate record to Cassandra.
    """
    DARK_THRESHOLD_HOURS = 2.0

    def __init__(self, session, insert_dark_stmt):
        self.last_seen: dict[str, datetime] = {}
        self.last_pos: dict[str, dict] = {}
        self.reported: set[str] = set()
        self.session = session
        self.stmt = insert_dark_stmt

    def update(self, mmsi: str, ts: datetime, lat: float, lon: float, speed: float):
        self.last_seen[mmsi] = ts
        self.last_pos[mmsi] = {"lat": lat, "lon": lon, "speed": speed}
        # If vessel reappears after being flagged, clear the flag
        self.reported.discard(mmsi)

    def check_all(self):
        """Call periodically to detect newly silent vessels."""
        now = datetime.now(timezone.utc)
        for mmsi, last_ts in list(self.last_seen.items()):
            silence_h = (now - last_ts).total_seconds() / 3600
            if silence_h >= self.DARK_THRESHOLD_HOURS and mmsi not in self.reported:
                self.reported.add(mmsi)
                pos = self.last_pos.get(mmsi, {})
                try:
                    self.session.execute(self.stmt, (
                        mmsi, last_ts,
                        pos.get("lat"), pos.get("lon"),
                        pos.get("speed"), round(silence_h, 2),
                        None,   # eez_zone_id — enriched by graph layer
                    ))
                    log.info("Dark event candidate: mmsi=%s silence=%.1fh", mmsi, silence_h)
                except Exception as e:
                    log.warning("Failed to write dark event for %s: %s", mmsi, e)


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def date_bucket(ts_iso: str) -> str:
    """Extract YYYY-MM-DD date bucket from ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run():
    session = connect_cassandra()

    # Prepare statements
    pos_stmt  = session.prepare(INSERT_POSITION)
    sum_stmt  = session.prepare(UPSERT_SUMMARY)
    dark_stmt = session.prepare(INSERT_DARK_CANDIDATE)

    tracker = DarkEventTracker(session, dark_stmt)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": KAFKA_GROUP_ID,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([KAFKA_TOPIC])

    log.info("AIS history writer started — consuming %s → Cassandra (TTL=%d days)",
             KAFKA_TOPIC, TTL_DAYS)

    batch        = BatchStatement()
    batch_count  = 0
    last_flush   = time.time()
    total_written = 0
    last_dark_check = time.time()

    try:
        while True:
            msg = consumer.poll(timeout=1.0)

            # Periodic dark event check every 60 seconds
            if time.time() - last_dark_check > 60:
                tracker.check_all()
                last_dark_check = time.time()

            if msg is None:
                # Flush on idle if we have pending rows
                if batch_count > 0 and time.time() - last_flush > FLUSH_INTERVAL_S:
                    session.execute(batch)
                    consumer.commit(asynchronous=True)
                    total_written += batch_count
                    log.debug("Flushed %d rows (idle) — total %d", batch_count, total_written)
                    batch = BatchStatement()
                    batch_count = 0
                    last_flush = time.time()
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka error: %s", msg.error())
                continue

            try:
                env = json.loads(msg.value().decode())
            except json.JSONDecodeError:
                continue

            mmsi = env.get("mmsi")
            ts   = env.get("timestamp")
            lat  = env.get("lat")
            lon  = env.get("lon")

            # Only write rows with position data
            if not mmsi or lat is None or lon is None:
                # Still update summary for static messages (name, flag, IMO)
                if mmsi and env.get("imo"):
                    batch.add(sum_stmt, (
                        mmsi, env.get("imo"), env.get("name"), env.get("flag"),
                        None, None, None, None,
                        None, None
                    ))
                    batch_count += 1
                continue

            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(timezone.utc)
            bucket = date_bucket(ts)

            # Position row (with 90-day TTL)
            batch.add(pos_stmt, (
                mmsi,
                bucket,
                ts_dt,
                env.get("imo"),
                env.get("name"),
                float(lat),
                float(lon),
                float(env.get("speed_kts") or 0),
                int(env.get("heading") or 511),
                int(env.get("nav_status") or 0),
                env.get("flag"),
                float(env.get("draught_m") or 0),
                env.get("source", "AIS_LIVE"),
            ))
            batch_count += 1

            # Summary upsert (no TTL — keeps last known position permanently)
            batch.add(sum_stmt, (
                mmsi,
                env.get("imo"),
                env.get("name"),
                env.get("flag"),
                float(lat),
                float(lon),
                float(env.get("speed_kts") or 0),
                int(env.get("heading") or 511),
                ts_dt,
                ts_dt,   # first_seen — Cassandra LWT would be more precise but costly
            ))
            batch_count += 1

            # Update dark event tracker
            tracker.update(mmsi, ts_dt, float(lat), float(lon),
                           float(env.get("speed_kts") or 0))

            # Flush when batch is full or time limit exceeded
            if batch_count >= BATCH_SIZE or time.time() - last_flush > FLUSH_INTERVAL_S:
                session.execute(batch)
                consumer.commit(asynchronous=True)
                total_written += batch_count
                if total_written % 5000 == 0:
                    log.info("Total rows written to Cassandra: %d", total_written)
                batch = BatchStatement()
                batch_count = 0
                last_flush = time.time()

    except KeyboardInterrupt:
        if batch_count > 0:
            session.execute(batch)
        log.info("Shutdown — %d total rows written", total_written)
    finally:
        consumer.close()
        session.cluster.shutdown()


if __name__ == "__main__":
    run()
