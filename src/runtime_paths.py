from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

APP_DIR_NAME = "transcribe-video"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_base_dir() -> Path:
    """exe/스크립트가 놓여있는 위치. runtime state가 아닌, 읽기 전용 리소스 기준점."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _state_candidates() -> list[Path]:
    """쓰기 가능한 runtime state 루트 후보. 앞에 있을수록 우선."""
    candidates: list[Path] = []
    if is_frozen():
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / APP_DIR_NAME)
        candidates.append(Path(tempfile.gettempdir()) / APP_DIR_NAME)
    else:
        candidates.append(Path(__file__).resolve().parent.parent)
    return candidates


def runtime_state_dir() -> Path:
    """logs/temp 등 쓰기 상태를 보관할 루트. frozen일 때는 exe 옆을 건드리지 않는다."""
    last_error: Exception | None = None
    for candidate in _state_candidates():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"No writable runtime state directory available: {last_error}")


def runtime_temp_dir() -> Path:
    temp_dir = runtime_state_dir() / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def runtime_log_dir() -> Path:
    log_dir = runtime_state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def make_temp_audio_path(source_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source_path.stem).strip("._")
    if not safe_stem:
        safe_stem = "audio"
    safe_stem = safe_stem[:48]
    return runtime_temp_dir() / f"{safe_stem}_{uuid4().hex[:12]}.wav"
