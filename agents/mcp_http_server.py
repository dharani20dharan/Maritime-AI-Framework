"""
Maritime Intelligence MCP Server — HTTP/SSE Transport
======================================================
This server exposes all maritime intelligence tools via HTTP (SSE transport)
so the Agent Zero Docker container can connect to it from inside the container.

The Agent Zero container reaches the host at: http://host.docker.internal:7331/sse

Run this on the HOST machine (not inside Docker):
    python agents/mcp_http_server.py

Then configure Agent Zero's MCP settings to point to:
    http://host.docker.internal:7331/sse
"""
import sys
import os
import json
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP
import agents.agent_zero_tools as az_tools
from tools.sanction_scorer import SanctionScorer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
log = logging.getLogger("maritime-mcp-http")

# Initialize MCP server with SSE transport
mcp = FastMCP(
    "Maritime Intelligence Server",
    host="0.0.0.0",  # Listen on all interfaces so Docker can reach it
    port=7331
)

# ─────────────────────────────────────────────────────────
# TOOL 1: Full Analysis Pipeline (PRIMARY TOOL — use this first)
# ─────────────────────────────────────────────────────────
@mcp.tool()
def run_full_maritime_analysis(imo_number: str) -> str:
    """
    PRIMARY TOOL: Runs the complete Maritime Intelligence analysis pipeline for a vessel.
    Performs: Data Collection → Behavior Analysis → Dark Ship Detection → Threat Assessment.
    Use this when investigating any suspicious vessel. Returns a full structured report.

    Args:
        imo_number: The 7-digit IMO number of the vessel (e.g. '9179385' or '9715751').
    """
    log.info(f"[run_full_maritime_analysis] Starting pipeline for IMO: {imo_number}")
    output = f"=== MARITIME INTELLIGENCE ANALYSIS: IMO {imo_number} ===\n\n"

    # Step 1: Data Collection
    output += "--- Step 1: Data Collection & Fusion ---\n"
    try:
        vessel_state = az_tools.collect_and_fuse_data(imo_number)
        output += f"  Vessel Name : {vessel_state.get('vessel_name', 'UNKNOWN')}\n"
        output += f"  MMSI        : {vessel_state.get('mmsi', 'UNKNOWN')}\n"
        output += f"  Type        : {vessel_state.get('vessel_type', 'UNKNOWN')}\n"
        registry = vessel_state.get("registry", {})
        output += f"  Flag        : {registry.get('flag', 'UNKNOWN')}\n"
        output += f"  Owner       : {registry.get('owner', 'UNKNOWN')}\n"
        output += f"  PSC Status  : {registry.get('psc_inspections', 'UNKNOWN')}\n"
        output += f"  Sources     : {', '.join(vessel_state.get('sources_fused', []))}\n\n"
    except Exception as e:
        output += f"  ERROR: {e}\n\n"
        vessel_state = {"imo": imo_number}

    # Step 2: Behavior Analysis
    output += "--- Step 2: Behavioral Anomaly Analysis ---\n"
    try:
        anomalies = az_tools.evaluate_vessel_behavior(vessel_state)
        if not anomalies:
            output += "  No behavioral anomalies detected.\n\n"
        else:
            output += f"  {len(anomalies)} anomaly(s) detected:\n"
            for a in anomalies:
                if "error" not in a:
                    output += f"    [{a.get('severity','?')}] {a.get('rule_id','?')}: {a.get('evidence','')}\n"
            output += "\n"
    except Exception as e:
        output += f"  ERROR: {e}\n\n"

    # Step 3: Dark Ship Detection
    output += "--- Step 3: Dark Ship / STS Transfer Detection ---\n"
    try:
        dark_events = az_tools.detect_dark_ship_events(imo_number)
        if not dark_events:
            output += "  No STS transfer events detected.\n\n"
        else:
            output += f"  {len(dark_events)} STS event(s) found:\n"
            for d in dark_events:
                if "error" not in d:
                    output += f"    Rendezvous: {d.get('peer_name','?')} for {d.get('duration_hours',0):.1f}h (Confidence: {d.get('confidence',0):.0%})\n"
            output += "\n"
    except Exception as e:
        output += f"  ERROR: {e}\n\n"

    # Step 4: Threat Assessment
    output += "--- Step 4: Threat Level Assessment ---\n"
    score = 0
    try:
        threat = az_tools.evaluate_threat_level(imo_number)
        score = threat.get("evasion_risk_score", 0)
        flags = threat.get("triggered_flags", [])
        level = "CRITICAL" if score >= 75 else "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW"
        output += f"  Final Risk Score : {score}/100  [{level}]\n"
        output += f"  Suspicious       : {'YES' if threat.get('is_suspicious') else 'No'}\n"
        if flags:
            output += "  Risk Flags:\n"
            for flag in flags:
                output += f"    - {flag}\n"
        output += "\n"
    except Exception as e:
        output += f"  ERROR: {e}\n\n"

    # Verdict
    output += "=== FINAL VERDICT ===\n"
    if score >= 50:
        output += f"VESSEL IMO {imo_number} IS SUSPICIOUS (Risk Score: {score}/100)\n"
        output += "Recommended Action: Enhanced surveillance, port authority notification, sanctions verification.\n"
    else:
        output += f"Vessel IMO {imo_number} appears NORMAL (Risk Score: {score}/100)\n"
        output += "Standard monitoring protocols apply.\n"

    log.info(f"[run_full_maritime_analysis] Done for IMO: {imo_number} — Score: {score}")
    return output


