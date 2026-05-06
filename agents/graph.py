"""
LangGraph Multi-Agent Workflow for Maritime Intelligence.
Orchestrates Data Retrieval, Rule Evaluation, and Output Generation.
"""
import sys
import os
import json
from pprint import pprint

# Ensure we can import from the tools directory at the project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.graph import StateGraph, END
from agents.state import AgentState
from tools.sanction_scorer import SanctionScorer

# ---------------------------------------------------------
# Node 1: Data Retrieval Agent
# ---------------------------------------------------------
def retrieve_data_node(state: AgentState):
    """Mocks retrieving raw AIS and registry data from Kafka/NiFi."""
    print(f"\n[Agent 1: Data Retriever] Fetching raw data for IMO: {state['vessel_imo']}")
    
    # In production, this calls the ingestion layer or Neo4j raw nodes
    mock_raw_data = {
        "vessel_name": "UNKNOWN",
        "last_known_port": "UNKNOWN"
    }
    if state["vessel_imo"] == "9988776":
        mock_raw_data["vessel_name"] = "SEA SHADOW"
        mock_raw_data["last_known_port"] = "IRBND (Bandar Abbas)"
    elif state["vessel_imo"] == "9123456":
        mock_raw_data["vessel_name"] = "OCEAN VOYAGER"
        mock_raw_data["last_known_port"] = "USNYC (New York)"

    return {"raw_vessel_data": mock_raw_data}

# ---------------------------------------------------------
# Node 2: Rule Evaluation Agent (The Critic)
# ---------------------------------------------------------
def evaluate_rules_node(state: AgentState):
    """Executes the Sanction Scorer (which runs Neo4j Cypher queries)."""
    print(f"[Agent 2: Rule Evaluator] Executing Neo4j anomaly queries...")
    
    scorer = SanctionScorer()
    try:
        score, flags = scorer.calculate_risk(state["vessel_imo"])
        is_suspicious = score >= 50
        
        print(f"   -> Calculated Risk Score: {score}")
        return {
            "risk_score": score,
            "anomaly_flags": flags,
            "is_suspicious": is_suspicious
        }
    finally:
        scorer.close()

# ---------------------------------------------------------
# Node 3: Output Generation Agent
# ---------------------------------------------------------
def generate_report_node(state: AgentState):
    """Uses an LLM to synthesize a Suspicious Activity Report (SAR)."""
    print(f"[Agent 3: Output Generator] Synthesizing Suspicious Activity Report (SAR)...")
    
    # We implement a mock LLM generator to allow this to run without an API key.
    # In production, use `ChatOpenAI` from langchain-openai.
    
    name = state["raw_vessel_data"]["vessel_name"]
    flags_formatted = "\n  - ".join(state["anomaly_flags"]) if state["anomaly_flags"] else "None"
    
    report = f"""
==================================================
SUSPICIOUS ACTIVITY REPORT (SAR)
==================================================
TARGET VESSEL : {name} (IMO: {state['vessel_imo']})
RISK SCORE    : {state['risk_score']}/100
CLASSIFICATION: {'HIGH RISK' if state['is_suspicious'] else 'LOW RISK'}

EVIDENCE EXTRACTED FROM KNOWLEDGE GRAPH:
  - {flags_formatted}

RECOMMENDATION:
"""
    if state["is_suspicious"]:
        report += "Immediate escalation to compliance team. Flag vessel in internal registry and halt commercial operations pending review."
    else:
        report += "No action required. Vessel cleared."
        
    report += "\n=================================================="
    return {"final_report": report}

# ---------------------------------------------------------
# Conditional Routing Edge
# ---------------------------------------------------------
def route_after_evaluation(state: AgentState):
    """If the vessel is not suspicious, skip the SAR generation to save LLM tokens."""
    if state["is_suspicious"]:
        print("   -> Routing to: Output Generator (SAR required)")
        return "generate_report"
    else:
        print("   -> Routing to: END (No SAR required)")
        return "end"

# ---------------------------------------------------------
# Build the LangGraph
# ---------------------------------------------------------
workflow = StateGraph(AgentState)

# Add nodes
workflow.add_node("retrieve_data", retrieve_data_node)
workflow.add_node("evaluate_rules", evaluate_rules_node)
workflow.add_node("generate_report", generate_report_node)

# Define edges
workflow.set_entry_point("retrieve_data")
workflow.add_edge("retrieve_data", "evaluate_rules")

# Conditional edge based on risk score
workflow.add_conditional_edges(
    "evaluate_rules",
    route_after_evaluation,
    {
        "generate_report": "generate_report",
        "end": END
    }
)

workflow.add_edge("generate_report", END)

# Compile graph
app = workflow.compile()

# ---------------------------------------------------------
# Example Execution
# ---------------------------------------------------------
if __name__ == "__main__":
    print("--- Executing LangGraph Workflow for Normal Vessel ---")
    initial_state_1 = {"vessel_imo": "9123456"}
    app.invoke(initial_state_1)
    
    print("\n--- Executing LangGraph Workflow for Suspicious Vessel ---")
    initial_state_2 = {"vessel_imo": "9988776"}
    final_state = app.invoke(initial_state_2)
    
    print("\n[Final Output Received from Graph]")
    print(final_state.get("final_report", "No report generated."))
