# MAF — Contract B1 : Neo4j Agent Context Interface
## Version: 1.0

This document specifies the read-only Neo4j Bolt connection interface used by Agent Zero
and all downstream analysis agents. Both engineers must agree on any changes before merging.

---

## 1. Connection Specification

| Parameter     | Value                              |
|---------------|------------------------------------|
| Protocol      | Bolt (`bolt://`)                   |
| Default Host  | `localhost`                        |
| Default Port  | `7687`                             |
| Auth          | Basic (user: `neo4j`, pass: env `NEO4J_PASSWORD`) |
| Database      | `neo4j` (default)                  |
| Mode          | **READ-ONLY** for agent queries    |
| Max Pool Size | 20 connections                     |
| Timeout       | 30 seconds                         |

> **CRITICAL**: Agent queries MUST NOT use `MERGE`, `CREATE`, `DELETE`, or `SET`.
> Write operations are reserved exclusively for the ETL ingestion pipeline.

---

## 2. Node Schemas

### Vessel
```cypher
(:Vessel {
  mmsi:        STRING,   // Primary key — 9-digit string
  imo:         STRING,   // 7-digit string, may be null
  name:        STRING,
  flag:        STRING,   // ISO 3166-1 alpha-2
  vessel_type: INTEGER,  // AIS type code
  risk_score:  FLOAT,    // 0.0–100.0, updated by scoring pipeline
  last_seen:   DATETIME
})
```

### Port
```cypher
(:Port {
  name:    STRING,
  country: STRING,
  lat:     FLOAT,
  lon:     FLOAT,
  unlocode: STRING  // UN/LOCODE 5-char code
})
```

### SanctionEntity
```cypher
(:SanctionEntity {
  name:    STRING,
  list:    STRING,  // e.g. "OFAC_SDN", "EU_COUNCIL", "UN_SANCTIONS"
  sdnType: STRING   // "vessel", "individual", "entity"
})
```

### FlagRegistry
```cypher
(:FlagRegistry {
  iso_code:    STRING,  // ISO 3166-1 alpha-2
  country:     STRING,
  risk_tier:   INTEGER  // 1=Low, 2=Medium, 3=High (open/convenience registry)
})
```

---

## 3. Relationship Schemas

| Relationship              | From → To                    | Key Properties                        |
|---------------------------|------------------------------|---------------------------------------|
| `CALLED_AT`               | Vessel → Port                | `timestamp`, `duration_hours`         |
| `LINKED_TO_SANCTION`      | Vessel → SanctionEntity      | `confidence`, `match_type`            |
| `FLEW_FLAG`               | Vessel → FlagRegistry        | `from_date`, `to_date`                |
| `CO_LOITERED_WITH`        | Vessel → Vessel              | `lat`, `lon`, `duration_hours`, `ts`  |
| `OWNERSHIP_CHAIN`         | Vessel → Vessel              | `relation_type`, `jurisdiction`       |

---

## 4. Standard Cypher Projections

### 4.1 Vessel Context (used by Agent Zero tool `query_neo4j_context`)
```cypher
MATCH (v:Vessel {mmsi: $mmsi})
OPTIONAL MATCH (v)-[:FLEW_FLAG]->(f:FlagRegistry)
OPTIONAL MATCH (v)-[:LINKED_TO_SANCTION]->(s:SanctionEntity)
OPTIONAL MATCH (v)-[:CALLED_AT]->(p:Port)
RETURN v, collect(DISTINCT f) AS flags, collect(DISTINCT s) AS sanctions,
       collect(DISTINCT p) AS ports
ORDER BY p.timestamp DESC
LIMIT 10
```

### 4.2 Flag-Hop Chain Detection
```cypher
MATCH (v:Vessel {mmsi: $mmsi})-[r:FLEW_FLAG]->(f:FlagRegistry)
WHERE r.from_date IS NOT NULL
RETURN f.iso_code AS flag, r.from_date AS from_date, r.to_date AS to_date
ORDER BY r.from_date
```

### 4.3 Ownership / Shell-Company Chain
```cypher
MATCH path = (v:Vessel {mmsi: $mmsi})-[:OWNERSHIP_CHAIN*1..5]->(related)
RETURN path
LIMIT 50
```

### 4.4 Co-Loitering Network (STS Graph)
```cypher
MATCH (v:Vessel {mmsi: $mmsi})-[r:CO_LOITERED_WITH]-(partner:Vessel)
WHERE r.duration_hours >= $min_hours
RETURN partner.mmsi, partner.name, partner.flag, r.lat, r.lon, r.duration_hours
ORDER BY r.duration_hours DESC
```

---

## 5. GDS Projection (for gds_centrality.py)

```cypher
// Create named projection — run once per analysis session
CALL gds.graph.project(
  'vessel-network',
  ['Vessel', 'SanctionEntity'],
  {
    CO_LOITERED_WITH: { orientation: 'UNDIRECTED' },
    LINKED_TO_SANCTION: { orientation: 'NATURAL' }
  }
)
```

---

## 6. Constraints

- All agent queries must use `$param` parameterized queries (no string interpolation).
- Query result sets are capped at **1000 rows** maximum to prevent memory exhaustion.
- Agents must catch `neo4j.exceptions.ServiceUnavailable` and fall back gracefully.
- Do not traverse ownership chains deeper than **5 hops** (`*1..5`).

---

## 7. Changelog

| Version | Date       | Author       | Change Description                         |
|---------|------------|--------------|--------------------------------------------|
| 1.0     | 2024-03-15 | Engineer A   | Initial specification                      |
