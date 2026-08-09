"""
Model Context Protocol (MCP) Server for Maritime Intelligence.
Exposes all maritime intelligence tools to any MCP-compatible
LLM client (e.g., Agent Zero web UI, Claude Desktop, LangChain MCP Toolkits).

Usage (Agent Zero website):
  Set the MCP server command to: python agents/mcp_server.py
  This exposes all maritime analysis tools to Agent Zero's native web UI.
"""
import sys
import os
import json
import logging

# Ensure we can import from the tools directory at the project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables first
from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP
from tools.sanction_scorer import SanctionScorer
import agents.agent_zero_tools as az_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

# Initialize the MCP Server
mcp = FastMCP("Maritime Intelligence Server")


@mcp.tool()
def calculate_vessel_evasion_risk(imo_number: str) -> str:
    """
    Evaluates a vessel for maritime sanction evasion risk using the Neo4j Knowledge Graph.
    Checks for: Flag Hopping, Obfuscated Ownership (Shell Companies), and STS Loitering.
    Returns an evasion risk score (0-100) and a list of triggered anomaly flags.

    Args:
        imo_number: The 7-digit International Maritime Organization (IMO) number of the vessel.
    """
    scorer = SanctionScorer()
    try:
        score, flags = scorer.calculate_risk(imo_number)
        result = f"Vessel IMO: {imo_number}\n"
        result += f"Evasion Risk Score: {score}/100\n"
        result += f"Risk Level: {'CRITICAL' if score >= 75 else 'HIGH' if score >= 50 else 'MEDIUM' if score >= 25 else 'LOW'}\n"
        result += "Triggered Anomaly Flags:\n"
        if not flags:
            result += "- None (Vessel appears safe)\n"
        else:
            for flag in flags:
                result += f"- {flag}\n"
        return result
    except Exception as e:
        return f"Error executing risk evaluation for IMO {imo_number}: {str(e)}"
    finally:
        scorer.close()


@mcp.tool()
def collect_vessel_data(imo_number: str) -> str:
    """
    Collection & Fusion Layer: Collects vessel registry details from MarineTraffic and Equasis,
    then fuses them with Neo4j database records into a unified vessel state.

    Args:
        imo_number: The 7-digit IMO number of the vessel (e.g., '9988776').
    """
    try:
        result = az_tools.collect_and_fuse_data(imo_number)
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error collecting data for IMO {imo_number}: {str(e)}"


@mcp.tool()
def query_knowledge_graph(cypher_query: str) -> str:
    """
    Knowledge Graph Layer: Executes a Cypher query on the Neo4j Maritime Knowledge Graph.
    Use this to explore vessel-company relationships, ownership chains, sanction links, and events.

    Example queries:
      - "MATCH (v:Vessel) RETURN v.imo, v.name LIMIT 10"
      - "MATCH (v:Vessel {imo: '9988776'})-[:SANCTIONED_BY]->(s:Sanction) RETURN s"
      - "MATCH (v:Vessel)-[:OWNED_BY]->(c:Company) RETURN v.name, c.name LIMIT 10"

    Args:
        cypher_query: A valid Cypher query string to execute against the Neo4j database.
    """
    try:
        results = az_tools.query_knowledge_graph(cypher_query)
        if not results:
            return "Query returned no results."
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        return f"Error executing knowledge graph query: {str(e)}"


@mcp.tool()
def analyze_vessel_behavior(imo_number: str) -> str:
    """
    Behavior Analysis Layer: Analyzes a vessel's AIS telemetry for anomalies including
    GPS spoofing, AIS transponder gaps, suspicious loitering, and track plausibility violations.
    First collects the vessel state, then runs the rule engine.

    Args:
        imo_number: The 7-digit IMO number of the vessel.
    """
    try:
        # Collect vessel state first
        vessel_state = az_tools.collect_and_fuse_data(imo_number)
        # Run behavior analysis
        anomalies = az_tools.evaluate_vessel_behavior(vessel_state)
        if not anomalies:
            return f"No behavioral anomalies detected for vessel IMO {imo_number}."
        output = f"Behavioral Analysis for IMO {imo_number}:\n"
        output += f"Found {len(anomalies)} anomaly/anomalies:\n"
        for a in anomalies:
            if "error" in a:
                output += f"  ERROR: {a['error']}\n"
            else:
                output += f"  [{a.get('severity','?')}] {a.get('rule_id','?')} - {a.get('name','?')}\n"
                output += f"    Evidence: {a.get('evidence','')}\n"
        return output
    except Exception as e:
        return f"Error analyzing behavior for IMO {imo_number}: {str(e)}"


@mcp.tool()
def detect_dark_ship_events(imo_number: str) -> str:
    """
    Dark Ship Detection Layer: Runs spatio-temporal correlation to detect Ship-to-Ship (STS)
    transfer events, mid-ocean rendezvous, and transponder gap overlaps with other vessels.

    Args:
        imo_number: The 7-digit IMO number of the target vessel.
    """
    try:
        detections = az_tools.detect_dark_ship_events(imo_number)
        if not detections:
            return f"No dark ship / STS transfer events detected for vessel IMO {imo_number}."
        output = f"Dark Ship Detection for IMO {imo_number}:\n"
        output += f"Found {len(detections)} STS transfer event(s):\n"
        for d in detections:
            if "error" in d:
                output += f"  ERROR: {d['error']}\n"
            else:
                output += f"  Peer vessel: {d.get('peer_name','?')} (IMO: {d.get('peer_imo','?')})\n"
                output += f"    Location: {d.get('location','?')}\n"
                output += f"    Duration: {d.get('duration_hours', 0):.1f} hours\n"
                output += f"    Confidence: {d.get('confidence', 0):.0%}\n"
        return output
    except Exception as e:
        return f"Error detecting dark ship events for IMO {imo_number}: {str(e)}"


