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

## Sanction Evasion Risk Scorer (`sanction_scorer.py`)

The `sanction_scorer.py` script bridges the gap between your autonomous agents and the Neo4j Knowledge Graph. It executes complex, multi-hop Cypher queries to detect sanction evasion patterns (like shell companies and flag hopping) and calculates an aggregate **Evasion Risk Score (0-100)**.

### How it works
The `SanctionScorer` class connects to the Neo4j database using a read-only Bolt connection (adhering to Contract B1). When you pass a vessel's IMO number, it queries the graph for:
1.  **Ownership Risk**: Traverses `OWNED_BY`/`MANAGED_BY` and `SUBSIDIARY_OF` relationships to see if the vessel is linked to a sanctioned entity through front companies.
2.  **Behavioral Risk**: Correlates the vessel's dark activity (`AIS_GAP`, `LOITERING`) with other vessels to detect Ship-to-Ship (STS) transfers.
3.  **Identity Risk**: Counts historical `REGISTERED_UNDER` relationships to penalize flag hopping.

The final output is capped at 100 and returns the specific evidence flags.

### Example Integration
```python
from tools.sanction_scorer import SanctionScorer

scorer = SanctionScorer()

# Pass the IMO of the vessel you want to investigate
risk_score, flags = scorer.calculate_risk("9988776")

print(f"Risk Score: {risk_score}")
print(f"Triggered Flags: {flags}")

scorer.close()
```

## Registry Scrapers (`scrapers.py`)

The `scrapers.py` module contains Web Scrapers designed to pull external registry data (MMSI, Flag, Ownership) from public maritime databases like **MarineTraffic** and **Equasis**.

### Anti-Bot Measures & Fallbacks
Because these websites employ aggressive anti-bot protections (Cloudflare, CAPTCHAs, and authenticated sessions), standard `requests` calls will often be blocked (`403 Forbidden`). 

To ensure our AI reasoning pipelines don't crash, these scrapers are built with a robust scaffolding: they *attempt* the live scrape, but if blocked, they gracefully catch the error and return **formatted JSON mock data**.

### Example Integration
```python
from tools.scrapers import MarineTrafficScraper, EquasisScraper

# 1. MarineTraffic
mt = MarineTrafficScraper(delay_seconds=2.0)
mt_data = mt.scrape_vessel("9988776")
print(mt_data)

# 2. Equasis
eq = EquasisScraper()
eq_data = eq.scrape_vessel("9123456")
print(eq_data)
```

> [!TIP]
> **Upgrading for Production:** To bypass the mock fallback in a real production environment, Engineer A should integrate a residential proxy network (e.g., BrightData) or inject authenticated session cookies into the `self.session` object inside the scraper classes.
