-- =============================================================================
--  MAF — Cassandra Schema
--  Keyspace : maf_ais
--  Purpose  : 90-day rolling AIS position history store
--  Engineer A owns this file.
--  Run via  : docker compose run --rm cassandra-init
-- =============================================================================


-- -----------------------------------------------------------------------------
--  KEYSPACE
--  SimpleStrategy is correct for a single-datacenter local deployment.
--  For multi-DC production: switch to NetworkTopologyStrategy and set a
--  replication factor per DC.
-- -----------------------------------------------------------------------------
CREATE KEYSPACE IF NOT EXISTS maf_ais
WITH replication = {
    'class'              : 'SimpleStrategy',
    'replication_factor' : 1
}
AND durable_writes = true;

USE maf_ais;


-- =============================================================================
--  TABLE 1 : ais_positions
--  The primary time-series ledger. Every AIS position report is written here.
--
--  PARTITION KEY : (mmsi, date_bucket)
--  ─────────────────────────────────
--  mmsi         : Groups all pings for one vessel on the same Cassandra node.
--
--  date_bucket  : A YYYY-MM-DD string (e.g. '2026-05-13'). Limits partition
--                 growth. Without bucketing, a 90-day partition for one active
--                 vessel accumulates ~12,960 rows (6 pings/hr × 24 × 90),
--                 which is manageable but leads to uneven compaction. With a
--                 daily bucket each partition holds ~144 rows — small, fast,
--                 and compacted efficiently by TimeWindowCompactionStrategy.
--
--  CLUSTERING KEY : timestamp DESC
--  ──────────────────────────────
--  Most queries want the newest positions first (track replay, last-known
--  position checks). DESC ordering means the storage engine returns the most
--  recent rows without a sort step.
--
--  TTL : 7,776,000 seconds = exactly 90 days
--  ──────────────────────────────────────────
--  Applied at write time using USING TTL in each INSERT statement.
--  Cassandra's TTL mechanism marks rows as expired and the compaction
--  process removes them automatically — no cron job or manual deletion needed.
--  TimeWindowCompactionStrategy is the correct compaction strategy for
--  TTL-heavy time-series workloads; it groups SSTables by time window
--  (one day here) so expired data is dropped in whole-file drops rather than
--  row-level scans.
--
--  WHY NOT Neo4j for this?
--  ───────────────────────
--  Neo4j PositionRecord nodes would grow to ~1.2 million rows per vessel over
--  90 days at 6 pings/hr. Graph databases are optimised for relationship
--  traversal, not time-ordered sequential scans. Cassandra handles this write
--  pattern — high volume, time-ordered, TTL-bounded — with no performance
--  penalty and no schema changes required as volume scales.
-- =============================================================================

CREATE TABLE IF NOT EXISTS ais_positions (

    -- Partition key
    mmsi            text,
    date_bucket     text,        -- YYYY-MM-DD

    -- Clustering key
    timestamp       timestamp,   -- UTC, clustered DESC

    -- Position fields (may be null for static-data messages)
    lat             double,
    lon             double,
    speed_kts       float,
    heading         int,         -- 0–359 true; 511 = not available
    nav_status      int,         -- AIS navigational status 0–15

    -- Vessel identity (denormalised for query convenience)
    imo             text,
    vessel_name     text,
    flag            text,        -- ISO 3166-1 alpha-2
    draught_m       float,

    -- Provenance
    source          text,        -- AIS_LIVE | SAR_FUSED | REGISTRY

    PRIMARY KEY ( (mmsi, date_bucket), timestamp )

) WITH CLUSTERING ORDER BY ( timestamp DESC )
  AND default_time_to_live = 7776000
  AND compaction = {
      'class'                  : 'TimeWindowCompactionStrategy',
      'compaction_window_unit' : 'DAYS',
      'compaction_window_size' : 1
  }
  AND comment = 'Rolling 90-day AIS position history. Partitioned by vessel + day, clustered newest-first. Auto-expires via TTL.';


-- =============================================================================
--  TABLE 2 : vessel_track_summary
--  Lightweight last-known-position table. Updated on every position write.
--  No TTL — this is a persistent summary, not raw history.
--  Allows instant single-row lookup of any vessel's current state.
-- =============================================================================

CREATE TABLE IF NOT EXISTS vessel_track_summary (

    mmsi            text PRIMARY KEY,

    -- Latest identity snapshot
    imo             text,
    vessel_name     text,
    flag            text,

    -- Latest position
    last_lat        double,
    last_lon        double,
    last_speed_kts  float,
    last_heading    int,

    -- Timing
    last_seen       timestamp,
    first_seen      timestamp

) WITH comment = 'Per-vessel latest position and identity. No TTL — persistent summary.';


-- =============================================================================
--  TABLE 3 : dark_event_candidates
--  Written by the AIS history writer when a vessel stops transmitting for
--  more than the configured silence threshold (default 2 hours).
--  Queried by the Stage 4 critic layer when assessing M-DARK-EVENT severity.
--  TTL matches the position history — candidates expire after 90 days.
-- =============================================================================

CREATE TABLE IF NOT EXISTS dark_event_candidates (

    mmsi                text,
    event_start         timestamp,   -- UTC timestamp when silence began

    -- Last known state before going dark
    last_known_lat      double,
    last_known_lon      double,
    last_known_speed    float,
    silence_hours       float,       -- duration at time of detection

    -- Context (enriched by graph layer if available)
    eez_zone_id         text,        -- MRGID of zone where vessel went dark

    -- Resolution (updated when vessel reappears)
    resolved            boolean,
    resolution_lat      double,
    resolution_lon      double,

    PRIMARY KEY ( mmsi, event_start )

) WITH CLUSTERING ORDER BY ( event_start DESC )
  AND default_time_to_live = 7776000
  AND comment = 'Candidate dark events — AIS silence periods awaiting critic layer assessment. Expires after 90 days.';


-- =============================================================================
--  SECONDARY INDEXES
--  Cassandra secondary indexes are appropriate here because:
--    1. These columns have low-to-medium cardinality relative to the partition.
--    2. Queries using them are supplementary (cross-reference lookups), not
--       the primary access pattern.
--  For high-cardinality lookups in production, consider a separate lookup table
--  keyed by imo → mmsi instead of a secondary index.
-- =============================================================================

CREATE INDEX IF NOT EXISTS ais_positions_by_imo
    ON ais_positions (imo);

CREATE INDEX IF NOT EXISTS ais_positions_by_flag
    ON ais_positions (flag);
