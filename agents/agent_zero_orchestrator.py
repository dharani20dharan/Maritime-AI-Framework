"""
Agent Zero Multi-Agent Orchestrator.
Coordinates the DRDO Capability-Centric Maritime Intelligence layers using
a hierarchical Superior-Subordinate delegation model.
"""
import sys
import os
import json
import logging
import time
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

# Load env variables
load_dotenv()

# Ensure we can import from the root and tools directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from neo4j import GraphDatabase

from agents.agent_zero_config import AGENT_PROFILES
import agents.agent_zero_tools as az_tools

# Setup colored output/logs for different agent roles
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# Helper logger that prints with distinct agent tags
def agent_log(agent_name: str, message: str, color=Colors.OKBLUE):
    print(f"{color}{Colors.BOLD}[{agent_name}]{Colors.ENDC}{color} {message}{Colors.ENDC}")

# ---------------------------------------------------------
# 1. Defining the Agent Zero Tools for LangChain
# ---------------------------------------------------------

@tool
def collect_and_fuse_data_tool(imo_number: str) -> str:
    """
    Collection & Fusion Layer: Collects registry details (MarineTraffic, Equasis) and fuses observations.
    
    Args:
        imo_number: The 7-digit IMO number of the vessel.
    """
    res = az_tools.collect_and_fuse_data(imo_number)
    return json.dumps(res, indent=2)

@tool
def query_knowledge_graph_tool(cypher_query: str) -> str:
    """
    Knowledge Graph Layer: Runs a Cypher query on the Neo4j Knowledge Graph database.
    
    Args:
        cypher_query: Cypher statement to run on Neo4j.
    """
    res = az_tools.query_knowledge_graph(cypher_query)
    return json.dumps(res, indent=2)

@tool
def evaluate_vessel_behavior_tool(vessel_state: dict) -> str:
    """
    Behavior Analysis Layer: Evaluates vessel state dictionary against rule engine (GPS spoofing, gaps, loitering).
    
    Args:
        vessel_state: Dictionary containing vessel history and metadata.
    """
    res = az_tools.evaluate_vessel_behavior(vessel_state)
    return json.dumps(res, indent=2)

@tool
def detect_dark_ship_events_tool(imo_number: str) -> str:
    """
    Dark Ship Detection Layer: Runs co-loitering/rendezvous analysis to detect Ship-to-Ship (STS) events.
    
    Args:
        imo_number: The 7-digit IMO number of the target vessel.
    """
    res = az_tools.detect_dark_ship_events(imo_number)
    return json.dumps(res, indent=2)

@tool
def evaluate_threat_level_tool(imo_number: str) -> str:
    """
    Threat Assessment Layer: Calculates evasion risk score (0-100) and extracts risk flags.
    
    Args:
        imo_number: The 7-digit IMO number of the target vessel.
    """
    res = az_tools.evaluate_threat_level(imo_number)
    return json.dumps(res, indent=2)

@tool
def save_suspicious_activity_report_tool(imo_number: str, hypothesis: str, evidence_for: List[str], evidence_against: List[str], verdict: str, confidence: float) -> str:
    """
    Recommendation & Action Layer: Saves the final Suspicious Activity Report (SAR) in Neo4j.
    
    Args:
        imo_number: Target vessel IMO.
        hypothesis: Narrative explanation of suspicious activity.
        evidence_for: List of evidence strings supporting the hypothesis.
        evidence_against: List of evidence strings contradicting the hypothesis.
        verdict: CONFIRMED, DISMISSED, or ESCALATE.
        confidence: Confidence score (0.0 to 1.0).
    """
    res = az_tools.save_suspicious_activity_report(imo_number, hypothesis, evidence_for, evidence_against, verdict, confidence)
    return json.dumps(res, indent=2)

# Tool Map for resolving tool executions
TOOL_MAP = {
    "collect_and_fuse_data": collect_and_fuse_data_tool,
    "query_knowledge_graph": query_knowledge_graph_tool,
    "evaluate_vessel_behavior": evaluate_vessel_behavior_tool,
    "detect_dark_ship_events": detect_dark_ship_events_tool,
    "evaluate_threat_level": evaluate_threat_level_tool,
    "save_suspicious_activity_report": save_suspicious_activity_report_tool
}

