"""
MAF — Contract C1 : Adversarial Debate Log & SAR Export Schema
================================================================
This contract defines the canonical output schema produced by Agent C (Verifier)
after the multi-turn A→B→C adversarial debate loop.

Both engineers reference this file:
  Agent A (Hypothesis Generator) → produces hypotheses for debate
  Agent B (Adversarial Critic)   → produces falsification evidence
  Agent C (Verifier/Mediator)    → produces DebateVerdict conforming to this schema

Any change must be agreed by both engineers. Version this file on breaking changes.
Schema version: 1.0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
import json
import uuid


# ── Enumerations ──────────────────────────────────────────────────────────────

class VerdictType(str, Enum):
    """Final verdict category from Agent C."""
    CONFIRMED_ANOMALY      = "CONFIRMED_ANOMALY"        # Strong multi-source evidence
    PROBABLE_ANOMALY       = "PROBABLE_ANOMALY"         # Weight of evidence suggests anomaly
    INCONCLUSIVE           = "INCONCLUSIVE"             # Evidence balanced; needs more data
    PROBABLE_BENIGN        = "PROBABLE_BENIGN"          # Likely legitimate activity
    FALSE_POSITIVE         = "FALSE_POSITIVE"           # Rule fired but evidence refuted it


class ThreatCategory(str, Enum):
    """Primary threat category assigned by Agent C."""
    SANCTIONS_EVASION      = "SANCTIONS_EVASION"
    DARK_SHIP_ACTIVITY     = "DARK_SHIP_ACTIVITY"
    IDENTITY_FRAUD         = "IDENTITY_FRAUD"
    ILLICIT_STS_TRANSFER   = "ILLICIT_STS_TRANSFER"
    FLAG_HOPPING           = "FLAG_HOPPING"
    OWNERSHIP_OBFUSCATION  = "OWNERSHIP_OBFUSCATION"
    GPS_SPOOFING           = "GPS_SPOOFING"
    AIS_MANIPULATION       = "AIS_MANIPULATION"
    ROUTE_ANOMALY          = "ROUTE_ANOMALY"
    PORT_CALL_ANOMALY      = "PORT_CALL_ANOMALY"
    UNKNOWN                = "UNKNOWN"


class RecommendedAction(str, Enum):
    """Recommended analyst follow-up actions."""
    ESCALATE_TO_AUTHORITIES = "ESCALATE_TO_AUTHORITIES"
    FLAG_FOR_REVIEW         = "FLAG_FOR_REVIEW"
    REQUEST_AIS_TRANSPONDER = "REQUEST_AIS_TRANSPONDER"
    NOTIFY_FLAG_STATE       = "NOTIFY_FLAG_STATE"
    MONITOR_CLOSELY         = "MONITOR_CLOSELY"
    FILE_SAR_REPORT         = "FILE_SAR_REPORT"
    NO_ACTION               = "NO_ACTION"


# ── Evidence Fragment ─────────────────────────────────────────────────────────

@dataclass
class EvidenceFragment:
    """A single piece of evidence for or against the hypothesis."""
    source: str                     # e.g. "anomaly_rules.M-DARK-LOITER", "neo4j.sanctions"
    description: str                # Human-readable description
    weight: float                   # 0.0–1.0 evidential weight
    timestamp: Optional[str] = None # ISO 8601 UTC when the evidence was recorded
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "description": self.description,
            "weight": self.weight,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


# ── Debate Round ──────────────────────────────────────────────────────────────

@dataclass
class DebateRound:
    """Represents one complete A→B exchange within the debate loop."""
    round_number: int
    hypothesis: str                         # Agent A's assertion
    critique: str                           # Agent B's falsification attempt
    hypothesis_evidence: List[EvidenceFragment] = field(default_factory=list)
    critique_evidence: List[EvidenceFragment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "round_number": self.round_number,
            "hypothesis": self.hypothesis,
            "critique": self.critique,
            "hypothesis_evidence": [e.to_dict() for e in self.hypothesis_evidence],
            "critique_evidence": [e.to_dict() for e in self.critique_evidence],
        }


# ── Main Contract: DebateVerdict ──────────────────────────────────────────────

@dataclass
class DebateVerdict:
    """
    Contract C1 — canonical output from Agent C (Verifier/Mediator).

    This is the authoritative record of the adversarial debate and its outcome.
    It forms the basis for SAR (Suspicious Activity Report) generation.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    verdict_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    vessel_mmsi: str = ""
    vessel_imo: Optional[str] = None
    vessel_name: Optional[str] = None

    # ── Temporal ─────────────────────────────────────────────────────────────
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    analysis_window_start: Optional[str] = None  # ISO 8601 UTC
    analysis_window_end: Optional[str] = None    # ISO 8601 UTC

    # ── Verdict ──────────────────────────────────────────────────────────────
    verdict: VerdictType = VerdictType.INCONCLUSIVE
    threat_category: ThreatCategory = ThreatCategory.UNKNOWN
    confidence: float = 0.0         # 0.0 (no confidence) to 1.0 (certain)
    risk_score: float = 0.0         # Normalized 0–100 risk score

    # ── Debate Content ────────────────────────────────────────────────────────
    primary_hypothesis: str = ""    # Agent A's initial full hypothesis
    final_synthesis: str = ""       # Agent C's synthesized conclusion
    debate_rounds: List[DebateRound] = field(default_factory=list)

    # ── Evidence Aggregated ───────────────────────────────────────────────────
    evidence_for: List[EvidenceFragment] = field(default_factory=list)
    evidence_against: List[EvidenceFragment] = field(default_factory=list)
    triggered_rules: List[str] = field(default_factory=list)  # e.g. ["M-DARK-LOITER", "M-FLAG-HOP"]

    # ── Actions ───────────────────────────────────────────────────────────────
    recommended_actions: List[RecommendedAction] = field(default_factory=list)

    # ── SAR Export ───────────────────────────────────────────────────────────
    sar_narrative: Optional[str] = None         # Free-text SAR narrative for authorities
    sar_priority: Optional[str] = None          # "HIGH" | "MEDIUM" | "LOW"
    sar_filed_at: Optional[str] = None          # ISO 8601 UTC when SAR was filed
    analyst_notes: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "verdict_id": self.verdict_id,
            "vessel_mmsi": self.vessel_mmsi,
            "vessel_imo": self.vessel_imo,
            "vessel_name": self.vessel_name,
            "timestamp": self.timestamp,
            "analysis_window_start": self.analysis_window_start,
            "analysis_window_end": self.analysis_window_end,
            "verdict": self.verdict.value,
            "threat_category": self.threat_category.value,
            "confidence": self.confidence,
            "risk_score": self.risk_score,
            "primary_hypothesis": self.primary_hypothesis,
            "final_synthesis": self.final_synthesis,
            "debate_rounds": [r.to_dict() for r in self.debate_rounds],
            "evidence_for": [e.to_dict() for e in self.evidence_for],
            "evidence_against": [e.to_dict() for e in self.evidence_against],
            "triggered_rules": self.triggered_rules,
            "recommended_actions": [a.value for a in self.recommended_actions],
            "sar_narrative": self.sar_narrative,
            "sar_priority": self.sar_priority,
            "sar_filed_at": self.sar_filed_at,
            "analyst_notes": self.analyst_notes,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "DebateVerdict":
        """Deserialize from a dictionary."""
        verdict = cls()
        verdict.verdict_id = data.get("verdict_id", str(uuid.uuid4()))
        verdict.vessel_mmsi = data.get("vessel_mmsi", "")
        verdict.vessel_imo = data.get("vessel_imo")
        verdict.vessel_name = data.get("vessel_name")
        verdict.timestamp = data.get("timestamp", verdict.timestamp)
        verdict.analysis_window_start = data.get("analysis_window_start")
        verdict.analysis_window_end = data.get("analysis_window_end")
        verdict.verdict = VerdictType(data.get("verdict", "INCONCLUSIVE"))
        verdict.threat_category = ThreatCategory(data.get("threat_category", "UNKNOWN"))
        verdict.confidence = float(data.get("confidence", 0.0))
        verdict.risk_score = float(data.get("risk_score", 0.0))
        verdict.primary_hypothesis = data.get("primary_hypothesis", "")
        verdict.final_synthesis = data.get("final_synthesis", "")
        verdict.triggered_rules = data.get("triggered_rules", [])
        verdict.recommended_actions = [
            RecommendedAction(a) for a in data.get("recommended_actions", [])
        ]
        verdict.sar_narrative = data.get("sar_narrative")
        verdict.sar_priority = data.get("sar_priority")
        verdict.sar_filed_at = data.get("sar_filed_at")
        verdict.analyst_notes = data.get("analyst_notes")
        return verdict

    def is_actionable(self) -> bool:
        """Returns True if the verdict warrants analyst action."""
        return self.verdict in (
            VerdictType.CONFIRMED_ANOMALY,
            VerdictType.PROBABLE_ANOMALY,
        )

    def generate_sar_narrative(self) -> str:
        """Auto-generate a basic SAR narrative from the verdict data."""
        lines = [
            f"SUSPICIOUS ACTIVITY REPORT",
            f"==========================",
            f"Vessel MMSI: {self.vessel_mmsi}",
        ]
        if self.vessel_imo:
            lines.append(f"IMO: {self.vessel_imo}")
        if self.vessel_name:
            lines.append(f"Name: {self.vessel_name}")
        lines += [
            f"Analysis Period: {self.analysis_window_start} to {self.analysis_window_end}",
            f"",
            f"VERDICT: {self.verdict.value} (Confidence: {self.confidence*100:.1f}%)",
            f"Threat Category: {self.threat_category.value}",
            f"Risk Score: {self.risk_score:.1f}/100",
            f"",
            f"TRIGGERED RULES: {', '.join(self.triggered_rules) if self.triggered_rules else 'None'}",
            f"",
            f"SUMMARY:",
            f"{self.final_synthesis}",
            f"",
            f"RECOMMENDED ACTIONS:",
        ]
        for action in self.recommended_actions:
            lines.append(f"  - {action.value}")
        return "\n".join(lines)


