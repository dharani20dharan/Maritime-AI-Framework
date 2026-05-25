import strawberry
from typing import List, Optional
import sys
import os

# Ensure we can import from tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sanction_scorer import SanctionScorer
from tools.gds_centrality import GDSCentralityJob
from tools.sts_detector import STSDetector

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
    duration_hours: Optional[float] = None
    confidence: Optional[float] = None
    description: Optional[str] = None

@strawberry.type
class Vessel:
    imo: str
    name: str
    mmsi: Optional[str] = None
    vessel_type: Optional[str] = None
    betweenness_score: Optional[float] = None
    high_betweenness_parent: Optional[bool] = None
    events: List[Event]

@strawberry.type
class RiskAssessment:
    imo: str
    risk_score: int
    flags: List[str]

@strawberry.type
class CentralityResult:
    nodes_projected: Optional[int] = None
    relationships_projected: Optional[int] = None
    nodes_calculated: Optional[int] = None
    companies_tagged: Optional[int] = None
    vessels_updated: Optional[int] = None
    status: str
    error: Optional[str] = None

@strawberry.type
class STSDetectionResult:
    peer_imo: str
    peer_name: str
    location: str
    start_time: str
    end_time: str
    duration_hours: float
    confidence: float

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
               v.betweenness_score AS betweenness_score,
               v.high_betweenness_parent AS high_betweenness_parent,
               collect({
                   event_id: e.event_id, 
                   event_type: e.event_type, 
                   location: toString(e.location), 
                   start_time: toString(e.start_time), 
                   end_time: toString(e.end_time),
                   duration_hours: e.duration_hours,
                   confidence: e.confidence,
                   description: e.description
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
                    end_time=e["end_time"],
                    duration_hours=e.get("duration_hours"),
                    confidence=e.get("confidence"),
                    description=e.get("description")
                ))
                
        return Vessel(
            imo=result["imo"],
            name=result["name"],
            mmsi=result["mmsi"],
            vessel_type=result["vessel_type"],
            betweenness_score=result["betweenness_score"],
            high_betweenness_parent=result["high_betweenness_parent"],
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

def run_centrality_resolver(info: strawberry.Info) -> CentralityResult:
    """Runs GDS Betweenness Centrality."""
    try:
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        pw = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
        job = GDSCentralityJob(uri=uri, auth=(user, pw))
        res = job.run_betweenness_centrality()
        job.close()
        return CentralityResult(
            nodes_projected=res.get("nodes_projected"),
            relationships_projected=res.get("relationships_projected"),
            nodes_calculated=res.get("nodes_calculated"),
            companies_tagged=res.get("companies_tagged"),
            vessels_updated=res.get("vessels_updated"),
            status=res.get("status", "error"),
            error=res.get("error")
        )
    except Exception as e:
        return CentralityResult(status="error", error=str(e))

def run_sts_detection_resolver(imo: str, info: strawberry.Info) -> List[STSDetectionResult]:
    """Runs Spatio-Temporal STS Detection for a vessel."""
    try:
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        pw = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")
        detector = STSDetector(uri=uri, auth=(user, pw))
        res = detector.run_sts_detection(imo)
        detector.close()
        
        results = []
        for r in res:
            results.append(STSDetectionResult(
                peer_imo=r["peer_imo"],
                peer_name=r["peer_name"],
                location=r["location"],
                start_time=r["start_time"],
                end_time=r["end_time"],
                duration_hours=r["duration_hours"],
                confidence=r["confidence"]
            ))
        return results
    except Exception as e:
        print(f"Error running STS detection: {e}")
        return []

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

    @strawberry.mutation
    def run_gds_centrality(self, info: strawberry.Info) -> CentralityResult:
        return run_centrality_resolver(info)

    @strawberry.mutation
    def run_sts_detection(self, imo: str, info: strawberry.Info) -> List[STSDetectionResult]:
        return run_sts_detection_resolver(imo, info)

schema = strawberry.Schema(query=Query, mutation=Mutation)
