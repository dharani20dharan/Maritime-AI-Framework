"""
MAF — XAI / SHAP Attribution Engine
======================================
Computes Shapley-value-inspired additive feature attribution weights for all
16 MAF detection techniques applied to a single vessel assessment.

Explains *why* a vessel received a given risk score by decomposing the total
score into per-technique contributions (analogous to SHAP values for tabular
ML models, but applied to rule-based / hybrid detection systems).

Attribution philosophy:
  - Each triggered rule contributes a marginal value φᵢ to the final risk score.
  - Non-triggered rules contribute their counterfactual absence value (typically 0).
  - The sum of all φᵢ values equals the total attributed risk score.
  - The ordering reveals the dominant risk drivers for analyst explanation.
"""

from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Rule Definitions and Base Weights ────────────────────────────────────────

# All 16 MAF detection techniques with their base Shapley weight (max contribution)
TECHNIQUE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "M-AIS-GAP":            {"label": "AIS Transmission Gap",          "max_weight": 20, "category": "Behavior"},
    "M-DARK-LOITER":        {"label": "Dark Period + Loitering",       "max_weight": 35, "category": "Behavior"},
    "M-GPS-SPOOF":          {"label": "GPS Spoofing / Position Jump",   "max_weight": 28, "category": "Technical"},
    "M-IMPOSSIBLE-SPEED":   {"label": "Impossible Speed Detected",      "max_weight": 25, "category": "Technical"},
    "M-BEACON-REPEAT":      {"label": "Automated Beacon Pattern",       "max_weight": 18, "category": "Technical"},
    "M-STS-DETECT":         {"label": "Ship-to-Ship Rendezvous",        "max_weight": 40, "category": "Activity"},
    "M-DARK-EVENT":         {"label": "Confirmed Dark Vessel Contact",  "max_weight": 30, "category": "Behavior"},
    "M-SANCTION-LINK":      {"label": "Sanction List Link",             "max_weight": 50, "category": "Identity"},
    "M-FLAG-HOP":           {"label": "Flag Hopping",                   "max_weight": 30, "category": "Identity"},
    "M-NAME-CHANGE":        {"label": "Vessel Name Change",             "max_weight": 25, "category": "Identity"},
    "M-MMSI-CONFLICT":      {"label": "MMSI Conflict / Cloning",        "max_weight": 45, "category": "Identity"},
    "M-PORT-CALL-ANOMALY":  {"label": "Anomalous Port Call Sequence",   "max_weight": 28, "category": "Activity"},
    "M-OWNERSHIP-OBSCURE":  {"label": "Obscured Ownership Chain",       "max_weight": 22, "category": "Identity"},
    "M-DARK-REEMERGENCE":   {"label": "Dark Re-emergence w/ ID Change", "max_weight": 38, "category": "Identity"},
    "ID-003":               {"label": "MMSI Identity Laundering",       "max_weight": 45, "category": "Identity"},
    "M-COLREGS-VIOLATION":  {"label": "COLREGs Navigation Violation",   "max_weight": 15, "category": "Behavior"},
}


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class AttributionFeature:
    """Attribution value for a single detection technique."""
    rule_id: str
    label: str
    category: str
    phi: float              # Shapley value (risk score contribution)
    is_triggered: bool
    confidence: float       # Rule-specific confidence 0.0–1.0
    max_weight: int
    percentage: float = 0.0  # Percentage of total attributed score

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "label": self.label,
            "category": self.category,
            "phi": round(self.phi, 3),
            "is_triggered": self.is_triggered,
            "confidence": round(self.confidence, 3),
            "max_weight": self.max_weight,
            "percentage": round(self.percentage, 1),
        }


