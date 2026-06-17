"""yt-dlp.exe 래퍼 — 유튜브 URL 해석 및 영상 다운로드.

pip 패키지가 아닌 단독 실행파일을 subprocess로 호출한다(ffmpeg와 동일 패턴).
유튜브가 자주 깨지므로 yt-dlp.exe는 whisper_server 재빌드 없이 교체할 수 있다.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_WINDOWS_RESERVED = r'[\\/:*?"<>|]'


class YoutubeError(Exception):
    """URL 해석 또는 다운로드 실패."""


@dataclass
class VideoEntry:
    id: str
    title: str
    url: str


@dataclass
class ResolveResult:
    is_playlist: bool
    playlist_title: str        # 단일 영상이면 ""
    entries: list[VideoEntry]


def sanitize_filename(name: str) -> str:
    """Windows 파일/폴더명에 못 쓰는 문자를 _ 로 치환한다."""
    cleaned = re.sub(_WINDOWS_RESERVED, "_", name).strip().strip(".")
    return cleaned or "untitled"


def find_ytdlp() -> Path:
    """yt-dlp.exe 위치를 찾는다.

    frozen 빌드: server.py와 같은 번들 디렉터리(_MEIPASS)에 함께 들어 있다.
    개발 실행: 저장소 bin/ 에 있다. 둘 다 없으면 PATH에 맡긴다.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "yt-dlp.exe",                  # frozen 번들 / src 옆
        here.parent / "bin" / "yt-dlp.exe",   # 개발: 저장소 bin/
    ]
    for c in candidates:
        if c.exists():
            return c
    return Path("yt-dlp.exe")  # PATH 폴백


def _parse_resolve_json(data: dict) -> ResolveResult:
    """yt-dlp --dump-single-json 결과(dict)를 ResolveResult로 변환한다."""
    if data.get("_type") == "playlist":
        entries: list[VideoEntry] = []
        for e in data.get("entries") or []:
            if not e:
                continue
            entries.append(VideoEntry(
                id=e.get("id", ""),
                title=e.get("title") or e.get("id") or "video",
                url=e.get("url") or e.get("webpage_url") or e.get("id") or "",
            ))
        return ResolveResult(
            is_playlist=True,
            playlist_title=sanitize_filename(data.get("title") or "playlist"),
            entries=entries,
        )
    # 단일 영상
    return ResolveResult(
        is_playlist=False,
        playlist_title="",
        entries=[VideoEntry(
            id=data.get("id", ""),
            title=data.get("title") or data.get("id") or "video",
            url=data.get("webpage_url") or data.get("original_url") or data.get("id") or "",
        )],
    )


def resolve(url: str) -> ResolveResult:
    """yt-dlp로 URL을 해석한다. 단일 영상 또는 재생목록."""
    ytdlp = find_ytdlp()
    try:
        completed = subprocess.run(
            [str(ytdlp), "--flat-playlist", "--dump-single-json", url],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YoutubeError(f"URL 해석 실패: {exc}") from exc
    if completed.returncode != 0:
        err = completed.stderr.decode("utf-8", errors="replace").strip()
        raise YoutubeError(f"URL 해석 실패: {err[-300:]}")
    try:
        data = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise YoutubeError("URL 해석 실패: JSON 파싱 오류") from exc
    return _parse_resolve_json(data)


def _build_download_cmd(ytdlp: Path, video_url: str, dest_dir: Path) -> list[str]:
    """download()가 실행할 yt-dlp 명령줄을 조립한다.

    --encoding UTF-8 이 핵심이다. yt-dlp.exe는 stdout이 파이프로 연결되면
    콘솔 코드페이지(한국어 Windows면 cp949)로 출력을 인코딩한다. 그런데
    download()는 stdout을 UTF-8로 읽으므로, 한글이 든 DLPATH 줄이 깨져
    파일 경로를 못 찾는다. 출력 인코딩을 UTF-8로 못박아 reader와 맞춘다.
    """
    out_template = str(dest_dir / "%(title).180B [%(id)s].%(ext)s")
    return [
        str(ytdlp),
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-overwrites",
        "--windows-filenames",
        # 파이프 출력을 UTF-8로 강제 — DLPATH/DLPCT 줄을 UTF-8로 읽기 위함.
        "--encoding", "UTF-8",
        "--newline",
        # --print이 --quiet을 함의해 진행률 출력이 막히므로, --progress로
        # quiet 상태에서도 진행바(progress-template)를 강제로 켠다.
        "--progress",
        "--progress-template", "DLPCT %(progress._percent_str)s",
        "--print", "after_move:DLPATH %(filepath)s",
        "-o", out_template,
        video_url,
    ]


def download(video_url: str, dest_dir: Path,
             progress_cb: Callable[[float], None]) -> Path:
    """영상을 dest_dir에 mp4로 다운로드한다. 다운로드된 파일 경로를 반환.

    progress_cb는 0~100 사이 퍼센트로 호출된다. 블로킹 함수다.
    """
    ytdlp = find_ytdlp()
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = _build_download_cmd(ytdlp, video_url, dest_dir)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise YoutubeError(f"다운로드 실행 실패: {exc}") from exc

    final_path: Path | None = None
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("DLPCT"):
            m = re.search(r"([\d.]+)", line)
            if m:
                progress_cb(float(m.group(1)))
        elif line.startswith("DLPATH "):
            final_path = Path(line[len("DLPATH "):].strip())
    proc.wait()
    if proc.returncode != 0:
        raise YoutubeError("다운로드 실패")
    if final_path is None or not final_path.exists():
        raise YoutubeError("다운로드된 파일을 찾지 못했습니다")
    return final_path
