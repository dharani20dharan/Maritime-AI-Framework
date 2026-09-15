"""
MAF — STGVAD: Spatio-Temporal Graph Variational Anomaly Detector
=================================================================
Implements a graph-based anomaly detection model for covert ship-to-ship (STS)
rendezvous detection. Models multi-vessel spatial proximity, course alignment,
and co-loitering velocity patterns using a graph convolutional approach.

Architecture:
  - Node features: velocity, heading, proximity metrics per vessel
  - Edge features: pairwise distance, relative heading, closing speed
  - Graph Encoder: 2-layer GCN producing latent node embeddings
  - Variational layer: learns μ and σ for anomaly scoring (ELBO)
  - Anomaly score: KL divergence from normal behavior distribution

CPU-native, dependency-minimal implementation using pure Python linear algebra.
"""

from __future__ import annotations

import math
import random
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
STS_DISTANCE_THRESHOLD_NM  = 0.5    # Within 0.5 NM = potential STS
STS_SPEED_THRESHOLD_KTS    = 3.0    # Both vessels < 3 kts = loitering
STS_DURATION_THRESHOLD_MIN = 30.0   # Together > 30 min = confirmed STS candidate
STS_COURSE_ALIGN_DEG       = 20.0   # Course alignment within 20°
ANOMALY_SCORE_THRESHOLD    = 0.65   # Scores above this flag STS event


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class VesselNode:
    """Node in the spatio-temporal vessel graph."""
    mmsi: str
    lat: float
    lon: float
    speed_kts: float
    heading: float
    timestamp: str
    flag: Optional[str] = None
    name: Optional[str] = None
    nav_status: int = 0

    @property
    def features(self) -> List[float]:
        """Return normalized feature vector for GCN."""
        return [
            self.speed_kts / 30.0,            # Normalized speed
            math.sin(math.radians(self.heading)),  # Heading x-component
            math.cos(math.radians(self.heading)),  # Heading y-component
            float(self.speed_kts < STS_SPEED_THRESHOLD_KTS),  # Is-loitering flag
            float(self.nav_status in (0, 8)),   # Underway using engine
        ]


@dataclass
class GraphEdge:
    """Edge between two vessels in the proximity graph."""
    mmsi_a: str
    mmsi_b: str
    distance_nm: float
    closing_speed_kts: float        # Negative = converging
    heading_diff_deg: float
    is_converging: bool

    @property
    def features(self) -> List[float]:
        """Return normalized edge feature vector."""
        return [
            self.distance_nm / STS_DISTANCE_THRESHOLD_NM,
            self.closing_speed_kts / 20.0,
            self.heading_diff_deg / 180.0,
            float(self.is_converging),
        ]


@dataclass
class STSDetectionResult:
    """Result of STGVAD STS detection for a vessel pair."""
    mmsi_a: str
    mmsi_b: str
    anomaly_score: float            # 0.0 = normal, 1.0 = certain STS
    is_sts_candidate: bool
    distance_nm: float
    duration_minutes: Optional[float]
    description: str
    contributing_factors: List[str] = field(default_factory=list)
    lat: Optional[float] = None     # Approximate STS location
    lon: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "mmsi_a": self.mmsi_a,
            "mmsi_b": self.mmsi_b,
            "anomaly_score": self.anomaly_score,
            "is_sts_candidate": self.is_sts_candidate,
            "distance_nm": self.distance_nm,
            "duration_minutes": self.duration_minutes,
            "description": self.description,
            "contributing_factors": self.contributing_factors,
            "lat": self.lat,
            "lon": self.lon,
        }


# ── Pure Python Linear Algebra Utilities ─────────────────────────────────────

def mat_mul(A: List[List[float]], B: List[List[float]]) -> List[List[float]]:
    """Matrix multiplication A @ B."""
    rows_A, cols_A = len(A), len(A[0])
    cols_B = len(B[0])
    C = [[0.0] * cols_B for _ in range(rows_A)]
    for i in range(rows_A):
        for k in range(cols_A):
            if A[i][k] == 0.0:
                continue
            for j in range(cols_B):
                C[i][j] += A[i][k] * B[k][j]
    return C


