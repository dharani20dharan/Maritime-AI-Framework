"""
MAF — Comprehensive Full Framework Test Suite
================================================
Automated unit and integration test suite validating:
  1. Contract schemas (B1, C1)
  2. COLREGs rule engine (colregs_rules.py)
  3. CA-CFAR radar backscatter detection (sar_fusion.py)
  4. YOLOv11n-OBB vessel extraction (sar_fusion.py)
  5. JVC bipartite matching / dark vessel identification (jvc_matcher.py)
  6. STGVAD spatio-temporal graph STS model (stgvad_model.py)
  7. Markov port call transition baseline (port_call_model.py)
  8. Multi-agent adversarial debate loop (adversarial_debate.py)
  9. Curriculum 50-scenario benchmark generator (curriculum_executor.py)
  10. XAI / SHAP attribution engine (xai_attribution.py)
  11. Telemetry and metric registration (shared/telemetry.py)
"""

import sys
import unittest
import numpy as np

# 1. Telemetry
from shared.telemetry import (
    span, trace_span, get_metrics_summary, record_latency, increment_counter, reset_metrics
)

# 2. Contracts
from shared.contracts.C1_debate_sar import (
    DebateVerdict, DebateRound, EvidenceFragment, VerdictType,
    ThreatCategory, RecommendedAction
)

# 3. COLREGs Rules
from tools.colregs_rules import (
    COLREGsEngine, IdentityRulesEngine, VesselSnapshot, COLREGsViolation
)

# 4. SAR Fusion & CV
from tools.sar_fusion import (
    CACFARDetector, OBBDetection, SARDetection, YOLOv11OBBParser
)

# 5. JVC Matcher
from tools.jvc_matcher import (
    JVCMatcher, RadarContact, DeadReckonedAIS, MatchResult, DarkVesselEvent
)

# 6. STGVAD STS Model
from tools.stgvad_model import (
    STGVADModel, VesselNode, STSDetectionResult
)

# 7. Port Call Model
from tools.port_call_model import (
    PortCallMarkovModel, PortCallEvent, PortCallAnomalyResult
)

# 8. Adversarial Debate
from agents.adversarial_debate import (
    AdversarialDebateOrchestrator, AgentA, AgentB, AgentC
)

# 9. Curriculum Executor
from agents.curriculum_executor import (
    CurriculumAgent, ExecutorAgent, EvasionScenario, BenchmarkReport
)

# 10. XAI Attribution
from tools.xai_attribution import (
    explain_vessel, load_thresholds, update_thresholds, TECHNIQUE_REGISTRY
)


class TestMAFContracts(unittest.TestCase):
    """Test data contracts and schemas."""

    def test_contract_c1_serialization(self):
        verdict = DebateVerdict(
            vessel_mmsi="123456789",
            vessel_name="TEST RUNNER",
            vessel_imo="9999999",
            verdict=VerdictType.CONFIRMED_ANOMALY,
            threat_category=ThreatCategory.DARK_SHIP_ACTIVITY,
            confidence=0.88,
            risk_score=85.0,
            primary_hypothesis="Vessel exhibiting dark loiter near terminal.",
            final_synthesis="Confirmed anomaly after multi-round debate.",
            recommended_actions=[RecommendedAction.FILE_SAR_REPORT],
            triggered_rules=["M-DARK-LOITER", "M-SANCTION-LINK"],
        )
        data = verdict.to_dict()
        self.assertEqual(data["vessel_mmsi"], "123456789")
        self.assertEqual(data["verdict"], "CONFIRMED_ANOMALY")
        self.assertEqual(data["threat_category"], "DARK_SHIP_ACTIVITY")
        self.assertIn("M-DARK-LOITER", data["triggered_rules"])
        json_str = verdict.to_json()
        self.assertIn("TEST RUNNER", json_str)


class TestCOLREGsEngine(unittest.TestCase):
    """Test deterministic COLREGs navigation & identity rules."""

    def test_head_on_encounter(self):
        engine = COLREGsEngine()
        v1 = VesselSnapshot(mmsi="111", lat=1.0, lon=100.0, speed_kts=12.0, heading=0)
        v2 = VesselSnapshot(mmsi="222", lat=1.005, lon=100.0, speed_kts=12.0, heading=180)
        violations = engine.evaluate_pair(v1, v2)
        self.assertGreaterEqual(len(violations), 1)
        rule_ids = [v.rule_id for v in violations]
        self.assertIn("COL-001", rule_ids)

    def test_flag_hop_detection(self):
        engine = IdentityRulesEngine()
        history = [
            {"flag": "Panama", "from_date": "2025-01-01", "to_date": "2025-03-01"},
            {"flag": "Liberia", "from_date": "2025-03-15", "to_date": "2025-06-01"},
            {"flag": "Gabon", "from_date": "2025-06-15", "to_date": "2025-09-01"},
        ]
        res = engine.check_flag_hopping("123456789", history)
        self.assertIsNotNone(res)
        self.assertEqual(res.rule_id, "ID-001")


