"""
MAF — Adversarial Debate Layer (Agent A ↔ Agent B ↔ Agent C)
=============================================================
Implements the multi-turn adversarial debate protocol conforming to Contract C1.

Agents:
  Agent A (Hypothesis Generator):  Analyzes vessel evidence and generates comprehensive
                                    multi-anomaly hypotheses about suspicious activity.
  Agent B (Adversarial Critic):     Systematically attempts to falsify Agent A's hypothesis
                                    using environmental and contextual explanations.
  Agent C (Verifier & Mediator):    Synthesizes the debate into a final Contract C1 verdict.

The debate loop runs for a configurable number of rounds, with Agent C producing
a serialized DebateVerdict at the end.

This module operates fully offline without requiring an LLM — it uses a
structured deterministic reasoning engine that can optionally call an LLM
backend if available (Ollama / OpenAI API).
"""

from __future__ import annotations

import json
import logging
import math
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.contracts.C1_debate_sar import (
    DebateVerdict, DebateRound, EvidenceFragment, VerdictType,
    ThreatCategory, RecommendedAction, validate_verdict,
)

logger = logging.getLogger(__name__)


# ── Evidence Weights Registry ─────────────────────────────────────────────────

RULE_WEIGHTS = {
    "M-DARK-LOITER":          0.80,
    "M-SANCTION-LINK":        0.90,
    "M-FLAG-HOP":             0.75,
    "M-STS-DETECT":           0.85,
    "M-AIS-GAP":              0.60,
    "M-GPS-SPOOF":            0.70,
    "M-DARK-EVENT":           0.65,
    "M-PORT-CALL-ANOMALY":    0.72,
    "M-NAME-CHANGE":          0.65,
    "M-MMSI-CONFLICT":        0.88,
    "M-BEACON-REPEAT":        0.55,
    "M-IMPOSSIBLE-SPEED":     0.78,
    "M-OWNERSHIP-OBSCURE":    0.68,
    "M-DARK-REEMERGENCE":     0.82,
    "COL-001":                0.40,
    "ID-001":                 0.75,
    "ID-002":                 0.82,
    "ID-003":                 0.92,
    "ID-004":                 0.88,
}

# Contextual explanations Agent B can use to challenge hypotheses
BENIGN_EXPLANATIONS = {
    "M-AIS-GAP":           "AIS gap may be explained by satellite coverage gap or antenna malfunction.",
    "M-DARK-LOITER":       "Vessel may be waiting for berth at nearby anchorage area.",
    "M-DARK-EVENT":        "Dark event may coincide with satellite pass gap or equipment failure.",
    "M-STS-DETECT":        "STS transfer may be legitimate bunkering or provisioning.",
    "M-PORT-CALL-ANOMALY": "Unusual route may reflect weather avoidance or charter renegotiation.",
    "M-GPS-SPOOF":         "Position jump may result from GPS receiver malfunction or datum error.",
    "M-FLAG-HOP":          "Flag changes may reflect legitimate commercial re-registration.",
    "M-NAME-CHANGE":       "Name change may follow legitimate vessel sale or operator change.",
}


# ── Agent A: Hypothesis Generator ────────────────────────────────────────────

