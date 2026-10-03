"""Replikasi run_loop daemon persis (2 iterasi cepat)."""
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
print("probe start", flush=True)
from src.ops.daemon import run_loop

run_loop(poll_interval=0.5, max_iters=2)
print("LOOP_PROBE_DONE", flush=True)
