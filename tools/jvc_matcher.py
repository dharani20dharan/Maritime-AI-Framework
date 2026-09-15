"""
MAF — JVC (Jonker-Volgenant-Castañon) Bipartite Matching for Dark Vessel Detection
=====================================================================================
Implements the Jonker-Volgenant-Castañon algorithm for global linear sum assignment
(minimum-cost bipartite matching) to associate radar/SAR detections with AIS positions.

Use case:
  - Left nodes:  SAR/radar vessel detections (physical contacts)
  - Right nodes: Dead-reckoned AIS positions (expected positions)
  - Edge cost:   Spatio-temporal distance + kinematic divergence

Unmatched radar contacts → confirmed dark vessels (M-DARK-EVENT)

References:
  - Jonker, R. & Volgenant, A. (1987). "A shortest augmenting paths algorithm
    for dense and sparse linear assignment problems"
  - Castañon, D. (1993). "Efficient algorithms for finding the K best paths
    through a trellis"
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Match quality thresholds
MAX_MATCH_DISTANCE_NM = 5.0          # Maximum distance for a valid match
MAX_HEADING_DIVERGENCE_DEG = 45.0    # Maximum heading difference for match
DARK_VESSEL_CONFIDENCE_BASE = 0.75   # Base confidence for unmatched contacts


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class RadarContact:
    """A physical vessel contact from radar/SAR imagery."""
    contact_id: str
    lat: float
    lon: float
    heading_deg: Optional[float] = None
    speed_kts: Optional[float] = None
    length_m: Optional[float] = None
    timestamp: str = ""
    confidence: float = 1.0
    source: str = "SAR"

    def to_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "lat": self.lat, "lon": self.lon,
            "heading_deg": self.heading_deg,
            "speed_kts": self.speed_kts,
            "length_m": self.length_m,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "source": self.source,
        }


@dataclass
class DeadReckonedAIS:
    """AIS position extrapolated forward to match the radar image timestamp."""
    mmsi: str
    lat: float
    lon: float
    heading_deg: Optional[float] = None
    speed_kts: Optional[float] = None
    last_seen_ts: str = ""
    extrapolation_minutes: float = 0.0
    imo: Optional[str] = None
    name: Optional[str] = None
    flag: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "lat": self.lat, "lon": self.lon,
            "heading_deg": self.heading_deg,
            "speed_kts": self.speed_kts,
            "last_seen_ts": self.last_seen_ts,
            "extrapolation_minutes": self.extrapolation_minutes,
        }


@dataclass
class MatchResult:
    """A successful radar-to-AIS match."""
    contact_id: str
    mmsi: str
    distance_nm: float
    cost: float
    heading_diff_deg: Optional[float] = None
    is_confirmed: bool = True

    def to_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "mmsi": self.mmsi,
            "distance_nm": self.distance_nm,
            "cost": self.cost,
            "heading_diff_deg": self.heading_diff_deg,
            "is_confirmed": self.is_confirmed,
        }


@dataclass
class DarkVesselEvent:
    """An unmatched radar contact = dark vessel (M-DARK-EVENT)."""
    contact_id: str
    lat: float
    lon: float
    length_m: Optional[float]
    heading_deg: Optional[float]
    confidence: float
    timestamp: str
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "lat": self.lat, "lon": self.lon,
            "length_m": self.length_m,
            "heading_deg": self.heading_deg,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "description": self.description,
        }


# ── Cost Computation ──────────────────────────────────────────────────────────

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in nautical miles."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_assignment_cost(
    contact: RadarContact,
    ais: DeadReckonedAIS,
    dist_weight: float = 1.0,
    heading_weight: float = 0.3,
    extrapolation_penalty: float = 0.02,
) -> float:
    """
    Compute the assignment cost between a radar contact and an AIS position.
    Lower cost = better match.

    Cost components:
      1. Spatial distance (normalized to MAX_MATCH_DISTANCE_NM)
      2. Heading divergence (if available)
      3. Extrapolation uncertainty penalty (older AIS = less reliable)
    """
    dist = haversine_nm(contact.lat, contact.lon, ais.lat, ais.lon)
    if dist > MAX_MATCH_DISTANCE_NM:
        return float("inf")  # Infeasible assignment

    # Normalized spatial cost (0 = same location, 1 = at max distance)
    spatial_cost = dist_weight * (dist / MAX_MATCH_DISTANCE_NM)

    # Heading divergence cost
    heading_cost = 0.0
    if contact.heading_deg is not None and ais.heading_deg is not None:
        diff = abs((contact.heading_deg - ais.heading_deg + 180) % 360 - 180)
        if diff > MAX_HEADING_DIVERGENCE_DEG:
            return float("inf")  # Heading too divergent
        heading_cost = heading_weight * (diff / MAX_HEADING_DIVERGENCE_DEG)

    # Extrapolation penalty (dead reckoning error grows with time)
    extrap_cost = extrapolation_penalty * ais.extrapolation_minutes

    return spatial_cost + heading_cost + extrap_cost


# ── JVC Algorithm ─────────────────────────────────────────────────────────────

class JVCMatcher:
    """
    Jonker-Volgenant-Castañon (JVC) algorithm for bipartite matching.

    Associates radar detections with AIS positions using the shortest
    augmenting paths approach to minimize total assignment cost.

    This is a pure Python implementation of the Hungarian/JVC algorithm
    with O(n³) complexity. For large n (>500), consider scipy.optimize.linear_sum_assignment.
    """

    def __init__(
        self,
        max_match_distance_nm: float = MAX_MATCH_DISTANCE_NM,
        dark_confidence_threshold: float = DARK_VESSEL_CONFIDENCE_BASE,
    ):
        self.max_dist = max_match_distance_nm
        self.dark_conf_thresh = dark_confidence_threshold

    def _build_cost_matrix(
        self,
        contacts: List[RadarContact],
        ais_positions: List[DeadReckonedAIS],
    ) -> List[List[float]]:
        """Build the n×m cost matrix where cost[i][j] = cost(contact_i → ais_j)."""
        n = len(contacts)
        m = len(ais_positions)
        # Make square by padding with large cost
        size = max(n, m)
        INF = 1e9

        cost = [[INF] * size for _ in range(size)]
        for i, contact in enumerate(contacts):
            for j, ais in enumerate(ais_positions):
                c = compute_assignment_cost(contact, ais)
                cost[i][j] = c if c != float("inf") else INF
        return cost

    def _hungarian(self, cost: List[List[float]]) -> List[int]:
        """
        Hungarian algorithm (augmenting path variant) for minimum-cost assignment.
        Returns assignment[i] = j meaning row i is assigned to column j.
        Returns -1 for rows with no feasible assignment.
        """
        n = len(cost)
        INF = float("inf")
        u = [0.0] * (n + 1)  # Potential for rows
        v = [0.0] * (n + 1)  # Potential for columns
        p = [0] * (n + 1)    # Column assigned to row i
        way = [0] * (n + 1)  # Augmenting path

        for i in range(1, n + 1):
            p[0] = i
            j0 = 0
            minVal = [INF] * (n + 1)
            used = [False] * (n + 1)

            while True:
                used[j0] = True
                i0 = p[j0]
                delta = INF
                j1 = -1

                for j in range(1, n + 1):
                    if not used[j]:
                        cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                        if cur < minVal[j]:
                            minVal[j] = cur
                            way[j] = j0
                        if minVal[j] < delta:
                            delta = minVal[j]
                            j1 = j

                if j1 == -1:
                    break

                for j in range(n + 1):
                    if used[j]:
                        u[p[j]] += delta
                        v[j] -= delta
                    else:
                        minVal[j] -= delta

                j0 = j1
                if p[j0] == 0:
                    break

            while j0:
                p[j0] = p[way[j0]]
                j0 = way[j0]

        assignment = [-1] * n
        for j in range(1, n + 1):
            if p[j] > 0:
                assignment[p[j] - 1] = j - 1
        return assignment

    def match(
        self,
        contacts: List[RadarContact],
        ais_positions: List[DeadReckonedAIS],
    ) -> Tuple[List[MatchResult], List[DarkVesselEvent], List[str]]:
        """
        Perform JVC bipartite matching between radar contacts and AIS positions.

        Returns:
            matched:       List of successful radar→AIS matches
            dark_vessels:  List of unmatched radar contacts (dark vessels)
            unmatched_ais: List of AIS MMSIs with no corresponding radar contact
        """
        if not contacts:
            logger.warning("JVC: No radar contacts provided")
            return [], [], [a.mmsi for a in ais_positions]

        if not ais_positions:
            # All contacts are dark
            dark = [self._make_dark_event(c, "No AIS positions in coverage area") for c in contacts]
            return [], dark, []

        n_contacts = len(contacts)
        n_ais = len(ais_positions)

        cost_matrix = self._build_cost_matrix(contacts, ais_positions)
        assignment = self._hungarian(cost_matrix)

        INF_COST = 1e8
        matched: List[MatchResult] = []
        matched_ais_indices = set()
        dark_vessels: List[DarkVesselEvent] = []
        unmatched_contact_indices = set()

        for i, j in enumerate(assignment):
            if i >= n_contacts:
                continue

            contact = contacts[i]
            if j >= n_ais or cost_matrix[i][j] >= INF_COST:
                # No valid match for this contact
                unmatched_contact_indices.add(i)
                dark_vessels.append(self._make_dark_event(
                    contact,
                    "Radar contact with no matching AIS position — confirmed dark vessel"
                ))
                continue

            ais = ais_positions[j]
            dist = haversine_nm(contact.lat, contact.lon, ais.lat, ais.lon)
            heading_diff = None
            if contact.heading_deg is not None and ais.heading_deg is not None:
                heading_diff = abs((contact.heading_deg - ais.heading_deg + 180) % 360 - 180)

            matched.append(MatchResult(
                contact_id=contact.contact_id,
                mmsi=ais.mmsi,
                distance_nm=dist,
                cost=cost_matrix[i][j],
                heading_diff_deg=heading_diff,
            ))
            matched_ais_indices.add(j)

        unmatched_ais = [
            ais_positions[j].mmsi
            for j in range(n_ais)
            if j not in matched_ais_indices
        ]

        logger.info(
            f"JVC: {len(contacts)} contacts, {n_ais} AIS | "
            f"Matched: {len(matched)}, Dark: {len(dark_vessels)}, "
            f"AIS-only (radar shadow): {len(unmatched_ais)}"
        )
        return matched, dark_vessels, unmatched_ais

    def _make_dark_event(self, contact: RadarContact, reason: str) -> DarkVesselEvent:
        """Create a dark vessel event from an unmatched radar contact."""
        confidence = contact.confidence * self.dark_conf_thresh + (
            0.15 if contact.length_m and contact.length_m > 100 else 0.0
        )
        return DarkVesselEvent(
            contact_id=contact.contact_id,
            lat=contact.lat,
            lon=contact.lon,
            length_m=contact.length_m,
            heading_deg=contact.heading_deg,
            confidence=min(confidence, 0.99),
            timestamp=contact.timestamp,
            description=reason,
        )

    def dead_reckon(
        self,
        mmsi: str,
        last_lat: float,
        last_lon: float,
        last_heading: float,
        last_speed_kts: float,
        minutes_elapsed: float,
        **kwargs,
    ) -> DeadReckonedAIS:
        """
        Dead-reckon an AIS position forward by minutes_elapsed.
        Uses great-circle constant-heading propagation.
        """
        hours = minutes_elapsed / 60.0
        dist_nm = last_speed_kts * hours

        # Convert nm displacement to lat/lon delta
        lat_rad = math.radians(last_lat)
        heading_rad = math.radians(last_heading)

        dlat = (dist_nm / 3440.065) * math.cos(heading_rad)
        dlon = (dist_nm / 3440.065) * math.sin(heading_rad) / math.cos(lat_rad)

        new_lat = last_lat + math.degrees(dlat)
        new_lon = last_lon + math.degrees(dlon)

        return DeadReckonedAIS(
            mmsi=mmsi,
            lat=new_lat,
            lon=new_lon,
            heading_deg=last_heading,
            speed_kts=last_speed_kts,
            extrapolation_minutes=minutes_elapsed,
            **kwargs,
        )


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matcher = JVCMatcher()

    # Simulate 3 radar contacts, 2 AIS positions, 1 dark vessel
    contacts = [
        RadarContact("RADAR-001", lat=1.280, lon=103.800, heading_deg=90, speed_kts=12, length_m=200),
        RadarContact("RADAR-002", lat=1.350, lon=103.900, heading_deg=45, speed_kts=8, length_m=150),
        RadarContact("RADAR-003", lat=1.100, lon=103.600, heading_deg=180, speed_kts=6, length_m=80),
    ]

    ais_positions = [
        DeadReckonedAIS("123456789", lat=1.281, lon=103.801, heading_deg=92,
                        speed_kts=12, extrapolation_minutes=15, name="VESSEL ALPHA"),
        DeadReckonedAIS("987654321", lat=1.351, lon=103.901, heading_deg=44,
                        speed_kts=8, extrapolation_minutes=30, name="VESSEL BETA"),
    ]

    matched, dark, unmatched_ais = matcher.match(contacts, ais_positions)

    print(f"\n=== JVC Matching Results ===")
    print(f"Matched pairs: {len(matched)}")
    for m in matched:
        print(f"  {m.contact_id} → MMSI {m.mmsi}: dist={m.distance_nm:.3f}NM, cost={m.cost:.4f}")
    print(f"\nDark vessels (unmatched): {len(dark)}")
    for d in dark:
        print(f"  {d.contact_id}: lat={d.lat:.3f}, lon={d.lon:.3f}, conf={d.confidence:.2f}")
        print(f"    Reason: {d.description}")
    print(f"\nAIS without radar echo: {unmatched_ais}")