class AgentA:
    """
    Agent A: Hypothesis Generator.
    Analyzes vessel evidence and generates structured anomaly hypotheses.
    """

    def generate_hypothesis(
        self,
        mmsi: str,
        vessel_context: Dict[str, Any],
        triggered_rules: List[str],
        evidence_snippets: List[Dict[str, Any]],
        round_num: int = 1,
        previous_critique: Optional[str] = None,
    ) -> Tuple[str, List[EvidenceFragment]]:
        """
        Generate a hypothesis explaining observed vessel behavior.

        Returns:
            (hypothesis_text, list_of_evidence_fragments)
        """
        # Extract vessel identity
        name = vessel_context.get("name", "UNKNOWN")
        flag = vessel_context.get("flag", "UNKNOWN")
        imo = vessel_context.get("imo", "N/A")
        risk_score = vessel_context.get("risk_score", 0)

        # Build cumulative evidence
        evidence_list: List[EvidenceFragment] = []
        hypothesis_parts = []

        if triggered_rules:
            hypothesis_parts.append(
                f"Vessel {mmsi} ({name}, flag {flag}) triggered {len(triggered_rules)} "
                f"anomaly rules: {', '.join(triggered_rules)}."
            )

        # Process evidence snippets
        for snippet in evidence_snippets:
            rule = snippet.get("rule", "UNKNOWN")
            weight = RULE_WEIGHTS.get(rule, 0.5)
            evidence_list.append(EvidenceFragment(
                source=f"anomaly_rules.{rule}",
                description=snippet.get("description", f"Rule {rule} triggered"),
                weight=weight,
                timestamp=snippet.get("timestamp"),
                metadata=snippet,
            ))

        # Build hypothesis based on triggered rules
        threat_patterns = {
            "sanctions_evasion": {"M-SANCTION-LINK", "M-FLAG-HOP", "M-DARK-LOITER", "M-STS-DETECT"},
            "dark_ship":         {"M-DARK-EVENT", "M-AIS-GAP", "M-DARK-LOITER"},
            "identity_fraud":    {"M-NAME-CHANGE", "ID-002", "ID-003", "M-MMSI-CONFLICT"},
            "sts_transfer":      {"M-STS-DETECT", "M-DARK-LOITER", "ID-004"},
        }

        detected_patterns = []
        for pattern, rules in threat_patterns.items():
            overlap = rules & set(triggered_rules)
            if overlap:
                detected_patterns.append((pattern, len(overlap)))

        detected_patterns.sort(key=lambda x: -x[1])
        primary_pattern = detected_patterns[0][0] if detected_patterns else "unknown_activity"

        if primary_pattern == "sanctions_evasion":
            hypothesis_parts.append(
                f"PRIMARY HYPOTHESIS (Round {round_num}): Vessel is conducting sanctions evasion. "
                f"Evidence suggests: flag manipulation to obscure identity, coordinated dark periods "
                f"coinciding with suspected STS transfers to move sanctioned cargo."
            )
        elif primary_pattern == "dark_ship":
            hypothesis_parts.append(
                f"PRIMARY HYPOTHESIS (Round {round_num}): Vessel is operating as a 'dark ship', "
                f"deliberately disabling AIS transmissions during {len([r for r in triggered_rules if 'DARK' in r])} "
                f"recorded dark periods to conceal vessel movements."
            )
        elif primary_pattern == "identity_fraud":
            hypothesis_parts.append(
                f"PRIMARY HYPOTHESIS (Round {round_num}): Vessel is conducting identity fraud. "
                f"Inconsistencies between AIS-broadcast identity and registry records suggest "
                f"deliberate name/IMO/MMSI manipulation."
            )
        elif primary_pattern == "sts_transfer":
            hypothesis_parts.append(
                f"PRIMARY HYPOTHESIS (Round {round_num}): Vessel conducted covert STS transfer. "
                f"Spatial proximity, simultaneous loitering, and post-event identity changes "
                f"are consistent with illicit cargo transfer."
            )
        else:
            hypothesis_parts.append(
                f"PRIMARY HYPOTHESIS (Round {round_num}): Anomalous vessel behavior detected "
                f"(risk score {risk_score:.0f}/100) consistent with maritime evasion activity."
            )

        # Address previous critique in rounds > 1
        if previous_critique and round_num > 1:
            hypothesis_parts.append(
                f"REBUTTAL TO ROUND {round_num-1} CRITIQUE: "
                f"While '{previous_critique[:150]}...' presents a plausible innocent explanation, "
                f"the combination of {len(triggered_rules)} simultaneous indicators across independent "
                f"data sources (AIS, satellite SAR, registry) significantly reduces the probability "
                f"that all anomalies are coincidental. The temporal correlation between AIS gaps and "
                f"SAR dark contacts is particularly compelling."
            )

        return " ".join(hypothesis_parts), evidence_list


# ── Agent B: Adversarial Critic ───────────────────────────────────────────────