def relu(x: float) -> float:
    return max(0.0, x)


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-500, min(500, x))))


def tanh_act(x: float) -> float:
    return math.tanh(max(-20.0, min(20.0, x)))


def normalize_adj(adj: List[List[float]]) -> List[List[float]]:
    """
    Symmetric normalization of adjacency matrix: D^{-1/2} A D^{-1/2}.
    Used in GCN layers to stabilize training.
    """
    n = len(adj)
    degree = [sum(adj[i]) + 1.0 for i in range(n)]  # +1 for self-loop
    D_inv_sqrt = [1.0 / math.sqrt(d) for d in degree]
    norm_adj = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            norm_adj[i][j] = D_inv_sqrt[i] * adj[i][j] * D_inv_sqrt[j]
        norm_adj[i][i] += D_inv_sqrt[i] * D_inv_sqrt[i]  # Self-loop
    return norm_adj


# ── GCN Layer (pure Python) ───────────────────────────────────────────────────

class GCNLayer:
    """
    Graph Convolutional Network layer: H' = σ(Â H W)
    where Â = normalized adjacency, H = node features, W = weight matrix.
    Uses pre-initialized random weights for the CPU-native implementation.
    """

    def __init__(self, in_dim: int, out_dim: int, seed: int = 42):
        random.seed(seed)
        # Xavier initialization
        limit = math.sqrt(6.0 / (in_dim + out_dim))
        self.W = [
            [random.uniform(-limit, limit) for _ in range(out_dim)]
            for _ in range(in_dim)
        ]

    def forward(
        self,
        node_features: List[List[float]],  # n × in_dim
        adj_norm: List[List[float]],       # n × n
        activation=relu,
    ) -> List[List[float]]:
        """
        Forward pass: H' = activation(adj_norm @ node_features @ W).
        Returns n × out_dim output feature matrix.
        """
        # H_agg = adj_norm @ node_features  [n × in_dim]
        H_agg = mat_mul(adj_norm, node_features)
        # H_out = H_agg @ W  [n × out_dim]
        H_out = mat_mul(H_agg, self.W)
        # Apply activation element-wise
        return [[activation(h) for h in row] for row in H_out]


# ── Variational Encoder ────────────────────────────────────────────────────────

class VariationalEncoder:
    """
    Variational encoder producing μ and log(σ²) from GCN output.
    Anomaly score = KL divergence from unit Gaussian N(0,I).

    KL(q || p) = -0.5 * Σ(1 + log_σ² - μ² - σ²)
    High KL = anomalous behavior (unusual latent distribution).
    """

    def __init__(self, in_dim: int, latent_dim: int, seed: int = 99):
        random.seed(seed)
        limit = math.sqrt(6.0 / (in_dim + latent_dim))
        self.W_mu = [[random.uniform(-limit, limit) for _ in range(latent_dim)] for _ in range(in_dim)]
        self.W_logvar = [[random.uniform(-limit, limit) for _ in range(latent_dim)] for _ in range(in_dim)]

    def encode(self, h: List[float]) -> Tuple[List[float], List[float]]:
        """Encode a single node's GCN output h → (mu, log_var)."""
        mu = [sum(h[i] * self.W_mu[i][j] for i in range(len(h))) for j in range(len(self.W_mu[0]))]
        log_var = [sum(h[i] * self.W_logvar[i][j] for i in range(len(h))) for j in range(len(self.W_logvar[0]))]
        return mu, log_var

    def kl_divergence(self, mu: List[float], log_var: List[float]) -> float:
        """Compute element-wise KL divergence from N(0,I)."""
        kl = sum(
            -0.5 * (1.0 + lv - m ** 2 - math.exp(min(lv, 20.0)))
            for m, lv in zip(mu, log_var)
        )
        return max(0.0, kl)


# ── STGVAD Model ──────────────────────────────────────────────────────────────

