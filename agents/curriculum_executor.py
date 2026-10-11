"""
MAF — Curriculum Executor: 50-Scenario Evasion Benchmark
==========================================================
Implements:
  1. Curriculum Agent: Procedurally generates 50 multi-technique synthetic evasion
     scenarios across 5 difficulty levels (Level 1 = simple to Level 5 = complex).
  2. Executor Agent: Runs automated evaluation across all 50 scenarios, measuring
     precision, recall, detection latency, and F1 score.

Scenario Levels:
  L1 (Simple):       Single indicator — AIS gap or loitering
  L2 (Moderate):     Two correlated indicators — dark period + spoofing
  L3 (Complex):      Three indicators — dark + STS + flag hop
  L4 (Advanced):     Four indicators — full evasion chain
  L5 (Expert):       Five+ indicators — shell company + clone + dark STS + port call anomaly

Output: Benchmark report with precision, recall, F1, and latency metrics per scenario.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
import sys
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.adversarial_debate import AdversarialDebateOrchestrator
from shared.contracts.C1_debate_sar import VerdictType

logger = logging.getLogger(__name__)


# ── Scenario Templates ────────────────────────────────────────────────────────

SCENARIO_TEMPLATES = {
    1: {
        "name": "Simple AIS Gap",
        "triggered_rules": ["M-AIS-GAP"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "Vessel has a single unexplained AIS gap in busy corridor.",
    },
    2: {
        "name": "Loitering Near Chokepoint",
        "triggered_rules": ["M-DARK-LOITER"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "Vessel loitering >8 hours with AIS active in non-anchorage area.",
    },
    3: {
        "name": "GPS Spoofing Detection",
        "triggered_rules": ["M-GPS-SPOOF", "M-IMPOSSIBLE-SPEED"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "Vessel position shows impossible jump speed (>80 kts).",
    },
    4: {
        "name": "AIS Gap + Dark Loiter",
        "triggered_rules": ["M-AIS-GAP", "M-DARK-LOITER"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "AIS gap coincides with loitering in an area with no traffic.",
    },
    5: {
        "name": "Sanction Link Discovery",
        "triggered_rules": ["M-SANCTION-LINK"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Vessel directly linked to OFAC SDN sanction list entity.",
    },
    6: {
        "name": "Beacon Pattern Anomaly",
        "triggered_rules": ["M-BEACON-REPEAT", "M-AIS-GAP"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "AIS beacon shows robotic repetition pattern inconsistent with real navigation.",
    },
    7: {
        "name": "Flag Hopping x3",
        "triggered_rules": ["M-FLAG-HOP", "ID-001"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Vessel changed flag 3 times in 12 months.",
    },
    8: {
        "name": "Simple STS Contact",
        "triggered_rules": ["M-STS-DETECT"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "Vessel made close-range contact with another vessel for 45 minutes.",
    },
    9: {
        "name": "Dark Period + Position Jump",
        "triggered_rules": ["M-DARK-EVENT", "M-GPS-SPOOF"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "AIS went dark then reappeared 200 NM away 4 hours later.",
    },
    10: {
        "name": "Name Inconsistency",
        "triggered_rules": ["M-NAME-CHANGE", "ID-002"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Vessel broadcasting name different from official registry.",
    },
    11: {
        "name": "STS + Dark Period",
        "triggered_rules": ["M-STS-DETECT", "M-DARK-LOITER", "M-AIS-GAP"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "STS contact during AIS-dark period in non-anchorage zone.",
    },
    12: {
        "name": "Flag Hop + Sanction",
        "triggered_rules": ["M-FLAG-HOP", "M-SANCTION-LINK", "ID-001"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Flag hopping vessel with sanction entity connection.",
    },
    13: {
        "name": "MMSI Conflict",
        "triggered_rules": ["ID-003", "M-MMSI-CONFLICT"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Same MMSI appeared at two locations 150 NM apart simultaneously.",
    },
    14: {
        "name": "Port Call Anomaly",
        "triggered_rules": ["M-PORT-CALL-ANOMALY"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "Vessel called at port in sanctioned country not matching declared destination.",
    },
    15: {
        "name": "Dark Re-emergence + Identity Change",
        "triggered_rules": ["ID-004", "M-DARK-REEMERGENCE", "M-NAME-CHANGE"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Vessel re-emerged from dark period with different name and flag.",
    },
    16: {
        "name": "GPS Spoof + STS",
        "triggered_rules": ["M-GPS-SPOOF", "M-STS-DETECT", "M-DARK-LOITER"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "GPS spoofing used to mask STS rendezvous location.",
    },
    17: {
        "name": "Full Evasion Chain L3",
        "triggered_rules": ["M-DARK-EVENT", "M-STS-DETECT", "M-FLAG-HOP", "ID-001"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Dark period + STS + flag change = textbook sanctions evasion chain.",
    },
    18: {
        "name": "Shell Company + Ownership Obscure",
        "triggered_rules": ["M-OWNERSHIP-OBSCURE", "M-FLAG-HOP", "M-SANCTION-LINK"],
        "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
        "description": "Vessel with 5-hop ownership chain ending in sanctioned jurisdiction.",
    },
    19: {
        "name": "COLREGs + AIS Manipulation",
        "triggered_rules": ["COL-001", "M-AIS-GAP", "M-GPS-SPOOF"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "COLREGs violation during AIS manipulation period.",
    },
    20: {
        "name": "Impossible Speed + Beacon",
        "triggered_rules": ["M-IMPOSSIBLE-SPEED", "M-BEACON-REPEAT"],
        "expected_verdict": VerdictType.PROBABLE_ANOMALY,
        "description": "Automated beacon broadcasting impossible speed values.",
    },
}

# Generate L4 and L5 scenarios programmatically
L4_SCENARIO_BASE = {
    "triggered_rules": ["M-SANCTION-LINK", "M-DARK-LOITER", "M-STS-DETECT", "M-FLAG-HOP", "ID-001"],
    "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
}
L5_SCENARIO_BASE = {
    "triggered_rules": ["M-SANCTION-LINK", "M-DARK-LOITER", "M-STS-DETECT", "M-FLAG-HOP",
                        "ID-001", "ID-003", "M-PORT-CALL-ANOMALY", "M-OWNERSHIP-OBSCURE"],
    "expected_verdict": VerdictType.CONFIRMED_ANOMALY,
}


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class EvasionScenario:
    """A synthetic evasion scenario for benchmark evaluation."""
    scenario_id: int
    level: int                          # 1–5 difficulty
    name: str
    description: str
    mmsi: str
    triggered_rules: List[str]
    expected_verdict: VerdictType
    vessel_context: Dict[str, Any] = field(default_factory=dict)
    evidence_snippets: List[Dict] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """Benchmark result for a single scenario run."""
    scenario_id: int
    scenario_name: str
    level: int
    expected_verdict: str
    actual_verdict: str
    is_correct: bool
    confidence: float
    risk_score: float
    latency_ms: float
    rules_detected: List[str]


@dataclass
class BenchmarkReport:
    """Aggregate benchmark results for all 50 scenarios."""
    total_scenarios: int
    correct_predictions: int
    precision: float
    recall: float
    f1_score: float
    avg_latency_ms: float
    results_by_level: Dict[int, Dict[str, float]]
    individual_results: List[ScenarioResult]

    def to_dict(self) -> dict:
        return {
            "total_scenarios": self.total_scenarios,
            "correct_predictions": self.correct_predictions,
            "precision": self.precision,
            "recall": self.recall,
            "f1_score": self.f1_score,
            "avg_latency_ms": self.avg_latency_ms,
            "results_by_level": self.results_by_level,
        }

    def print_summary(self):
        print(f"\n{'='*65}")
        print(f"  MAF CURRICULUM BENCHMARK — {self.total_scenarios} SCENARIOS")
        print(f"{'='*65}")
        print(f"  Overall Precision:  {self.precision*100:.1f}%")
        print(f"  Overall Recall:     {self.recall*100:.1f}%")
        print(f"  Overall F1 Score:   {self.f1_score*100:.1f}%")
        print(f"  Avg Latency:        {self.avg_latency_ms:.0f}ms")
        print(f"  Correct:            {self.correct_predictions}/{self.total_scenarios}")
        print(f"\n  Results by Level:")
        for level, metrics in sorted(self.results_by_level.items()):
            acc = metrics.get("accuracy", 0)
            avg_lat = metrics.get("avg_latency_ms", 0)
            print(f"    L{level}: Accuracy={acc*100:.1f}%, Avg Latency={avg_lat:.0f}ms")
        print(f"{'='*65}\n")


# ── Curriculum Agent ──────────────────────────────────────────────────────────

class CurriculumAgent:
    """
    Procedurally generates synthetic evasion scenarios across 5 difficulty levels.
    Produces exactly 50 scenarios: 4 per L1, 4 per L2, 4 per L3, 4 per L4, 4 per L5,
    plus remaining from the template library.
    """

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def _make_vessel_context(self, scenario_id: int, level: int) -> Dict[str, Any]:
        flags = ["PW", "KM", "PA", "SG", "LR", "MH", "IR"]
        names = ["GHOST TRADER", "DARK PHOENIX", "SHADOW TANKER", "SILENT RUNNER",
                 "VEIL CAPTAIN", "PHANTOM CARRIER", "NULL POINTER", "CIPHER VESSEL",
                 "EVASION PRO", "STEALTH OPS"]
        return {
            "name": self.rng.choice(names),
            "flag": self.rng.choice(flags),
            "imo": f"IMO{self.rng.randint(1000000, 9999999)}",
            "risk_score": 30.0 + level * 12.0 + self.rng.uniform(-5, 5),
            "nav_status": 0,
        }

    def _make_evidence(self, rules: List[str]) -> List[Dict]:
        return [
            {
                "rule": rule,
                "description": f"Automated detection: {rule} triggered",
                "timestamp": "2024-03-15T02:00:00Z",
            }
            for rule in rules
        ]

    def generate_all_scenarios(self) -> List[EvasionScenario]:
        """Generate all 50 benchmark scenarios."""
        scenarios = []

        template_levels = {
            1: list(range(1, 5)),   # Simple: scenarios 1-4
            2: list(range(5, 9)),   # Moderate: 5-8
            3: list(range(9, 21)),  # Complex: 9-20
        }
        for level, ids in template_levels.items():
            for sid in ids:
                if sid in SCENARIO_TEMPLATES:
                    tmpl = SCENARIO_TEMPLATES[sid]
                    ctx = self._make_vessel_context(sid, level)
                    scenarios.append(EvasionScenario(
                        scenario_id=sid,
                        level=level,
                        name=tmpl["name"],
                        description=tmpl["description"],
                        mmsi=f"{self.rng.randint(100000000, 999999999)}",
                        triggered_rules=tmpl["triggered_rules"],
                        expected_verdict=tmpl["expected_verdict"],
                        vessel_context=ctx,
                        evidence_snippets=self._make_evidence(tmpl["triggered_rules"]),
                    ))

        # Generate L4 scenarios (IDs 21-30)
        for i in range(10):
            sid = 21 + i
            extra_rules = self.rng.sample(
                ["M-AIS-GAP", "M-GPS-SPOOF", "ID-002", "M-PORT-CALL-ANOMALY"], 1
            )
            rules = L4_SCENARIO_BASE["triggered_rules"] + extra_rules
            ctx = self._make_vessel_context(sid, 4)
            scenarios.append(EvasionScenario(
                scenario_id=sid,
                level=4,
                name=f"Advanced Evasion Chain {i+1}",
                description=f"4+ indicator multi-technique evasion scenario {i+1}",
                mmsi=f"{self.rng.randint(100000000, 999999999)}",
                triggered_rules=rules,
                expected_verdict=VerdictType.CONFIRMED_ANOMALY,
                vessel_context=ctx,
                evidence_snippets=self._make_evidence(rules),
            ))

        # Generate L5 scenarios (IDs 31-50)
        for i in range(20):
            sid = 31 + i
            extra_rules = self.rng.sample(
                ["M-BEACON-REPEAT", "ID-004", "COL-001", "M-IMPOSSIBLE-SPEED"], 2
            )
            rules = list(set(L5_SCENARIO_BASE["triggered_rules"] + extra_rules))
            ctx = self._make_vessel_context(sid, 5)
            scenarios.append(EvasionScenario(
                scenario_id=sid,
                level=5,
                name=f"Expert Shell+Clone+STS Evasion {i+1}",
                description=f"Shell company + MMSI clone + dark STS + port call anomaly chain {i+1}",
                mmsi=f"{self.rng.randint(100000000, 999999999)}",
                triggered_rules=rules,
                expected_verdict=VerdictType.CONFIRMED_ANOMALY,
                vessel_context=ctx,
                evidence_snippets=self._make_evidence(rules),
            ))

        return scenarios[:50]  # Ensure exactly 50


# ── Executor Agent ────────────────────────────────────────────────────────────

class ExecutorAgent:
    """
    Runs automated evaluation across all 50 curriculum scenarios.
    Computes precision, recall, F1, and detection latency benchmarks.
    """

    def __init__(self, debate_rounds: int = 2):
        self.orchestrator = AdversarialDebateOrchestrator(max_rounds=debate_rounds)

    def run_scenario(self, scenario: EvasionScenario) -> ScenarioResult:
        """Run a single scenario through the debate engine."""
        t0 = time.perf_counter()

        verdict = self.orchestrator.run_debate(
            mmsi=scenario.mmsi,
            vessel_context=scenario.vessel_context,
            triggered_rules=scenario.triggered_rules,
            evidence_snippets=scenario.evidence_snippets,
            verbose=False,
        )

        latency_ms = (time.perf_counter() - t0) * 1000

        # Determine if prediction is correct
        # For anomaly detection: positive = CONFIRMED or PROBABLE anomaly
        POSITIVE_VERDICTS = {VerdictType.CONFIRMED_ANOMALY, VerdictType.PROBABLE_ANOMALY}
        expected_is_positive = scenario.expected_verdict in POSITIVE_VERDICTS
        actual_is_positive = verdict.verdict in POSITIVE_VERDICTS
        is_correct = expected_is_positive == actual_is_positive

        return ScenarioResult(
            scenario_id=scenario.scenario_id,
            scenario_name=scenario.name,
            level=scenario.level,
            expected_verdict=scenario.expected_verdict.value,
            actual_verdict=verdict.verdict.value,
            is_correct=is_correct,
            confidence=verdict.confidence,
            risk_score=verdict.risk_score,
            latency_ms=latency_ms,
            rules_detected=verdict.triggered_rules,
        )

    def run_benchmark(
        self, scenarios: List[EvasionScenario], verbose: bool = True
    ) -> BenchmarkReport:
        """Run all scenarios and compute aggregate metrics."""
        results: List[ScenarioResult] = []

        if verbose:
            print(f"\nRunning {len(scenarios)} benchmark scenarios...")

        for i, scenario in enumerate(scenarios):
            result = self.run_scenario(scenario)
            results.append(result)
            if verbose and (i + 1) % 10 == 0:
                correct_so_far = sum(r.is_correct for r in results)
                print(f"  [{i+1}/{len(scenarios)}] Accuracy so far: {correct_so_far/(i+1)*100:.1f}%")

        # Compute aggregate metrics
        total = len(results)
        POSITIVE_VERDICTS = {VerdictType.CONFIRMED_ANOMALY.value, VerdictType.PROBABLE_ANOMALY.value}

        tp = sum(1 for r in results if r.actual_verdict in POSITIVE_VERDICTS and r.expected_verdict in POSITIVE_VERDICTS)
        fp = sum(1 for r in results if r.actual_verdict in POSITIVE_VERDICTS and r.expected_verdict not in POSITIVE_VERDICTS)
        fn = sum(1 for r in results if r.actual_verdict not in POSITIVE_VERDICTS and r.expected_verdict in POSITIVE_VERDICTS)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
        avg_latency = sum(r.latency_ms for r in results) / len(results) if results else 0.0
        correct = sum(r.is_correct for r in results)

        # Metrics by level
        levels = {1, 2, 3, 4, 5}
        results_by_level = {}
        for level in levels:
            level_results = [r for r in results if r.level == level]
            if not level_results:
                continue
            level_correct = sum(r.is_correct for r in level_results)
            level_lat = sum(r.latency_ms for r in level_results) / len(level_results)
            results_by_level[level] = {
                "accuracy": level_correct / len(level_results),
                "avg_latency_ms": level_lat,
                "count": len(level_results),
            }

        return BenchmarkReport(
            total_scenarios=total,
            correct_predictions=correct,
            precision=precision,
            recall=recall,
            f1_score=f1,
            avg_latency_ms=avg_latency,
            results_by_level=results_by_level,
            individual_results=results,
        )


# ── CLI Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    print("MAF Curriculum Benchmark — Generating 50 evasion scenarios...")
    curriculum = CurriculumAgent(seed=42)
    scenarios = curriculum.generate_all_scenarios()
    print(f"Generated {len(scenarios)} scenarios across 5 levels.")

    executor = ExecutorAgent(debate_rounds=2)
    report = executor.run_benchmark(scenarios, verbose=True)

    report.print_summary()

    # Save report to JSON
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "output", "benchmark_report.json"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            **report.to_dict(),
            "individual_results": [
                {k: v for k, v in r.__dict__.items()}
                for r in report.individual_results
            ],
        }, f, indent=2)
    print(f"Report saved to: {output_path}")