# ---------------------------------------------------------
# 2. Defining the Agent Class
# ---------------------------------------------------------

class AgentZero:
    def __init__(self, profile_name: str, depth: int = 0):
        if profile_name not in AGENT_PROFILES:
            raise ValueError(f"Agent profile '{profile_name}' not found in configuration.")
            
        self.profile_name = profile_name
        self.config = AGENT_PROFILES[profile_name]
        self.depth = depth
        self.agent_id = f"{profile_name} (D-{depth})"
        
        # Select colors based on depth/role
        self.color = Colors.OKCYAN if depth > 0 else Colors.OKGREEN
        if profile_name == "OrchestratorAgent":
            self.color = Colors.HEADER
            
        # Initialize LLM
        model_name = self.config["model"]
        provider = os.getenv("LLM_PROVIDER", "groq").lower()
        
        if provider == "ollama":
            from langchain_openai import ChatOpenAI
            api_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
            self.llm = ChatOpenAI(
                model=model_name,
                temperature=0.1,
                openai_api_key="ollama",  # dummy key required by LangChain ChatOpenAI class
                openai_api_base=api_base
            )
        elif provider == "openai":
            from langchain_openai import ChatOpenAI
            self.llm = ChatOpenAI(
                model=model_name,
                temperature=0.1,
                openai_api_key=os.getenv("OPENAI_API_KEY")
            )
        else:  # Default to Groq
            api_key = os.getenv("GROQ_API_KEY")
            try:
                self.llm = ChatGroq(model=model_name, temperature=0.1, groq_api_key=api_key)
            except Exception as e:
                # Fallback to the ultra-fast 8b model if 70b rate limits/authentication fails
                agent_log(self.agent_id, f"Failed to load Groq model {model_name}: {e}. Falling back to llama-3.1-8b-instant.", Colors.WARNING)
                self.llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0.1, groq_api_key=api_key)
            
        self.history = []
        
        # Bind available tools
        self.tools = []
        for tname in self.config["tools"]:
            if tname == "call_subordinate":
                # We define call_subordinate tool inline to capture self/context reference
                @tool
                def call_subordinate(agent_profile_name: str, task_description: str) -> str:
                    """
                    Delegates a sub-task or analysis layer to a specialized subordinate agent.
                    Available agent profiles:
                    - CollectionFusionAgent (Collects & fuses data)
                    - KnowledgeGraphAgent (Queries Neo4j relationships)
                    - BehaviorDarkShipAgent (Checks speed/AIS anomalies and STS transfers)
                    - ThreatAssessmentAgent (Evaluates evasion risk scores)
                    - RecommendationActionAgent (Saves SAR report in database)
                    """
                    sub_agent = AgentZero(agent_profile_name, depth=self.depth + 1)
                    agent_log(self.agent_id, f"Spawning Subordinate -> {agent_profile_name} for task: '{task_description}'", Colors.WARNING)
                    result = sub_agent.communicate(task_description)
                    agent_log(self.agent_id, f"Received response from Subordinate -> {agent_profile_name}.", Colors.OKBLUE)
                    return result
                self.tools.append(call_subordinate)
            elif tname in TOOL_MAP:
                self.tools.append(TOOL_MAP[tname])
                
        if self.tools:
            self.llm_with_tools = self.llm.bind_tools(self.tools)
        else:
            self.llm_with_tools = self.llm

    def communicate(self, message: str) -> str:
        """Runs the main agent loop (Reasoning -> Tool call -> Response)."""
        agent_log(self.agent_id, f"Instruction: '{message}'", self.color)
        
        self.history.append(HumanMessage(content=message))
        
        max_iterations = 4 if self.depth > 0 else 8
        for i in range(max_iterations):
            # Formulate the prompt messages list
            messages = [SystemMessage(content=self.config["system_prompt"])] + self.history
            
            agent_log(self.agent_id, f"Reasoning (Step {i+1})...", self.color)
            
            try:
                response = self.llm_with_tools.invoke(messages)
            except Exception as e:
                err_msg = f"LLM Invocation error: {e}"
                agent_log(self.agent_id, err_msg, Colors.FAIL)
                return f"Error: {err_msg}"
                
            self.history.append(response)
            
            # Check for tool calls
            if hasattr(response, 'tool_calls') and response.tool_calls:
                for tool_call in response.tool_calls:
                    tname = tool_call["name"]
                    targs = tool_call["args"]
                    tcall_id = tool_call["id"]
                    
                    agent_log(self.agent_id, f"Executing Tool: {tname} with args: {targs}", self.color)
                    
                    # Find tool in our tools list
                    matching_tool = next((t for t in self.tools if t.name == tname), None)
                    if matching_tool:
                        try:
                            # Invoke tool
                            tool_result = matching_tool.invoke(targs)
                        except Exception as t_err:
                            tool_result = f"Tool execution failed: {t_err}"
                    else:
                        tool_result = f"Tool '{tname}' is not bound to this agent."
                        
                    agent_log(self.agent_id, f"Tool Result: {tool_result[:300]}...", self.color)
                    
                    # Append tool result to history
                    self.history.append(ToolMessage(content=str(tool_result), tool_call_id=tcall_id))
            else:
                # No tool calls means agent completed reasoning and returned final answer
                agent_log(self.agent_id, f"Completed Task. Output: {response.content}", self.color)
                return response.content
                
        agent_log(self.agent_id, "Max reasoning steps reached without final response.", Colors.WARNING)
        return "Error: Maximum reasoning iterations exceeded."