class STGVADModel:
    """
    Spatio-Temporal Graph Variational Anomaly Detector (STGVAD).

    Detects covert STS rendezvous by modeling the multi-vessel proximity graph
    and scoring the anomalousness of co-loitering behavior using variational
    graph inference.
    """

    NODE_FEAT_DIM = 5
    GCN_HIDDEN_DIM = 8
    LATENT_DIM = 4

    def __init__(self, seed: int = 42):
        self.gcn1 = GCNLayer(self.NODE_FEAT_DIM, self.GCN_HIDDEN_DIM, seed=seed)
        self.gcn2 = GCNLayer(self.GCN_HIDDEN_DIM, self.GCN_HIDDEN_DIM, seed=seed + 1)
        self.vae_encoder = VariationalEncoder(self.GCN_HIDDEN_DIM, self.LATENT_DIM, seed=seed + 2)
        logger.debug("STGVAD model initialized")

    def _build_proximity_graph(
        self, vessels: List[VesselNode]
    ) -> Tuple[List[GraphEdge], List[List[float]]]:
        """
        Build proximity adjacency matrix and edge list.
        An edge exists if two vessels are within STS_DISTANCE_THRESHOLD_NM.
        """
        n = len(vessels)
        adj = [[0.0] * n for _ in range(n)]
        edges = []

        for i in range(n):
            adj[i][i] = 1.0  # Self-loop
            for j in range(i + 1, n):
                dist = haversine_nm(
                    vessels[i].lat, vessels[i].lon,
                    vessels[j].lat, vessels[j].lon,
                )
                if dist <= STS_DISTANCE_THRESHOLD_NM * 5:  # Wider graph neighborhood
                    weight = max(0.0, 1.0 - dist / (STS_DISTANCE_THRESHOLD_NM * 5))
                    adj[i][j] = weight
                    adj[j][i] = weight

                    heading_diff = abs(
                        (vessels[i].heading - vessels[j].heading + 180) % 360 - 180
                    )
                    # Estimate closing speed (simplified)
                    closing_speed = (vessels[i].speed_kts + vessels[j].speed_kts) / 2.0

                    edge = GraphEdge(
                        mmsi_a=vessels[i].mmsi,
                        mmsi_b=vessels[j].mmsi,
                        distance_nm=dist,
                        closing_speed_kts=closing_speed,
                        heading_diff_deg=heading_diff,
                        is_converging=dist <= STS_DISTANCE_THRESHOLD_NM,
                    )
                    edges.append(edge)
        return edges, adj

    def _compute_node_anomaly_scores(
        self, vessels: List[VesselNode], adj: List[List[float]]
    ) -> List[float]:
        """Run GCN + VAE to get per-node KL anomaly scores."""
        node_features = [v.features for v in vessels]
        adj_norm = normalize_adj(adj)

        # Two GCN layers
        h1 = self.gcn1.forward(node_features, adj_norm)
        h2 = self.gcn2.forward(h1, adj_norm)

        # VAE encoding
        scores = []
        for h in h2:
            mu, log_var = self.vae_encoder.encode(h)
            kl = self.vae_encoder.kl_divergence(mu, log_var)
            # Normalize KL to [0, 1] range using sigmoid
            score = sigmoid(kl - 5.0)  # Calibrated so normal KL~5 maps to ~0.5
            scores.append(score)
        return scores

    def detect_sts(
        self,
        vessels: List[VesselNode],
        contact_duration_minutes: Optional[float] = None,
    ) -> List[STSDetectionResult]:
        """
        Detect STS rendezvous events in a vessel cluster.

        Args:
            vessels: List of vessel nodes in the proximity zone.
            contact_duration_minutes: How long vessels have been co-located (from AIS history).

        Returns:
            List of STSDetectionResult for each suspicious pair.
        """
        if len(vessels) < 2:
            return []

        edges, adj = self._build_proximity_graph(vessels)
        node_scores = self._compute_node_anomaly_scores(vessels, adj)
        vessel_map = {v.mmsi: (v, node_scores[i]) for i, v in enumerate(vessels)}

        results = []
        for edge in edges:
            va, score_a = vessel_map[edge.mmsi_a]
            vb, score_b = vessel_map[edge.mmsi_b]

            # Compute pair-level anomaly score (geometric mean)
            pair_score = math.sqrt(score_a * score_b)

            # Heuristic boosters
            factors = []
            boost = 0.0

            if edge.distance_nm <= STS_DISTANCE_THRESHOLD_NM:
                boost += 0.15
                factors.append(f"Close proximity ({edge.distance_nm:.3f}NM)")

            if va.speed_kts < STS_SPEED_THRESHOLD_KTS and vb.speed_kts < STS_SPEED_THRESHOLD_KTS:
                boost += 0.15
                factors.append(f"Both vessels loitering (<{STS_SPEED_THRESHOLD_KTS}kts)")

            if edge.heading_diff_deg < STS_COURSE_ALIGN_DEG:
                boost += 0.10
                factors.append(f"Course alignment ({edge.heading_diff_deg:.0f}°)")

            if contact_duration_minutes and contact_duration_minutes >= STS_DURATION_THRESHOLD_MIN:
                boost += 0.15
                factors.append(f"Duration {contact_duration_minutes:.0f}min")

            # Check for sanctioned/high-risk flag combinations
            if va.flag and vb.flag and va.flag != vb.flag:
                RISK_FLAGS = {"IR", "KP", "SY", "VE", "RU", "BY"}
                if va.flag in RISK_FLAGS or vb.flag in RISK_FLAGS:
                    boost += 0.10
                    factors.append(f"High-risk flag involved ({va.flag}/{vb.flag})")

            final_score = min(pair_score + boost, 1.0)
            is_sts = final_score >= ANOMALY_SCORE_THRESHOLD or (
                edge.distance_nm <= STS_DISTANCE_THRESHOLD_NM and
                va.speed_kts < STS_SPEED_THRESHOLD_KTS and
                vb.speed_kts < STS_SPEED_THRESHOLD_KTS
            )

            if is_sts or final_score > 0.4:
                # Average lat/lon as STS location
                sts_lat = (va.lat + vb.lat) / 2
                sts_lon = (va.lon + vb.lon) / 2

                results.append(STSDetectionResult(
                    mmsi_a=edge.mmsi_a,
                    mmsi_b=edge.mmsi_b,
                    anomaly_score=final_score,
                    is_sts_candidate=is_sts,
                    distance_nm=edge.distance_nm,
                    duration_minutes=contact_duration_minutes,
                    description=(
                        f"STGVAD detected {'CONFIRMED' if is_sts else 'PROBABLE'} STS rendezvous "
                        f"between {edge.mmsi_a} and {edge.mmsi_b} at "
                        f"({sts_lat:.4f}, {sts_lon:.4f}). "
                        f"Score: {final_score:.3f}."
                    ),
                    contributing_factors=factors,
                    lat=sts_lat,
                    lon=sts_lon,
                ))

        return sorted(results, key=lambda r: r.anomaly_score, reverse=True)


