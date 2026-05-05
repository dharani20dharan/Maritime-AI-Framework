import json
from datetime import datetime
import math

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
        
        # In a real system, we'd check timestamps of flag changes. 
        # Here we simplify by checking the length of recent changes.
        if len(flag_history) > max_changes_per_year:
            return True, f"Identity Launder Risk: {len(flag_history)} flag changes recently"
        return False, "Normal flag registration"


# ---------------------------------------------------------
# Rule Engine Implementation
# ---------------------------------------------------------

class RuleEngine:
    def __init__(self):
        # Register rules with their metadata (JSON-serializable definitions)
        self.rules = [
            {
                "rule_id": "M-AIS-GAP",
                "name": "Extended AIS Gap",
                "category": "Evasion",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_ais_gap
            },
            {
                "rule_id": "M-SPOOF-SPEED",
                "name": "Impossible Speed (Spoofing)",
                "category": "Spoofing",
                "severity": "CRITICAL",
                "evaluator": MaritimeRules.check_spoofing_speed
            },
            {
                "rule_id": "M-STS-LOITER",
                "name": "Suspicious Loitering / STS Risk",
                "category": "Behavior",
                "severity": "MEDIUM",
                "evaluator": MaritimeRules.check_suspicious_loitering
            },
            {
                "rule_id": "M-FLAG-HOP",
                "name": "Flag Hopping",
                "category": "Identity",
                "severity": "HIGH",
                "evaluator": MaritimeRules.check_flag_hopping
            }
        ]

    def evaluate(self, vessel_state):
        """Evaluates all rules against a given vessel state and returns detected anomalies."""
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
