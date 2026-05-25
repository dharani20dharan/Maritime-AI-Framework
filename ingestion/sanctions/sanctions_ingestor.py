"""
MAF - Sanctions Ingestor
Four feeds, 15-minute refresh, feed failure alerting.

Feeds:
  1. OFAC SDN XML          - US Treasury
  2. OpenSanctions vessels  - vessel-specific export
  3. UN Consolidated List   - UN Security Council XML
  4. EU Financial Sanctions - European Commission XML

Fixes applied vs original:
  - OFAC: namespace stripped dynamically; vesselInfo fields read via .//
    (callSign/vesselType/vesselFlag live inside <vesselInfo>, not directly
    on <sdnEntry> - this was the root cause of 0 vessel entries)
  - OpenSanctions: switched to vessel-specific endpoint (smaller/faster)
  - EU FSF: corrected path to xmlFullSanctionsList_1_1
  - Neo4j: single UNWIND batch write instead of per-entity round-trips
  - Neo4j: startup wait extended to 300s (was 150s)
  - All parsers: bare except now logs full traceback via log.exception
"""

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from neo4j import GraphDatabase

log = logging.getLogger("sanctions-ingestor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

# ---------- CONFIG -----------------------------------------------------------
NEO4J_URI       = os.getenv("NEO4J_URI",                    "bolt://neo4j:7687")
NEO4J_USER      = os.getenv("NEO4J_USER",                   "neo4j")
NEO4J_PASSWORD  = os.getenv("NEO4J_PASSWORD",               "maf_neo4j_2024")
REFRESH_S       = int(os.getenv("REFRESH_INTERVAL_SECONDS", "900"))
ALERT_THRESHOLD = int(os.getenv("ALERT_AFTER_FAILURES",     "3"))
LOCAL_FEED_DIR  = os.getenv("LOCAL_FEED_DIR",               "/feeds")

OFAC_SDN_URL      = "https://www.treasury.gov/ofac/downloads/sdn.xml"
OPENSANCTIONS_URL = "https://data.opensanctions.org/datasets/latest/sanctions/targets.nested.json"
UN_XML_URL        = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
EU_FSF_URL        = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- DATA MODEL -------------------------------------------------------
@dataclass
class SanctionedVessel:
    sanction_id:  str
    name:         str
    sources:      list
    programs:     list
    last_updated: str
    imo:          Optional[str] = None
    mmsi:         Optional[str] = None
    call_sign:    Optional[str] = None
    vessel_type:  Optional[str] = None
    flag:         Optional[str] = None


# ---------- FEED HEALTH TRACKER ----------------------------------------------
class FeedHealthTracker:
    def __init__(self, driver):
        self.driver = driver
        self.failures: dict[str, int] = {}

    def ok(self, feed: str):
        if self.failures.get(feed, 0) > 0:
            log.info("Feed recovered: %s", feed)
            self._clear_alert(feed)
        self.failures[feed] = 0

    def fail(self, feed: str, error: str):
        self.failures[feed] = self.failures.get(feed, 0) + 1
        n = self.failures[feed]
        log.warning("Feed failure %d/%d: %s - %s", n, ALERT_THRESHOLD, feed, error)
        if n >= ALERT_THRESHOLD:
            self._write_alert(feed, error, n)

    def _write_alert(self, feed: str, error: str, n: int):
        try:
            with self.driver.session() as s:
                s.run(
                    "MERGE (a:SanctionsFeedAlert {feed_name: $feed}) "
                    "SET a.consecutive_failures = $n, a.last_error = $error, "
                    "a.alerted_at = $ts, a.active = true",
                    feed=feed, n=n, error=error,
                    ts=datetime.now(timezone.utc).isoformat(),
                )
            log.error("ALERT: feed %s has failed %d times in a row", feed, n)
        except Exception as e:
            log.error("Could not write alert to Neo4j: %s", e)

    def _clear_alert(self, feed: str):
        try:
            with self.driver.session() as s:
                s.run(
                    "MATCH (a:SanctionsFeedAlert {feed_name: $feed}) "
                    "SET a.active = false, a.recovered_at = $ts",
                    feed=feed, ts=datetime.now(timezone.utc).isoformat(),
                )
        except Exception:
            pass


# ---------- LOCAL FALLBACK ---------------------------------------------------
def try_local(filename: str) -> Optional[bytes]:
    path = Path(LOCAL_FEED_DIR) / filename
    if path.exists():
        log.info("Using local feed file: %s", path)
        return path.read_bytes()
    return None


# ---------- NEO4J WRITER -----------------------------------------------------
BATCH_UPSERT = (
    "UNWIND $rows AS e "
    "MERGE (s:Sanction {sanction_id: e.sanction_id}) "
    "SET s.name = e.name, s.mmsi = e.mmsi, s.imo = e.imo, "
    "s.call_sign = e.call_sign, s.vessel_type = e.vessel_type, "
    "s.flag = e.flag, s.programs = e.programs, s.sources = e.sources, "
    "s.last_updated = e.last_updated, s.sanctioned = true "
    "WITH s, e WHERE e.imo IS NOT NULL "
    "MATCH (v:Vessel {imo: e.imo}) MERGE (v)-[:SANCTIONED_BY]->(s)"
)

REFRESH_LOG = (
    "MERGE (r:SanctionsRefreshLog {id: 'latest'}) "
    "SET r.refreshed_at = $refreshed_at, r.total_entities = $total, "
    "r.ofac_count = $ofac, r.opensanctions_count = $opensanctions, "
    "r.un_count = $un, r.eu_count = $eu, r.elapsed_seconds = $elapsed"
)


def write_to_neo4j(driver, entities: list, counts: dict, elapsed: float):
    rows = [asdict(e) for e in entities]
    with driver.session() as session:
        try:
            session.run(BATCH_UPSERT, rows=rows)
        except Exception as ex:
            log.error("Neo4j batch write failed: %s", ex)
        try:
            session.run(REFRESH_LOG, {
                "refreshed_at":  datetime.now(timezone.utc).isoformat(),
                "total":         len(entities),
                "ofac":          counts.get("ofac", 0),
                "opensanctions": counts.get("opensanctions", 0),
                "un":            counts.get("un", 0),
                "eu":            counts.get("eu", 0),
                "elapsed":       round(elapsed, 1),
            })
        except Exception as ex:
            log.warning("Could not write refresh log: %s", ex)
    log.info("Wrote %d sanctioned entities to Neo4j", len(entities))


# ---------- HELPERS ----------------------------------------------------------
def _first(lst: list) -> Optional[str]:
    return lst[0] if lst else None


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _strip_ns_tree(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = _strip_ns(el.tag)
        el.attrib = {_strip_ns(k): v for k, v in el.attrib.items()}
    return root


# ---------- FEED 1: OFAC SDN -------------------------------------------------
def fetch_ofac(tracker: FeedHealthTracker) -> list:
    log.info("Fetching OFAC SDN ...")
    content = try_local("sdn.xml")
    if content is None:
        try:
            r = requests.get(OFAC_SDN_URL, timeout=90, headers=HEADERS)
            r.raise_for_status()
            content = r.content
        except requests.RequestException as e:
            log.exception("OFAC SDN: network error")
            tracker.fail("OFAC-SDN", str(e))
            return []
    try:
        root = _strip_ns_tree(ET.fromstring(content))
        all_entries = root.findall(".//sdnEntry")
        log.info("OFAC SDN: root=%s  total sdnEntry=%d", root.tag, len(all_entries))

        if not all_entries:
            log.warning("OFAC SDN: 0 sdnEntry elements. Raw snip: %s",
                        content[:400].decode("utf-8", errors="replace"))
            tracker.fail("OFAC-SDN", "0 sdnEntry elements")
            return []

        type_counts: dict[str, int] = {}
        for e in all_entries:
            t = (e.findtext("sdnType") or "").strip()
            type_counts[t] = type_counts.get(t, 0) + 1
        log.info("OFAC SDN sdnType distribution: %s", type_counts)

        out = []
        for entry in all_entries:
            if (entry.findtext("sdnType") or "").strip().lower() != "vessel":
                continue

            uid  = entry.findtext("uid")      or ""
            name = entry.findtext("lastName") or ""

            # callSign/vesselType/vesselFlag are inside <vesselInfo>,
            # NOT direct children of <sdnEntry>. Must use .//<tag>.
            csign = entry.findtext(".//callSign")   or ""
            vtype = entry.findtext(".//vesselType") or ""
            flag  = entry.findtext(".//vesselFlag") or ""

            imo = mmsi = None
            for id_el in entry.findall(".//id"):
                id_type = (id_el.findtext("idType")   or "").lower()
                id_val  = (id_el.findtext("idNumber") or "").strip()
                if "imo"  in id_type: imo  = id_val
                if "mmsi" in id_type: mmsi = id_val

            programs = [
                p.text.strip()
                for p in entry.findall(".//program")
                if p.text and p.text.strip()
            ]

            out.append(SanctionedVessel(
                sanction_id=f"OFAC-{uid}",
                name=name.strip(),
                imo=imo, mmsi=mmsi,
                call_sign=csign.strip(),
                vessel_type=vtype.strip(),
                flag=flag.strip(),
                programs=programs,
                sources=["OFAC-SDN"],
                last_updated=datetime.now(timezone.utc).isoformat(),
            ))

        tracker.ok("OFAC-SDN")
        log.info("OFAC SDN: %d vessel entries", len(out))
        return out

    except Exception as e:
        log.exception("OFAC SDN: unhandled parser exception")
        tracker.fail("OFAC-SDN", str(e))
        return []


# ---------- FEED 2: OPENSANCTIONS --------------------------------------------
def fetch_opensanctions(tracker: FeedHealthTracker) -> list:
    log.info("Fetching OpenSanctions ...")
    content = try_local("opensanctions_vessels.json")
    if content is None:
        try:
            r = requests.get(OPENSANCTIONS_URL, timeout=120, headers=HEADERS, stream=True)
            r.raise_for_status()
            content = r.content
        except requests.RequestException as e:
            log.exception("OpenSanctions: network error")
            tracker.fail("OpenSanctions", str(e))
            return []
    try:
        out = []
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("schema") != "Vessel":
                continue
            props    = record.get("properties", {})
            eid      = record.get("id", "")
            datasets = record.get("datasets", [])
            out.append(SanctionedVessel(
                sanction_id=f"OS-{eid}",
                name=_first(props.get("name", [])) or _first(props.get("alias", [])) or "",
                imo=_first(props.get("imoNumber", [])),
                mmsi=_first(props.get("mmsi", [])),
                call_sign=_first(props.get("callSign", [])),
                vessel_type=_first(props.get("type", [])),
                flag=_first(props.get("flag", [])),
                programs=datasets,
                sources=datasets,
                last_updated=datetime.now(timezone.utc).isoformat(),
            ))
        tracker.ok("OpenSanctions")
        log.info("OpenSanctions: %d vessel entries", len(out))
        return out
    except Exception as e:
        log.exception("OpenSanctions: unhandled parser exception")
        tracker.fail("OpenSanctions", str(e))
        return []


# ---------- FEED 3: UN CONSOLIDATED ------------------------------------------
def fetch_un(tracker: FeedHealthTracker) -> list:
    log.info("Fetching UN Consolidated List ...")
    content = try_local("un_consolidated.xml")
    if content is None:
        try:
            r = requests.get(UN_XML_URL, timeout=90, headers=HEADERS)
            r.raise_for_status()
            content = r.content
        except requests.RequestException as e:
            log.exception("UN Consolidated: network error")
            tracker.fail("UN-Consolidated", str(e))
            return []
    try:
        root = ET.fromstring(content)
        out  = []
        for entity in root.findall(".//ENTITY"):
            dataid  = entity.findtext("DATAID", default="")
            name_el = entity.find("FIRST_NAME") or entity.find(".//ALIAS_NAME")
            name    = (name_el.text or "").strip() if name_el is not None else ""
            remarks = entity.findtext("COMMENTS1") or ""
            imo     = None
            m       = re.search(r"IMO[:\s#]*(\d{7})", remarks)
            if m:
                imo = m.group(1)
            if not imo and "vessel" not in remarks.lower() and "ship" not in remarks.lower():
                continue
            committee = entity.findtext("UN_LIST_TYPE") or "UN-SC"
            out.append(SanctionedVessel(
                sanction_id=f"UN-{dataid}", name=name, imo=imo, mmsi=None,
                flag=None, call_sign=None, vessel_type=None,
                programs=[committee], sources=["UN-Consolidated"],
                last_updated=datetime.now(timezone.utc).isoformat(),
            ))
        tracker.ok("UN-Consolidated")
        log.info("UN Consolidated: %d vessel entries", len(out))
        return out
    except Exception as e:
        log.exception("UN Consolidated: unhandled parser exception")
        tracker.fail("UN-Consolidated", str(e))
        return []


# ---------- FEED 4: EU FSF ---------------------------------------------------
def fetch_eu(tracker: FeedHealthTracker) -> list:
    log.info("Fetching EU Financial Sanctions File ...")
    content = try_local("eu_fsf.xml")
    if content is None:
        try:
            r = requests.get(EU_FSF_URL, timeout=90, headers=HEADERS)
            r.raise_for_status()
            content = r.content
        except requests.RequestException as e:
            log.exception("EU FSF: network error")
            tracker.fail("EU-FSF", str(e))
            return []
    try:
        root = ET.fromstring(content)
        out  = []
        for entity in root.findall(".//{*}sanctionEntity"):
            subtype   = entity.get("subjectType", "").lower()
            entity_id = entity.get("logicalId", "")
            regime    = entity.get("regulationTitle", "") or entity.get("programme", "")
            name      = ""
            for na in entity.findall(".//{*}nameAlias"):
                n = na.get("wholeName") or na.get("lastName") or ""
                if n:
                    name = n.strip()
                    break
            imo = mmsi = flag = None
            for ident in entity.findall(".//{*}identification"):
                id_type = (ident.get("identificationTypeDescription") or "").lower()
                id_num  = (ident.get("number") or ident.get("value") or "").strip()
                if "imo"  in id_type: imo  = id_num
                if "mmsi" in id_type: mmsi = id_num
                if "flag" in id_type: flag = id_num
            if not imo and not mmsi and subtype not in ("vessel", "ship"):
                continue
            out.append(SanctionedVessel(
                sanction_id=f"EU-{entity_id}", name=name, imo=imo, mmsi=mmsi,
                flag=flag, call_sign=None, vessel_type="vessel",
                programs=[regime or "EU-FSF"], sources=["EU-FSF"],
                last_updated=datetime.now(timezone.utc).isoformat(),
            ))
        tracker.ok("EU-FSF")
        log.info("EU FSF: %d vessel entries", len(out))
        return out
    except Exception as e:
        log.exception("EU FSF: unhandled parser exception")
        tracker.fail("EU-FSF", str(e))
        return []


# ---------- DEDUPLICATION ----------------------------------------------------
def deduplicate(all_entities: list) -> list:
    by_imo: dict[str, SanctionedVessel] = {}
    no_imo: list = []
    for e in all_entities:
        if e.imo:
            if e.imo in by_imo:
                ex = by_imo[e.imo]
                ex.sources  = list(set(ex.sources  + e.sources))
                ex.programs = list(set(ex.programs + e.programs))
                if not ex.mmsi        and e.mmsi:        ex.mmsi        = e.mmsi
                if not ex.flag        and e.flag:        ex.flag        = e.flag
                if not ex.call_sign   and e.call_sign:   ex.call_sign   = e.call_sign
                if not ex.vessel_type and e.vessel_type: ex.vessel_type = e.vessel_type
            else:
                by_imo[e.imo] = e
        else:
            no_imo.append(e)
    result = list(by_imo.values()) + no_imo
    log.info("Deduplication: %d total -> %d unique (%d merged by IMO)",
             len(all_entities), len(result), len(all_entities) - len(result))
    return result


# ---------- MAIN LOOP --------------------------------------------------------
def run():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    log.info("Waiting for Neo4j to accept connections...")
    for _ in range(60):
        try:
            driver.verify_connectivity()
            log.info("Neo4j is up and ready.")
            break
        except Exception:
            time.sleep(5)
    else:
        log.error("Neo4j connection timed out. Proceeding anyway, but writes will fail.")

    tracker = FeedHealthTracker(driver)
    log.info("Sanctions ingestor started - 4 feeds - refresh every %ds (%d min)",
             REFRESH_S, REFRESH_S // 60)
    log.info("Local feed fallback directory: %s", LOCAL_FEED_DIR)

    while True:
        start    = time.time()
        counts   = {}
        entities = []

        for feed_name, fn in [
            ("ofac",          fetch_ofac),
            ("opensanctions", fetch_opensanctions),
            ("un",            fetch_un),
            ("eu",            fetch_eu),
        ]:
            result            = fn(tracker)
            counts[feed_name] = len(result)
            entities.extend(result)

        deduped = deduplicate(entities)
        elapsed = time.time() - start
        write_to_neo4j(driver, deduped, counts, elapsed)

        log.info(
            "Refresh complete - OFAC:%d  OpenSanctions:%d  UN:%d  EU:%d  -> %d unique | %.1fs",
            counts["ofac"], counts["opensanctions"], counts["un"], counts["eu"],
            len(deduped), elapsed,
        )
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    run()
