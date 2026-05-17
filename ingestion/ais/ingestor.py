"""
MAF — AIS Ingestor (Engineer A, Stage 1)
Connects to AISStream.io websocket OR replays a local NDJSON file.
Publishes every message to Kafka topic: ais.raw
A separate validation pass then publishes to: ais.validated
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets
from confluent_kafka import Producer, KafkaException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
log = logging.getLogger("ais-ingestor")

# ── CONFIG ────────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
TOPIC_RAW         = os.getenv("KAFKA_TOPIC_RAW", "ais.raw")
TOPIC_VALIDATED   = os.getenv("KAFKA_TOPIC_VALIDATED", "ais.validated")
AISSTREAM_API_KEY = os.getenv("AISSTREAM_API_KEY", "")
REPLAY_MODE       = os.getenv("REPLAY_MODE", "true").lower() == "true"
REPLAY_FILE       = os.getenv("REPLAY_FILE", "/data/sample_ais.ndjson")
AISSTREAM_WS_URL  = "wss://stream.aisstream.io/v0/stream"


# ── KAFKA PRODUCER ────────────────────────────────────────────────────────────
def make_producer() -> Producer:
    return Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "acks": "all",
        "retries": 5,
        "retry.backoff.ms": 500,
    })

def delivery_report(err, msg):
    if err:
        log.error("Delivery failed: %s", err)


# ── CONTRACT A1 — message envelope ───────────────────────────────────────────
# Every message published to Kafka must conform to this schema.
# Reference: shared/contracts/A1_ais_envelope.py
def build_envelope(raw: dict, source: str = "AIS_LIVE") -> dict | None:
    """
    Normalise an AISStream message into the Contract A1 envelope.
    Returns None if the message cannot be normalised (e.g. unknown type).
    """
    msg_type = raw.get("MessageType", "")
    meta      = raw.get("MetaData", {})
    msg       = raw.get("Message", {})

    if msg_type == "PositionReport":
        pos = msg.get("PositionReport", {})
        return {
            "mmsi":      str(meta.get("MMSI", "")),
            "imo":       str(meta.get("IMO")) if meta.get("IMO") else None,
            "name":      meta.get("ShipName", "").strip(),
            "timestamp": meta.get("time_utc") or datetime.now(timezone.utc).isoformat(),
            "lat":       float(pos.get("Latitude", 0)),
            "lon":       float(pos.get("Longitude", 0)),
            "speed_kts": float(pos.get("Sog", 0)),        # Speed Over Ground
            "heading":   int(pos.get("TrueHeading", 511)), # 511 = not available
            "nav_status":int(pos.get("NavigationalStatus", 0)),
            "source":    source,
            "_raw_type": msg_type,
        }

    if msg_type == "ShipStaticData":
        static = msg.get("ShipStaticData", {})
        return {
            "mmsi":       str(meta.get("MMSI", "")),
            "imo":        str(static.get("ImoNumber")) if static.get("ImoNumber") else None,
            "name":       static.get("Name", "").strip(),
            "timestamp":  meta.get("time_utc") or datetime.now(timezone.utc).isoformat(),
            "lat":        None,
            "lon":        None,
            "speed_kts":  None,
            "heading":    None,
            "nav_status": None,
            "flag":       static.get("CountryCode", ""),
            "vessel_type":static.get("Type", 0),
            "draught_m":  float(static.get("MaximumStaticDraught", 0)) / 10.0,
            "destination":static.get("Destination", "").strip(),
            "dim_a": static.get("Dimension", {}).get("A", 0),
            "dim_b": static.get("Dimension", {}).get("B", 0),
            "source": source,
            "_raw_type": msg_type,
        }

    return None   # unsupported message type — silently drop


# ── VALIDATION ────────────────────────────────────────────────────────────────
def validate(envelope: dict) -> tuple[bool, str]:
    """Light schema validation. Returns (is_valid, reason)."""
    if not envelope.get("mmsi") or len(envelope["mmsi"]) != 9:
        return False, f"invalid_mmsi:{envelope.get('mmsi')}"
    if envelope.get("lat") is not None:
        if not (-90 <= envelope["lat"] <= 90):
            return False, f"lat_out_of_range:{envelope['lat']}"
        if not (-180 <= envelope["lon"] <= 180):
            return False, f"lon_out_of_range:{envelope['lon']}"
        if envelope.get("speed_kts", 0) > 102.2:   # AIS physical max
            return False, f"speed_impossible:{envelope['speed_kts']}"
    return True, "ok"


# ── LIVE FEED ────────────────────────────────────────────────────────────────
async def consume_live(producer: Producer):
    if not AISSTREAM_API_KEY:
        raise ValueError("AISSTREAM_API_KEY is not set. Use REPLAY_MODE=true or set the key.")

    subscription = {
        "APIKey": AISSTREAM_API_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],  # global
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    log.info("Connecting to AISStream.io ...")
    async with websockets.connect(AISSTREAM_WS_URL) as ws:
        await ws.send(json.dumps(subscription))
        log.info("Subscribed — receiving global AIS feed")
        async for raw_msg in ws:
            raw = json.loads(raw_msg)
            envelope = build_envelope(raw, source="AIS_LIVE")
            if envelope is None:
                continue
            _publish(producer, envelope)


# ── REPLAY MODE ───────────────────────────────────────────────────────────────
async def consume_replay(producer: Producer):
    path = Path(REPLAY_FILE)
    if not path.exists():
        log.warning("Replay file not found: %s — generating synthetic data", REPLAY_FILE)
        _generate_synthetic_replay(path)

    log.info("Replaying AIS data from %s", path)
    with path.open() as f:
        lines = f.readlines()

    for line in lines:
        try:
            raw = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        envelope = build_envelope(raw, source="AIS_LIVE")
        if envelope:
            _publish(producer, envelope)
        await asyncio.sleep(0.05)   # ~20 msgs/sec replay rate

    log.info("Replay complete — %d messages processed", len(lines))


# ── SYNTHETIC DATA GENERATOR (stub until real data is available) ──────────────
def _generate_synthetic_replay(path: Path):
    """
    Generates a minimal synthetic AIS replay file for smoke-testing.
    50 vessels, 100 position reports each, seeded for reproducibility.
    Replace with real NDJSON data from marinecadastre.gov or similar.
    """
    import random
    random.seed(42)
    path.parent.mkdir(parents=True, exist_ok=True)
    vessels = [
        {"mmsi": f"2369{str(i).zfill(5)}", "imo": f"900{str(i).zfill(4)}",
         "name": f"VESSEL_{i:03d}", "lat": random.uniform(-60, 60), "lon": random.uniform(-180, 180)}
        for i in range(50)
    ]
    with path.open("w") as f:
        for vessel in vessels:
            for t in range(100):
                msg = {
                    "MessageType": "PositionReport",
                    "MetaData": {
                        "MMSI": vessel["mmsi"],
                        "IMO":  vessel["imo"],
                        "ShipName": vessel["name"],
                        "time_utc": datetime.now(timezone.utc).isoformat(),
                    },
                    "Message": {
                        "PositionReport": {
                            "Latitude":  vessel["lat"] + random.gauss(0, 0.01),
                            "Longitude": vessel["lon"] + random.gauss(0, 0.01),
                            "Sog":       round(random.uniform(0, 18), 1),
                            "TrueHeading": random.randint(0, 359),
                            "NavigationalStatus": 0,
                        }
                    }
                }
                f.write(json.dumps(msg) + "\n")
    log.info("Generated synthetic replay: %d messages at %s", 50 * 100, path)


# ── KAFKA PUBLISH ─────────────────────────────────────────────────────────────
def _publish(producer: Producer, envelope: dict):
    is_valid, reason = validate(envelope)

    # Always publish raw
    producer.produce(
        TOPIC_RAW,
        key=envelope["mmsi"].encode(),
        value=json.dumps(envelope).encode(),
        callback=delivery_report,
    )

    if is_valid:
        producer.produce(
            TOPIC_VALIDATED,
            key=envelope["mmsi"].encode(),
            value=json.dumps(envelope).encode(),
            callback=delivery_report,
        )
    else:
        log.debug("Dropped invalid message — %s — mmsi=%s", reason, envelope["mmsi"])

    producer.poll(0)


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────
async def main():
    producer = make_producer()
    log.info("AIS Ingestor starting — kafka=%s replay=%s", KAFKA_BOOTSTRAP, REPLAY_MODE)
    try:
        if REPLAY_MODE:
            await consume_replay(producer)
        else:
            await consume_live(producer)
    finally:
        producer.flush()
        log.info("Producer flushed — exiting")

if __name__ == "__main__":
    asyncio.run(main())
