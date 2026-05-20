"""
FastAPI 서버 - faster-whisper 음성인식 + 실시간 WebSocket 스트리밍
실행: python src/server.py
"""

import os
import sys
import re
import asyncio
import threading
import logging
import tempfile
import faulthandler
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from audio_activity import (
    analyze_audio_activity,
    build_transcribe_options,
    empty_transcription_result,
    format_activity_summary,
    normalize_transcription_result,
)
from runtime_paths import (
    is_frozen,
    make_temp_audio_path,
    runtime_base_dir,
    runtime_log_dir,
)
from folder_dialog import pick_folder

APP_NAME = "whisper_server"
IS_FROZEN = is_frozen()
LOG_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S"

# faster-whisper(CTranslate2)와 PyTorch가 각자 OpenMP 런타임을 들고 있는데,
# PyInstaller frozen 빌드에서 두 OMP DLL이 같은 프로세스에 로드되면 종료 시점
# 또는 스레드 풀 정리 시점에 네이티브 abort가 나는 사례가 관측되므로 진입
# 전에 libomp 중복 로드를 허용하고 OpenMP 스레드 수를 보수적으로 고정한다.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def _open_log_stream() -> tuple[Path, TextIO]:
    try:
        log_path = runtime_log_dir() / f"{APP_NAME}.log"
        return log_path, log_path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        fallback_path = Path(tempfile.gettempdir()) / f"{APP_NAME}.log"
        return fallback_path, fallback_path.open("a", encoding="utf-8", buffering=1)


LOG_FILE_PATH, LOG_FILE_STREAM = _open_log_stream()


def _append_raw_log_text(text: str) -> None:
    if not text:
        return
    try:
        LOG_FILE_STREAM.write(text)
        LOG_FILE_STREAM.flush()
    except Exception:
        pass


def _flush_log_stream() -> None:
    try:
        LOG_FILE_STREAM.flush()
    except Exception:
        pass


def _configure_logging() -> logging.Logger:
    _append_raw_log_text(
        "\n"
        + ("=" * 72)
        + "\n"
        + f"{datetime.now().strftime(LOG_TIMESTAMP_FORMAT)} pid={os.getpid()} frozen={IS_FROZEN}\n"
        + f"argv={sys.argv!r}\n"
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(LOG_FILE_STREAM)]
    if sys.__stderr__ is not None:
        handlers.insert(0, logging.StreamHandler(sys.__stderr__))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt=LOG_TIMESTAMP_FORMAT,
        handlers=handlers,
        force=True,
    )

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    logger = logging.getLogger(APP_NAME)
    logger.info("Logging initialized")
    logger.info("Log file: %s", LOG_FILE_PATH)
    logger.info("Base directory: %s", runtime_base_dir())
    return logger


LOGGER = _configure_logging()