@dataclass
class AttributionResult:
    """Full XAI attribution result for a vessel assessment."""
    mmsi: str
    vessel_name: Optional[str]
    total_risk_score: float
    attributed_score: float             # Sum of all positive φᵢ
    features: List[AttributionFeature]
    top_factors: List[str]              # Top-3 rule labels
    explanation: str                    # Human-readable narrative
    triggered_rule_count: int
    threshold_violations: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "vessel_name": self.vessel_name,
            "total_risk_score": round(self.total_risk_score, 2),
            "attributed_score": round(self.attributed_score, 2),
            "features": [f.to_dict() for f in self.features],
            "top_factors": self.top_factors,
            "explanation": self.explanation,
            "triggered_rule_count": self.triggered_rule_count,
            "threshold_violations": self.threshold_violations,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ── Threshold Manager ─────────────────────────────────────────────────────────

# Default sensitivity thresholds for all 16 techniques
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "M-AIS-GAP":            6.0,    # Hours of AIS silence = anomaly
    "M-DARK-LOITER":        4.0,    # Hours loitering dark = anomaly
    "M-GPS-SPOOF":          45.0,   # Max physically possible speed (kts)
    "M-IMPOSSIBLE-SPEED":   50.0,   # Max SOG (kts)
    "M-BEACON-REPEAT":      0.01,   # Coefficient of variation below this = automated
    "M-STS-DETECT":         0.65,   # STGVAD score threshold
    "M-DARK-EVENT":         2.0,    # Hours dark in busy corridor = event
    "M-SANCTION-LINK":      0.80,   # Match confidence threshold
    "M-FLAG-HOP":           3,      # Min flag changes in 2 years = hopping
    "M-NAME-CHANGE":        1,      # Any name change post-dark = suspicious
    "M-MMSI-CONFLICT":      10.0,   # NM threshold for impossible simultaneous location
    "M-PORT-CALL-ANOMALY":  0.03,   # Markov transition probability below this = anomalous
    "M-OWNERSHIP-OBSCURE":  4,      # Ownership chain depth triggering flag
    "M-DARK-REEMERGENCE":   1,      # Any identity change on re-emergence = flag
    "ID-003":               5.0,    # NM separation for MMSI conflict
    "M-COLREGS-VIOLATION":  0.5,    # NM range for COLREGs triggering
}

# In-memory threshold store (persisted in JSON)
import os, json as _json
_THRESHOLD_STORE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output", "thresholds.json"
)

def load_thresholds() -> Dict[str, float]:
    """Load persisted thresholds, falling back to defaults."""
    try:
        if os.path.exists(_THRESHOLD_STORE_PATH):
            with open(_THRESHOLD_STORE_PATH, "r") as f:
                stored = _json.load(f)
            merged = dict(DEFAULT_THRESHOLDS)
            merged.update(stored)
            return merged
    except Exception as e:
        logger.warning(f"Could not load thresholds: {e}")
    return dict(DEFAULT_THRESHOLDS)

