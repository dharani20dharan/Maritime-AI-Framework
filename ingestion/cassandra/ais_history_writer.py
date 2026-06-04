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
  - Write strategy: individual execute_async() calls (not BatchStatement).
    Cassandra batches are for atomicity across partitions, NOT for throughput.
    Batching multi-partition writes (different mmsi values) triggers coordinator
    overhead and hits the 50 KB batch size guard. Async individual writes are
    faster, simpler, and never hit batch size limits.
  - Concurrency: up to MAX_IN_FLIGHT futures outstanding at once.  When the
    queue is full we block briefly, which also provides natural back-pressure
    against a fast Kafka topic.
  - The writer is idempotent: re-inserting the same (mmsi, date_bucket,
    timestamp) is a no-op at the storage level.
"""

import json
import logging
import os
import re as _re
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import DCAwareRoundRobinPolicy, RetryPolicy
from cassandra.query import PreparedStatement, ConsistencyLevel
from confluent_kafka import Consumer, KafkaError

log = logging.getLogger("ais-history-writer")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

# ── CONFIG ────────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
KAFKA_TOPIC       = os.getenv("KAFKA_INPUT_TOPIC", "ais.validated")
KAFKA_GROUP_ID    = os.getenv("KAFKA_GROUP_ID", "maf-cassandra-writer")
CASSANDRA_HOST    = os.getenv("CASSANDRA_HOST", "cassandra")
CASSANDRA_PORT    = int(os.getenv("CASSANDRA_PORT", "9042"))
CASSANDRA_DC      = os.getenv("CASSANDRA_DC", "datacenter1")   # Docker default
KEYSPACE          = os.getenv("CASSANDRA_KEYSPACE", "maf_ais")
TTL_DAYS          = int(os.getenv("HISTORY_TTL_DAYS", "90"))
TTL_SECONDS       = TTL_DAYS * 86400
# Max outstanding async futures before we pause and drain.
# Higher = more throughput; lower = less memory pressure.
MAX_IN_FLIGHT     = int(os.getenv("CASSANDRA_MAX_IN_FLIGHT", "512"))
COMMIT_INTERVAL_S = 5.0   # commit Kafka offsets at most this often
LOG_INTERVAL      = 5000  # log progress every N rows


# ── CASSANDRA SESSION ─────────────────────────────────────────────────────────

def connect_cassandra():
    """Connect with retry — Cassandra takes ~60 s to start in Docker."""
    profile = ExecutionProfile(
        load_balancing_policy=DCAwareRoundRobinPolicy(local_dc=CASSANDRA_DC),
        retry_policy=RetryPolicy(),
        consistency_level=ConsistencyLevel.LOCAL_ONE,
    )
    for attempt in range(20):
        try:
            cluster = Cluster(
                [CASSANDRA_HOST],
                port=CASSANDRA_PORT,
                protocol_version=5,          # pin — avoids noisy negotiation warnings
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

INCR_TOTAL_POSITIONS = """
UPDATE dashboard_stats
SET value = value + 1
WHERE metric = 'total_positions'
"""

INCR_TRACKED_VESSELS = """
UPDATE dashboard_stats
SET value = value + 1
WHERE metric = 'tracked_vessels'
"""

INSERT_DARK_CANDIDATE = """
INSERT INTO dark_event_candidates (
  mmsi, event_start, last_known_lat, last_known_lon,
  last_known_speed, silence_hours, eez_zone_id, resolved
) VALUES (?, ?, ?, ?, ?, ?, ?, false)
USING TTL {ttl}
""".format(ttl=TTL_SECONDS)


# ── TIMESTAMP PARSER ──────────────────────────────────────────────────────────

def parse_ts(ts: str) -> datetime:
    """
    Parse a timestamp string into an aware datetime, handling multiple formats:

      • Standard ISO-8601 / RFC-3339  — "2026-05-25T06:30:59.123456+00:00"
      • ISO with Z suffix             — "2026-05-25T06:30:59.123456Z"
      • Go time.String() format       — "2026-05-25 06:30:59.825701693 +0000 UTC"
        (space-separated, nanosecond precision, trailing " UTC" label)

    Always returns a UTC-aware datetime.  Falls back to now() on any parse error.
    """
    if not ts:
        return datetime.now(timezone.utc)
    try:
        s = ts.strip()
        if s.endswith(" UTC"):           # strip Go's trailing " UTC" label
            s = s[:-4].strip()
        s = _re.sub(r' (\+|-)', r'T\1', s, count=1)   # space-before-offset → T
        s = _re.sub(r'(\.\d{6})\d+', r'\1', s)         # nanoseconds → microseconds
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = _re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)  # +0000 → +00:00
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        log.debug("Could not parse timestamp %r — using now()", ts)
        return datetime.now(timezone.utc)


def date_bucket(ts: str) -> str:
    """Extract YYYY-MM-DD date bucket from a timestamp string."""
    return parse_ts(ts).strftime("%Y-%m-%d")


# ── DARK EVENT TRACKER ────────────────────────────────────────────────────────

class DarkEventTracker:
    """
    Tracks per-vessel silence.  When a vessel hasn't transmitted for more than
    DARK_THRESHOLD_HOURS, writes a dark_event_candidate record to Cassandra.
    """
    DARK_THRESHOLD_HOURS = 2.0

    def __init__(self, session, insert_dark_stmt):
        self.last_seen: dict[str, datetime] = {}
        self.last_pos:  dict[str, dict]     = {}
        self.reported:  set[str]            = set()
        self.session = session
        self.stmt    = insert_dark_stmt

    def update(self, mmsi: str, ts: datetime, lat: float, lon: float, speed: float):
        self.last_seen[mmsi] = ts
        self.last_pos[mmsi]  = {"lat": lat, "lon": lon, "speed": speed}
        self.reported.discard(mmsi)   # vessel reappeared — clear flag

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


# ── ASYNC WRITE HELPER ────────────────────────────────────────────────────────

class AsyncWriter:
    """
    Wraps execute_async() with a bounded in-flight queue.

    Why not BatchStatement?
    -----------------------
    Cassandra's batch guard (batch_size_fail_threshold_in_kb = 50 KB by default)
    applies to the *serialised size* of all statements in a batch.  With 200
    statements × ~400 bytes each we routinely exceed 50 KB and crash with
    "Batch too large".  Individual async writes have no such limit, are faster
    (no coordinator fan-out overhead), and provide better partition-level
    load balancing.
    """

    def __init__(self, session, max_in_flight: int = MAX_IN_FLIGHT):
        self._session       = session
        self._max           = max_in_flight
        self._futures: list = []
        self._errors        = 0

    def write(self, stmt, params):
        """Submit one async write.  Blocks if the in-flight queue is full."""
        if len(self._futures) >= self._max:
            self._drain()
        self._futures.append(self._session.execute_async(stmt, params))

    def flush(self):
        """Wait for all outstanding futures to complete."""
        self._drain(wait_all=True)

    def _drain(self, wait_all: bool = False):
        """Collect completed futures; if wait_all, block until queue is empty."""
        limit = 0 if wait_all else self._max // 2
        while len(self._futures) > limit:
            fut = self._futures.pop(0)
            try:
                fut.result()
            except Exception as e:
                self._errors += 1
                log.warning("Cassandra write error (%d total): %s", self._errors, e)

    @property
    def error_count(self):
        return self._errors


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    session = connect_cassandra()

    pos_stmt   = session.prepare(INSERT_POSITION)
    sum_stmt   = session.prepare(UPSERT_SUMMARY)
    dark_stmt  = session.prepare(INSERT_DARK_CANDIDATE)
    incr_pos_stmt     = session.prepare(INCR_TOTAL_POSITIONS)
    incr_vessel_stmt  = session.prepare(INCR_TRACKED_VESSELS)

    tracker = DarkEventTracker(session, dark_stmt)
    writer  = AsyncWriter(session, max_in_flight=MAX_IN_FLIGHT)

    # In-process set of MMSIs seen since startup.
    # Used to avoid double-counting tracked_vessels for repeat messages.
    # NOTE: This does NOT survive restarts.  On a cold start a new MMSI that
    # was already in vessel_track_summary will increment the counter once more.
    # That's acceptable drift; the counter can be reseeded via init_counters.py
    # whenever exact accuracy is needed.
    seen_mmsis: set[str] = set()

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id":          KAFKA_GROUP_ID,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([KAFKA_TOPIC])

    log.info("AIS history writer started — consuming %s → Cassandra (TTL=%d days, max_in_flight=%d)",
             KAFKA_TOPIC, TTL_DAYS, MAX_IN_FLIGHT)

    total_written   = 0
    last_commit     = time.time()
    last_dark_check = time.time()
    pending_commit  = False

    try:
        while True:
            msg = consumer.poll(timeout=1.0)

            # Periodic dark event check every 60 seconds
            if time.time() - last_dark_check > 60:
                tracker.check_all()
                last_dark_check = time.time()

            if msg is None:
                # On idle: flush any buffered futures and commit Kafka offsets
                if pending_commit and time.time() - last_commit > COMMIT_INTERVAL_S:
                    writer.flush()
                    consumer.commit(asynchronous=True)
                    pending_commit = False
                    last_commit = time.time()
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

            # Static message (no position) — update summary with identity fields only
            if not mmsi or lat is None or lon is None:
                if mmsi and env.get("imo"):
                    writer.write(sum_stmt, (
                        mmsi, env.get("imo"), env.get("name"), env.get("flag"),
                        None, None, None, None,
                        None, None,
                    ))
                    total_written += 1
                    pending_commit = True
                continue

            ts_dt  = parse_ts(ts)
            bucket = date_bucket(ts)

            # Position row (90-day TTL)
            writer.write(pos_stmt, (
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
            # Increment total_positions counter for every position row written.
            writer.write(incr_pos_stmt, ())

            # Summary upsert (no TTL — keeps last known position permanently)
            writer.write(sum_stmt, (
                mmsi,
                env.get("imo"),
                env.get("name"),
                env.get("flag"),
                float(lat),
                float(lon),
                float(env.get("speed_kts") or 0),
                int(env.get("heading") or 511),
                ts_dt,
                ts_dt,
            ))
            # Increment tracked_vessels only the first time we see this MMSI
            # within the current process lifetime (Option A).
            if mmsi not in seen_mmsis:
                seen_mmsis.add(mmsi)
                writer.write(incr_vessel_stmt, ())

            total_written += 2  # pos_stmt + sum_stmt (counter writes not counted)
            pending_commit = True

            tracker.update(mmsi, ts_dt, float(lat), float(lon),
                           float(env.get("speed_kts") or 0))

            if total_written % LOG_INTERVAL < 2:
                log.info("Rows written to Cassandra: %d (async errors: %d)",
                         total_written, writer.error_count)

            # Commit Kafka offsets periodically (not per-message — expensive)
            if pending_commit and time.time() - last_commit > COMMIT_INTERVAL_S:
                writer.flush()
                consumer.commit(asynchronous=True)
                pending_commit = False
                last_commit = time.time()

    except KeyboardInterrupt:
        writer.flush()
        if pending_commit:
            consumer.commit(asynchronous=False)
        log.info("Shutdown — %d total rows written (async errors: %d)",
                 total_written, writer.error_count)
    finally:
        consumer.close()
        session.cluster.shutdown()


if __name__ == "__main__":
    run()
