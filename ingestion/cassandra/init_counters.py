"""
MAF — One-time counter seeding script
======================================
Run this ONCE after deploying the dashboard_stats counter approach to seed
the counters with the current state of your existing data.

    python init_counters.py

Safe to re-run: UPDATE ... SET value = %s is idempotent.

WARNING: COUNT(*) on large tables is slow in Cassandra.
If ais_positions has millions of rows, prefer the nodetool estimate instead:

    docker exec -it maf-cassandra nodetool tablestats maf_ais ais_positions

and supply the row count via --total-positions <N>.
"""

import argparse
import logging
import os
import sys
import time

from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import DCAwareRoundRobinPolicy, RetryPolicy
from cassandra.query import ConsistencyLevel

log = logging.getLogger("init-counters")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

CASSANDRA_HOST = os.getenv("CASSANDRA_HOST", "127.0.0.1")
CASSANDRA_PORT = int(os.getenv("CASSANDRA_PORT", "9042"))
CASSANDRA_DC   = os.getenv("CASSANDRA_DC", "datacenter1")
KEYSPACE       = os.getenv("CASSANDRA_KEYSPACE", "maf_ais")


def connect():
    profile = ExecutionProfile(
        load_balancing_policy=DCAwareRoundRobinPolicy(local_dc=CASSANDRA_DC),
        retry_policy=RetryPolicy(),
        consistency_level=ConsistencyLevel.LOCAL_ONE,
    )
    for attempt in range(10):
        try:
            cluster = Cluster(
                [CASSANDRA_HOST],
                port=CASSANDRA_PORT,
                protocol_version=5,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                connect_timeout=15,
            )
            session = cluster.connect(KEYSPACE)
            log.info("Connected to Cassandra at %s:%d / keyspace=%s",
                     CASSANDRA_HOST, CASSANDRA_PORT, KEYSPACE)
            return cluster, session
        except Exception as e:
            log.warning("Not ready (attempt %d/10): %s", attempt + 1, e)
            time.sleep(5)
    raise RuntimeError("Could not connect to Cassandra after 10 attempts")


def main():
    parser = argparse.ArgumentParser(description="Seed dashboard_stats counters")
    parser.add_argument(
        "--total-positions", type=int, default=None,
        help="Skip COUNT(*) on ais_positions and use this value instead "
             "(recommended for large tables).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the counts that would be written without writing them.",
    )
    args = parser.parse_args()

    cluster, session = connect()

    # ── Ensure rows exist ────────────────────────────────────────────────────
    # INSERT IF NOT EXISTS so we never clobber a counter that's already running.
    session.execute(
        "INSERT INTO dashboard_stats (metric, value) VALUES ('total_positions', 0) IF NOT EXISTS"
    )
    session.execute(
        "INSERT INTO dashboard_stats (metric, value) VALUES ('tracked_vessels', 0) IF NOT EXISTS"
    )
    log.info("Ensured both counter rows exist in dashboard_stats.")

    # ── Count tracked vessels ────────────────────────────────────────────────
    log.info("Counting distinct MMSIs in vessel_track_summary …")
    tracked = session.execute("SELECT COUNT(*) FROM vessel_track_summary").one()[0]
    log.info("  tracked_vessels = %d", tracked)

    # ── Count total positions ────────────────────────────────────────────────
    if args.total_positions is not None:
        total = args.total_positions
        log.info("  total_positions = %d  (supplied via --total-positions)", total)
    else:
        log.info("Counting rows in ais_positions — this may take a while on large tables …")
        log.info("  Tip: use --total-positions <N> to skip this scan.")
        total = session.execute("SELECT COUNT(*) FROM ais_positions").one()[0]
        log.info("  total_positions = %d", total)

    # ── Write (or report) ────────────────────────────────────────────────────
    if args.dry_run:
        log.info("DRY RUN — would write: tracked_vessels=%d, total_positions=%d", tracked, total)
    else:
        session.execute(
            "UPDATE dashboard_stats SET value = %s WHERE metric = 'tracked_vessels'",
            (tracked,),
        )
        session.execute(
            "UPDATE dashboard_stats SET value = %s WHERE metric = 'total_positions'",
            (total,),
        )
        log.info("Counters written successfully.")

        # Verify
        rows = session.execute("SELECT metric, value FROM dashboard_stats")
        log.info("Current dashboard_stats:")
        for row in rows:
            log.info("  %-20s = %d", row.metric, row.value)

    cluster.shutdown()
    log.info("Done.")


if __name__ == "__main__":
    main()
