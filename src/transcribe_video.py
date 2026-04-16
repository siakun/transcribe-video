"""
동영상 음성 → 텍스트 변환 스크립트
  - openai-whisper 로컬 실행 (인터넷/API 키 불필요, 완전 무료)
  - RTX 5080 최적화 (FP16, GPU 가속)
  - 단일 파일 또는 폴더 전체 배치 처리 지원
  - 중단 후 이어서 처리 가능 (이미 완료된 파일 건너뜀)

사용법:
  # 단일 파일
  python src/transcribe_video.py "파이썬 활용 인공지능 이론과 실습 1차.mp4"

  # 폴더 전체 배치 처리 (하위 폴더 포함)
  python src/transcribe_video.py "C:\\Users\\User\\Videos" --batch

  # 빠른 처리 (turbo 모델, large-v3 수준 정확도 + 6배 빠름)
  python src/transcribe_video.py "..." --model turbo

  # 특정 언어 지정
  python src/transcribe_video.py "..." --language ko
"""

import os
import sys
import argparse
import subprocess
import time
from pathlib import Path

# ─────────────────────────────────────────────
# 모델 정보
# ─────────────────────────────────────────────
MODEL_INFO = {
    "tiny":    {"size": "75MB",   "vram": "~1GB",  "speed": "32x"},
    "base":    {"size": "145MB",  "vram": "~1GB",  "speed": "16x"},
    "small":   {"size": "467MB",  "vram": "~2GB",  "speed": "6x"},
    "medium":  {"size": "1.5GB",  "vram": "~5GB",  "speed": "2x"},
    "large":   {"size": "2.9GB",  "vram": "~10GB", "speed": "1x"},
    "large-v3":{"size": "3.1GB",  "vram": "~10GB", "speed": "1x"},
    "turbo":   {"size": "809MB",  "vram": "~6GB",  "speed": "8x"},  # 권장: large-v3 수준 + 6배 빠름
}


def extract_audio(video_path: Path, audio_path: Path) -> bool:
    """MP4에서 16kHz 모노 WAV 오디오 추출"""
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(video_path),
            "-ac", "1",        # 모노
            "-ar", "16000",    # 16kHz (Whisper 요구사항)
            "-vn",             # 비디오 스트림 제외
            "-y",              # 덮어쓰기
            str(audio_path)
        ],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ❌ ffmpeg 오류: {result.stderr[-500:]}")
        return False
    return True


def transcribe(audio_path: Path, model, language: str) -> dict:
    """Whisper로 음성 인식 수행"""
    result = model.transcribe(
        str(audio_path),
        language=language if language != "auto" else None,
        fp16=True,          # RTX 5080에서 2배 빠름
        verbose=False,      # 배치 처리 시 출력 정리
        condition_on_previous_text=True,  # 긴 영상에서 문맥 유지
        no_speech_threshold=0.6,          # 무음 구간 필터링
        compression_ratio_threshold=2.4,  # 반복 감지
    )
    return result


def save_text(text: str, output_path: Path):
    """텍스트 파일로 저장"""
    output_path.write_text(text, encoding="utf-8")


