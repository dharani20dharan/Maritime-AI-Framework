"""
MAF — EEZ Loader (Engineer A, Stage 1, one-shot)
Reads the Marine Regions World EEZ v12 Shapefile and writes
EEZZone nodes into Neo4j for zone-entry detection (M-EEZ-VIOLATION).

Download: https://www.marineregions.org/downloads.php
Format: Shapefile (World EEZ v12)
The downloaded zip will contain: eez_v12.shp, eez_v12.dbf, eez_v12.prj, eez_v12.shx
Unzip it into: maf/ingestion/eez/data/
Run once: docker compose run --rm eez-loader
"""

import glob
import logging
import os

from neo4j import GraphDatabase

log = logging.getLogger("eez-loader")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

EEZ_DIR      = os.getenv("EEZ_DIR", "/data")
NEO4J_URI    = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER   = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS   = os.getenv("NEO4J_PASSWORD", "maf_neo4j_2024")

UPSERT_EEZ = """
MERGE (e:EEZZone {zone_id: $zone_id})
SET e.country_iso  = $country_iso,
    e.zone_name    = $zone_name,
    e.mrgid        = $mrgid,
    e.sovereign    = $sovereign,
    e.geometry_wkt = $geometry_wkt
"""


def find_shapefile(directory: str) -> str | None:
    """Find the .shp file anywhere inside the given directory."""
    matches = glob.glob(os.path.join(directory, "**", "*.shp"), recursive=True)
    if not matches:
        return None
    # Prefer the main EEZ file over any auxiliary ones
    for m in matches:
        if "eez" in os.path.basename(m).lower():
            return m
    return matches[0]


def run():
    import shapefile          # pyshp — no GDAL dependency needed
    from shapely.geometry import shape

    shp_path = find_shapefile(EEZ_DIR)

    if not shp_path:
        log.error(
            "No .shp file found in %s\n"
            "  1. Download World EEZ v12 Shapefile from:\n"
            "     https://www.marineregions.org/downloads.php\n"
            "  2. Unzip the downloaded file into:\n"
            "     maf/ingestion/eez/data/\n"
            "  3. Re-run: docker compose run --rm eez-loader",
            EEZ_DIR
        )
        return

    log.info("Reading Shapefile: %s", shp_path)
    sf     = shapefile.Reader(shp_path)
    fields = [f[0] for f in sf.fields[1:]]   # skip deletion flag
    total  = len(sf.shapes())
    log.info("Found %d EEZ zones — writing to Neo4j ...", total)

    driver  = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    loaded  = 0
    skipped = 0

    with driver.session() as session:
        for rec in sf.iterShapeRecords():
            props = dict(zip(fields, rec.record))

            # Marine Regions v12 field names
            zone_id     = str(props.get("MRGID",      props.get("mrgid",    "")))
            zone_name   = str(props.get("GeoName",    props.get("GEONAME",  "")))
            country_iso = str(props.get("ISO_SOV1",   props.get("ISO_3",    "")))
            sovereign   = str(props.get("SOVEREIGN1", props.get("COUNTRY",  "")))

            if not zone_id:
                skipped += 1
                continue

            # Convert shapefile geometry → WKT
            # Store a simplified bounding box string for quick spatial checks.
            # Full polygon stored as WKT for the graph anomaly layer.
            try:
                geom     = shape(rec.shape.__geo_interface__)
                wkt      = geom.wkt
                # Cap at 5000 chars — Neo4j string property limit is generous
                # but very large polygons can slow ingestion
                if len(wkt) > 5000:
                    wkt = geom.simplify(0.05).wkt
            except Exception:
                # Fallback: store bounding box only
                bbox = rec.shape.bbox  # [xmin, ymin, xmax, ymax]
                wkt  = (f"POLYGON(({bbox[0]} {bbox[1]}, {bbox[2]} {bbox[1]}, "
                        f"{bbox[2]} {bbox[3]}, {bbox[0]} {bbox[3]}, "
                        f"{bbox[0]} {bbox[1]}))")

            session.run(UPSERT_EEZ, {
                "zone_id":      zone_id,
                "zone_name":    zone_name,
                "country_iso":  country_iso,
                "sovereign":    sovereign,
                "mrgid":        zone_id,
                "geometry_wkt": wkt,
            })
            loaded += 1

            if loaded % 50 == 0:
                log.info("  %d / %d zones written ...", loaded, total)

    log.info("EEZ load complete — %d zones written, %d skipped", loaded, skipped)
    driver.close()


if __name__ == "__main__":
    run()