@mcp.tool()
def evaluate_threat_level(imo_number: str) -> str:
    """
    Threat Assessment Layer: Computes the aggregate evasion risk score (0-100),
    extracts risk flags, and runs betweenness centrality to identify fleet broker connections.

    Args:
        imo_number: The 7-digit IMO number of the target vessel.
    """
    try:
        result = az_tools.evaluate_threat_level(imo_number)
        score = result.get("evasion_risk_score", 0)
        flags = result.get("triggered_flags", [])
        is_suspicious = result.get("is_suspicious", False)

        output = f"Threat Assessment for IMO {imo_number}:\n"
        output += f"  Evasion Risk Score: {score}/100\n"
        output += f"  Risk Level: {'CRITICAL' if score >= 75 else 'HIGH' if score >= 50 else 'MEDIUM' if score >= 25 else 'LOW'}\n"
        output += f"  Suspicious: {'YES - Recommend further investigation' if is_suspicious else 'No'}\n"
        output += "  Risk Flags:\n"
        if not flags:
            output += "    - None triggered\n"
        else:
            for flag in flags:
                output += f"    - {flag}\n"

        gds = result.get("gds_betweenness_centrality", {})
        if gds and "status" in gds:
            output += f"\n  Fleet Broker Analysis (GDS): {gds.get('status','')}\n"
            if "companies_tagged" in gds:
                output += f"    Companies analyzed: {gds['companies_tagged']}\n"

        if "error" in result:
            output += f"\n  Note: Partial result - DB error: {result['error']}\n"

        return output
    except Exception as e:
        return f"Error evaluating threat level for IMO {imo_number}: {str(e)}"


@mcp.tool()
def run_full_maritime_analysis(imo_number: str) -> str:
    """
    FULL ANALYSIS: Runs the complete Maritime Intelligence pipeline for a vessel.
    Performs: Data Collection → Behavior Analysis → Dark Ship Detection → Threat Assessment.
    This is the primary tool to use when investigating a suspicious vessel.

    Args:
        imo_number: The 7-digit IMO number of the vessel to investigate (e.g., '9988776').
    """
    output = f"=== MARITIME INTELLIGENCE ANALYSIS: IMO {imo_number} ===\n\n"

    # Step 1: Data Collection
    output += "--- Step 1: Data Collection & Fusion ---\n"
    try:
        vessel_state = az_tools.collect_and_fuse_data(imo_number)
        output += f"  Vessel Name: {vessel_state.get('vessel_name', 'UNKNOWN')}\n"
        output += f"  MMSI: {vessel_state.get('mmsi', 'UNKNOWN')}\n"
        output += f"  Type: {vessel_state.get('vessel_type', 'UNKNOWN')}\n"
        registry = vessel_state.get("registry", {})
        output += f"  Flag: {registry.get('flag', 'UNKNOWN')}\n"
        output += f"  Owner: {registry.get('owner', 'UNKNOWN')}\n"
        output += f"  PSC Status: {registry.get('psc_inspections', 'UNKNOWN')}\n"
        output += f"  Sources fused: {', '.join(vessel_state.get('sources_fused', []))}\n\n"
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
            output += f"  {len(anomalies)} anomaly/anomalies detected:\n"
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
            output += f"  {len(dark_events)} STS event(s) detected:\n"
            for d in dark_events:
                if "error" not in d:
                    output += f"    Rendezvous with {d.get('peer_name','?')} for {d.get('duration_hours',0):.1f}h (Conf: {d.get('confidence',0):.0%})\n"
            output += "\n"
    except Exception as e:
        output += f"  ERROR: {e}\n\n"

    # Step 4: Threat Assessment
    output += "--- Step 4: Threat Level Assessment ---\n"
    try:
        threat = az_tools.evaluate_threat_level(imo_number)
        score = threat.get("evasion_risk_score", 0)
        flags = threat.get("triggered_flags", [])
        output += f"  Final Risk Score: {score}/100\n"
        output += f"  Risk Level: {'🔴 CRITICAL' if score >= 75 else '🟠 HIGH' if score >= 50 else '🟡 MEDIUM' if score >= 25 else '🟢 LOW'}\n"
        if flags:
            output += "  Risk Flags:\n"
            for flag in flags:
                output += f"    - {flag}\n"
        output += "\n"
    except Exception as e:
        output += f"  ERROR: {e}\n\n"
        score = 0

    # Final verdict
    output += "=== FINAL VERDICT ===\n"
    if score >= 50:
        output += f"⚠️  VESSEL IMO {imo_number} IS SUSPICIOUS (Score: {score}/100)\n"
        output += "Recommended Actions: Enhanced monitoring, port authority notification, sanctions check.\n"
    else:
        output += f"✅ Vessel IMO {imo_number} appears NORMAL (Score: {score}/100)\n"
        output += "Standard monitoring protocols apply.\n"

    return output


if __name__ == "__main__":
    # Run the server using Standard Input/Output (stdio) which is standard for MCP
    print("Starting Maritime Intelligence MCP Server...", file=sys.stderr)
    print("Available tools:", file=sys.stderr)
    print("  - calculate_vessel_evasion_risk", file=sys.stderr)
    print("  - collect_vessel_data", file=sys.stderr)
    print("  - query_knowledge_graph", file=sys.stderr)
    print("  - analyze_vessel_behavior", file=sys.stderr)
    print("  - detect_dark_ship_events", file=sys.stderr)
    print("  - evaluate_threat_level", file=sys.stderr)
    print("  - run_full_maritime_analysis", file=sys.stderr)
    mcp.run()
