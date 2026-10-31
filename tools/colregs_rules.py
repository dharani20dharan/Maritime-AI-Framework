"""
MAF — COLREGs Semantic Rule Engine
====================================
Deterministic rule-firing engine for International Regulations for Preventing
Collisions at Sea (COLREGs / COLREG-72), combined with maritime identity
verification rules for flag hopping and name-swapping detection.

Rules implemented:
  - COL-001: Head-On Situation (Rule 14)
  - COL-002: Crossing Situation (Rule 15)
  - COL-003: Overtaking Situation (Rule 13)
  - COL-004: Vessel Not Under Command (Rule 27)
  - COL-005: Restricted Visibility / Close Quarters (Rule 19)
  - ID-001:  Flag Hopping Detection
  - ID-002:  Name / IMO Consistency Verification
  - ID-003:  MMSI Conflict / Identity Laundering
  - ID-004:  Dark Period + Re-emergence with Different Identity
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CLOSE_QUARTERS_NM = 0.5          # < 0.5 NM = close quarters
HEAD_ON_BEARING_DEG = 6.0        # ±6° of reciprocal course = head-on
OVERTAKING_SECTOR_DEG = 112.5    # Overtaking if bearing from stern ±112.5°
CROSSING_MIN_RANGE_NM = 0.25     # Must be within 0.25 NM to be a real crossing risk
FLAG_HOP_MAX_INTERVAL_DAYS = 180 # Flag change within 6 months = suspicious
MIN_FLAGS_FOR_HOP = 3            # 3+ flags in 2 years = definite flag hopping


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class VesselSnapshot:
    """Real-time vessel state snapshot for COLREGs evaluation."""
    mmsi: str
    lat: float
    lon: float
    speed_kts: float
    heading: int            # True heading 0-359
    nav_status: int = 0     # AIS nav status code
    name: Optional[str] = None
    flag: Optional[str] = None
    imo: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class COLREGsViolation:
    """A detected COLREGs situation or identity fraud event."""
    rule_id: str
    rule_name: str
    severity: str           # "HIGH" | "MEDIUM" | "LOW"
    description: str
    vessel_a_mmsi: str
    vessel_b_mmsi: Optional[str] = None
    bearing_deg: Optional[float] = None
    range_nm: Optional[float] = None
    confidence: float = 1.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "description": self.description,
            "vessel_a_mmsi": self.vessel_a_mmsi,
            "vessel_b_mmsi": self.vessel_b_mmsi,
            "bearing_deg": self.bearing_deg,
            "range_nm": self.range_nm,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


# ── Geometric Utilities ───────────────────────────────────────────────────────

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in nautical miles."""
    R = 3440.065  # Earth radius in NM
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return initial bearing from point 1 to point 2 in degrees (0–360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def angle_diff(a: float, b: float) -> float:
    """Return shortest angular difference between two headings (0–180)."""
    diff = abs((a - b + 180) % 360 - 180)
    return diff


# ── COLREGs Rule Engine ───────────────────────────────────────────────────────

class COLREGsEngine:
    """
    Deterministic COLREGs rule-firing engine.
    Evaluates a pair of vessel snapshots and returns any applicable violations.
    """

    def evaluate_pair(
        self,
        vessel_a: VesselSnapshot,
        vessel_b: VesselSnapshot,
    ) -> List[COLREGsViolation]:
        """Evaluate all COLREGs rules for a pair of vessels."""
        violations: List[COLREGsViolation] = []

        # Skip if either vessel is anchored / moored (nav_status 1 or 5)
        if vessel_a.nav_status in (1, 5) or vessel_b.nav_status in (1, 5):
            return violations

        # Skip if speed is negligible
        if vessel_a.speed_kts < 0.5 or vessel_b.speed_kts < 0.5:
            return violations

        range_nm = haversine_nm(vessel_a.lat, vessel_a.lon, vessel_b.lat, vessel_b.lon)
        bearing_a_to_b = bearing_between(vessel_a.lat, vessel_a.lon, vessel_b.lat, vessel_b.lon)

        v = self._check_head_on(vessel_a, vessel_b, range_nm, bearing_a_to_b)
        if v:
            violations.append(v)

        v = self._check_crossing(vessel_a, vessel_b, range_nm, bearing_a_to_b)
        if v:
            violations.append(v)

        v = self._check_overtaking(vessel_a, vessel_b, range_nm, bearing_a_to_b)
        if v:
            violations.append(v)

        v = self._check_not_under_command(vessel_a, range_nm)
        if v:
            violations.append(v)

        v = self._check_close_quarters(vessel_a, vessel_b, range_nm)
        if v:
            violations.append(v)

        return violations

    def _check_head_on(
        self,
        a: VesselSnapshot,
        b: VesselSnapshot,
        range_nm: float,
        bearing_a_to_b: float,
    ) -> Optional[COLREGsViolation]:
        """
        COL-001: Head-on situation (COLREGs Rule 14).
        Both vessels on nearly reciprocal courses, meeting nearly head-on.
        """
        # Check if courses are nearly reciprocal (±HEAD_ON_BEARING_DEG of 180)
        course_diff = angle_diff(a.heading, b.heading)
        if abs(course_diff - 180) > HEAD_ON_BEARING_DEG:
            return None

        # Check if A is bearing toward B (A sees B nearly ahead)
        bearing_from_own_bow = angle_diff(a.heading, bearing_a_to_b)
        if bearing_from_own_bow > 15:
            return None

        if range_nm > 2.0:  # Only flag if within 2 NM
            return None

        return COLREGsViolation(
            rule_id="COL-001",
            rule_name="Head-On Situation (Rule 14)",
            severity="HIGH",
            description=(
                f"Vessels {a.mmsi} and {b.mmsi} are on reciprocal courses "
                f"({a.heading}° vs {b.heading}°) at {range_nm:.2f} NM range. "
                f"Both must alter course to starboard."
            ),
            vessel_a_mmsi=a.mmsi,
            vessel_b_mmsi=b.mmsi,
            bearing_deg=bearing_a_to_b,
            range_nm=range_nm,
            confidence=0.95,
        )

    def _check_crossing(
        self,
        a: VesselSnapshot,
        b: VesselSnapshot,
        range_nm: float,
        bearing_a_to_b: float,
    ) -> Optional[COLREGsViolation]:
        """
        COL-002: Crossing situation (COLREGs Rule 15).
        Vessel B is on A's starboard side within crossing sector (5°–112.5° of bow).
        """
        # B's bearing relative to A's bow
        relative_bearing = (bearing_a_to_b - a.heading + 360) % 360
        if not (5 <= relative_bearing <= 112.5):
            return None

        # Courses should be crossing (not parallel)
        course_diff = angle_diff(a.heading, b.heading)
        if course_diff < 30 or course_diff > 150:
            return None

        if range_nm > 3.0 or range_nm < CROSSING_MIN_RANGE_NM:
            return None

        return COLREGsViolation(
            rule_id="COL-002",
            rule_name="Crossing Situation (Rule 15)",
            severity="MEDIUM",
            description=(
                f"Vessel {b.mmsi} is crossing ahead of {a.mmsi} on starboard side "
                f"(relative bearing {relative_bearing:.0f}°) at {range_nm:.2f} NM. "
                f"Vessel {a.mmsi} is the give-way vessel."
            ),
            vessel_a_mmsi=a.mmsi,
            vessel_b_mmsi=b.mmsi,
            bearing_deg=bearing_a_to_b,
            range_nm=range_nm,
            confidence=0.85,
        )

    def _check_overtaking(
        self,
        a: VesselSnapshot,
        b: VesselSnapshot,
        range_nm: float,
        bearing_a_to_b: float,
    ) -> Optional[COLREGsViolation]:
        """
        COL-003: Overtaking situation (COLREGs Rule 13).
        A is approaching B from more than 22.5° abaft B's beam and is faster.
        """
        # A's bearing from B (reverse)
        bearing_b_to_a = (bearing_a_to_b + 180) % 360
        stern_sector = angle_diff(b.heading, bearing_b_to_a)

        # Must be approaching from within 112.5° of B's stern
        if stern_sector > OVERTAKING_SECTOR_DEG:
            return None

        # A must be faster than B
        if a.speed_kts <= b.speed_kts * 1.1:
            return None

        if range_nm > 2.0:
            return None

        return COLREGsViolation(
            rule_id="COL-003",
            rule_name="Overtaking Situation (Rule 13)",
            severity="LOW",
            description=(
                f"Vessel {a.mmsi} ({a.speed_kts:.1f}kts) is overtaking {b.mmsi} "
                f"({b.speed_kts:.1f}kts) from its stern sector at {range_nm:.2f} NM. "
                f"Overtaking vessel must keep clear."
            ),
            vessel_a_mmsi=a.mmsi,
            vessel_b_mmsi=b.mmsi,
            bearing_deg=bearing_a_to_b,
            range_nm=range_nm,
            confidence=0.80,
        )

    def _check_not_under_command(
        self, vessel: VesselSnapshot, range_nm: float
    ) -> Optional[COLREGsViolation]:
        """
        COL-004: Vessel Not Under Command or Restricted Maneuverability (Rule 27).
        AIS nav_status 1 = NUC, 2 = Restricted maneuverability.
        """
        NUC_STATUSES = {1: "Not Under Command", 2: "Restricted Maneuverability"}
        if vessel.nav_status not in NUC_STATUSES:
            return None

        return COLREGsViolation(
            rule_id="COL-004",
            rule_name="Vessel Not Under Command (Rule 27)",
            severity="HIGH",
            description=(
                f"Vessel {vessel.mmsi} is broadcasting nav status: "
                f"{NUC_STATUSES[vessel.nav_status]}. "
                f"All vessels must keep clear and not impede passage."
            ),
            vessel_a_mmsi=vessel.mmsi,
            range_nm=range_nm,
            confidence=1.0,
            metadata={"nav_status": vessel.nav_status},
        )

    def _check_close_quarters(
        self, a: VesselSnapshot, b: VesselSnapshot, range_nm: float
    ) -> Optional[COLREGsViolation]:
        """
        COL-005: Close-quarters situation (Rule 8 / Rule 19).
        """
        if range_nm > CLOSE_QUARTERS_NM:
            return None

        return COLREGsViolation(
            rule_id="COL-005",
            rule_name="Close Quarters Situation (Rule 8/19)",
            severity="HIGH",
            description=(
                f"Vessels {a.mmsi} and {b.mmsi} are in close-quarters situation: "
                f"{range_nm * 1852:.0f}m separation. Immediate action required."
            ),
            vessel_a_mmsi=a.mmsi,
            vessel_b_mmsi=b.mmsi,
            range_nm=range_nm,
            confidence=1.0,
        )


# ── Identity Rules Engine ─────────────────────────────────────────────────────

class IdentityRulesEngine:
    """
    Identity verification rules for detecting flag hopping,
    name/IMO inconsistency, MMSI conflicts, and identity laundering.
    """

    def check_flag_hopping(
        self,
        mmsi: str,
        flag_history: List[dict],  # [{"flag": "PA", "from_date": "2022-01-01", "to_date": "2022-08-15"}, ...]
    ) -> Optional[COLREGsViolation]:
        """
        ID-001: Flag Hopping Detection.
        Detects suspicious rapid changes of flag state.
        """
        if len(flag_history) < 2:
            return None

        # Sort by from_date
        try:
            sorted_flags = sorted(
                flag_history,
                key=lambda x: datetime.fromisoformat(x.get("from_date", "2000-01-01"))
            )
        except (ValueError, TypeError):
            sorted_flags = flag_history

        # Count flags in last 2 years
        recent_flags = []
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
        for entry in sorted_flags:
            try:
                from_date = datetime.fromisoformat(entry.get("from_date", "2000-01-01"))
                years_ago = (cutoff - from_date).days / 365.25
                if years_ago <= 2.0:
                    recent_flags.append(entry)
            except (ValueError, TypeError):
                continue

        if len(recent_flags) < MIN_FLAGS_FOR_HOP:
            return None

        # Check if any consecutive change was suspiciously fast
        rapid_changes = 0
        for i in range(1, len(sorted_flags)):
            try:
                prev_to = sorted_flags[i - 1].get("to_date")
                curr_from = sorted_flags[i].get("from_date")
                if prev_to and curr_from:
                    interval = (
                        datetime.fromisoformat(curr_from) - datetime.fromisoformat(prev_to)
                    ).days
                    if interval < FLAG_HOP_MAX_INTERVAL_DAYS:
                        rapid_changes += 1
            except (ValueError, TypeError):
                continue

        if rapid_changes == 0 and len(recent_flags) < 4:
            return None

        flag_seq = " → ".join(e.get("flag", "?") for e in sorted_flags[-5:])
        return COLREGsViolation(
            rule_id="ID-001",
            rule_name="Flag Hopping Detected",
            severity="HIGH",
            description=(
                f"Vessel {mmsi} has changed flag {len(recent_flags)} times in 2 years "
                f"({rapid_changes} rapid changes <{FLAG_HOP_MAX_INTERVAL_DAYS}d). "
                f"Flag sequence: {flag_seq}. Indicative of sanctions evasion."
            ),
            vessel_a_mmsi=mmsi,
            confidence=min(0.7 + rapid_changes * 0.1, 1.0),
            metadata={
                "flag_changes_2yr": len(recent_flags),
                "rapid_changes": rapid_changes,
                "flag_sequence": [e.get("flag") for e in sorted_flags],
            },
        )

    def check_imo_name_consistency(
        self,
        mmsi: str,
        current_imo: Optional[str],
        current_name: Optional[str],
        registry_imo: Optional[str],
        registry_name: Optional[str],
    ) -> Optional[COLREGsViolation]:
        """
        ID-002: IMO / Name consistency check.
        Compares AIS-broadcast identity against official registry records.
        """
        issues = []

        if current_imo and registry_imo and current_imo != registry_imo:
            issues.append(f"IMO mismatch: AIS={current_imo}, Registry={registry_imo}")

        if current_name and registry_name:
            # Normalize for comparison
            norm_ais = current_name.upper().strip()
            norm_reg = registry_name.upper().strip()
            if norm_ais != norm_reg:
                issues.append(f"Name mismatch: AIS='{current_name}', Registry='{registry_name}'")

        if not issues:
            return None

        return COLREGsViolation(
            rule_id="ID-002",
            rule_name="IMO/Name Identity Inconsistency",
            severity="HIGH",
            description=(
                f"Vessel {mmsi} is broadcasting identity inconsistent with registry records. "
                f"{'; '.join(issues)}. Possible name-swapping or AIS spoofing."
            ),
            vessel_a_mmsi=mmsi,
            confidence=0.95,
            metadata={
                "ais_imo": current_imo,
                "registry_imo": registry_imo,
                "ais_name": current_name,
                "registry_name": registry_name,
                "issues": issues,
            },
        )

    def check_mmsi_conflict(
        self,
        mmsi: str,
        observed_positions: List[dict],  # [{"lat": ..., "lon": ..., "timestamp": ...}, ...]
    ) -> Optional[COLREGsViolation]:
        """
        ID-003: MMSI Conflict / Identity Laundering.
        Detects if a single MMSI appears at two physically impossible locations simultaneously.
        """
        if len(observed_positions) < 2:
            return None

        # Check all pairs within a 10-minute window
        MAX_PHYSICALLY_POSSIBLE_NM = 10  # 10 NM in 10 min = 60 knots (max possible)
        conflicts = []

        for i in range(len(observed_positions)):
            for j in range(i + 1, len(observed_positions)):
                p1 = observed_positions[i]
                p2 = observed_positions[j]
                try:
                    t1 = datetime.fromisoformat(p1["timestamp"].replace("Z", "+00:00"))
                    t2 = datetime.fromisoformat(p2["timestamp"].replace("Z", "+00:00"))
                    dt_hours = abs((t2 - t1).total_seconds()) / 3600.0
                    if dt_hours > (10 / 60):  # only check within 10-minute window
                        continue
                    dist_nm = haversine_nm(p1["lat"], p1["lon"], p2["lat"], p2["lon"])
                    if dist_nm > MAX_PHYSICALLY_POSSIBLE_NM:
                        conflicts.append({
                            "distance_nm": dist_nm,
                            "interval_min": dt_hours * 60,
                            "positions": [p1, p2],
                        })
                except (KeyError, ValueError):
                    continue

        if not conflicts:
            return None

        worst = max(conflicts, key=lambda c: c["distance_nm"])
        return COLREGsViolation(
            rule_id="ID-003",
            rule_name="MMSI Conflict / Identity Laundering",
            severity="HIGH",
            description=(
                f"MMSI {mmsi} detected at {len(conflicts)} physically impossible "
                f"simultaneous location pairs. Worst case: {worst['distance_nm']:.1f} NM "
                f"in {worst['interval_min']:.1f} min. Indicates MMSI cloning / identity laundering."
            ),
            vessel_a_mmsi=mmsi,
            confidence=0.99,
            metadata={"conflict_count": len(conflicts), "worst_conflict": worst},
        )

    def check_dark_reemergence(
        self,
        mmsi: str,
        dark_event: dict,       # {"start": "...", "end": "...", "location_before": {...}}
        reemergence: dict,      # {"timestamp": "...", "imo": "...", "name": "...", "flag": "..."}
        pre_dark_identity: dict,  # {"imo": "...", "name": "...", "flag": "..."}
    ) -> Optional[COLREGsViolation]:
        """
        ID-004: Dark Period + Re-emergence with Different Identity.
        After an AIS dark event, vessel re-appears with changed name/IMO/flag.
        """
        identity_changes = []

        if (pre_dark_identity.get("imo") and reemergence.get("imo") and
                pre_dark_identity["imo"] != reemergence["imo"]):
            identity_changes.append("IMO changed")

        if (pre_dark_identity.get("name") and reemergence.get("name") and
                pre_dark_identity["name"].upper() != reemergence["name"].upper()):
            identity_changes.append("Name changed")

        if (pre_dark_identity.get("flag") and reemergence.get("flag") and
                pre_dark_identity["flag"] != reemergence["flag"]):
            identity_changes.append("Flag changed")

        if not identity_changes:
            return None

        try:
            dark_start = dark_event.get("start", "")
            dark_end = dark_event.get("end", "")
            dark_duration = "unknown"
            if dark_start and dark_end:
                t1 = datetime.fromisoformat(dark_start.replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(dark_end.replace("Z", "+00:00"))
                dark_hours = (t2 - t1).total_seconds() / 3600.0
                dark_duration = f"{dark_hours:.1f} hours"
        except (ValueError, TypeError):
            dark_duration = "unknown"

        return COLREGsViolation(
            rule_id="ID-004",
            rule_name="Dark Re-emergence with Identity Change",
            severity="HIGH",
            description=(
                f"Vessel {mmsi} went dark for {dark_duration} and re-emerged with "
                f"changed identity: {', '.join(identity_changes)}. "
                f"Before: {pre_dark_identity}. After: {reemergence}. "
                f"Classic identity laundering after illicit STS transfer."
            ),
            vessel_a_mmsi=mmsi,
            confidence=0.90,
            metadata={
                "identity_changes": identity_changes,
                "dark_duration": dark_duration,
                "pre_dark": pre_dark_identity,
                "post_dark": reemergence,
            },
        )


# ── Unified COLREGs + Identity Rule Evaluator ─────────────────────────────────

class MaritimeLegalRuleEngine:
    """
    Top-level rule engine combining COLREGs collision-avoidance rules
    with vessel identity verification rules.
    """

    def __init__(self):
        self.colregs = COLREGsEngine()
        self.identity = IdentityRulesEngine()

    def evaluate_vessel_pair(
        self, vessel_a: VesselSnapshot, vessel_b: VesselSnapshot
    ) -> List[COLREGsViolation]:
        """Evaluate COLREGs rules for a vessel pair."""
        return self.colregs.evaluate_pair(vessel_a, vessel_b)

    def evaluate_vessel_identity(
        self,
        mmsi: str,
        flag_history: Optional[List[dict]] = None,
        current_imo: Optional[str] = None,
        current_name: Optional[str] = None,
        registry_imo: Optional[str] = None,
        registry_name: Optional[str] = None,
        mmsi_positions: Optional[List[dict]] = None,
        dark_event: Optional[dict] = None,
        reemergence: Optional[dict] = None,
        pre_dark_identity: Optional[dict] = None,
    ) -> List[COLREGsViolation]:
        """Evaluate all identity rules for a single vessel."""
        violations = []

        if flag_history:
            v = self.identity.check_flag_hopping(mmsi, flag_history)
            if v:
                violations.append(v)

        if current_imo or current_name:
            v = self.identity.check_imo_name_consistency(
                mmsi, current_imo, current_name, registry_imo, registry_name
            )
            if v:
                violations.append(v)

        if mmsi_positions:
            v = self.identity.check_mmsi_conflict(mmsi, mmsi_positions)
            if v:
                violations.append(v)

        if dark_event and reemergence and pre_dark_identity:
            v = self.identity.check_dark_reemergence(
                mmsi, dark_event, reemergence, pre_dark_identity
            )
            if v:
                violations.append(v)

        return violations


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = MaritimeLegalRuleEngine()

    # Test COLREGs head-on
    va = VesselSnapshot(mmsi="123456789", lat=1.0, lon=103.8, speed_kts=12, heading=45)
    vb = VesselSnapshot(mmsi="987654321", lat=1.01, lon=103.81, speed_kts=10, heading=225)
    violations = engine.evaluate_vessel_pair(va, vb)
    for v in violations:
        print(f"[{v.rule_id}] {v.severity}: {v.description}")

    # Test flag hopping
    flag_history = [
        {"flag": "PA", "from_date": "2022-01-01", "to_date": "2022-06-01"},
        {"flag": "PW", "from_date": "2022-06-15", "to_date": "2022-10-01"},
        {"flag": "TZ", "from_date": "2022-10-20", "to_date": "2023-03-01"},
        {"flag": "KM", "from_date": "2023-03-15", "to_date": None},
    ]
    violations = engine.evaluate_vessel_identity("123456789", flag_history=flag_history)
    for v in violations:
        print(f"[{v.rule_id}] {v.severity}: {v.description}")