# ---------------------------------------------------------
# 3. Main runner for analysis testing
# ---------------------------------------------------------

if __name__ == "__main__":
    print(f"{Colors.HEADER}{Colors.BOLD}=== Starting Agent Zero Capability-Centric Orchestrator ==={Colors.ENDC}\n")
    
    # 1. Fetch test vessels from database
    test_vessels = []
    try:
        driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "maf_neo4j_2024"))
        with driver.session() as session:
            # High-risk target
            risk_result = session.run("MATCH (v:Vessel)-[:INVOLVED_IN]->(e:Event) WHERE v.imo STARTS WITH '900' RETURN DISTINCT v.imo AS imo LIMIT 1")
            test_vessels.extend([record["imo"] for record in risk_result])
            
            # Normal commercial target
            safe_result = session.run("MATCH (v:Vessel) WHERE NOT v.imo STARTS WITH '900' RETURN DISTINCT v.imo AS imo LIMIT 1")
            test_vessels.extend([record["imo"] for record in safe_result])
        driver.close()
    except Exception as e:
        print(f"Failed to fetch live vessels from Neo4j: {e}. Falling back to static test IMOs.")
        # Fallbacks: 9988776 (Suspicious), 9123456 (Safe)
        test_vessels = ["9988776", "9123456"]
        
    if not test_vessels:
        print("No active vessels found for test.")
    else:
        # Initialize Orchestrator Agent (Superior)
        orchestrator = AgentZero("OrchestratorAgent")
        
        for imo in test_vessels:
            print(f"\n{Colors.BOLD}------------------------------------------------------------")
            print(f"INVESTIGATING TARGET VESSEL IMO: {imo}")
            print(f"------------------------------------------------------------{Colors.ENDC}\n")
            
            mission_prompt = f"Perform a comprehensive capability-centric threat analysis for vessel IMO {imo}. Query its registry details, inspect for speed/AIS behavioral anomalies, check for dark ship activities, compute the evasion risk level, and if deemed suspicious (risk score >= 50), save the final SAR report."
            
            start_time = time.time()
            final_assessment = orchestrator.communicate(mission_prompt)
            duration = time.time() - start_time
            
            print(f"\n{Colors.OKGREEN}{Colors.BOLD}=== ANALYSIS SUMMARY FOR IMO {imo} ({duration:.2f} seconds) ==={Colors.ENDC}")
            print(final_assessment)
            print("\n")
