"""Where the benchmark scripts keep their (large, uncommitted) corpus and results, and which Python has faster-whisper."""
import os
import sys
from pathlib import Path

BENCH = os.environ.get("VOLT_BENCH_DIR", "D:/scratch/bench")  # corpus, clips and results live here, never in the repo
FW_PYTHON = os.environ.get("VOLT_FW_PYTHON", sys.executable)  # a Python that has faster-whisper installed
HERE = str(Path(__file__).resolve().parent)
CONTROL = str(Path(__file__).resolve().parents[1])
for _p in (HERE, CONTROL):
    if _p not in sys.path:
        sys.path.insert(0, _p)