def _install_diagnostic_hooks() -> None:
    try:
        faulthandler.enable(LOG_FILE_STREAM, all_threads=True)
    except Exception:
        LOGGER.warning("faulthandler could not be enabled", exc_info=True)

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        LOGGER.critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        _flush_log_stream()
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def handle_thread_exception(args):
        if args.exc_type is None or issubclass(args.exc_type, KeyboardInterrupt):
            return
        thread_name = args.thread.name if args.thread else "unknown"
        LOGGER.critical(
            "Unhandled exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _flush_log_stream()

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


_install_diagnostic_hooks()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI(title="Whisper 음성인식 서버")
LOGGER.info("FastAPI imports completed")


@app.on_event("startup")
async def log_startup():
    LOGGER.info("Application startup complete")


@app.on_event("shutdown")
async def log_shutdown():
    LOGGER.info("Application shutdown complete")

# ─────────────────────────────────────────────
# 전역 상태
# ─────────────────────────────────────────────
is_running = False
cancel_flag = threading.Event()

# 로드된 WhisperModel을 세션 간에 공유한다. 함수 로컬에 두면 핸들러 리턴 시
# GC가 CTranslate2 모델을 해제하면서 Windows frozen 빌드에서 "Fatal Python
# error: Aborted"로 터지는 현상이 있었고, 같은 모델로 여러 파일을 돌릴 때
# 재로드 비용도 불필요하므로 모듈 전역에 하나만 유지한다.
_model_cache_lock = threading.Lock()
_cached_model = None
_cached_model_key: tuple | None = None


# ─────────────────────────────────────────────
# HTML 제공
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────
# 폴더 스캔 API
# ─────────────────────────────────────────────
@app.get("/api/scan")
async def scan_folder(folder: str):
    folder_path = Path(folder)
    if not folder_path.exists():
        return JSONResponse({"error": f"폴더를 찾을 수 없습니다: {folder}"}, status_code=404)
    if not folder_path.is_dir():
        return JSONResponse({"error": "폴더 경로가 아닙니다."}, status_code=400)

    def natural_key(p: Path):
        """1강 < 2강 < 3강 ... < 10강 < 11강 순으로 정렬"""
        return [int(c) if c.isdigit() else c.lower()
                for c in re.split(r'(\d+)', p.name)]

    extensions = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
    all_files_found = []
    for ext in extensions:
        all_files_found.extend(folder_path.rglob(f"*{ext}"))

    files = []
    for p in sorted(all_files_found, key=natural_key):
            txt_exists = p.with_suffix(".txt").exists()
            srt_exists = p.with_suffix(".srt").exists()
            files.append({
                "name": p.name,
                "path": str(p),
                "rel": str(p.relative_to(folder_path)),
                "size_mb": round(p.stat().st_size / 1024 / 1024, 1),
                "done": txt_exists,
                "has_srt": srt_exists,
            })

    return JSONResponse({"folder": str(folder_path), "count": len(files), "files": files})


# ─────────────────────────────────────────────
# 취소 API
# ─────────────────────────────────────────────
@app.post("/api/cancel")
async def cancel():
    global is_running
    cancel_flag.set()
    is_running = False
    return {"ok": True}


# ─────────────────────────────────────────────
# 폴더 선택 대화상자 API
# ─────────────────────────────────────────────
@app.post("/api/pick-folder")
async def pick_folder_endpoint():
    """네이티브 폴더 대화상자를 띄우고 선택된 절대경로를 반환한다."""
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, pick_folder)
    return {"path": path}


