from __future__ import annotations

import re
import sys
from pathlib import Path
from uuid import uuid4


def runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_temp_dir() -> Path:
    temp_dir = runtime_base_dir() / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def make_temp_audio_path(source_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source_path.stem).strip("._")
    if not safe_stem:
        safe_stem = "audio"
    safe_stem = safe_stem[:48]
    return runtime_temp_dir() / f"{safe_stem}_{uuid4().hex[:12]}.wav"
