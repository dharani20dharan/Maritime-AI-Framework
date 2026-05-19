// ─────────────────────────────────────────────────────────────────────────────
//  MAF — Neo4j Temporal Knowledge Graph : Schema Initialisation
//  Engineer A owns this file. Engineer B reads from these nodes.
//  Run once on a fresh Neo4j instance, or on startup via the ETL service.
// ─────────────────────────────────────────────────────────────────────────────

// ── CONSTRAINTS (uniqueness + existence) ─────────────────────────────────────

// Vessel — primary key is IMO (globally unique per physical ship)
CREATE CONSTRAINT vessel_imo IF NOT EXISTS
  FOR (v:Vessel) REQUIRE v.imo IS UNIQUE;

// Vessel — MMSI is not unique (can be reused/spoofed) but indexed
CREATE INDEX vessel_mmsi IF NOT EXISTS
  FOR (v:Vessel) ON (v.mmsi);

// Company — unique by registry_id (IMO company number or constructed key)
CREATE CONSTRAINT company_id IF NOT EXISTS
  FOR (c:Company) REQUIRE c.registry_id IS UNIQUE;

// Flag state
CREATE CONSTRAINT flag_code IF NOT EXISTS
  FOR (f:FlagState) REQUIRE f.iso_code IS UNIQUE;

// EEZ zone
CREATE CONSTRAINT eez_id IF NOT EXISTS
  FOR (e:EEZZone) REQUIRE e.zone_id IS UNIQUE;

// Sanctioned entity
CREATE CONSTRAINT sanctioned_id IF NOT EXISTS
  FOR (s:SanctionedEntity) REQUIRE s.entity_id IS UNIQUE;

// Port
CREATE CONSTRAINT port_unlocode IF NOT EXISTS
  FOR (p:Port) REQUIRE p.unlocode IS UNIQUE;

// Anomaly event
CREATE CONSTRAINT anomaly_event_id IF NOT EXISTS
  FOR (a:AnomalyEvent) REQUIRE a.event_id IS UNIQUE;


// ── INDEXES (query performance) ───────────────────────────────────────────────

// Vessel spatial lookups
CREATE INDEX vessel_last_lat IF NOT EXISTS
  FOR (v:Vessel) ON (v.last_lat);

CREATE INDEX vessel_last_lon IF NOT EXISTS
  FOR (v:Vessel) ON (v.last_lon);

// Vessel name search
CREATE INDEX vessel_name IF NOT EXISTS
  FOR (v:Vessel) ON (v.name);

// Technique-based anomaly lookup
CREATE INDEX anomaly_technique IF NOT EXISTS
  FOR (a:AnomalyEvent) ON (a.technique);

// Sanctioned vessel IMO lookup
CREATE INDEX sanctioned_imo IF NOT EXISTS
  FOR (s:SanctionedEntity) ON (s.imo);

// Company betweenness centrality score (set by GDS job)
CREATE INDEX company_centrality IF NOT EXISTS
  FOR (c:Company) ON (c.betweenness_centrality);


// ── NODE TEMPLATES (documentation — not executable) ──────────────────────────
/*
  :Vessel {
    imo: string,            // IMO number — primary key
    mmsi: string,           // current MMSI (may change — see flag hop detection)
    name: string,
    flag: string,           // ISO 3166-1 alpha-2
    vessel_type: int,       // AIS type code
    draught_m: float,
    last_lat: float,
    last_lon: float,
    last_seen: string,      // ISO 8601
    speed_kts: float,
    nav_status: int,
    risk_score: float,      // computed by GDS / critic layer
    sanctioned: boolean,
  }

  :Company {
    registry_id: string,    // IMO company number or constructed key
    name: string,
    jurisdiction: string,   // ISO country code
    incorporation_date: string,
    is_shell_suspected: boolean,
    betweenness_centrality: float,  // set by GDS PageRank/BC job
  }

  :FlagState {
    iso_code: string,       // e.g. "PA", "LR", "MH"
    name: string,
    flag_of_convenience: boolean,
    ihs_risk_tier: int,     // 1–5
  }

  :EEZZone {
    zone_id: string,        // MRGID from MarineRegions.org
    country_iso: string,
    zone_name: string,
    geometry_wkt: string,   // WKT polygon for spatial queries
  }

  :SanctionedEntity {
    entity_id: string,
    name: string,
    imo: string,
    mmsi: string,
    programs: [string],     // e.g. ["IRAN", "RUSSIA-EO14024"]
    sources: [string],      // e.g. ["OFAC-SDN", "UN-SC"]
    last_updated: string,
  }

  :AnomalyEvent {
    event_id: string,       // uuid
    technique: string,      // e.g. "M-AIS-BEACON"
    mmsi: string,
    imo: string,
    detected_at: string,
    confidence: float,      // 0.0–1.0
    stage: int,             // 1–5
    detail: string,
    evidence: string,       // JSON blob
    verdict: string,        // null | "CONFIRMED" | "DISMISSED" | "ESCALATE"
  }
*/

// ── RELATIONSHIPS (documentation) ────────────────────────────────────────────
/*
  (Vessel)-[:FLAGGED_UNDER]->(FlagState)
  (Vessel)-[:OWNED_BY]->(Company)
  (Vessel)-[:OPERATED_BY]->(Company)
  (Vessel)-[:SISTER_OF]->(Vessel)
  (Vessel)-[:VISITED_PORT {arrived_at, departed_at}]->(Port)
  (Vessel)-[:ENTERED_EEZ {timestamp, authorised: boolean}]->(EEZZone)
  (Vessel)-[:SANCTIONED_AS]->(SanctionedEntity)
  (Vessel)-[:TRIGGERED]->(AnomalyEvent)
  (Company)-[:CONTROLS]->(Company)          // shell chain edges
  (Company)-[:REGISTERED_IN]->(FlagState)
*/

// ── SANCTIONS MONITORING (added Week 1 update) ────────────────────────────────
CREATE CONSTRAINT sanctions_alert_feed IF NOT EXISTS
  FOR (a:SanctionsFeedAlert) REQUIRE a.feed_name IS UNIQUE;

CREATE CONSTRAINT sanctions_refresh_log IF NOT EXISTS
  FOR (r:SanctionsRefreshLog) REQUIRE r.id IS UNIQUE;