# ─────────────────────────────────────────────────────────
# TOOL 2: Evasion Risk Score Only
# ─────────────────────────────────────────────────────────
@mcp.tool()
def calculate_vessel_evasion_risk(imo_number: str) -> str:
    """
    Evaluates a vessel for maritime sanction evasion risk.
    Returns an evasion risk score (0-100) and triggered anomaly flags.
    Use this for a quick risk check on a specific vessel.

    Args:
        imo_number: The 7-digit IMO number of the vessel.
    """
    scorer = SanctionScorer()
    try:
        score, flags = scorer.calculate_risk(imo_number)
        level = "CRITICAL" if score >= 75 else "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW"
        result = f"IMO {imo_number} — Evasion Risk Score: {score}/100 [{level}]\n"
        result += "Triggered Flags:\n"
        if not flags:
            result += "  None — vessel appears safe\n"
        else:
            for flag in flags:
                result += f"  - {flag}\n"
        return result
    except Exception as e:
        return f"Error evaluating IMO {imo_number}: {str(e)}"
    finally:
        scorer.close()


# ─────────────────────────────────────────────────────────
# TOOL 3: Knowledge Graph Query
# ─────────────────────────────────────────────────────────
@mcp.tool()
def query_maritime_knowledge_graph(cypher_query: str) -> str:
    """
    Executes a Cypher query on the Neo4j Maritime Knowledge Graph.
    Use to explore vessel-company-sanction relationships, ownership chains, events.

    Example queries:
      MATCH (v:Vessel) RETURN v.imo, v.name LIMIT 10
      MATCH (v:Vessel)-[:SANCTIONED_BY]->(s:Sanction) RETURN v.name, s.programs LIMIT 5
      MATCH (v:Vessel {imo: '9179385'})-[:INVOLVED_IN]->(e:Event) RETURN e

    Args:
        cypher_query: A valid Cypher query string.
    """
    try:
        results = az_tools.query_knowledge_graph(cypher_query)
        if not results:
            return "Query returned no results."
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        return f"Query error: {str(e)}"


@mcp.tool()
def query_knowledge_graph(cypher_query: str) -> str:
    """
    Executes a Cypher query on the Neo4j Maritime Knowledge Graph.
    Alias for query_maritime_knowledge_graph.

    Args:
        cypher_query: A valid Cypher query string.
    """
    return query_maritime_knowledge_graph(cypher_query)