def save_srt(segments: list, output_path: Path):
    """SRT 자막 파일로 저장"""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = format_timestamp(seg["start"])
        end   = format_timestamp(seg["end"])
        lines.append(f"{i}\n{start} --> {end}\n{seg['text'].strip()}\n")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def format_timestamp(seconds: float) -> str:
    """초 → SRT 타임스탬프 (HH:MM:SS,mmm)"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def process_single_file(
    video_path: Path,
    model,
    language: str,
    output_dir: Path | None,
    save_srt_flag: bool,
    keep_audio: bool,
) -> bool:
    """단일 파일 처리"""
    # 출력 경로 결정
    base_dir = output_dir if output_dir else video_path.parent
    stem = video_path.stem
    txt_path = base_dir / f"{stem}.txt"
    srt_path = base_dir / f"{stem}.srt"
    audio_path = base_dir / f"{stem}_temp.wav"

    # 이미 처리된 파일 건너뜀
    if txt_path.exists():
        print(f"  ⏭️  건너뜀 (이미 완료): {txt_path.name}")
        return True

    start_time = time.time()
    print(f"  🎬 오디오 추출 중...")

    if not extract_audio(video_path, audio_path):
        return False

    print(f"  🤖 음성 인식 중...")
    try:
        result = transcribe(audio_path, model, language)
    except Exception as e:
        print(f"  ❌ 인식 오류: {e}")
        if audio_path.exists():
            audio_path.unlink()
        return False

    # 결과 저장
    save_text(result["text"], txt_path)
    if save_srt_flag:
        save_srt(result["segments"], srt_path)

    # 임시 오디오 삭제
    if not keep_audio and audio_path.exists():
        audio_path.unlink()

    elapsed = time.time() - start_time
    detected_lang = result.get("language", "?")
    print(f"  ✅ 완료 ({elapsed:.0f}초) | 언어: {detected_lang} | 저장: {txt_path.name}")
    if save_srt_flag:
        print(f"           SRT 자막: {srt_path.name}")
    return True


def find_videos(folder: Path) -> list[Path]:
    """폴더에서 모든 동영상 파일 찾기"""
    extensions = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
    videos = []
    for ext in extensions:
        videos.extend(folder.rglob(f"*{ext}"))
    return sorted(videos)


def main():
    parser = argparse.ArgumentParser(
        description="동영상 음성을 텍스트로 변환 (로컬 Whisper, 완전 무료)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
모델 비교 (RTX 5080 기준):
  turbo    → 권장! 처리속도 약 8배 (실시간 대비), 정확도 large-v3 수준
  large-v3 → 최고 정확도, 처리속도 약 1-2배
  medium   → 빠름, 한국어 정확도 좋음
  small    → 더 빠름, 일반 강의 충분

예시:
  # 단일 파일 (권장: turbo 모델)
  python src/transcribe_video.py "파이썬 활용 인공지능 이론과 실습 1차.mp4" --model turbo

  # 폴더 전체 일괄 처리
  python src/transcribe_video.py "C:\\Users\\User\\Videos" --batch --model turbo

  # SRT 자막도 함께 생성
  python src/transcribe_video.py "..." --model turbo --srt
        """
    )
    parser.add_argument("input", help="동영상 파일 또는 폴더 경로")
    parser.add_argument(
        "--model", "-m",
        choices=list(MODEL_INFO.keys()),
        default="turbo",
        help="Whisper 모델 (기본값: turbo)"
    )
    parser.add_argument(
        "--language", "-l",
        default="ko",
        help="언어 코드 (기본값: ko / 자동감지: auto)"
    )
    parser.add_argument(
        "--batch", "-b",
        action="store_true",
        help="폴더 내 모든 동영상 일괄 처리"
    )
    parser.add_argument(
        "--srt",
        action="store_true",
        help="SRT 자막 파일도 함께 생성"
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="결과 파일 저장 폴더 (기본값: 원본 파일과 같은 폴더)"
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="중간 WAV 파일 보존"
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else None

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────
    # 파일 목록 수집
    # ─────────────────────────────────────────────
    if args.batch:
        if not input_path.is_dir():
            print(f"❌ 폴더가 아닙니다: {input_path}")
            sys.exit(1)
        videos = find_videos(input_path)
        if not videos:
            print(f"❌ 동영상 파일을 찾을 수 없습니다: {input_path}")
            sys.exit(1)
    else:
        if not input_path.exists():
            print(f"❌ 파일을 찾을 수 없습니다: {input_path}")
            sys.exit(1)
        videos = [input_path]

    # ─────────────────────────────────────────────
    # 모델 로딩
    # ─────────────────────────────────────────────
    info = MODEL_INFO[args.model]
    print("=" * 60)
    print(f"🎙️  Whisper 로컬 음성 인식 (완전 무료, GPU 가속)")
    print(f"   모델: {args.model} ({info['size']}, VRAM {info['vram']}, 처리속도 {info['speed']})")
    print(f"   언어: {args.language}")
    print(f"   파일: {len(videos)}개")
    print("=" * 60)

    import whisper
    import torch

    if not torch.cuda.is_available():
        print("⚠️  GPU를 찾을 수 없습니다. CPU로 실행합니다 (매우 느림).")
        print("   CUDA 설치 여부를 확인하세요: https://developer.nvidia.com/cuda-downloads")
        device = "cpu"
    else:
        gpu_name = torch.cuda.get_device_name(0)
        vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
        print(f"✅ GPU: {gpu_name} ({vram_gb:.1f}GB VRAM)")
        device = "cuda"

    print(f"\n📥 모델 로딩 중: {args.model} (첫 실행 시 다운로드)")
    model = whisper.load_model(args.model, device=device)
    print("✅ 모델 로딩 완료\n")

    # ─────────────────────────────────────────────
    # 배치 처리
    # ─────────────────────────────────────────────
    success_count = 0
    fail_count = 0
    total_start = time.time()

    for i, video_path in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {video_path.name}")
        ok = process_single_file(
            video_path=video_path,
            model=model,
            language=args.language,
            output_dir=output_dir,
            save_srt_flag=args.srt,
            keep_audio=args.keep_audio,
        )
        if ok:
            success_count += 1
        else:
            fail_count += 1

        # 진행 상황 및 예상 잔여 시간
        if len(videos) > 1 and i < len(videos):
            elapsed = time.time() - total_start
            avg = elapsed / i
            remaining = avg * (len(videos) - i)
            h, m = divmod(int(remaining), 3600)
            m //= 60
            print(f"     ⏱️  예상 잔여 시간: {h}시간 {m:02d}분\n")

    # ─────────────────────────────────────────────
    # 최종 요약
    # ─────────────────────────────────────────────
    total_elapsed = time.time() - total_start
    th, tr = divmod(int(total_elapsed), 3600)
    tm = tr // 60
    print("\n" + "=" * 60)
    print(f"🎉 완료! 총 {th}시간 {tm:02d}분 소요")
    print(f"   ✅ 성공: {success_count}개  ❌ 실패: {fail_count}개")
    print("=" * 60)


if __name__ == "__main__":
    main()
