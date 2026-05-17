# MAF — Cassandra Query Reference

Connect with: `docker compose exec cassandra cqlsh`
Then switch keyspace: `USE maf_ais;`

---

## Get last 50 positions for a vessel today
```cql
SELECT timestamp, lat, lon, speed_kts, heading
FROM ais_positions
WHERE mmsi = '236900001'
  AND date_bucket = '2026-05-13'
LIMIT 50;
```

## Get a vessel's track across multiple days
```cql
SELECT timestamp, lat, lon, speed_kts
FROM ais_positions
WHERE mmsi = '236900001'
  AND date_bucket IN ('2026-05-11', '2026-05-12', '2026-05-13')
ORDER BY timestamp DESC;
```

## Last known position for any vessel (instant lookup)
```cql
SELECT mmsi, last_lat, last_lon, last_speed_kts, last_seen
FROM vessel_track_summary
WHERE mmsi = '236900001';
```

## All current dark event candidates (unresolved)
```cql
SELECT mmsi, event_start, silence_hours, last_known_lat, last_known_lon
FROM dark_event_candidates
WHERE resolved = false
ALLOW FILTERING;
```

## Count total rows in position history
```cql
SELECT COUNT(*) FROM ais_positions;
```

## Check how many vessels are being tracked
```cql
SELECT COUNT(*) FROM vessel_track_summary;
```

## Find all positions for a vessel by IMO (secondary index)
```cql
SELECT mmsi, date_bucket, timestamp, lat, lon
FROM ais_positions
WHERE imo = '9000001';
```

---

## Notes
- Always include `date_bucket` in WHERE clauses for `ais_positions` — it is
  part of the partition key. Omitting it forces a full table scan.
- Rows expire automatically after 90 days. No manual deletion needed.
- `vessel_track_summary` has no TTL — it always holds the last known position.
