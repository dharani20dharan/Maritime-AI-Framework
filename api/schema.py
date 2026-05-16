import strawberry
from typing import List, Optional
import sys
import os

# Ensure we can import from tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sanction_scorer import SanctionScorer

# ---------------------------------------------------------
# GraphQL Types
# ---------------------------------------------------------

@strawberry.type
class Event:
    event_id: str
    event_type: str
    location: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None

@strawberry.type
class Vessel:
    imo: str
    name: str
    mmsi: Optional[str] = None
    vessel_type: Optional[str] = None
    events: List[Event]

@strawberry.type
class RiskAssessment:
    imo: str
    risk_score: int
    flags: List[str]

# ---------------------------------------------------------
# Resolvers
# ---------------------------------------------------------

def get_vessel_resolver(imo: str, info: strawberry.Info) -> Optional[Vessel]:
    """Retrieves a vessel and its associated events from Neo4j."""
    driver = info.context["db"]
    with driver.session() as session:
        query = """
        MATCH (v:Vessel {imo: $imo})
        OPTIONAL MATCH (v)-[:INVOLVED_IN]->(e:Event)
        RETURN v.imo AS imo, v.name AS name, v.mmsi AS mmsi, v.type AS vessel_type,
               collect({
                   event_id: e.event_id, 
                   event_type: e.event_type, 
                   location: toString(e.location), 
                   start_time: toString(e.start_time), 
                   end_time: toString(e.end_time)
               }) AS events
        """
        result = session.run(query, imo=imo).single()
        
        if not result or not result["imo"]:
            return None
            
        events = []
        for e in result["events"]:
            if e["event_id"]: # Check if event actually exists
                events.append(Event(
                    event_id=e["event_id"],
                    event_type=e["event_type"],
                    location=e["location"],
                    start_time=e["start_time"],
                    end_time=e["end_time"]
                ))
                
        return Vessel(
            imo=result["imo"],
            name=result["name"],
            mmsi=result["mmsi"],
            vessel_type=result["vessel_type"],
            events=events
        )

def evaluate_vessel_risk_resolver(imo: str) -> RiskAssessment:
    """Executes the Python SanctionScorer tool."""
    scorer = SanctionScorer()
    try:
        score, flags = scorer.calculate_risk(imo)
        return RiskAssessment(
            imo=imo,
            risk_score=score,
            flags=flags
        )
    finally:
        scorer.close()

# ---------------------------------------------------------
# Schema Definition
# ---------------------------------------------------------

@strawberry.type
class Query:
    @strawberry.field
    def get_vessel(self, imo: str, info: strawberry.Info) -> Optional[Vessel]:
        return get_vessel_resolver(imo, info)

@strawberry.type
class Mutation:
    @strawberry.mutation
    def evaluate_vessel_risk(self, imo: str) -> RiskAssessment:
        return evaluate_vessel_risk_resolver(imo)

schema = strawberry.Schema(query=Query, mutation=Mutation)