# ─────────────────────────────────────────────
# 전사 도우미
# ─────────────────────────────────────────────
def _format_hms(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _run_transcribe_with_progress(
    wmodel,
    audio_path: Path,
    activity,
    language: str,
    loop: asyncio.AbstractEventLoop,
    websocket: WebSocket,
) -> dict:
    """faster-whisper의 transcribe는 (segments_generator, info) 튜플을 반환하고
    generator 반복이 실제 추론을 트리거한다. generator를 별도 스레드에서
    돌리며 각 Segment 도착 시점에 asyncio.Queue를 통해 메인 코루틴에 넘겨
    곧바로 WebSocket으로 progress 메시지를 전송한다. 결과는
    normalize_transcription_result가 기대하는 dict 형태로 돌려준다.

    기존 openai-whisper 경로와 달리 tqdm stderr 캡처가 필요 없어서
    WsStream/ProgressRelay/pump_progress 구조 전체가 제거됐다.
    """
    q: asyncio.Queue = asyncio.Queue()
    TAG_INFO = "info"
    TAG_SEG = "segment"
    TAG_DONE = "done"
    TAG_ERR = "error"

    clip_timestamps = activity.effective_clip_timestamps()
    options = build_transcribe_options(
        language=language,
        clip_timestamps=clip_timestamps,
    )

    def worker():
        try:
            segments_gen, info = wmodel.transcribe(str(audio_path), **options)
            loop.call_soon_threadsafe(q.put_nowait, (TAG_INFO, info))
            for seg in segments_gen:
                loop.call_soon_threadsafe(q.put_nowait, (TAG_SEG, seg))
            loop.call_soon_threadsafe(q.put_nowait, (TAG_DONE, None))
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, (TAG_ERR, e))

    fut = loop.run_in_executor(None, worker)

    collected: list[dict] = []
    info_obj = None
    try:
        while True:
            tag, payload = await q.get()
            if tag == TAG_ERR:
                raise payload
            if tag == TAG_DONE:
                # 마지막 음성 세그먼트 end가 항상 info.duration에 도달하는 건
                # 아니라 99%대에서 끝난 것처럼 보이는 경우가 있다. 전사 자체는
                # 여기서 완전히 끝난 시점이므로 100% progress를 한 번 마지막으로
                # 내보내 UI의 진행 바가 가득 차도록 한다.
                if info_obj and info_obj.duration and info_obj.duration > 0:
                    total_hms = _format_hms(info_obj.duration)
                    try:
                        await websocket.send_json({
                            "type": "progress",
                            "msg": f"100.0% | {total_hms} / {total_hms}",
                            "pct": 100.0,
                        })
                    except Exception:
                        pass
                break
            if tag == TAG_INFO:
                info_obj = payload
                continue
            # TAG_SEG
            seg = payload
            seg_dict: dict[str, Any] = {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg.text,
                "avg_logprob": float(seg.avg_logprob),
                "no_speech_prob": float(seg.no_speech_prob),
                "compression_ratio": float(seg.compression_ratio),
            }
            words = getattr(seg, "words", None)
            if words:
                seg_dict["words"] = [
                    {
                        "start": float(w.start),
                        "end": float(w.end),
                        "word": w.word,
                        "probability": float(w.probability),
                    }
                    for w in words
                ]
            collected.append(seg_dict)

            if info_obj and info_obj.duration and info_obj.duration > 0:
                total = float(info_obj.duration)
                cur = float(seg.end)
                pct = min(100.0, cur / total * 100.0)
                msg = f"{pct:5.1f}% | {_format_hms(cur)} / {_format_hms(total)}"
                try:
                    await websocket.send_json({
                        "type": "progress",
                        "msg": msg,
                        "pct": pct,
                    })
                except Exception:
                    # WebSocket이 중간에 닫혀도 전사 자체는 끝까지 간다.
                    pass
    finally:
        try:
            await fut
        except Exception:
            pass

    return normalize_transcription_result({
        "segments": collected,
        "text": "",
        "language": info_obj.language if info_obj else language,
    })


# ─────────────────────────────────────────────
# 모델 로딩 + 파일 1개 전사 (로컬 파일·유튜브 경로가 공유)
# ─────────────────────────────────────────────
async def ensure_model(
    websocket: WebSocket,
    model_name: str,
    loop: asyncio.AbstractEventLoop,
):
    """faster-whisper 모델을 로딩(또는 캐시 재사용)하고 wmodel을 반환한다.

    모델 로딩·GPU 로그를 WebSocket으로 전송한다.
    """
    await websocket.send_json({
        "type": "log",
        "msg": f"[모델 로딩] faster-whisper {model_name} 로딩 중...",
    })

    from faster_whisper import WhisperModel
    import torch

    if torch.cuda.is_available():
        device = "cuda"
        gpu_name = torch.cuda.get_device_name(0)
        compute_type = "float16"
    else:
        device = "cpu"
        gpu_name = "CPU"
        compute_type = "int8"

    await websocket.send_json({
        "type": "log",
        "msg": f"[GPU] {gpu_name} 사용 (compute_type={compute_type})",
    })

    def _load_or_get_cached():
        global _cached_model, _cached_model_key
        key = (model_name, device, compute_type)
        with _model_cache_lock:
            if _cached_model is not None and _cached_model_key == key:
                return _cached_model, True
            # 모델 키가 바뀌면 기존 캐시를 먼저 비워서 두 개의 CTranslate2
            # 모델이 동시에 GPU 메모리를 점유하지 않게 한다.
            _cached_model = None
            _cached_model_key = None
            new_model = WhisperModel(model_name, device=device, compute_type=compute_type)
            _cached_model = new_model
            _cached_model_key = key
            return new_model, False

    wmodel, was_cached = await loop.run_in_executor(None, _load_or_get_cached)
    await websocket.send_json({
        "type": "log",
        "msg": "[모델 로딩] 완료 ✓ (캐시 재사용)" if was_cached else "[모델 로딩] 완료 ✓",
    })
    return wmodel


