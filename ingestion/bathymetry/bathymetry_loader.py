"""
MAF — Bathymetry Loader (Engineer A, Week 2)
Reads GEBCO NetCDF when available, builds a depth-lookup index,
and initializes coordinates within the database for physical track checks.
"""
import os
import sys
import logging

# Ensure root directory is in import path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.bathymetry import BathymetryEngine

log = logging.getLogger("bathymetry-loader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

GEBCO_PATH = os.getenv("GEBCO_NC_PATH", "/data/GEBCO_2026.nc")

def run():
    log.info("Starting Bathymetry Loader service...")
    
    # Initialize the engine
    engine = BathymetryEngine(netcdf_path=GEBCO_PATH)
    
    if engine.use_netcdf:
        log.info("Authoritative GEBCO NetCDF detected — ready for real-time telemetry indexing.")
    else:
        log.info("No NetCDF database found at /data/GEBCO_2026.nc — fallback mathematical spatial depth lookup is active.")
        log.info("Bathymetry Loader successfully loaded and configured in offline spatial profile mode.")

if __name__ == "__main__":
    run()