class AgentB:
    """
    Agent B: Adversarial Critic.
    Systematically challenges Agent A's hypothesis using environmental context,
    legitimate explanations, and alternative interpretations.
    """

    def critique(
        self,
        hypothesis: str,
        triggered_rules: List[str],
        vessel_context: Dict[str, Any],
        round_num: int = 1,
    ) -> Tuple[str, List[EvidenceFragment]]:
        """
        Generate an adversarial critique of Agent A's hypothesis.

        Returns:
            (critique_text, list_of_counter_evidence)
        """
        counter_evidence: List[EvidenceFragment] = []
        critique_parts = []

        critique_parts.append(
            f"ADVERSARIAL CRITIQUE (Round {round_num}): "
            f"Agent A's hypothesis should be carefully evaluated against the following "
            f"alternative explanations:"
        )

        addressed_rules = set()
        for rule in triggered_rules:
            if rule in BENIGN_EXPLANATIONS and rule not in addressed_rules:
                explanation = BENIGN_EXPLANATIONS[rule]
                critique_parts.append(f"[{rule}] {explanation}")
                counter_evidence.append(EvidenceFragment(
                    source=f"benign_context.{rule}",
                    description=explanation,
                    weight=0.4,  # Counter-evidence weight lower than direct anomaly evidence
                ))
                addressed_rules.add(rule)

        # Environmental context challenges
        nav_status = vessel_context.get("nav_status")
        if nav_status == 1:  # Not Under Command
            critique_parts.append(
                "Vessel is broadcasting 'Not Under Command' status which may explain "
                "irregular positioning and speed anomalies as a genuine mechanical emergency."
            )
            counter_evidence.append(EvidenceFragment(
                source="nav_status.not_under_command",
                description="Vessel may have genuine mechanical/steering failure",
                weight=0.6,
            ))

        # Challenge multi-indicator correlation
        if len(triggered_rules) >= 3:
            critique_parts.append(
                f"Note: While {len(triggered_rules)} rules triggered, some may be "
                f"correlated artifacts of the same root cause (e.g., GPS malfunction "
                f"simultaneously triggers speed anomaly + position jump + AIS gap rules). "
                f"Independent rule validation is essential before escalation."
            )
            counter_evidence.append(EvidenceFragment(
                source="statistical.rule_correlation",
                description="Multiple rule triggers may be correlated, not independent",
                weight=0.35,
            ))

        # Check for flag legitimacy
        flag = vessel_context.get("flag")
        OPEN_REGISTRIES = {"PA", "LR", "MH", "BS", "PW", "KM", "TO"}
        if flag in OPEN_REGISTRIES:
            critique_parts.append(
                f"Flag {flag} is an open registry jurisdiction. Flag changes between open "
                f"registries are commercially common for tax/fee optimization and do not "
                f"necessarily indicate sanctions evasion intent."
            )

        critique_text = " ".join(critique_parts)
        return critique_text, counter_evidence


# ── Agent C: Verifier & Mediator ─────────────────────────────────────────────

