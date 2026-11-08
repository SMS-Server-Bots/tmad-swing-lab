"""Run one TMAD validator after installing the hermetic validation guard."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
runpy.run_path(str(HERE / "sitecustomize.py"), run_name="tmad_validation_guard")

if len(sys.argv) < 2:
    raise SystemExit("Usage: validation_runner.py <validator.py> [args...]")
target = Path(sys.argv[1]).resolve()
sys.argv = [str(target), *sys.argv[2:]]
runpy.run_path(str(target), run_name="__main__")
