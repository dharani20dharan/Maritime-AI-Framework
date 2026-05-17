"""
Model Context Protocol (MCP) Server for Maritime Intelligence.
Exposes our custom Neo4j tools and anomaly detection engine to any MCP-compatible
LLM client (e.g., Claude Desktop, LangChain MCP Toolkits).
"""
import sys
import os

# Ensure we can import from the tools directory at the project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from tools.sanction_scorer import SanctionScorer

# Initialize the MCP Server
mcp = FastMCP("Maritime Intelligence Server")

@mcp.tool()
def calculate_vessel_evasion_risk(imo_number: str) -> str:
    """
    Evaluates a vessel for maritime sanction evasion risk using the Neo4j Knowledge Graph.
    Checks for: Flag Hopping, Obfuscated Ownership (Shell Companies), and STS Loitering.
    
    Args:
        imo_number: The 7-digit International Maritime Organization (IMO) number of the vessel.
    """
    scorer = SanctionScorer()
    try:
        score, flags = scorer.calculate_risk(imo_number)
        
        # Format the output cleanly for the LLM
        result = f"Evasion Risk Score: {score}/100\n"
        result += "Triggered Anomaly Flags:\n"
        if not flags:
            result += "- None (Vessel appears safe)\n"
        else:
            for flag in flags:
                result += f"- {flag}\n"
        return result
    except Exception as e:
        return f"Error executing graph query: {str(e)}"
    finally:
        scorer.close()

if __name__ == "__main__":
    # Run the server using Standard Input/Output (stdio) which is standard for MCP
    print("Starting Maritime Intelligence MCP Server...", file=sys.stderr)
    mcp.run()
