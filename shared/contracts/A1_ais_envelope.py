"""
MAF — Contract A1 : AIS Message Envelope
=========================================
This is the canonical message schema for all AIS data published to Kafka.
Both engineers reference this file.

  Engineer A  → produces messages conforming to this schema (ais.validated topic)
  Engineer B  → consumes messages from ais.validated; ETL writes Vessel nodes to Neo4j

Any change to this schema must be agreed by both engineers before merging.
Version this file if breaking changes are introduced.

Schema version: 1.0
"""

from dataclasses import dataclass, field
from typing import Literal


SOURCE_TYPE = Literal["AIS_LIVE", "SAR_FUSED", "REGISTRY"]


@dataclass
class AISEnvelope:
    """
    Contract A1 — mandatory fields for all messages on ais.validated.

    position fields (lat, lon, speed_kts, heading) may be None
    for static data messages (MessageType = ShipStaticData).
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    mmsi: str               # 9-digit string, always present
    imo:  str | None        # 7-digit string, or None if not broadcast

    # ── Position (None for static-only messages) ──────────────────────────────
    lat:       float | None  # WGS84 decimal degrees, -90 to +90
    lon:       float | None  # WGS84 decimal degrees, -180 to +180
    speed_kts: float | None  # Speed Over Ground in knots
    heading:   int   | None  # True heading 0–359; 511 = not available

    # ── Timing ───────────────────────────────────────────────────────────────
    timestamp: str           # ISO 8601 UTC, e.g. "2024-03-15T10:22:01Z"

    # ── Source ───────────────────────────────────────────────────────────────
    source: SOURCE_TYPE      # "AIS_LIVE" | "SAR_FUSED" | "REGISTRY"

    # ── Optional static fields ────────────────────────────────────────────────
    name:        str | None = None
    flag:        str | None = None   # ISO 3166-1 alpha-2
    vessel_type: int | None = None   # AIS type code
    draught_m:   float | None = None # Draught in metres
    destination: str | None = None
    nav_status:  int | None = None   # AIS navigational status 0–15
    dim_a:       int | None = None   # Dimension A (metres, bow to GPS)
    dim_b:       int | None = None   # Dimension B (metres, GPS to stern)

    # ── Internal ─────────────────────────────────────────────────────────────
    _raw_type: str | None = None     # original AISStream message type

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


# ── VALIDATION RULES (reference) ─────────────────────────────────────────────
VALIDATION_RULES = {
    "mmsi_length":        "Must be exactly 9 characters",
    "lat_range":          "-90.0 to +90.0",
    "lon_range":          "-180.0 to +180.0",
    "speed_physical_max": "102.2 knots (AIS spec max)",
    "timestamp_format":   "ISO 8601 UTC",
    "source_enum":        ["AIS_LIVE", "SAR_FUSED", "REGISTRY"],
}


# ── ANOMALY DETECTION THRESHOLDS (configurable at service level) ──────────────
DEFAULT_THRESHOLDS = {
    "M_AIS_BEACON_CV":    0.01,   # CV below this = automated beacon
    "M_SPEED_ANOMALY_KTS":50.0,   # SOG above this = impossible speed
    "M_LOITER_HOURS":     6.0,    # stationary beyond this = loitering
    "M_DARK_EVENT_HOURS": 2.0,    # silence beyond this in busy corridor = dark event
}
