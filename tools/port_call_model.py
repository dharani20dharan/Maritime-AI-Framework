"""
MAF — Port Call Markov Anomaly Model
======================================
Probabilistic Markov trade-corridor transition model for detecting port call anomalies.

Detects:
  - M-PORT-CALL-ANOMALY: Vessel follows unexpected route not matching historical
    trade corridor transition probabilities.
  - Deviation from declared destination port.
  - Anomalous port clustering (e.g., sanctions country port visits).

The model maintains a transition probability matrix P[origin][destination]
learned from historical legitimate vessel traffic. Low-probability transitions
signal route manipulation or sanctions evasion.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── High-Risk Port Registry ────────────────────────────────────────────────────

# Ports associated with sanctions evasion or illicit trade
HIGH_RISK_PORTS = {
    "IRBAN": "Iran - Bandar Abbas",
    "IRKHK": "Iran - Kharg Island",
    "KRWON": "North Korea - Wonsan",
    "KRNMP": "North Korea - Nampo",
    "SYBEY": "Syria - Baniyas",
    "SYTAR": "Syria - Tartus",
    "VNHPH": "Venezuela - Jose Terminal",
    "RUVVO": "Russia - Vladivostok (sanctioned routes)",
    "MMRGN": "Myanmar - Rangoon",
}

# UN/LOCODE to region mapping for corridor detection
PORT_REGION = {
    "SGSIN": "SE_ASIA", "MYPEN": "SE_ASIA", "THBKK": "SE_ASIA",
    "VNSGN": "SE_ASIA", "PHMNL": "SE_ASIA", "IDTPK": "SE_ASIA",
    "CNSHA": "EAST_ASIA", "CNTXG": "EAST_ASIA", "CNQIN": "EAST_ASIA",
    "JPYOK": "EAST_ASIA", "KRPUS": "EAST_ASIA", "TWKHH": "EAST_ASIA",
    "AEDXB": "GULF", "KWKWI": "GULF", "SADMM": "GULF",
    "IQUQR": "GULF", "PKPQQ": "GULF",
    "NLRTM": "EUROPE", "DEHAM": "EUROPE", "GBFXT": "EUROPE",
    "FRFOS": "EUROPE", "ESVLC": "EUROPE",
    "USNYC": "N_AMERICA", "USLAX": "N_AMERICA", "USHOU": "N_AMERICA",
    "EGPSD": "SUEZ", "EGASK": "SUEZ",
    "PAONX": "PANAMA",
}

# Historical transition frequency matrix (normalized to probability)
# Format: {origin_region: {dest_region: probability}}
CORRIDOR_TRANSITION_MATRIX: Dict[str, Dict[str, float]] = {
    "SE_ASIA": {
        "EAST_ASIA": 0.35, "GULF": 0.25, "EUROPE": 0.15,
        "SE_ASIA": 0.10, "N_AMERICA": 0.08, "SUEZ": 0.05, "PANAMA": 0.02,
    },
    "GULF": {
        "EUROPE": 0.30, "EAST_ASIA": 0.25, "SE_ASIA": 0.20,
        "N_AMERICA": 0.10, "GULF": 0.08, "SUEZ": 0.05, "PANAMA": 0.02,
    },
    "EUROPE": {
        "N_AMERICA": 0.30, "GULF": 0.20, "SE_ASIA": 0.18,
        "EUROPE": 0.15, "EAST_ASIA": 0.10, "SUEZ": 0.05, "PANAMA": 0.02,
    },
    "EAST_ASIA": {
        "SE_ASIA": 0.30, "N_AMERICA": 0.25, "EUROPE": 0.20,
        "GULF": 0.12, "EAST_ASIA": 0.08, "SUEZ": 0.03, "PANAMA": 0.02,
    },
    "N_AMERICA": {
        "EUROPE": 0.35, "SE_ASIA": 0.25, "GULF": 0.15,
        "N_AMERICA": 0.10, "EAST_ASIA": 0.10, "SUEZ": 0.03, "PANAMA": 0.02,
    },
    "SUEZ": {
        "EUROPE": 0.40, "GULF": 0.30, "SE_ASIA": 0.20,
        "EAST_ASIA": 0.05, "N_AMERICA": 0.03, "SUEZ": 0.02,
    },
    "PANAMA": {
        "N_AMERICA": 0.40, "EUROPE": 0.25, "SE_ASIA": 0.20,
        "EAST_ASIA": 0.10, "GULF": 0.04, "PANAMA": 0.01,
    },
}

# Minimum probability threshold — below this is anomalous
ANOMALY_PROBABILITY_THRESHOLD = 0.03


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class PortCallEvent:
    """A single port call record."""
    port_unlocode: str
    port_name: str
    arrival_timestamp: str
    departure_timestamp: Optional[str] = None
    declared_destination: Optional[str] = None
    country: Optional[str] = None

    @property
    def region(self) -> str:
        return PORT_REGION.get(self.port_unlocode, "UNKNOWN")

    @property
    def is_high_risk(self) -> bool:
        return self.port_unlocode in HIGH_RISK_PORTS


@dataclass
class PortCallAnomalyResult:
    """Result of port call anomaly analysis."""
    mmsi: str
    is_anomalous: bool
    anomaly_type: str
    confidence: float
    description: str
    transition_probability: Optional[float] = None
    high_risk_ports_visited: List[str] = field(default_factory=list)
    destination_mismatch: bool = False
    anomalous_transitions: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "is_anomalous": self.is_anomalous,
            "anomaly_type": self.anomaly_type,
            "confidence": self.confidence,
            "description": self.description,
            "transition_probability": self.transition_probability,
            "high_risk_ports_visited": self.high_risk_ports_visited,
            "destination_mismatch": self.destination_mismatch,
            "anomalous_transitions": self.anomalous_transitions,
        }


# ── Markov Port Call Model ────────────────────────────────────────────────────

class PortCallMarkovModel:
    """
    Markov chain model for detecting anomalous port call sequences.

    Uses a pre-trained transition probability matrix over maritime trade
    corridors to evaluate whether a vessel's observed route is consistent
    with legitimate commercial traffic patterns.
    """

    def __init__(
        self,
        transition_matrix: Optional[Dict[str, Dict[str, float]]] = None,
        anomaly_threshold: float = ANOMALY_PROBABILITY_THRESHOLD,
        high_risk_port_registry: Optional[Dict[str, str]] = None,
    ):
        self.matrix = transition_matrix or CORRIDOR_TRANSITION_MATRIX
        self.threshold = anomaly_threshold
        self.high_risk = high_risk_port_registry or HIGH_RISK_PORTS

    def get_transition_probability(self, origin_region: str, dest_region: str) -> float:
        """Return P(dest | origin) from the transition matrix."""
        if origin_region not in self.matrix:
            return 0.01  # Unknown region: assign minimal baseline probability
        dest_probs = self.matrix[origin_region]
        return dest_probs.get(dest_region, 0.01)

    def compute_route_likelihood(self, port_calls: List[PortCallEvent]) -> float:
        """
        Compute the joint log-likelihood of an observed port call sequence.
        Returns the geometric mean of transition probabilities.
        """
        if len(port_calls) < 2:
            return 1.0

        log_probs = []
        for i in range(1, len(port_calls)):
            origin = port_calls[i - 1].region
            dest = port_calls[i].region
            p = self.get_transition_probability(origin, dest)
            log_probs.append(math.log(max(p, 1e-10)))

        # Geometric mean (average log prob back to probability)
        avg_log = sum(log_probs) / len(log_probs)
        return math.exp(avg_log)

    def analyze(self, mmsi: str, port_calls: List[PortCallEvent]) -> PortCallAnomalyResult:
        """
        Full port call anomaly analysis for a vessel.

        Checks:
          1. Individual transition probabilities (route corridor analysis)
          2. High-risk port visits
          3. Declared destination mismatch
        """
        anomalous_transitions = []
        high_risk_visited = []
        destination_mismatches = []

        # Check 1: High-risk port visits
        for call in port_calls:
            if call.is_high_risk:
                high_risk_visited.append(
                    f"{call.port_unlocode} ({self.high_risk[call.port_unlocode]})"
                )

        # Check 2: Anomalous transitions
        for i in range(1, len(port_calls)):
            origin = port_calls[i - 1]
            dest = port_calls[i]
            p = self.get_transition_probability(origin.region, dest.region)
            if p < self.threshold:
                anomalous_transitions.append({
                    "from_port": origin.port_unlocode,
                    "from_region": origin.region,
                    "to_port": dest.port_unlocode,
                    "to_region": dest.region,
                    "transition_probability": p,
                    "anomaly_score": 1.0 - (p / self.threshold),
                })

        # Check 3: Declared destination mismatch
        for call in port_calls[:-1]:  # All except last
            if call.declared_destination and len(port_calls) > port_calls.index(call) + 1:
                actual_next = port_calls[port_calls.index(call) + 1]
                if (call.declared_destination.upper().strip() !=
                        actual_next.port_unlocode.upper()):
                    destination_mismatches.append({
                        "declared": call.declared_destination,
                        "actual": actual_next.port_unlocode,
                    })

        # Compute overall route likelihood
        route_likelihood = self.compute_route_likelihood(port_calls)

        # Determine if anomalous
        is_anomalous = bool(
            anomalous_transitions or high_risk_visited or destination_mismatches
        )

        # Calculate confidence
        confidence = 0.0
        anomaly_type = "NONE"
        description = f"Port call sequence for {mmsi} appears normal."

        if high_risk_visited:
            confidence = max(confidence, 0.90)
            anomaly_type = "HIGH_RISK_PORT_VISIT"
            description = (
                f"Vessel {mmsi} visited {len(high_risk_visited)} high-risk port(s): "
                f"{', '.join(high_risk_visited)}. Possible sanctions violation."
            )

        if anomalous_transitions:
            worst = max(anomalous_transitions, key=lambda x: x["anomaly_score"])
            trans_confidence = 0.7 + worst["anomaly_score"] * 0.3
            if trans_confidence > confidence:
                confidence = trans_confidence
                anomaly_type = "M-PORT-CALL-ANOMALY"
                description = (
                    f"Vessel {mmsi} made {len(anomalous_transitions)} anomalous trade "
                    f"corridor transitions (worst P={worst['transition_probability']:.4f} "
                    f"for {worst['from_region']}→{worst['to_region']}). Route manipulation suspected."
                )

        if destination_mismatches:
            confidence = max(confidence, 0.80)
            if anomaly_type == "NONE":
                anomaly_type = "DESTINATION_MISMATCH"
            description += (
                f" Declared destination mismatched actual port in {len(destination_mismatches)} case(s)."
            )

        return PortCallAnomalyResult(
            mmsi=mmsi,
            is_anomalous=is_anomalous,
            anomaly_type=anomaly_type,
            confidence=confidence,
            description=description,
            transition_probability=route_likelihood,
            high_risk_ports_visited=high_risk_visited,
            destination_mismatch=bool(destination_mismatches),
            anomalous_transitions=anomalous_transitions,
        )


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    model = PortCallMarkovModel()

    # Normal commercial route: Singapore → Rotterdam → New York
    normal_calls = [
        PortCallEvent("SGSIN", "Singapore", "2024-01-01T00:00:00Z", "2024-01-03T00:00:00Z", "NLRTM"),
        PortCallEvent("NLRTM", "Rotterdam", "2024-01-20T00:00:00Z", "2024-01-22T00:00:00Z", "USNYC"),
        PortCallEvent("USNYC", "New York", "2024-02-01T00:00:00Z"),
    ]
    result = model.analyze("123456789", normal_calls)
    print(f"Normal route: anomalous={result.is_anomalous}, type={result.anomaly_type}")

    # Suspicious route: Gulf → Iran → North Korea (impossible/sanctions)
    suspicious_calls = [
        PortCallEvent("AEDXB", "Dubai", "2024-01-01T00:00:00Z", "2024-01-03T00:00:00Z", "IRBAN"),
        PortCallEvent("IRBAN", "Bandar Abbas", "2024-01-10T00:00:00Z", "2024-01-12T00:00:00Z"),
        PortCallEvent("KRNMP", "Nampo", "2024-02-01T00:00:00Z"),
    ]
    result = model.analyze("987654321", suspicious_calls)
    print(f"Suspicious route: anomalous={result.is_anomalous}, type={result.anomaly_type}")
    print(f"  Confidence: {result.confidence:.2f}")
    print(f"  Description: {result.description}")
    print(f"  High-risk ports: {result.high_risk_ports_visited}")