async def transcribe_file(
    websocket: WebSocket,
    wmodel,
    idx: int,
    total: int,
    file_path: str,
    language: str,
    make_srt: bool,
    loop: asyncio.AbstractEventLoop,
) -> bool:
    """파일 1개를 전사하고 txt/srt를 저장한다. file_start ~ file_done 메시지를 보낸다.

    반환값: 계속 진행하면 True, 세션 전체를 중단해야 하면 False(ffmpeg가 없으면
    다음 파일도 실패하므로 중단한다). 호출 측에서 cancel_flag 검사와 모델 로딩을
    책임진다. 로컬 파일(ws_transcribe)과 유튜브(ws_youtube) 경로가 공유한다.
    """
    import time

    p = Path(file_path)
    await websocket.send_json({
        "type": "file_start",
        "idx": idx,
        "total": total,
        "name": p.name,
        "path": str(file_path),
    })

    # 이미 처리된 파일
    if p.with_suffix(".txt").exists():
        await websocket.send_json({"type": "log", "msg": f"  ⏭ 건너뜀 (이미 완료): {p.name}"})
        await websocket.send_json({
            "type": "file_done",
            "idx": idx,
            "name": p.name,
            "skipped": True,
            "has_srt": p.with_suffix(".srt").exists(),
        })
        return True

    # 오디오 추출
    await websocket.send_json({"type": "log", "msg": f"  🎬 오디오 추출 중..."})
    audio_path = make_temp_audio_path(p)

    try:
        # ffmpeg가 한글 경로를 인식하지 못하는 버그를 피하기 위해,
        # Python에서 파일을 열어 ffmpeg stdin으로 직접 파이프합니다.
        with open(p, "rb") as video_file:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", "pipe:0",
                "-ac", "1", "-ar", "16000", "-vn", str(audio_path),
                stdin=video_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                await websocket.send_json({"type": "log", "msg": f"  ❌ ffmpeg 오류: {stderr.decode(errors='replace')[-300:]}"})
                await websocket.send_json({"type": "file_error", "idx": idx, "name": p.name})
                return True
    except FileNotFoundError:
        await websocket.send_json({"type": "log", "msg": "  ❌ ffmpeg를 찾을 수 없습니다. 설치 필요: winget install ffmpeg"})
        return False

    await websocket.send_json({"type": "log", "msg": "  🔎 음성 구간 분석 중..."})
    try:
        activity = await loop.run_in_executor(None, lambda: analyze_audio_activity(audio_path))
    except Exception as e:
        LOGGER.exception("Audio activity analysis failed for %s", p)
        await websocket.send_json({"type": "log", "msg": f"  ❌ 오디오 분석 오류: {e}"})
        await websocket.send_json({"type": "file_error", "idx": idx, "name": p.name})
        if audio_path.exists():
            audio_path.unlink()
        return True

    await websocket.send_json({"type": "log", "msg": f"  🧭 {format_activity_summary(activity)}"})

    if not activity.regions:
        result = empty_transcription_result(language)
        elapsed = 0
        detected = result.get("language", "?")
        txt_path = p.with_suffix(".txt")
        txt_path.write_text(result["text"], encoding="utf-8")
        await websocket.send_json({"type": "log", "msg": f"  💾 저장: {txt_path.name}"})

        if make_srt:
            srt_path = p.with_suffix(".srt")
            srt_path.write_text("", encoding="utf-8")
            await websocket.send_json({"type": "log", "msg": f"  💾 저장: {srt_path.name}"})

        if audio_path.exists():
            audio_path.unlink()

        await websocket.send_json({
            "type": "file_done",
            "idx": idx,
            "name": p.name,
            "elapsed": elapsed,
            "language": detected,
            "skipped": False,
            "preview": "",
            "has_srt": make_srt,
        })
        await websocket.send_json({
            "type": "log",
            "msg": "  ✅ 완료 (음성 패턴 미검출, 빈 결과 저장)",
        })
        return True

    await websocket.send_json({"type": "log", "msg": "  🤖 음성 인식 중..."})
    t0 = time.time()

    try:
        result = await _run_transcribe_with_progress(
            wmodel, audio_path, activity, language, loop, websocket,
        )
    except Exception as e:
        LOGGER.exception("Transcription failed for %s", p)
        await websocket.send_json({"type": "log", "msg": f"  ❌ 인식 오류: {e}"})
        await websocket.send_json({"type": "file_error", "idx": idx, "name": p.name})
        if audio_path.exists():
            audio_path.unlink()
        return True

    elapsed = round(time.time() - t0)
    detected = result.get("language", "?")
    LOGGER.info("전사 완료: %s (%d초, 언어=%s)", p.name, elapsed, detected)

    # 텍스트 저장
    txt_path = p.with_suffix(".txt")
    txt_path.write_text(result["text"], encoding="utf-8")
    await websocket.send_json({"type": "log", "msg": f"  💾 저장: {txt_path.name}"})

    # SRT 저장
    if make_srt:
        srt_path = p.with_suffix(".srt")
        srt_lines = []
        for i, seg in enumerate(result["segments"], 1):
            def ts(s):
                h, r = divmod(s, 3600)
                m, s2 = divmod(r, 60)
                return f"{int(h):02d}:{int(m):02d}:{int(s2):02d},{int((s2%1)*1000):03d}"
            srt_lines.append(f"{i}\n{ts(seg['start'])} --> {ts(seg['end'])}\n{seg['text'].strip()}\n")
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
        await websocket.send_json({"type": "log", "msg": f"  💾 저장: {srt_path.name}"})

    # 임시 파일 삭제
    if audio_path.exists():
        audio_path.unlink()

    await websocket.send_json({
        "type": "file_done",
        "idx": idx,
        "name": p.name,
        "elapsed": elapsed,
        "language": detected,
        "skipped": False,
        "preview": result["text"][:200],
        "has_srt": make_srt,
    })
    await websocket.send_json({
        "type": "log",
        "msg": f"  ✅ 완료 ({elapsed}초) | 언어: {detected}",
    })
    return True


