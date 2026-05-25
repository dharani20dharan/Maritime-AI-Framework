import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime
import math
import logging
from tools.bathymetry import BathymetryEngine

bathymetry_engine = BathymetryEngine()

try:
    from cassandra.cluster import Cluster
    CASSANDRA_AVAILABLE = True
except Exception:
    CASSANDRA_AVAILABLE = False


"""
PROWL-Style Maritime Anomaly Detection Engine
This module provides a modular, rule-based engine to detect maritime anomalies
such as AIS gaps, GPS spoofing, COLREGs violations, and suspicious evasion behaviors.

Rules are defined as dictionaries (JSON-compatible) mapping a rule name
to an evaluation function and metadata.
"""

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in nautical miles between two coordinates using Haversine."""
    R = 3440.065 # Radius of earth in nautical miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi/2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# ---------------------------------------------------------
# Rule Definitions (PROWL-Style Logic)
# ---------------------------------------------------------

class MaritimeRules:
    @staticmethod
    def check_ais_gap(vessel_state, threshold_hours=6):
        """Detects if the time between the last two AIS transmissions exceeds a threshold."""
        history = vessel_state.get("history", [])
        if len(history) < 2:
            return False, "Insufficient history"
        
        t1 = datetime.fromisoformat(history[-2]["timestamp"].replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(history[-1]["timestamp"].replace('Z', '+00:00'))
        gap_hours = (t2 - t1).total_seconds() / 3600.0
        
        if gap_hours > threshold_hours:
            return True, f"AIS Gap detected: {gap_hours:.1f} hours"
        return False, "Normal transmission rate"

    @staticmethod
    def check_spoofing_speed(vessel_state, max_knots=45):
        """Detects GPS spoofing by checking if calculated physical speed is impossible."""
        history = vessel_state.get("history", [])
        if len(history) < 2:
            return False, "Insufficient history"
        
        p1, p2 = history[-2], history[-1]
        dist_nm = calculate_distance(p1["lat"], p1["lon"], p2["lat"], p2["lon"])
        
        t1 = datetime.fromisoformat(p1["timestamp"].replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(p2["timestamp"].replace('Z', '+00:00'))
        time_hours = (t2 - t1).total_seconds() / 3600.0
        
        if time_hours <= 0:
            return True, "Invalid timestamp sequence (Spoofing/Error)"
            
        calc_speed = dist_nm / time_hours
        if calc_speed > max_knots:
            return True, f"Spoofing detected: Impossible speed of {calc_speed:.1f} knots"
        return False, f"Speed normal: {calc_speed:.1f} knots"

    @staticmethod
    def check_suspicious_loitering(vessel_state, max_speed=2.0, min_hours=12):
        """Detects potential Ship-to-Ship (STS) transfers or smuggling (slow speed in open water)."""
        history = vessel_state.get("history", [])
        if len(history) < 2:
            return False, "Insufficient history"
            
        # Check if the last N points span min_hours and speed stayed below max_speed
        t_end = datetime.fromisoformat(history[-1]["timestamp"].replace('Z', '+00:00'))
        t_start = t_end
        
        for point in reversed(history):
            if point.get("speed", 0) > max_speed:
                break
            t_start = datetime.fromisoformat(point["timestamp"].replace('Z', '+00:00'))
            
        loiter_duration = (t_end - t_start).total_seconds() / 3600.0
        if loiter_duration >= min_hours:
            return True, f"Suspicious Loitering: {loiter_duration:.1f} hours at <{max_speed} knots"
        return False, "No extended loitering detected"

    @staticmethod
    def check_flag_hopping(vessel_state, max_changes_per_year=2):
        """Detects Identity Laundering via frequent flag changes."""
        metadata = vessel_state.get("metadata", {})
        flag_history = metadata.get("flag_history", [])
        
        if len(flag_history) > max_changes_per_year:
            return True, f"Identity Launder Risk: {len(flag_history)} flag changes recently"
        return False, "Normal flag registration"

    @staticmethod
    def check_track_plausibility(vessel_state):
        history = vessel_state.get("history", [])
        if not history:
            return False, "No history available"
        
        metadata = vessel_state.get("metadata", {})
        draft = metadata.get("draft") or metadata.get("max_draft")
        
        # If draft is available, execute bathymetric depth vs draft check
        if draft and len(history) > 0:
            last_pos = history[-1]
            lat = last_pos.get("lat")
            lon = last_pos.get("lon")
            if lat is not None and lon is not None:
                plausible, depth, msg = bathymetry_engine.verify_draft_plausibility(lat, lon, float(draft))
                if not plausible:
                    return True, f"M-TRACK-PLAUSIBILITY: {msg}"

        # Standard sharp turn check fallback
        if len(history) < 3:
            return False, "Insufficient history for kinetic track check"
        p2, p3 = history[-2], history[-1]
        try:
            h1 = p2.get("heading") or p2.get("course", 0)
            h2 = p3.get("heading") or p3.get("course", 0)
            diff = abs(h2 - h1)
            if diff > 180:
                diff = 360 - diff
            if diff > 120 and p3.get("speed", 0) > 10:
                return True, f"Track Plausibility Violation: Sharp turn of {diff:.1f}° at speed {p3.get('speed', 0)} kts"
        except Exception:
            pass
        return False, "Track plausible"

    @staticmethod
    def check_mmsi_spoof(vessel_state):
        metadata = vessel_state.get("metadata", {})
        names = metadata.get("historical_names", [])
        if len(set(names)) > 2:
            return True, f"MMSI Spoofing: {len(set(names))} distinct vessel names claimed in short period"
        return False, "MMSI consistent"

    @staticmethod
    def check_identity_launder(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("simultaneous_identity_changes", 0) > 0:
            return True, "Identity Laundering: Simultaneous change of MMSI, vessel name, and flag detected"
        return False, "Identity stable"

    @staticmethod
    def check_imo_clone(vessel_state):
        metadata = vessel_state.get("metadata", {})
        clones = metadata.get("cloned_mmsis", [])
        if len(clones) > 0:
            return True, f"IMO Cloning Risk: {len(clones)} distinct MMSIs claiming this vessel's IMO"
        return False, "IMO unique"

    @staticmethod
    def check_ais_beacon(vessel_state):
        history = vessel_state.get("history", [])
        if len(history) < 20:
            return False, "Insufficient history for ping periodicity check"
        import statistics
        try:
            intervals = []
            for i in range(len(history) - 1):
                t1 = datetime.fromisoformat(history[i]["timestamp"].replace('Z', '+00:00'))
                t2 = datetime.fromisoformat(history[i+1]["timestamp"].replace('Z', '+00:00'))
                intervals.append((t2 - t1).total_seconds())
            mean_int = statistics.mean(intervals)
            if mean_int > 0:
                stdev = statistics.stdev(intervals)
                cv = stdev / mean_int
                if cv < 0.01:
                    return True, f"Automated Beacon Pattern: Scripted AIS ping intervals (CV={cv:.6f} < 0.01)"
        except Exception:
            pass
        return False, "Beacon pattern normal"

    @staticmethod
    def check_port_call_anomaly(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("irregular_port_visit", False):
            return True, "Port Call Pattern Anomaly: Statistical irregularity in port call trade sequence"
        return False, "Port visits normal"

    @staticmethod
    def check_eez_violation(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("eez_unauthorized_entry", False):
            return True, "EEZ Violation: Unauthorized entry into foreign Exclusive Economic Zone"
        return False, "No EEZ violations"

    @staticmethod
    def check_sanctioned_zone(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("sanctioned_zone_entry", False) or metadata.get("flag", "") in ["IR", "KP", "RU"]:
            return True, f"Sanctioned Zone Entry: Vessel operated in restricted/sanctioned waters (Flag: {metadata.get('flag')})"
        return False, "Clear of sanctioned zones"

    @staticmethod
    def check_sts_transfer(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("sts_rendezvous_detected", False):
            return True, "Ship-to-Ship Transfer Risk: Sustainable close-proximity co-loitering event detected"
        return False, "No STS rendezvous detected"

    @staticmethod
    def check_fleet_broker(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("high_betweenness_parent", False):
            return True, "Fleet Broker Connection: Managed by corporate broker representing large sanctioned fleet"
        return False, "Normal management structure"

    @staticmethod
    def check_shell_chain(vessel_state):
        metadata = vessel_state.get("metadata", {})
        chain_depth = metadata.get("ownership_chain_depth", 1)
        if chain_depth > 3:
            return True, f"Shell Company Chain: Deeply layered ownership structure ({chain_depth} levels removed)"
        return False, "Direct/Standard ownership"

    @staticmethod
    def check_sister_risk(vessel_state):
        metadata = vessel_state.get("metadata", {})
        if metadata.get("sister_ship_sanctioned", False):
            return True, "Sister Ship Risk Propagation: Structural identical vessel under active sanction"
        return False, "No sister ship risk"


# ---------------------------------------------------------
# Rule Engine Implementation
# ---------------------------------------------------------

class RuleEngine:
    def __init__(self, cassandra_contact_points=["localhost"], cassandra_keyspace="maf_ais"):
        self.cassandra_session = None
        if CASSANDRA_AVAILABLE:
            try:
                cluster = Cluster(cassandra_contact_points)
                self.cassandra_session = cluster.connect(cassandra_keyspace)
            except Exception as e:
                logging.warning(f"Failed to connect to Cassandra: {e}")
                
        # Register rules with their metadata (JSON-serializable definitions)
        self.rules = [
            {
                "rule_id": "M-SPEED-ANOMALY",
                "name": "Impossible Speed (Spoofing)",
                "category": "Physics",
                "severity": "CRITICAL",
                "evaluator": MaritimeRules.check_spoofing_speed
            },
            {
                "rule_id": "M-TRACK-PLAUSIBILITY",
                "name": "Impossible Track Curvature",
                "category": "Physics",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_track_plausibility
            },
            {
                "rule_id": "M-LOITERING",
                "name": "Suspicious Loiter",
                "category": "Behavior",
                "severity": "MEDIUM",
                "evaluator": MaritimeRules.check_suspicious_loitering
            },
            {
                "rule_id": "M-MMSI-SPOOF",
                "name": "MMSI Spoofing",
                "category": "Identity",
                "severity": "CRITICAL",
                "evaluator": MaritimeRules.check_mmsi_spoof
            },
            {
                "rule_id": "M-FLAG-HOP",
                "name": "Flag Hopping",
                "category": "Identity",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_flag_hopping
            },
            {
                "rule_id": "M-IDENTITY-LAUNDER",
                "name": "Identity Laundering",
                "category": "Identity",
                "severity": "CRITICAL",
                "evaluator": MaritimeRules.check_identity_launder
            },
            {
                "rule_id": "M-IMO-CLONE",
                "name": "IMO Cloning",
                "category": "Identity",
                "severity": "CRITICAL",
                "evaluator": MaritimeRules.check_imo_clone
            },
            {
                "rule_id": "M-DARK-EVENT",
                "name": "AIS Gap Anomaly",
                "category": "Behavior",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_ais_gap
            },
            {
                "rule_id": "M-AIS-BEACON",
                "name": "Automated Beacon Pattern",
                "category": "Telemetry",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_ais_beacon
            },
            {
                "rule_id": "M-PORT-CALL-ANOMALY",
                "name": "Port Call Anomaly",
                "category": "Behavior",
                "severity": "MEDIUM",
                "evaluator": MaritimeRules.check_port_call_anomaly
            },
            {
                "rule_id": "M-EEZ-VIOLATION",
                "name": "EEZ Violation",
                "category": "Regulatory",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_eez_violation
            },
            {
                "rule_id": "M-SANCTIONED-ZONE",
                "name": "Sanctioned Zone Entry",
                "category": "Regulatory",
                "severity": "CRITICAL",
                "evaluator": MaritimeRules.check_sanctioned_zone
            },
            {
                "rule_id": "M-STS-TRANSFER",
                "name": "Ship-to-Ship Transfer Risk",
                "category": "Behavior",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_sts_transfer
            },
            {
                "rule_id": "M-FLEET-BROKER",
                "name": "Fleet Broker Detection",
                "category": "Ownership",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_fleet_broker
            },
            {
                "rule_id": "M-SHELL-CHAIN",
                "name": "Shell Company Chain",
                "category": "Ownership",
                "severity": "MEDIUM",
                "evaluator": MaritimeRules.check_shell_chain
            },
            {
                "rule_id": "M-SISTER-RISK",
                "name": "Sister Ship Risk",
                "category": "Ownership",
                "severity": "MEDIUM",
                "evaluator": MaritimeRules.check_sister_risk
            }
        ]

    def _fetch_history_from_cassandra(self, mmsi):
        if not self.cassandra_session:
            return []
        
        query = "SELECT lat, lon, speed_kts AS speed, timestamp FROM ais_positions WHERE mmsi = %s LIMIT 1000"
        rows = self.cassandra_session.execute(query, (mmsi,))
        return [{"lat": r.lat, "lon": r.lon, "speed": r.speed, "timestamp": r.timestamp.isoformat() + "Z"} for r in rows]

    def evaluate(self, vessel_state):
        """Evaluates all rules against a given vessel state and returns detected anomalies."""
        
        # If history is missing and Cassandra is available, pull it
        if "history" not in vessel_state or not vessel_state["history"]:
            if "mmsi" in vessel_state:
                vessel_state["history"] = self._fetch_history_from_cassandra(vessel_state["mmsi"])
                
        results = []
        for rule in self.rules:
            is_anomaly, evidence = rule["evaluator"](vessel_state)
            if is_anomaly:
                results.append({
                    "rule_id": rule["rule_id"],
                    "name": rule["name"],
                    "category": rule["category"],
                    "severity": rule["severity"],
                    "evidence": evidence
                })
        return results

# ---------------------------------------------------------
# Example Execution
# ---------------------------------------------------------
if __name__ == "__main__":
    engine = RuleEngine()

    # Example 1: Normal Vessel
    normal_vessel = {
        "mmsi": "123456789",
        "metadata": {"vessel_type": "Cargo", "flag": "PA", "flag_history": ["PA"]},
        "history": [
            {"timestamp": "2023-10-01T10:00:00Z", "lat": 40.0, "lon": -70.0, "speed": 14.0},
            {"timestamp": "2023-10-01T10:30:00Z", "lat": 40.1, "lon": -70.0, "speed": 14.1}
        ]
    }

    # Example 2: Vessel engaged in Spoofing and Flag Hopping
    anomalous_vessel = {
        "mmsi": "987654321",
        "metadata": {"vessel_type": "Tanker", "flag": "IR", "flag_history": ["PA", "LR", "CY", "IR"]},
        "history": [
            # Jumped from NY coast to mid-Atlantic in 30 minutes (impossible speed)
            {"timestamp": "2023-10-01T10:00:00Z", "lat": 40.0, "lon": -70.0, "speed": 12.0},
            {"timestamp": "2023-10-01T10:30:00Z", "lat": 45.0, "lon": -60.0, "speed": 12.0} 
        ]
    }
    
    # Example 3: AIS Gap & STS Loitering
    dark_vessel = {
        "mmsi": "555555555",
        "metadata": {"vessel_type": "Tanker", "flag": "LR", "flag_history": []},
        "history": [
            # 10 hour gap, then moving at 1 knot for 13 hours
            {"timestamp": "2023-10-01T08:00:00Z", "lat": 24.0, "lon": 55.0, "speed": 12.0},
            {"timestamp": "2023-10-01T18:00:00Z", "lat": 24.5, "lon": 55.5, "speed": 0.5},
            {"timestamp": "2023-10-02T07:30:00Z", "lat": 24.51, "lon": 55.51, "speed": 0.5}
        ]
    }

    print("--- Evaluating Normal Vessel ---")
    print(json.dumps(engine.evaluate(normal_vessel), indent=2))

    print("\n--- Evaluating Spoofing/Flag Hopping Vessel ---")
    print(json.dumps(engine.evaluate(anomalous_vessel), indent=2))

    print("\n--- Evaluating Dark/STS Loitering Vessel ---")
    print(json.dumps(engine.evaluate(dark_vessel), indent=2))