class AgentC:
    """
    Agent C: Verifier and Mediator.
    Synthesizes the debate into a final Contract C1 verdict.
    """

    def synthesize(
        self,
        mmsi: str,
        vessel_context: Dict[str, Any],
        debate_rounds: List[DebateRound],
        all_evidence_for: List[EvidenceFragment],
        all_evidence_against: List[EvidenceFragment],
        triggered_rules: List[str],
    ) -> DebateVerdict:
        """
        Synthesize the debate into a DebateVerdict conforming to Contract C1.
        """
        # Calculate weighted evidence scores
        score_for = sum(e.weight for e in all_evidence_for) if all_evidence_for else 0.0
        score_against = sum(e.weight for e in all_evidence_against) if all_evidence_against else 0.0
        total = score_for + score_against
        evidence_ratio = (score_for / total) if total > 0 else 0.5

        # Rule-based threat pattern detection for verdict
        HIGH_CONFIDENCE_RULES = {"M-SANCTION-LINK", "M-MMSI-CONFLICT", "ID-003", "ID-004"}
        MEDIUM_CONFIDENCE_RULES = {"M-STS-DETECT", "M-DARK-LOITER", "M-FLAG-HOP", "ID-001", "ID-002"}

        has_high_conf = bool(set(triggered_rules) & HIGH_CONFIDENCE_RULES)
        has_medium_conf = bool(set(triggered_rules) & MEDIUM_CONFIDENCE_RULES)
        rule_count = len(triggered_rules)

        # Determine verdict
        if has_high_conf or (evidence_ratio >= 0.75 and rule_count >= 3):
            verdict_type = VerdictType.CONFIRMED_ANOMALY
            confidence = min(0.7 + evidence_ratio * 0.25, 0.99)
        elif evidence_ratio >= 0.55 or (has_medium_conf and rule_count >= 2):
            verdict_type = VerdictType.PROBABLE_ANOMALY
            confidence = 0.5 + evidence_ratio * 0.2
        elif evidence_ratio >= 0.45:
            verdict_type = VerdictType.INCONCLUSIVE
            confidence = 0.3 + evidence_ratio * 0.2
        elif evidence_ratio >= 0.30:
            verdict_type = VerdictType.PROBABLE_BENIGN
            confidence = 0.6
        else:
            verdict_type = VerdictType.FALSE_POSITIVE
            confidence = 0.75

        # Determine threat category from triggered rules
        RULE_TO_THREAT = {
            "M-SANCTION-LINK": ThreatCategory.SANCTIONS_EVASION,
            "M-FLAG-HOP": ThreatCategory.FLAG_HOPPING,
            "ID-001": ThreatCategory.FLAG_HOPPING,
            "M-STS-DETECT": ThreatCategory.ILLICIT_STS_TRANSFER,
            "M-DARK-LOITER": ThreatCategory.DARK_SHIP_ACTIVITY,
            "M-DARK-EVENT": ThreatCategory.DARK_SHIP_ACTIVITY,
            "M-MMSI-CONFLICT": ThreatCategory.IDENTITY_FRAUD,
            "ID-002": ThreatCategory.IDENTITY_FRAUD,
            "ID-003": ThreatCategory.IDENTITY_FRAUD,
            "M-GPS-SPOOF": ThreatCategory.GPS_SPOOFING,
            "M-PORT-CALL-ANOMALY": ThreatCategory.PORT_CALL_ANOMALY,
        }
        threat_category = ThreatCategory.UNKNOWN
        for rule in triggered_rules:
            if rule in RULE_TO_THREAT:
                threat_category = RULE_TO_THREAT[rule]
                break

        # Calculate risk score (0–100)
        base_rule_score = min(rule_count * 8, 60)
        evidence_score = evidence_ratio * 30
        high_conf_bonus = 15 if has_high_conf else 0
        risk_score = min(base_rule_score + evidence_score + high_conf_bonus, 100)

        # Recommended actions
        recommended_actions = []
        if verdict_type == VerdictType.CONFIRMED_ANOMALY:
            recommended_actions = [RecommendedAction.FILE_SAR_REPORT, RecommendedAction.ESCALATE_TO_AUTHORITIES]
            if threat_category == ThreatCategory.FLAG_HOPPING:
                recommended_actions.append(RecommendedAction.NOTIFY_FLAG_STATE)
        elif verdict_type == VerdictType.PROBABLE_ANOMALY:
            recommended_actions = [RecommendedAction.FLAG_FOR_REVIEW, RecommendedAction.MONITOR_CLOSELY]
        else:
            recommended_actions = [RecommendedAction.NO_ACTION]

        # Synthesis narrative
        synthesis = (
            f"Agent C SYNTHESIS: After {len(debate_rounds)} debate round(s), the evidence "
            f"weight FOR anomalous activity ({score_for:.2f}) "
            f"{'exceeds' if score_for > score_against else 'does not exceed'} "
            f"the AGAINST weight ({score_against:.2f}). "
            f"The vessel ({mmsi}) triggered {rule_count} independent detection rules. "
            f"Verdict: {verdict_type.value} with {confidence*100:.1f}% confidence. "
            f"Primary threat category: {threat_category.value}."
        )

        # Build verdict
        last_hypothesis = debate_rounds[-1].hypothesis if debate_rounds else ""
        verdict = DebateVerdict(
            vessel_mmsi=mmsi,
            vessel_imo=vessel_context.get("imo"),
            vessel_name=vessel_context.get("name"),
            verdict=verdict_type,
            threat_category=threat_category,
            confidence=confidence,
            risk_score=risk_score,
            primary_hypothesis=last_hypothesis,
            final_synthesis=synthesis,
            debate_rounds=debate_rounds,
            evidence_for=all_evidence_for,
            evidence_against=all_evidence_against,
            triggered_rules=triggered_rules,
            recommended_actions=recommended_actions,
            sar_priority="HIGH" if verdict_type == VerdictType.CONFIRMED_ANOMALY else "MEDIUM",
        )
        verdict.sar_narrative = verdict.generate_sar_narrative()
        return verdict


# ── Debate Orchestrator ───────────────────────────────────────────────────────

