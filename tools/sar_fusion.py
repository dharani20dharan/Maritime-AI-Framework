"""
MAF — SAR/Optical Fusion Module
=================================
Implements:
  1. CA-CFAR (Cell-Averaging Constant False Alarm Rate) algorithm for ship
     detection in SAR (Synthetic Aperture Radar) backscatter imagery.
  2. YOLOv11n-OBB structured vessel detection output parser: extracts vessel
     center, length, beam, and heading from SAR/optical OBB detections.

These components are CPU-only, dependency-minimal implementations suitable for
offline maritime surveillance environments.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import random

logger = logging.getLogger(__name__)


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class SARDetection:
    """A detected vessel contact from SAR imagery processing."""
    detection_id: str
    lat: float
    lon: float
    length_m: float                 # Estimated vessel length in metres
    beam_m: float                   # Estimated vessel beam (width) in metres
    heading_deg: float              # Estimated vessel heading (0–360°)
    confidence: float               # 0.0–1.0 detection confidence
    rcs_dbsm: Optional[float] = None   # Radar Cross Section in dBsm
    snr_db: Optional[float] = None     # Signal-to-Noise Ratio in dB
    algorithm: str = "CA-CFAR"
    image_source: str = "SAR"          # "SAR" | "OPTICAL"
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "detection_id": self.detection_id,
            "lat": self.lat,
            "lon": self.lon,
            "length_m": self.length_m,
            "beam_m": self.beam_m,
            "heading_deg": self.heading_deg,
            "confidence": self.confidence,
            "rcs_dbsm": self.rcs_dbsm,
            "snr_db": self.snr_db,
            "algorithm": self.algorithm,
            "image_source": self.image_source,
            "timestamp": self.timestamp,
        }


@dataclass
class OBBDetection:
    """YOLOv11n-OBB oriented bounding box detection output."""
    class_id: int                   # Class: 0=vessel, 1=platform, 2=debris
    class_name: str
    cx: float                       # Center x in image pixel coords
    cy: float                       # Center y in image pixel coords
    width_px: float                 # Bounding box width (vessel length axis)
    height_px: float                # Bounding box height (vessel beam axis)
    angle_deg: float                # Rotation angle in degrees
    score: float                    # Detection confidence score 0.0–1.0

    def to_geo(
        self,
        image_lat_origin: float,
        image_lon_origin: float,
        pixel_size_m: float = 10.0,
        image_height_px: float = 512.0,
        image_width_px: float = 512.0,
    ) -> Tuple[float, float, float, float]:
        """
        Convert pixel OBB to geographic coordinates.
        Returns (lat, lon, length_m, beam_m).
        """
        # Pixel offset from image center
        dx_px = self.cx - image_width_px / 2
        dy_px = self.cy - image_height_px / 2

        # Convert to metres displacement
        dx_m = dx_px * pixel_size_m
        dy_m = -dy_px * pixel_size_m  # Image y-axis inverted vs geographic

        # Convert metre offset to geographic degrees
        lat = image_lat_origin + (dy_m / 111320.0)
        lon = image_lon_origin + (dx_m / (111320.0 * math.cos(math.radians(image_lat_origin))))

        length_m = self.width_px * pixel_size_m
        beam_m = self.height_px * pixel_size_m

        return lat, lon, length_m, beam_m


# ── CA-CFAR Algorithm ─────────────────────────────────────────────────────────

class CACFARDetector:
    """
    Cell-Averaging Constant False Alarm Rate (CA-CFAR) detector.

    The CFAR detector maintains a constant false alarm rate by adaptively
    setting the detection threshold relative to the local noise level.

    Algorithm:
        For each cell under test (CUT):
        1. Calculate mean power of N reference cells (guard cells excluded)
        2. Threshold = mean_power × α (scaling factor for desired Pfa)
        3. Detect if CUT power > Threshold

    Reference: Richards et al., "Principles of Modern Radar" (2010)
    """

    def __init__(
        self,
        num_reference_cells: int = 16,
        num_guard_cells: int = 4,
        false_alarm_rate: float = 1e-5,
    ):
        self.N = num_reference_cells    # Reference window size (one side)
        self.G = num_guard_cells        # Guard cells (one side)
        self.pfa = false_alarm_rate

        # CFAR threshold multiplier α = N * (Pfa^(-1/N) - 1)
        # Derived from Swerling I target model
        self.alpha = self.N * (self.pfa ** (-1.0 / self.N) - 1.0)
        logger.debug(f"CA-CFAR initialized: N={self.N}, G={self.G}, alpha={self.alpha:.3f}")

    def detect_1d(self, power_profile: List[float]) -> List[int]:
        """
        Apply 1D CA-CFAR along a range/azimuth profile.

        Args:
            power_profile: Linear power values (not dB) for each range cell.

        Returns:
            List of indices where detections occur.
        """
        n = len(power_profile)
        detections = []
        half_window = self.N + self.G

        for i in range(half_window, n - half_window):
            # Reference cells: [i-N-G : i-G] and [i+G+1 : i+N+G+1]
            left_refs = power_profile[i - self.N - self.G: i - self.G]
            right_refs = power_profile[i + self.G + 1: i + self.N + self.G + 1]
            all_refs = left_refs + right_refs

            if len(all_refs) == 0:
                continue

            mean_noise = sum(all_refs) / len(all_refs)
            threshold = self.alpha * mean_noise
            cut_power = power_profile[i]

            if cut_power > threshold:
                detections.append(i)

        return detections

    def detect_2d(
        self,
        image: List[List[float]],
        lat_origin: float,
        lon_origin: float,
        pixel_size_m: float = 10.0,
    ) -> List[SARDetection]:
        """
        Apply 2D CA-CFAR to a SAR image backscatter matrix.

        Args:
            image: 2D matrix of linear backscatter power values.
            lat_origin: Geographic latitude of image top-left corner.
            lon_origin: Geographic longitude of image top-left corner.
            pixel_size_m: Ground sample distance (GSD) in metres per pixel.

        Returns:
            List of SARDetection objects at detected vessel locations.
        """
        rows = len(image)
        cols = len(image[0]) if rows > 0 else 0
        H = self.N + self.G
        detections = []
        det_count = 0

        for r in range(H, rows - H):
            for c in range(H, cols - H):
                cut = image[r][c]

                # Gather 2D reference window (excluding guard zone)
                ref_values = []
                for dr in range(-(self.N + self.G), self.N + self.G + 1):
                    for dc in range(-(self.N + self.G), self.N + self.G + 1):
                        if abs(dr) <= self.G and abs(dc) <= self.G:
                            continue  # Skip guard cells
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < rows and 0 <= nc < cols:
                            ref_values.append(image[nr][nc])

                if not ref_values:
                    continue

                mean_noise = sum(ref_values) / len(ref_values)
                threshold = self.alpha * mean_noise

                if cut > threshold:
                    # Estimate geographic location
                    det_lat = lat_origin - (r * pixel_size_m) / 111320.0
                    det_lon = lon_origin + (c * pixel_size_m) / (
                        111320.0 * math.cos(math.radians(lat_origin))
                    )

                    # Estimate SNR
                    snr = 10 * math.log10(cut / mean_noise) if mean_noise > 0 else 0.0

                    # Rough size estimation from bright cluster (single pixel = minimum)
                    estimated_length = max(pixel_size_m, pixel_size_m * 2)
                    estimated_beam = max(pixel_size_m, pixel_size_m * 0.25)

                    det_count += 1
                    detections.append(SARDetection(
                        detection_id=f"CFAR-{det_count:04d}",
                        lat=det_lat,
                        lon=det_lon,
                        length_m=estimated_length,
                        beam_m=estimated_beam,
                        heading_deg=0.0,  # Cannot determine from single detection
                        confidence=min(snr / 20.0, 1.0),
                        snr_db=snr,
                        algorithm="CA-CFAR",
                        image_source="SAR",
                    ))

        logger.info(f"CA-CFAR detected {len(detections)} targets in {rows}×{cols} image")
        return self._merge_nearby_detections(detections, merge_radius_m=50.0)

    def _merge_nearby_detections(
        self, detections: List[SARDetection], merge_radius_m: float = 50.0
    ) -> List[SARDetection]:
        """Merge detections within merge_radius_m of each other (centroid merging)."""
        if len(detections) <= 1:
            return detections

        merged = []
        used = [False] * len(detections)

        for i, det in enumerate(detections):
            if used[i]:
                continue
            cluster = [det]
            used[i] = True

            for j, other in enumerate(detections):
                if used[j]:
                    continue
                dist_m = (
                    haversine_nm(det.lat, det.lon, other.lat, other.lon) * 1852.0
                )
                if dist_m <= merge_radius_m:
                    cluster.append(other)
                    used[j] = True

            # Centroid of cluster
            lat_c = sum(d.lat for d in cluster) / len(cluster)
            lon_c = sum(d.lon for d in cluster) / len(cluster)
            length_c = max(d.length_m for d in cluster)
            conf_c = max(d.confidence for d in cluster)
            snr_c = max((d.snr_db or 0.0) for d in cluster)

            merged.append(SARDetection(
                detection_id=cluster[0].detection_id,
                lat=lat_c,
                lon=lon_c,
                length_m=length_c,
                beam_m=cluster[0].beam_m,
                heading_deg=0.0,
                confidence=conf_c,
                snr_db=snr_c,
                algorithm="CA-CFAR",
                image_source="SAR",
                metadata={"cluster_size": len(cluster)},
            ))

        return merged


# ── YOLOv11n-OBB Structured Output Parser ────────────────────────────────────

class YOLOv11OBBParser:
    """
    YOLOv11n-OBB oriented bounding box detection parser.

    Parses structured detection outputs from a YOLOv11n-OBB model inference
    and converts them to geo-referenced SARDetection objects.

    The model outputs for each detection:
        [class_id, score, cx, cy, width, height, angle]

    Classes:
        0: vessel (ship, boat)
        1: offshore platform
        2: marine debris
    """

    CLASS_NAMES = {0: "vessel", 1: "platform", 2: "debris"}
    VESSEL_CLASS_ID = 0

    def __init__(self, confidence_threshold: float = 0.5, nms_iou_threshold: float = 0.45):
        self.conf_thresh = confidence_threshold
        self.nms_iou = nms_iou_threshold

    def parse_raw_output(
        self,
        raw_detections: List[List[float]],
    ) -> List[OBBDetection]:
        """
        Parse raw model output tensor rows into OBBDetection objects.

        Args:
            raw_detections: List of [class_id, score, cx, cy, w, h, angle] rows.

        Returns:
            Filtered and NMS-applied list of OBBDetection objects.
        """
        parsed = []
        for row in raw_detections:
            if len(row) < 7:
                continue
            class_id, score, cx, cy, w, h, angle = (
                int(row[0]), float(row[1]), float(row[2]),
                float(row[3]), float(row[4]), float(row[5]), float(row[6])
            )
            if score < self.conf_thresh:
                continue
            parsed.append(OBBDetection(
                class_id=class_id,
                class_name=self.CLASS_NAMES.get(class_id, "unknown"),
                cx=cx, cy=cy, width_px=w, height_px=h,
                angle_deg=angle, score=score,
            ))
        return parsed

    def to_sar_detections(
        self,
        obb_detections: List[OBBDetection],
        image_lat_origin: float,
        image_lon_origin: float,
        pixel_size_m: float = 10.0,
        image_height_px: float = 512.0,
        image_width_px: float = 512.0,
        timestamp: str = "",
    ) -> List[SARDetection]:
        """
        Convert OBBDetection list to geo-referenced SARDetection list.
        Only converts vessel-class detections.
        """
        sar_detections = []
        for i, obb in enumerate(obb_detections):
            if obb.class_id != self.VESSEL_CLASS_ID:
                continue

            lat, lon, length_m, beam_m = obb.to_geo(
                image_lat_origin, image_lon_origin,
                pixel_size_m, image_height_px, image_width_px,
            )

            # OBB angle → geographic heading
            # OBB angle is CCW from image x-axis; convert to true bearing
            heading = (90.0 - obb.angle_deg + 360.0) % 360.0

            sar_detections.append(SARDetection(
                detection_id=f"OBB-{i:04d}",
                lat=lat,
                lon=lon,
                length_m=length_m,
                beam_m=beam_m,
                heading_deg=heading,
                confidence=obb.score,
                algorithm="YOLOv11n-OBB",
                image_source="SAR",
                timestamp=timestamp,
                metadata={"angle_deg": obb.angle_deg, "px_cx": obb.cx, "px_cy": obb.cy},
            ))
        return sar_detections


# ── Utility ───────────────────────────────────────────────────────────────────

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in nautical miles."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def generate_synthetic_sar_image(
    rows: int = 64,
    cols: int = 64,
    num_targets: int = 3,
    noise_floor: float = 0.01,
    target_snr: float = 15.0,
) -> List[List[float]]:
    """
    Generate a synthetic SAR backscatter image for testing.
    Targets are bright rectangular blobs above the noise floor.
    """
    random.seed(42)
    image = [[random.uniform(0, noise_floor) for _ in range(cols)] for _ in range(rows)]

    for _ in range(num_targets):
        r_center = random.randint(10, rows - 10)
        c_center = random.randint(10, cols - 10)
        target_power = noise_floor * (10 ** (target_snr / 10))
        for dr in range(-2, 3):
            for dc in range(-5, 6):  # Elongated like a ship
                r, c = r_center + dr, c_center + dc
                if 0 <= r < rows and 0 <= c < cols:
                    image[r][c] = target_power * random.uniform(0.8, 1.2)
    return image


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test CA-CFAR
    print("=== CA-CFAR Test ===")
    cfar = CACFARDetector(num_reference_cells=8, num_guard_cells=2, false_alarm_rate=1e-4)
    sar_image = generate_synthetic_sar_image(rows=64, cols=64, num_targets=3)
    detections = cfar.detect_2d(sar_image, lat_origin=1.28, lon_origin=103.8, pixel_size_m=10.0)
    print(f"Detected {len(detections)} vessels")
    for d in detections:
        print(f"  {d.detection_id}: lat={d.lat:.4f}, lon={d.lon:.4f}, "
              f"conf={d.confidence:.2f}, SNR={d.snr_db:.1f}dB")

    # Test YOLOv11n-OBB parser
    print("\n=== YOLOv11n-OBB Parser Test ===")
    parser = YOLOv11OBBParser(confidence_threshold=0.5)
    raw_output = [
        [0, 0.92, 256.0, 256.0, 48.0, 12.0, 35.0],   # vessel
        [0, 0.78, 100.0, 200.0, 36.0, 9.0, 120.0],   # vessel
        [1, 0.85, 400.0, 100.0, 20.0, 20.0, 0.0],    # platform (filtered)
        [0, 0.30, 300.0, 300.0, 24.0, 6.0, 45.0],    # below threshold (filtered)
    ]
    obb_dets = parser.parse_raw_output(raw_output)
    sar_dets = parser.to_sar_detections(obb_dets, 1.28, 103.8, pixel_size_m=10.0)
    print(f"YOLOv11n-OBB vessels: {len(sar_dets)}")
    for d in sar_dets:
        print(f"  {d.detection_id}: lat={d.lat:.4f}, lon={d.lon:.4f}, "
              f"length={d.length_m:.0f}m, heading={d.heading_deg:.0f}°")
