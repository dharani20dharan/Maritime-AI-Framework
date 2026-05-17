"""
MAF — Bathymetry Loader (Engineer A, Week 2)
Reads GEBCO NetCDF, builds a lightweight depth-lookup Parquet index,
and loads it into Neo4j for track plausibility checks (M-TRACK-PLAUSIBILITY).

Week 1 stub — full implementation in Week 2 after GEBCO download.
"""
import os
import logging

log = logging.getLogger("bathymetry-loader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

GEBCO_PATH = os.getenv("GEBCO_NC_PATH", "/data/GEBCO_2026.nc")

def run():
    if not os.path.exists(GEBCO_PATH):
        log.warning(
            "GEBCO file not found at %s — skipping. "
            "Download from https://download.gebco.net/ and re-run this container.",
            GEBCO_PATH
        )
        return
    log.info("GEBCO file found — full loader implementation coming in Week 2.")

if __name__ == "__main__":
    run()