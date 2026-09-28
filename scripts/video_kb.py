#!/usr/bin/env python3
"""Distribution entrypoint; no credential or user-content output."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_kb.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
