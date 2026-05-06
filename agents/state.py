from typing import TypedDict, List, Dict, Any, Optional

class AgentState(TypedDict):
    """
    Represents the state of the LangGraph workflow as it passes between agents.
    """
    vessel_imo: str
    raw_vessel_data: Optional[Dict[str, Any]]
    risk_score: int
    anomaly_flags: List[str]
    final_report: Optional[str]
    is_suspicious: bool
