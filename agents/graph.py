"""
LangGraph Multi-Agent Workflow for Maritime Intelligence.
Orchestrates Data Retrieval, Rule Evaluation, and Output Generation.
"""
import sys
import os
import json
from pprint import pprint
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Ensure we can import from the tools directory at the project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.graph import StateGraph, END
from agents.state import AgentState
from tools.sanction_scorer import SanctionScorer
from tools.scrapers import RegistryCrossReferencer
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field
"""
Maritime AI Framework - Agentic Orchestrator (LangGraph)

This module defines the core multi-agent reasoning layer of the MAF system.
It utilizes LangGraph to manage state transitions between Data Retrieval,
Deterministic Rule Evaluation, and LLM-based Report Synthesis.

Routing logic guarantees that the LLM is only invoked for high-risk vessels,
optimizing performance and API costs.
"""

from typing import List

# ---------------------------------------------------------
# Output Schema
# ---------------------------------------------------------
class SAROutput(BaseModel):
    hypothesis: str = Field(description="A brief hypothesis on what the vessel might be doing based on the data.")
    evidence_for: List[str] = Field(description="List of evidence points that support the hypothesis.")
    evidence_against: List[str] = Field(description="List of evidence points that contradict the hypothesis or suggest normal behavior.")
    verdict: str = Field(description="Final classification: CONFIRMED, DISMISSED, or ESCALATE")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")

# ---------------------------------------------------------
# Node 1: Data Retrieval Agent
# ---------------------------------------------------------
def retrieve_data_node(state: AgentState):
    """Retrieves raw AIS and registry data."""
    print(f"\n[Agent 1: Data Retriever] Fetching raw data and registry info for IMO: {state['vessel_imo']}")
    
    # Mock AIS Data (In production this comes from Kafka/Neo4j)
    mock_raw_data = {
        "last_known_port": "UNKNOWN"
    }
    if state["vessel_imo"] == "9988776":
        mock_raw_data["last_known_port"] = "IRBND (Bandar Abbas)"
    elif state["vessel_imo"] == "9123456":
        mock_raw_data["last_known_port"] = "USNYC (New York)"

    # Parallel Registry Scraping
    referencer = RegistryCrossReferencer()
    registry_data = referencer.scrape_parallel(state["vessel_imo"])

    return {
        "raw_vessel_data": mock_raw_data,
        "registry_data": registry_data
    }

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
    except Exception as e:
        print(f"   -> [Warning] Neo4j connection failed: {e}. Using mock scores for testing.")
        # Provide a mock score to test the LLM node when DB is down
        mock_score = 85 if state["vessel_imo"] == "9988776" else 10
        mock_flags = ["Mocked Flag: Flag Hopping Risk"] if mock_score > 50 else []
        return {
            "risk_score": mock_score,
            "anomaly_flags": mock_flags,
            "is_suspicious": mock_score >= 50
        }
    finally:
        scorer.close()

# ---------------------------------------------------------
# Node 3: Output Generation Agent
# ---------------------------------------------------------
def generate_report_node(state: AgentState):
    """Uses an LLM to synthesize a Suspicious Activity Report (SAR)."""
    print(f"[Agent 3: Output Generator] Synthesizing Suspicious Activity Report (SAR) via Gemini...")
    
    registry = state.get("registry_data", {})
    vessel_name = registry.get("vessel_name", "UNKNOWN")
    flags_formatted = "\n  - ".join(state["anomaly_flags"]) if state["anomaly_flags"] else "None"
    
    system_prompt = f"""
    You are an expert maritime intelligence analyst. Your task is to review the following vessel data and generate a Suspicious Activity Report (SAR).
    
    TARGET VESSEL: {vessel_name} (IMO: {state['vessel_imo']})
    LAST KNOWN PORT: {state['raw_vessel_data'].get('last_known_port')}
    REGISTRY OWNER: {registry.get('registered_owner', 'Unknown')}
    REGISTRY FLAG: {registry.get('flag', 'Unknown')}
    
    RISK SCORE: {state['risk_score']}/100
    
    EVIDENCE FLAGS EXTRACTED FROM KNOWLEDGE GRAPH:
      - {flags_formatted}
      
    Evaluate the evidence carefully and output your hypothesis according to the required schema.
    """
    
    try:
        # We use Groq's super-fast Llama 3 model for structured reasoning
        llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0.1)
        structured_llm = llm.with_structured_output(SAROutput)
        
        result = structured_llm.invoke(system_prompt)
        
        # Convert pydantic model to dict for state storage
        report_dict = result.dict()
        print(f"   -> Verdict: {report_dict['verdict']} (Confidence: {report_dict['confidence']})")
        
        return {"final_report": report_dict}
        
    except Exception as e:
        print(f"   -> Error calling Gemini: {e}")
        return {"final_report": {"error": str(e), "hypothesis": "Failed to generate report."}}

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
    report = final_state.get("final_report")
    if report:
        pprint(report)
    else:
        print("No report generated.")
