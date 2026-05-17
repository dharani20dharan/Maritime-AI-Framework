"""
MAF — Sanctions Ingestor (Engineer A, Stage 1 — gap fix)
Pulls vessel entries from:
  - OFAC SDN list (official US Treasury XML)
  - OpenSanctions vessels feed (aggregates UN, EU, UK, OFAC)
Writes SanctionedEntity nodes to Neo4j.
Refreshes every REFRESH_INTERVAL_SECONDS (default: 3600).
"""

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from neo4j import GraphDatabase

log = logging.getLogger("sanctions-ingestor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

NEO4J_URI      = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
REFRESH_S      = int(os.getenv("REFRESH_INTERVAL_SECONDS", "3600"))

# Official OFAC SDN XML (no API key required — public download)
OFAC_SDN_XML_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"

# OpenSanctions vessel-specific feed (free for non-commercial / OSINT use)
OPENSANCTIONS_VESSELS_URL = "https://data.opensanctions.org/datasets/latest/vessels/targets.nested.json"


# ── NEO4J WRITER ─────────────────────────────────────────────────────────────

UPSERT_SANCTIONED = """
MERGE (s:SanctionedEntity {entity_id: $entity_id})
SET s.name            = $name,
    s.mmsi            = $mmsi,
    s.imo             = $imo,
    s.call_sign       = $call_sign,
    s.vessel_type     = $vessel_type,
    s.flag            = $flag,
    s.programs        = $programs,
    s.sources         = $sources,
    s.last_updated    = $last_updated,
    s.sanctioned      = true
WITH s
WHERE $imo IS NOT NULL
MATCH (v:Vessel {imo: $imo})
MERGE (v)-[:SANCTIONED_AS]->(s)
"""

def write_sanctions(driver, entities: list[dict]):
    with driver.session() as session:
        for e in entities:
            try:
                session.run(UPSERT_SANCTIONED, **e)
            except Exception as ex:
                log.warning("Neo4j write failed for %s: %s", e.get("entity_id"), ex)
    log.info("Wrote %d sanctioned entities to Neo4j", len(entities))


# ── OFAC SDN PARSER ───────────────────────────────────────────────────────────

def fetch_ofac_sdn() -> list[dict]:
    """
    Downloads the official OFAC SDN XML and extracts vessel entries.
    OFAC SDN contains ~12k entries; vessel subset is ~800-1200 depending on
    active sanctions programs.
    """
    log.info("Fetching OFAC SDN XML ...")
    try:
        r = requests.get(OFAC_SDN_XML_URL, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("OFAC fetch failed: %s", e)
        return []

    root = ET.fromstring(r.content)
    ns = {"ofac": "http://tempuri.org/sdnList.xsd"}
    entities = []

    for entry in root.findall(".//ofac:sdnEntry", ns):
        sdn_type = entry.findtext("ofac:sdnType", default="", namespaces=ns)
        if sdn_type.lower() != "vessel":
            continue

        entity_id  = entry.findtext("ofac:uid", default="", namespaces=ns)
        last_name  = entry.findtext("ofac:lastName", default="", namespaces=ns)
        call_sign  = entry.findtext("ofac:callSign", default="", namespaces=ns)
        vessel_type= entry.findtext("ofac:vesselType", default="", namespaces=ns)
        flag       = entry.findtext("ofac:vesselFlag", default="", namespaces=ns)

        # Extract IMO from ID list
        imo = None
        mmsi = None
        for id_elem in entry.findall(".//ofac:id", ns):
            id_type = id_elem.findtext("ofac:idType", default="", namespaces=ns)
            id_val  = id_elem.findtext("ofac:idNumber", default="", namespaces=ns)
            if "imo" in id_type.lower():
                imo = id_val.strip()
            if "mmsi" in id_type.lower():
                mmsi = id_val.strip()

        programs = [p.findtext("ofac:program", default="", namespaces=ns)
                    for p in entry.findall(".//ofac:program", ns)]

        entities.append({
            "entity_id":   f"OFAC-SDN-{entity_id}",
            "name":        last_name.strip(),
            "mmsi":        mmsi,
            "imo":         imo,
            "call_sign":   call_sign.strip(),
            "vessel_type": vessel_type.strip(),
            "flag":        flag.strip(),
            "programs":    programs,
            "sources":     ["OFAC-SDN"],
            "last_updated":datetime.now(timezone.utc).isoformat(),
        })

    log.info("OFAC SDN: %d vessel entries parsed", len(entities))
    return entities


# ── OPENSANCTIONS PARSER ──────────────────────────────────────────────────────

def fetch_opensanctions_vessels() -> list[dict]:
    """
    OpenSanctions vessels feed aggregates OFAC, UN, EU, UK OFSI, and others.
    Free for non-commercial use. Returns targets in FtM (FollowTheMoney) format.
    """
    log.info("Fetching OpenSanctions vessels feed ...")
    try:
        r = requests.get(OPENSANCTIONS_VESSELS_URL, timeout=120, stream=True)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("OpenSanctions fetch failed: %s", e)
        return []

    entities = []
    for line in r.iter_lines():
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        props = record.get("properties", {})
        entity_id = record.get("id", "")

        # Extract identifiers
        imo  = _first(props.get("imoNumber", []))
        mmsi = _first(props.get("mmsi", []))
        flag = _first(props.get("flag", []))
        name = _first(props.get("name", [])) or _first(props.get("alias", []))

        datasets = record.get("datasets", [])

        entities.append({
            "entity_id":   f"OS-{entity_id}",
            "name":        name or "",
            "mmsi":        mmsi,
            "imo":         imo,
            "call_sign":   _first(props.get("callSign", [])),
            "vessel_type": _first(props.get("type", [])),
            "flag":        flag,
            "programs":    datasets,
            "sources":     datasets,
            "last_updated":datetime.now(timezone.utc).isoformat(),
        })

    log.info("OpenSanctions: %d vessel entries parsed", len(entities))
    return entities


def _first(lst: list) -> str | None:
    return lst[0] if lst else None


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    log.info("Sanctions ingestor started — refresh every %ds", REFRESH_S)

    while True:
        start = time.time()
        all_entities: list[dict] = []

        ofac = fetch_ofac_sdn()
        all_entities.extend(ofac)

        os_vessels = fetch_opensanctions_vessels()
        all_entities.extend(os_vessels)

        # Deduplicate by IMO where possible
        seen_imos = set()
        deduped = []
        for e in all_entities:
            key = e.get("imo") or e["entity_id"]
            if key not in seen_imos:
                seen_imos.add(key)
                deduped.append(e)

        write_sanctions(driver, deduped)
        log.info("Sanctions refresh complete — %d unique entities | elapsed=%.1fs",
                 len(deduped), time.time() - start)

        time.sleep(REFRESH_S)


if __name__ == "__main__":
    run()
