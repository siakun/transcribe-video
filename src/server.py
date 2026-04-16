"""
FastAPI 서버 - Whisper 음성인식 + 실시간 WebSocket 스트리밍
실행: python src/server.py
"""

import os
import sys
import re
import asyncio
import threading
import queue
import subprocess
import contextlib
import logging
import tempfile
import faulthandler
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

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

APP_NAME = "whisper_server"
IS_FROZEN = is_frozen()
LOG_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S"


def _point_cuda_path_at_bundled_triton() -> None:
    """Frozen 빌드에서 triton-windows가 번들된 ptxas/cuda.h/cuda.lib를 찾게 한다.

    triton-windows의 find_cuda_bundled는 sysconfig.get_paths()["platlib"] 아래
    triton/backends/nvidia 를 기준으로 동작하는데, PyInstaller onedir 빌드에서
    platlib는 _internal 을 가리키지 않아 "Failed to find CUDA" 경고가 난다.
    번들에는 해당 파일들이 _MEIPASS/triton/backends/nvidia 에 제대로 들어가
    있으므로, 사용자가 CUDA_PATH 를 따로 지정한 게 아니라면 이 경로로 설정해
    find_cuda_env 가 맨 먼저 해석되게 한다.
    """
    if not IS_FROZEN:
        return
    if os.environ.get("CUDA_PATH") or os.environ.get("CUDA_HOME"):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    candidate = Path(meipass) / "triton" / "backends" / "nvidia"
    if (candidate / "bin" / "ptxas.exe").exists():
        os.environ["CUDA_PATH"] = str(candidate)


_point_cuda_path_at_bundled_triton()


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
from fastapi.staticfiles import StaticFiles
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
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
# WebSocket 로깅용 스트림
# ─────────────────────────────────────────────
class WsStream:
    def __init__(self, relay):
        self.relay = relay
        self.buf = ""

    def write(self, s):
        # relay로 가는 경로가 우선이다. 서버 콘솔 미러링(sys.__stderr__)
        # 이나 raw 로그 파일 기록이 어떤 이유로 예외를 던지더라도 웹 UI로
        # 가는 progress 전달이 중단되지 않도록 먼저 buf를 소비한다.
        self.buf += s
        while "\r" in self.buf or "\n" in self.buf:
            idx_r = self.buf.find("\r")
            idx_n = self.buf.find("\n")

            if idx_r != -1 and (idx_n == -1 or idx_r < idx_n):
                line = self.buf[:idx_r]
                self.buf = self.buf[idx_r+1:]
            else:
                line = self.buf[:idx_n]
                self.buf = self.buf[idx_n+1:]

            self.relay.push(line)

        # 보조적인 터미널 미러링과 raw 로그 기록은 best-effort로 돌린다.
        try:
            if sys.__stderr__ is not None:
                sys.__stderr__.write(s)
                sys.__stderr__.flush()
        except Exception:
            pass
        try:
            _append_raw_log_text(s)
        except Exception:
            pass

    def flush(self):
        # 버퍼에 남은 꼬리(주로 tqdm의 마지막 프레임)를 relay에 흘려보내면
        # transcribe가 끝난 뒤 done 이후에 뒤늦은 progress가 튀어나와 UI가
        # "진행중"처럼 보이게 된다. 원본 stderr와 raw 로그에는 이미 기록되어
        # 있으므로 여기서는 relay에는 밀지 않고 버퍼만 비운다.
        sys.__stderr__.flush()
        self.buf = ""
        _flush_log_stream()


class ProgressRelay:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest: Optional[str] = None
        self.closed = False
        self.push_count = 0  # 진단용: push 단계가 아예 호출되지 않는 경로를 가려내기 위한 카운터

    def push(self, line: str):
        clean = ANSI_ESCAPE_RE.sub("", line).strip()
        if not clean or clean.startswith("warnings.warn"):
            return
        with self.lock:
            self.latest = clean
            self.push_count += 1

    def pop_latest(self) -> Optional[str]:
        with self.lock:
            line = self.latest
            self.latest = None
            return line

    def has_pending(self) -> bool:
        with self.lock:
            return self.latest is not None

    def close(self):
        with self.lock:
            self.closed = True

    def is_closed(self) -> bool:
        with self.lock:
            return self.closed


async def pump_progress(relay: ProgressRelay, websocket: WebSocket):
    # progress 파이프(WsStream → relay → pump → WS)가 어디서 끊기는지 진단하기
    # 위한 카운터. 세션 종료 자리에 단 한 줄만 찍어 빈도 노이즈 없이 "서버가
    # 몇 개나 송출했는가"를 드러낸다. 0이면 서버 측 capture가 못 된 것이고,
    # N>0인데 UI에 안 보이면 클라이언트 렌더링 쪽 문제다.
    last_sent = None
    sent_count = 0
    try:
        while not relay.is_closed() or relay.has_pending():
            line = relay.pop_latest()
            if line and line != last_sent:
                await websocket.send_json({"type": "progress", "msg": line})
                last_sent = line
                sent_count += 1
            else:
                await asyncio.sleep(0.12)
    except Exception:
        LOGGER.exception("Progress relay failed")
        relay.close()
    finally:
        LOGGER.info(
            "progress 송출 요약: push=%d건, send=%d건",
            relay.push_count,
            sent_count,
        )