# ── Validation Helper ─────────────────────────────────────────────────────────

def validate_verdict(verdict: DebateVerdict) -> tuple[bool, list[str]]:
    """
    Validate a DebateVerdict against Contract C1 constraints.
    Returns (is_valid, list_of_errors).
    """
    errors = []

    if not verdict.vessel_mmsi or len(verdict.vessel_mmsi) != 9:
        errors.append("vessel_mmsi must be a 9-character string")
    if not (0.0 <= verdict.confidence <= 1.0):
        errors.append("confidence must be between 0.0 and 1.0")
    if not (0.0 <= verdict.risk_score <= 100.0):
        errors.append("risk_score must be between 0.0 and 100.0")
    if not verdict.primary_hypothesis:
        errors.append("primary_hypothesis cannot be empty")
    if not verdict.final_synthesis:
        errors.append("final_synthesis cannot be empty")
    if verdict.verdict in (VerdictType.CONFIRMED_ANOMALY, VerdictType.PROBABLE_ANOMALY):
        if not verdict.recommended_actions:
            errors.append("Actionable verdict must have at least one recommended_action")

    return len(errors) == 0, errors


if __name__ == "__main__":
    # Self-test: create a sample verdict and validate it
    sample = DebateVerdict(
        vessel_mmsi="123456789",
        vessel_imo="IMO1234567",
        vessel_name="GHOST TRADER",
        verdict=VerdictType.CONFIRMED_ANOMALY,
        threat_category=ThreatCategory.SANCTIONS_EVASION,
        confidence=0.91,
        risk_score=87.5,
        primary_hypothesis="Vessel conducted dark STS transfer with sanctioned tanker.",
        final_synthesis="Strong multi-source evidence confirms sanctions evasion.",
        triggered_rules=["M-DARK-LOITER", "M-STS-DETECT", "M-SANCTION-LINK"],
        recommended_actions=[RecommendedAction.FILE_SAR_REPORT, RecommendedAction.ESCALATE_TO_AUTHORITIES],
        sar_priority="HIGH",
    )
    sample.sar_narrative = sample.generate_sar_narrative()
    is_valid, errors = validate_verdict(sample)
    print(f"Verdict valid: {is_valid}")
    if errors:
        print(f"Errors: {errors}")
    print(sample.to_json())