# ─────────────────────────────────────────────────────────
# TOOL 4: List Vessels in Database
# ─────────────────────────────────────────────────────────
@mcp.tool()
def list_vessels(limit: int = 10, sanctioned_only: bool = False, min_risk: int = 0, minRisk: int = 0) -> str:
    """
    Lists vessels in the Maritime Knowledge Graph database.
    Can filter by risk score or show only sanctioned vessels.

    Args:
        limit: Number of vessels to return (default: 10, max: 100).
        sanctioned_only: If true, only returns vessels with active sanctions.
        min_risk: Minimum risk score threshold (0-100).
        minRisk: Alias parameter for minimum risk threshold.
    """
    try:
        limit = min(limit, 100)
        risk_threshold = max(min_risk, minRisk)
        if sanctioned_only:
            query = f"""
            MATCH (v:Vessel)-[:SANCTIONED_BY]->(s:Sanction)
            WHERE v.imo IS NOT NULL AND v.risk_score >= {risk_threshold}
            RETURN DISTINCT v.imo AS imo, v.mmsi AS mmsi, v.name AS name, v.risk_score AS risk_score, collect(s.programs)[0] AS sanction_program
            ORDER BY v.risk_score DESC
            LIMIT {limit}
            """
        elif risk_threshold > 0:
            query = f"""
            MATCH (v:Vessel)
            WHERE v.risk_score >= {risk_threshold}
            OPTIONAL MATCH (v)-[:REGISTERED_UNDER]->(f:Flag)
            OPTIONAL MATCH (v)-[:SANCTIONED_BY]->(s:Sanction)
            RETURN v.imo AS imo, v.mmsi AS mmsi, v.name AS name, f.country_code AS flag, v.risk_score AS risk_score, (s IS NOT NULL) AS is_sanctioned
            ORDER BY v.risk_score DESC
            LIMIT {limit}
            """
        else:
            query = f"""
            MATCH (v:Vessel)
            WHERE v.imo IS NOT NULL
            RETURN v.imo AS imo, v.mmsi AS mmsi, v.name AS name, v.vessel_type AS type, v.risk_score AS risk_score
            LIMIT {limit}
            """
        results = az_tools.query_knowledge_graph(query)
        if not results:
            return "No vessels found in database matching criteria."
        lines = [f"Found {len(results)} vessel(s):\n"]
        for r in results:
            imo = r.get('imo', 'N/A')
            mmsi = r.get('mmsi', 'N/A')
            name = r.get('name', 'N/A')
            risk = r.get('risk_score', 0)
            extra = r.get('type', '') or r.get('sanction_program', '')
            lines.append(f"  IMO: {imo} | MMSI: {mmsi} | Name: {name} | Risk Score: {risk}/100 | {extra}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing vessels: {str(e)}"


@mcp.tool()
def get_top_risk_vessels(limit: int = 5) -> str:
    """
    Returns the top highest risk vessels from the Maritime Knowledge Graph.
    Lists vessel name, IMO, MMSI, flag country code, risk score, sanction status, and threat rationale.

    Args:
        limit: Number of top risk vessels to return (default: 5).
    """
    try:
        limit = min(limit, 20)
        query = f"""
        MATCH (v:Vessel)
        WHERE v.risk_score IS NOT NULL AND v.risk_score > 0
        OPTIONAL MATCH (v)-[:REGISTERED_UNDER]->(f:Flag)
        OPTIONAL MATCH (v)-[:SANCTIONED_BY]->(s:Sanction)
        OPTIONAL MATCH (v)-[:INVOLVED_IN]->(e:Event)
        RETURN v.imo AS imo, v.mmsi AS mmsi, v.name AS name, f.country_code AS flag,
               v.risk_score AS risk_score, (s IS NOT NULL) AS is_sanctioned,
               collect(DISTINCT e.event_type) AS anomaly_events
        ORDER BY v.risk_score DESC, v.name ASC
        LIMIT {limit}
        """
        results = az_tools.query_knowledge_graph(query)
        if not results:
            return "No high risk vessels found."

        output = f"=== TOP {len(results)} HIGHEST RISK MARITIME VESSELS ===\n\n"
        for i, r in enumerate(results, 1):
            imo = r.get('imo') or 'UNASSIGNED'
            mmsi = r.get('mmsi') or 'UNKNOWN'
            name = r.get('name') or 'UNKNOWN_VESSEL'
            flag = r.get('flag') or 'FOREIGN / UNKNOWN'
            score = r.get('risk_score', 0)
            sanctioned = "YES 🚨 (Active Watchlist Match)" if r.get('is_sanctioned') else "No"
            events = ", ".join([ev for ev in r.get('anomaly_events', []) if ev]) or "High Risk Profile / Dark Event Vector"

            output += f"{i}. {name}\n"
            output += f"   - IMO Number     : {imo}\n"
            output += f"   - MMSI           : {mmsi}\n"
            output += f"   - Flag Country   : {flag}\n"
            output += f"   - Risk Score     : {score}/100 [CRITICAL]\n"
            output += f"   - Sanctioned     : {sanctioned}\n"
            output += f"   - Threat Rationale: Identified with {events} and high evasion probability.\n\n"
        return output
    except Exception as e:
        return f"Error retrieving top risk vessels: {str(e)}"


# ─────────────────────────────────────────────────────────
# TOOL 5: Collect Vessel Registry Data
# ─────────────────────────────────────────────────────────
@mcp.tool()
def collect_vessel_data(imo_number: str) -> str:
    """
    Collects and fuses vessel registry data from MarineTraffic, Equasis, and the knowledge graph.
    Returns vessel name, MMSI, flag, owner, and PSC inspection status.

    Args:
        imo_number: The 7-digit IMO number of the vessel.
    """
    try:
        result = az_tools.collect_and_fuse_data(imo_number)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"Error collecting data for IMO {imo_number}: {str(e)}"


if __name__ == "__main__":
    print("=" * 60)
    print("  Maritime Intelligence MCP HTTP Server")
    print("  Port: 7331 | Transport: SSE")
    print()
    print("  Agent Zero MCP config URL:")
    print("  http://host.docker.internal:7331/sse")
    print()
    print("  Available tools:")
    print("    run_full_maritime_analysis   <- USE THIS FIRST")
    print("    calculate_vessel_evasion_risk")
    print("    query_maritime_knowledge_graph")
    print("    list_vessels")
    print("    collect_vessel_data")
    print("=" * 60)
    log.info("Starting Maritime Intelligence MCP HTTP Server on port 7331...")
    mcp.run(transport="sse")
