# Tools Directory

This directory contains the standalone tools, registry scrapers, and the core anomaly detection engine for the Maritime AI Framework (Engineer B domain).

## Anomaly Detection Engine (`anomaly_rules.py`)

The `anomaly_rules.py` script implements a PROWL-style, rule-based engine designed to detect maritime anomalies from vessel state data. 

### How it works
The `RuleEngine` class evaluates a JSON-compatible vessel state dictionary against a suite of rules. Each rule is defined as an independent static method within the `MaritimeRules` class, ensuring strict modularity.

### Available Rules
*   **`M-AIS-GAP`**: Detects missing AIS transmissions over a configurable time window (default > 6 hours).
*   **`M-SPOOF-SPEED`**: Detects GPS spoofing by calculating the Haversine distance between two points and flagging physically impossible speeds.
*   **`M-STS-LOITER`**: Detects suspicious loitering patterns (e.g., moving < 2 knots for > 12 hours outside of a port), indicating potential Ship-to-Ship (STS) cargo transfers or smuggling.
*   **`M-FLAG-HOP`**: Flags identity laundering risks based on a high frequency of registry (flag state) changes.

### Adding New Rules (For Other Engineers)
To extend the engine (for example, adding a bathymetric depth check or a TSS speed limit rule):
1. **Define the Logic**: Add a new static method to the `MaritimeRules` class. Your method must return a tuple: `(is_anomaly: bool, evidence_description: str)`.
2. **Register the Rule**: Add your rule to the `self.rules` list inside `RuleEngine.__init__`. Provide a unique `rule_id`, a `name`, a `category`, and a `severity` level (LOW/MEDIUM/HIGH/CRITICAL).

### Example Integration
```python
from tools.anomaly_rules import RuleEngine

# Initialize the engine
engine = RuleEngine()

# Example JSON state from the ingestion pipeline (Contract A1 -> B1)
vessel_state = {
    "mmsi": "123456789",
    "history": [
        {"timestamp": "2023-10-01T10:00:00Z", "lat": 40.0, "lon": -70.0, "speed": 14.0},
        {"timestamp": "2023-10-01T10:30:00Z", "lat": 40.1, "lon": -70.0, "speed": 14.1}
    ],
    "metadata": {
        "vessel_type": "Cargo",
        "flag": "PA",
        "flag_history": ["PA"]
    }
}

# Run evaluation
detected_anomalies = engine.evaluate(vessel_state)

# Output is a JSON-compatible list of triggered rules
print(detected_anomalies)
```