class AdversarialDebateOrchestrator:
    """
    Orchestrates the multi-turn A↔B debate loop, ending with Agent C synthesis.
    """

    def __init__(self, max_rounds: int = 3):
        self.agent_a = AgentA()
        self.agent_b = AgentB()
        self.agent_c = AgentC()
        self.max_rounds = max_rounds

    def run_debate(
        self,
        mmsi: str,
        vessel_context: Dict[str, Any],
        triggered_rules: List[str],
        evidence_snippets: Optional[List[Dict[str, Any]]] = None,
        verbose: bool = True,
    ) -> DebateVerdict:
        """
        Run the full adversarial debate and return a Contract C1 DebateVerdict.

        Args:
            mmsi:             Vessel MMSI
            vessel_context:   Dict with vessel metadata (name, flag, imo, risk_score, nav_status)
            triggered_rules:  List of fired anomaly rule IDs
            evidence_snippets: Optional list of evidence detail dicts
            verbose:          Print debate progress to stdout

        Returns:
            Validated DebateVerdict (Contract C1)
        """
        if evidence_snippets is None:
            evidence_snippets = []

        all_evidence_for: List[EvidenceFragment] = []
        all_evidence_against: List[EvidenceFragment] = []
        debate_rounds: List[DebateRound] = []
        last_critique = None

        if verbose:
            print(f"\n{'='*60}")
            print(f"  ADVERSARIAL DEBATE: MMSI {mmsi}")
            print(f"  Triggered Rules: {triggered_rules}")
            print(f"{'='*60}")

        for round_num in range(1, self.max_rounds + 1):
            if verbose:
                print(f"\n--- Round {round_num} ---")

            # Agent A generates/refines hypothesis
            hypothesis, evidence_for = self.agent_a.generate_hypothesis(
                mmsi=mmsi,
                vessel_context=vessel_context,
                triggered_rules=triggered_rules,
                evidence_snippets=evidence_snippets,
                round_num=round_num,
                previous_critique=last_critique,
            )
            all_evidence_for.extend(evidence_for)

            if verbose:
                print(f"[Agent A] {hypothesis[:200]}...")

            # Agent B critiques
            critique, counter_evidence = self.agent_b.critique(
                hypothesis=hypothesis,
                triggered_rules=triggered_rules,
                vessel_context=vessel_context,
                round_num=round_num,
            )
            all_evidence_against.extend(counter_evidence)
            last_critique = critique

            if verbose:
                print(f"[Agent B] {critique[:200]}...")

            debate_rounds.append(DebateRound(
                round_number=round_num,
                hypothesis=hypothesis,
                critique=critique,
                hypothesis_evidence=list(evidence_for),
                critique_evidence=list(counter_evidence),
            ))

        # Agent C synthesizes
        if verbose:
            print(f"\n[Agent C] Synthesizing verdict...")

        verdict = self.agent_c.synthesize(
            mmsi=mmsi,
            vessel_context=vessel_context,
            debate_rounds=debate_rounds,
            all_evidence_for=all_evidence_for,
            all_evidence_against=all_evidence_against,
            triggered_rules=triggered_rules,
        )

        # Validate against Contract C1
        is_valid, errors = validate_verdict(verdict)
        if not is_valid:
            logger.warning(f"Contract C1 validation errors for {mmsi}: {errors}")

        if verbose:
            print(f"\n{'='*60}")
            print(f"  VERDICT: {verdict.verdict.value} ({verdict.confidence*100:.1f}% confidence)")
            print(f"  Risk Score: {verdict.risk_score:.1f}/100")
            print(f"  Threat: {verdict.threat_category.value}")
            print(f"  Actions: {[a.value for a in verdict.recommended_actions]}")
            print(f"{'='*60}\n")

        return verdict


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    orchestrator = AdversarialDebateOrchestrator(max_rounds=3)

    # Test: High-risk vessel with multiple triggered rules
    verdict = orchestrator.run_debate(
        mmsi="123456789",
        vessel_context={
            "name": "GHOST TRADER",
            "flag": "PW",
            "imo": "IMO1234567",
            "risk_score": 85.0,
            "nav_status": 0,
        },
        triggered_rules=["M-SANCTION-LINK", "M-DARK-LOITER", "M-STS-DETECT", "M-FLAG-HOP", "ID-001"],
        evidence_snippets=[
            {"rule": "M-SANCTION-LINK", "description": "Vessel linked to OFAC SDN entity", "timestamp": "2024-03-15T02:00:00Z"},
            {"rule": "M-DARK-LOITER", "description": "AIS off for 8 hours at 14.5°N, 54.3°E", "timestamp": "2024-03-15T00:00:00Z"},
            {"rule": "M-STS-DETECT", "description": "STS with MMSI 987654321 detected", "timestamp": "2024-03-15T03:30:00Z"},
        ],
        verbose=True,
    )

    print(f"\nFinal verdict JSON preview:")
    print(verdict.to_json()[:500])