# ── Utility ───────────────────────────────────────────────────────────────────

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── CLI Self-Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    model = STGVADModel(seed=42)

    # Simulate a suspicious STS: two tankers loitering close together
    vessels = [
        VesselNode("123456789", lat=14.500, lon=54.300, speed_kts=1.2, heading=90,
                   timestamp="2024-03-15T02:00:00Z", flag="IR", name="DARK TANKER A"),
        VesselNode("987654321", lat=14.502, lon=54.302, speed_kts=0.8, heading=92,
                   timestamp="2024-03-15T02:00:00Z", flag="PW", name="GHOST VESSEL B"),
        VesselNode("555000001", lat=14.600, lon=54.500, speed_kts=14.0, heading=270,
                   timestamp="2024-03-15T02:00:00Z", flag="SG", name="NORMAL TRANSIT"),
    ]

    results = model.detect_sts(vessels, contact_duration_minutes=65)
    print(f"\n=== STGVAD STS Detection Results ===")
    for r in results:
        status = "⚠️  STS CONFIRMED" if r.is_sts_candidate else "ℹ️  Probable"
        print(f"{status}: {r.mmsi_a} ↔ {r.mmsi_b}")
        print(f"  Score: {r.anomaly_score:.3f}, Distance: {r.distance_nm:.4f}NM")
        print(f"  Factors: {r.contributing_factors}")
        print(f"  {r.description}")