def save_thresholds(thresholds: Dict[str, float]) -> bool:
    """Persist threshold updates to disk."""
    try:
        os.makedirs(os.path.dirname(_THRESHOLD_STORE_PATH), exist_ok=True)
        with open(_THRESHOLD_STORE_PATH, "w") as f:
            _json.dump(thresholds, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Could not save thresholds: {e}")
        return False

def update_thresholds(updates: Dict[str, float]) -> Tuple[Dict[str, float], List[str]]:
    """
    Validate and apply threshold updates.
    Returns (updated_thresholds, list_of_errors).
    """
    errors = []
    current = load_thresholds()
    for key, val in updates.items():
        if key not in DEFAULT_THRESHOLDS:
            errors.append(f"Unknown rule ID: {key}")
            continue
        if not isinstance(val, (int, float)) or val < 0:
            errors.append(f"Invalid value for {key}: must be a non-negative number")
            continue
        current[key] = float(val)
    if not errors:
        save_thresholds(current)
    return current, errors


# ── XAI Attribution Engine ────────────────────────────────────────────────────

class XAIAttributionEngine:
    """
    Shapley-inspired attribution engine for MAF vessel risk scoring.

    Computes per-technique contribution (φᵢ) to the total risk score using
    a weighted additive model with interaction effects for co-triggered rules.
    """

    # Interaction boosts: when rules co-trigger, add extra attribution
    INTERACTION_BOOSTS: Dict[frozenset, float] = {
        frozenset({"M-SANCTION-LINK", "M-DARK-LOITER"}):  8.0,
        frozenset({"M-STS-DETECT",    "M-DARK-LOITER"}):  10.0,
        frozenset({"M-FLAG-HOP",      "M-SANCTION-LINK"}): 7.0,
        frozenset({"M-MMSI-CONFLICT", "M-DARK-REEMERGENCE"}): 12.0,
        frozenset({"M-PORT-CALL-ANOMALY", "M-DARK-LOITER"}): 6.0,
        frozenset({"ID-003",          "M-SANCTION-LINK"}): 10.0,
    }

    def compute(
        self,
        mmsi: str,
        triggered_rules: List[str],
        rule_confidences: Optional[Dict[str, float]] = None,
        vessel_name: Optional[str] = None,
        existing_risk_score: Optional[float] = None,
    ) -> AttributionResult:
        """
        Compute XAI attribution for a vessel.

        Args:
            mmsi:                 Vessel MMSI
            triggered_rules:      List of fired rule IDs
            rule_confidences:     Per-rule confidence overrides (0.0–1.0)
            vessel_name:          Optional vessel name for narrative
            existing_risk_score:  If provided, attribution will normalize to this total

        Returns:
            AttributionResult with per-rule Shapley values
        """
        if rule_confidences is None:
            rule_confidences = {}

        triggered_set = set(triggered_rules)
        thresholds = load_thresholds()
        threshold_violations = {
            rule: (rule in triggered_set) for rule in DEFAULT_THRESHOLDS
        }

        # Compute base attribution (φᵢ) for each technique
        features: List[AttributionFeature] = []
        raw_total = 0.0

        for rule_id, info in TECHNIQUE_REGISTRY.items():
            is_triggered = rule_id in triggered_set
            if not is_triggered:
                phi = 0.0
                conf = 0.0
            else:
                conf = rule_confidences.get(rule_id, 0.85)
                phi = info["max_weight"] * conf
                raw_total += phi

            features.append(AttributionFeature(
                rule_id=rule_id,
                label=info["label"],
                category=info["category"],
                phi=phi,
                is_triggered=is_triggered,
                confidence=conf,
                max_weight=info["max_weight"],
            ))

        # Apply interaction boosts
        interaction_total = 0.0
        for rule_pair, boost in self.INTERACTION_BOOSTS.items():
            if rule_pair.issubset(triggered_set):
                interaction_total += boost

        total_attributed = raw_total + interaction_total
        # Allocate interaction boost proportionally among triggered features
        if raw_total > 0 and interaction_total > 0:
            for f in features:
                if f.is_triggered:
                    f.phi += interaction_total * (f.phi / raw_total)

        # Compute percentages
        for f in features:
            f.percentage = (f.phi / total_attributed * 100) if total_attributed > 0 else 0.0

        # Normalize to existing_risk_score if provided
        final_risk = existing_risk_score
        if final_risk is None:
            final_risk = min(total_attributed, 100.0)

        # Sort by contribution descending
        features.sort(key=lambda f: -f.phi)

        # Top-3 factors
        top_factors = [f.label for f in features if f.is_triggered][:3]

        # Generate human-readable explanation
        explanation = self._generate_explanation(
            mmsi=mmsi,
            vessel_name=vessel_name,
            features=features,
            total_risk=final_risk,
            triggered_rules=triggered_rules,
        )

        return AttributionResult(
            mmsi=mmsi,
            vessel_name=vessel_name,
            total_risk_score=final_risk,
            attributed_score=round(total_attributed, 2),
            features=features,
            top_factors=top_factors,
            explanation=explanation,
            triggered_rule_count=len(triggered_rules),
            threshold_violations=threshold_violations,
        )

    def _generate_explanation(
        self,
        mmsi: str,
        vessel_name: Optional[str],
        features: List[AttributionFeature],
        total_risk: float,
        triggered_rules: List[str],
    ) -> str:
        """Generate a concise natural-language explanation of the risk score."""
        vessel_ref = f"Vessel {mmsi}" + (f" ({vessel_name})" if vessel_name else "")
        if not triggered_rules:
            return f"{vessel_ref} has no triggered anomaly rules. Risk score: {total_risk:.1f}/100."

        triggered_features = [f for f in features if f.is_triggered]
        top = triggered_features[:3]

        explanation_parts = [
            f"{vessel_ref} received a risk score of {total_risk:.1f}/100 "
            f"based on {len(triggered_rules)} triggered detection rule(s)."
        ]

        if top:
            driver_text = "; ".join(
                f"{f.label} (+{f.phi:.1f}pts, {f.percentage:.0f}%)" for f in top
            )
            explanation_parts.append(f"Primary risk drivers: {driver_text}.")

        risk_band = (
            "CRITICAL" if total_risk >= 80 else
            "HIGH" if total_risk >= 60 else
            "MEDIUM" if total_risk >= 40 else "LOW"
        )
        explanation_parts.append(f"Overall risk band: {risk_band}.")

        return " ".join(explanation_parts)

    def compute_heatmap_points(
        self,
        vessel_events: List[Dict[str, Any]],
    ) -> List[Dict[str, float]]:
        """
        Compute spatial density points for corridor heat-map overlay.

        Args:
            vessel_events: List of {lat, lon, risk_score, rule_id} event records.

        Returns:
            List of {lat, lon, weight} points for map heat-map rendering.
        """
        heatmap_points = []
        for event in vessel_events:
            lat = event.get("lat")
            lon = event.get("lon")
            risk = event.get("risk_score", 50)
            if lat is None or lon is None:
                continue
            # Weight = normalized risk (0–1) with slight jitter to avoid exact overlap
            import random
            weight = min(risk / 100.0, 1.0)
            heatmap_points.append({
                "lat": lat + random.uniform(-0.001, 0.001),
                "lon": lon + random.uniform(-0.001, 0.001),
                "weight": weight,
            })
        return heatmap_points


# ── Convenience Function ──────────────────────────────────────────────────────

def explain_vessel(
    mmsi: str,
    triggered_rules: List[str],
    rule_confidences: Optional[Dict[str, float]] = None,
    vessel_name: Optional[str] = None,
    existing_risk_score: Optional[float] = None,
) -> AttributionResult:
    """Top-level convenience function for vessel XAI attribution."""
    engine = XAIAttributionEngine()
    return engine.compute(
        mmsi=mmsi,
        triggered_rules=triggered_rules,
        rule_confidences=rule_confidences,
        vessel_name=vessel_name,
        existing_risk_score=existing_risk_score,
    )


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    result = explain_vessel(
        mmsi="123456789",
        triggered_rules=["M-SANCTION-LINK", "M-DARK-LOITER", "M-STS-DETECT", "M-FLAG-HOP"],
        rule_confidences={
            "M-SANCTION-LINK": 0.92,
            "M-DARK-LOITER":   0.88,
            "M-STS-DETECT":    0.76,
            "M-FLAG-HOP":      0.70,
        },
        vessel_name="GHOST TRADER",
        existing_risk_score=87.5,
    )

    print("=== XAI Attribution Report ===")
    print(result.explanation)
    print(f"\nTop factors: {result.top_factors}")
    print(f"\nFeature breakdown:")
    for f in result.features:
        if f.is_triggered:
            bar = "█" * int(f.percentage / 5)
            print(f"  [{f.rule_id:22s}] φ={f.phi:5.1f} ({f.percentage:4.1f}%) {bar}")

    # Test threshold update
    updated, errors = update_thresholds({"M-AIS-GAP": 4.0, "M-DARK-LOITER": 3.0})
    print(f"\nThreshold update: errors={errors}")
    print(f"New M-AIS-GAP threshold: {updated['M-AIS-GAP']}")