# ─────────────────────────────────────────────
# WebSocket - 실시간 변환
# ─────────────────────────────────────────────
@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket):
    global is_running
    await websocket.accept()

    try:
        data = await websocket.receive_json()
        files: list[str] = data.get("files", [])
        model_name: str = data.get("model", "turbo")
        language: str = data.get("language", "ko")
        make_srt: bool = data.get("srt", True)

        if not files:
            await websocket.send_json({"type": "error", "msg": "파일이 선택되지 않았습니다."})
            return

        if is_running:
            await websocket.send_json({"type": "error", "msg": "이미 변환이 실행 중입니다."})
            return

        is_running = True
        cancel_flag.clear()

        await websocket.send_json({"type": "start", "total": len(files)})

        loop = asyncio.get_event_loop()
        wmodel = await ensure_model(websocket, model_name, loop)

        for idx, file_path in enumerate(files, 1):
            if cancel_flag.is_set():
                await websocket.send_json({"type": "cancelled", "msg": "사용자가 취소했습니다."})
                break
            ok = await transcribe_file(
                websocket, wmodel, idx, len(files), file_path, language, make_srt, loop,
            )
            if not ok:
                break

        if not cancel_flag.is_set():
            await websocket.send_json({"type": "done", "msg": "모든 파일 처리 완료!"})
            LOGGER.info("세션 종료: 전체 파일 처리 완료 (%d개)", len(files))
        else:
            LOGGER.info("세션 종료: 사용자 취소")

    except WebSocketDisconnect:
        LOGGER.info("세션 종료: WebSocket 연결 끊김")
    except Exception as e:
        LOGGER.exception("WebSocket transcription failed")
        await websocket.send_json({"type": "error", "msg": str(e)})
    finally:
        is_running = False
        cancel_flag.clear()


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
if __name__ == "__main__":
    LOGGER.info("Starting Whisper server")
    LOGGER.info("Open http://localhost:8765 after startup")
    print("=" * 50)
    print("  Whisper 음성인식 서버 시작")
    print("  브라우저에서 열기: http://localhost:8765")
    print(f"  Log file: {LOG_FILE_PATH}")
    print("=" * 50)
    try:
        uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning", log_config=None)
    except Exception:
        LOGGER.exception("Server terminated during startup")
        raise
    finally:
        LOGGER.info("Process exiting")
        _flush_log_stream()