class TestSARFusionAndCV(unittest.TestCase):
    """Test CA-CFAR radar processing and YOLOv11n-OBB."""

    def test_ca_cfar_detection(self):
        detector = CACFARDetector(num_guard_cells=2, num_reference_cells=6, false_alarm_rate=1e-3)
        rng = np.random.RandomState(42)
        radar_img = rng.exponential(scale=10.0, size=(64, 64)).tolist()
        radar_img[32][32] = 200.0

        detections = detector.detect_2d(radar_img, lat_origin=1.25, lon_origin=103.80)
        self.assertIsInstance(detections, list)
        self.assertGreater(len(detections), 0)

    def test_yolov11n_obb_parser(self):
        parser = YOLOv11OBBParser()
        raw_rows = [
            [0, 0.92, 256.0, 256.0, 80.0, 20.0, 45.0],
        ]
        obbs = parser.parse_raw_output(raw_rows)
        self.assertEqual(len(obbs), 1)
        obb = obbs[0]
        self.assertEqual(obb.class_name, "vessel")
        self.assertAlmostEqual(obb.score, 0.92)
        lat, lon, length_m, beam_m = obb.to_geo(image_lat_origin=1.25, image_lon_origin=103.80)
        self.assertGreater(length_m, 0)
        self.assertGreater(beam_m, 0)


class TestJVCMatcher(unittest.TestCase):
    """Test Jonker-Volgenant-Castañon bipartite matching."""

    def test_dark_vessel_unmatched(self):
        matcher = JVCMatcher(max_match_distance_nm=5.0)
        contacts = [
            RadarContact(contact_id="R1", lat=1.20, lon=103.80, length_m=180.0),
            RadarContact(contact_id="R2", lat=2.50, lon=105.00, length_m=240.0),
        ]
        ais_tracks = [
            DeadReckonedAIS(mmsi="111222333", lat=1.205, lon=103.805, speed_kts=10.0, heading_deg=45.0),
        ]
        matches, dark_events, unmatched_ais = matcher.match(contacts, ais_tracks)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].contact_id, "R1")
        self.assertEqual(len(dark_events), 1)
        self.assertEqual(dark_events[0].contact_id, "R2")


class TestSTGVADandMarkov(unittest.TestCase):
    """Test STGVAD STS rendezvous model and Port Call Markov model."""

    def test_stgvad_sts_detection(self):
        model = STGVADModel()
        v1 = VesselNode(mmsi="111", lat=1.20, lon=103.80, speed_kts=1.0, heading=90.0, timestamp="2026-10-11T10:00:00Z")
        v2 = VesselNode(mmsi="222", lat=1.202, lon=103.802, speed_kts=1.2, heading=92.0, timestamp="2026-10-11T10:00:00Z")
        results = model.detect_sts([v1, v2])
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        self.assertTrue(results[0].is_sts_candidate)

    def test_port_call_markov(self):
        model = PortCallMarkovModel()
        # Normal corridor sequence
        norm_events = [
            PortCallEvent("SGSIN", "Singapore", "2026-01-01"),
            PortCallEvent("CNSHA", "Shanghai", "2026-01-10"),
        ]
        res_norm = model.analyze("111", norm_events)
        self.assertFalse(res_norm.is_anomalous)

        # High risk sanctions port visit
        anom_events = [
            PortCallEvent("SGSIN", "Singapore", "2026-01-01"),
            PortCallEvent("IRBAN", "Bandar Abbas", "2026-01-10"),
        ]
        res_anom = model.analyze("222", anom_events)
        self.assertTrue(res_anom.is_anomalous)


class TestAdversarialDebateAndCurriculum(unittest.TestCase):
    """Test Agent A/B/C debate mediation and curriculum benchmark."""

    def test_debate_execution(self):
        orchestrator = AdversarialDebateOrchestrator(max_rounds=2)
        verdict = orchestrator.run_debate(
            mmsi="999888777",
            vessel_context={"name": "SHADOW VOYAGER", "flag": "Panama", "imo": "9123456"},
            triggered_rules=["M-DARK-LOITER", "M-STS-DETECT", "M-SANCTION-LINK"],
            verbose=False
        )
        self.assertEqual(verdict.vessel_mmsi, "999888777")
        self.assertEqual(len(verdict.debate_rounds), 2)
        self.assertIn(verdict.verdict, [VerdictType.CONFIRMED_ANOMALY, VerdictType.PROBABLE_ANOMALY])

    def test_curriculum_scenarios(self):
        agent = CurriculumAgent()
        scenarios = agent.generate_all_scenarios()
        self.assertEqual(len(scenarios), 50)
        levels = {s.level for s in scenarios}
        self.assertEqual(levels, {1, 2, 3, 4, 5})

        executor = ExecutorAgent(debate_rounds=1)
        report = executor.run_benchmark(scenarios[:3], verbose=False)
        self.assertEqual(report.total_scenarios, 3)


class TestXAIandTelemetry(unittest.TestCase):
    """Test XAI / SHAP attribution and telemetry."""

    def test_xai_attribution(self):
        res = explain_vessel(
            mmsi="123456789",
            triggered_rules=["M-SANCTION-LINK", "M-DARK-LOITER"],
            vessel_name="TEST VESSEL"
        )
        self.assertGreater(res.total_risk_score, 50.0)
        self.assertEqual(len(res.top_factors), 2)
        self.assertIn("Sanction List Link", res.top_factors)
        data = res.to_dict()
        self.assertEqual(data["mmsi"], "123456789")

    def test_telemetry_span(self):
        reset_metrics()
        @trace_span("test_unit_span")
        def work():
            return "ok"
        self.assertEqual(work(), "ok")
        summary = get_metrics_summary()
        self.assertIn("span.success.test_unit_span", summary["counters"])


if __name__ == "__main__":
    unittest.main()