# ─────────────────────────────────────────────
# WebSocket - 실시간 변환
# ─────────────────────────────────────────────
@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket):
    global is_running
    await websocket.accept()
    progress_relay = ProgressRelay()
    progress_task = asyncio.create_task(pump_progress(progress_relay, websocket))

    try:
        data = await websocket.receive_json()
        files: list[str] = data.get("files", [])
        model: str = data.get("model", "turbo")
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

        # 모델 로딩 (한 번만)
        await websocket.send_json({"type": "log", "msg": f"[모델 로딩] whisper {model} 로딩 중..."})

        import whisper
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        gpu_name = torch.cuda.get_device_name(0) if device == "cuda" else "CPU"
        await websocket.send_json({"type": "log", "msg": f"[GPU] {gpu_name} 사용"})

        # 별도 스레드에서 모델 로딩 (블로킹 방지). whisper.load_model은 캐시가
        # 없으면 OpenAI CDN에서 모델을 받아오면서 tqdm을 stderr에 찍는데,
        # 전사 때 쓰는 것과 같은 WsStream/progress_relay 경로로 묶으면 그
        # 다운로드 진행률이 UI의 progress 줄에 그대로 나타난다.
        loop = asyncio.get_event_loop()

        def do_load_model():
            stream = WsStream(progress_relay)
            with contextlib.redirect_stderr(stream), contextlib.redirect_stdout(stream):
                try:
                    return whisper.load_model(model, device=device)
                finally:
                    stream.flush()

        wmodel = await loop.run_in_executor(None, do_load_model)
        await websocket.send_json({"type": "log", "msg": f"[모델 로딩] 완료 ✓"})

        import time

        for idx, file_path in enumerate(files, 1):
            if cancel_flag.is_set():
                await websocket.send_json({"type": "cancelled", "msg": "사용자가 취소했습니다."})
                break

            p = Path(file_path)
            await websocket.send_json({
                "type": "file_start",
                "idx": idx,
                "total": len(files),
                "name": p.name,
                "path": file_path,
            })

            # 이미 처리된 파일
            if p.with_suffix(".txt").exists():
                await websocket.send_json({"type": "log", "msg": f"  ⏭ 건너뜀 (이미 완료): {p.name}"})
                await websocket.send_json({"type": "file_done", "idx": idx, "name": p.name, "skipped": True})
                continue

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
                        continue
            except FileNotFoundError:
                await websocket.send_json({"type": "log", "msg": "  ❌ ffmpeg를 찾을 수 없습니다. 설치 필요: winget install ffmpeg"})
                break

            await websocket.send_json({"type": "log", "msg": "  🔎 음성 구간 분석 중..."})
            try:
                activity = await loop.run_in_executor(None, lambda: analyze_audio_activity(audio_path))
            except Exception as e:
                LOGGER.exception("Audio activity analysis failed for %s", p)
                await websocket.send_json({"type": "log", "msg": f"  ❌ 오디오 분석 오류: {e}"})
                await websocket.send_json({"type": "file_error", "idx": idx, "name": p.name})
                if audio_path.exists():
                    audio_path.unlink()
                continue

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
                })
                await websocket.send_json({
                    "type": "log",
                    "msg": "  ✅ 완료 (음성 패턴 미검출, 빈 결과 저장)",
                })
                continue

            await websocket.send_json({"type": "log", "msg": f"  🤖 음성 인식 중... (모델: {model})"})
            t0 = time.time()

            # 블로킹 인식 → executor에서 실행
            def do_transcribe():
                stream = WsStream(progress_relay)
                with contextlib.redirect_stderr(stream), contextlib.redirect_stdout(stream):
                    try:
                        return wmodel.transcribe(
                            str(audio_path),
                            **build_transcribe_options(
                                language=language,
                                fp16=(device == "cuda"),
                                clip_timestamps=activity.effective_clip_timestamps(),
                            ),
                        )
                    finally:
                        stream.flush()

            try:
                result = normalize_transcription_result(await loop.run_in_executor(None, do_transcribe))
            except Exception as e:
                LOGGER.exception("Transcription failed for %s", p)
                await websocket.send_json({"type": "log", "msg": f"  ❌ 인식 오류: {e}"})
                await websocket.send_json({"type": "file_error", "idx": idx, "name": p.name})
                if audio_path.exists():
                    audio_path.unlink()
                continue

            elapsed = round(time.time() - t0)
            detected = result.get("language", "?")
            # Whisper tqdm은 clip_timestamps가 음성 구간만 커버하면 100%에 못
            # 닿은 채 leave=True로 마지막 프레임이 서버 콘솔에 박혀 있어서,
            # 전사가 끝난 뒤에도 화면상 "진행중"처럼 보인다. 서버 측 로그를
            # 한 줄 찍어 커서를 tqdm 다음 줄로 밀어내고 완료를 명시한다.
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
            })
            await websocket.send_json({
                "type": "log",
                "msg": f"  ✅ 완료 ({elapsed}초) | 언어: {detected}",
            })

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
        progress_relay.close()
        await progress_task


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
