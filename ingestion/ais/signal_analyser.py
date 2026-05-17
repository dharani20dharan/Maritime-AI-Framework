"""
MAF — Signal Analyser (Engineer A, Stage 1)
Consumes ais.validated, applies:
  - M-AIS-BEACON  : CV < 0.01 on per-MMSI ping intervals
  - M-SPEED-ANOMALY: SOG exceeds vessel-class maximum
Publishes anomaly events to ais.anomalies
"""

import json
import logging
import os
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone

from confluent_kafka import Consumer, Producer, KafkaError

log = logging.getLogger("signal-analyser")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
INPUT_TOPIC       = os.getenv("KAFKA_INPUT_TOPIC", "ais.validated")
OUTPUT_TOPIC      = os.getenv("KAFKA_OUTPUT_TOPIC", "ais.anomalies")
CV_THRESHOLD      = float(os.getenv("CV_BEACON_THRESHOLD", "0.01"))
SPEED_MAX_KNOTS   = float(os.getenv("SPEED_MAX_KNOTS", "50"))
WINDOW_SIZE       = int(os.getenv("CV_WINDOW_SIZE", "20"))    # pings to accumulate before scoring

# Per-vessel state: sliding window of ping timestamps (epoch seconds)
ping_windows: dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW_SIZE))


# ── TECHNIQUE DETECTORS ───────────────────────────────────────────────────────

def detect_beacon_pattern(mmsi: str, timestamp_iso: str) -> dict | None:
    """
    M-AIS-BEACON
    If the Coefficient of Variation of inter-ping intervals for this MMSI
    falls below CV_THRESHOLD, the ping cadence is unnaturally regular —
    characteristic of script-generated position data.
    CV = stdev / mean. CV < 0.01 → flag.
    """
    try:
        ts = datetime.fromisoformat(timestamp_iso).timestamp()
    except (ValueError, TypeError):
        return None

    window = ping_windows[mmsi]
    window.append(ts)

    if len(window) < WINDOW_SIZE:
        return None   # not enough data yet

    intervals = [window[i+1] - window[i] for i in range(len(window) - 1)]
    if len(intervals) < 2:
        return None

    mean_interval = statistics.mean(intervals)
    if mean_interval == 0:
        return None

    stdev = statistics.stdev(intervals)
    cv = stdev / mean_interval

    if cv < CV_THRESHOLD:
        return {
            "technique": "M-AIS-BEACON",
            "mmsi": mmsi,
            "cv": round(cv, 6),
            "mean_interval_s": round(mean_interval, 2),
            "window_size": WINDOW_SIZE,
            "confidence": round(1.0 - (cv / CV_THRESHOLD), 4),
            "detail": f"CV={cv:.6f} < threshold={CV_THRESHOLD} — automated ping pattern detected",
        }
    return None


def detect_speed_anomaly(envelope: dict) -> dict | None:
    """
    M-SPEED-ANOMALY
    SOG exceeds the configurable maximum (default 50 kts).
    For production, this should be parameterised per vessel type.
    """
    speed = envelope.get("speed_kts")
    if speed is None:
        return None
    if speed > SPEED_MAX_KNOTS:
        return {
            "technique": "M-SPEED-ANOMALY",
            "mmsi": envelope["mmsi"],
            "speed_kts": speed,
            "threshold_kts": SPEED_MAX_KNOTS,
            "confidence": min(1.0, round((speed - SPEED_MAX_KNOTS) / SPEED_MAX_KNOTS, 4)),
            "detail": f"SOG={speed}kts exceeds maximum {SPEED_MAX_KNOTS}kts",
        }
    return None


# ── ANOMALY ENVELOPE ─────────────────────────────────────────────────────────
def build_anomaly_event(detection: dict, source_envelope: dict) -> dict:
    return {
        "event_type": "ANOMALY_DETECTION",
        "technique": detection["technique"],
        "mmsi": detection["mmsi"],
        "imo": source_envelope.get("imo"),
        "vessel_name": source_envelope.get("name"),
        "lat": source_envelope.get("lat"),
        "lon": source_envelope.get("lon"),
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "confidence": detection.get("confidence", 1.0),
        "detail": detection.get("detail", ""),
        "evidence": detection,
        "stage": 1,
    }


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def run():
    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "maf-signal-analyser",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    })
    producer = Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "acks": "all",
    })

    consumer.subscribe([INPUT_TOPIC])
    log.info("Signal analyser running — consuming %s → publishing anomalies to %s", INPUT_TOPIC, OUTPUT_TOPIC)

    anomaly_count = 0
    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka error: %s", msg.error())
                continue

            try:
                envelope = json.loads(msg.value().decode())
            except json.JSONDecodeError:
                continue

            mmsi = envelope.get("mmsi")
            if not mmsi:
                continue

            detections = []

            # Run all Stage-1 signal detectors
            beacon = detect_beacon_pattern(mmsi, envelope.get("timestamp", ""))
            if beacon:
                detections.append(beacon)

            speed = detect_speed_anomaly(envelope)
            if speed:
                detections.append(speed)

            for detection in detections:
                event = build_anomaly_event(detection, envelope)
                producer.produce(
                    OUTPUT_TOPIC,
                    key=mmsi.encode(),
                    value=json.dumps(event).encode(),
                )
                anomaly_count += 1
                log.info("Anomaly: %s | mmsi=%s | confidence=%.3f",
                         detection["technique"], mmsi, detection.get("confidence", 1.0))

            producer.poll(0)

    except KeyboardInterrupt:
        log.info("Shutting down — %d anomalies detected", anomaly_count)
    finally:
        consumer.close()
        producer.flush()


if __name__ == "__main__":
    run()
