from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ANALYSIS_WINDOW_SEC = 0.5
MIN_SPEECH_REGION_SEC = 1.0
MERGE_GAP_SEC = 0.75
REGION_PADDING_SEC = 0.5
MIN_PARTIAL_WINDOW_RATIO = 0.35
# Whisper의 clip_timestamps가 content_frames를 넘으면 무한 루프가 발생하므로
# 마지막 region end를 오디오 끝에서 이 시간만큼 당겨둔다.
CLIP_SAFETY_MARGIN_SEC = 0.05
# 음성이 대부분이면 clip_timestamps로 얻는 이득이 없고 엣지케이스만 키우므로 생략한다.
CLIP_COVERAGE_SKIP_THRESHOLD = 0.95


@dataclass(frozen=True)
class AudioRegion:
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return max(self.end_sec - self.start_sec, 0.0)


@dataclass(frozen=True)
class AudioActivityReport:
    duration_sec: float
    window_sec: float
    noise_floor_db: float
    activity_threshold_db: float
    active_window_count: int
    speech_window_count: int
    noise_window_count: int
    regions: tuple[AudioRegion, ...]

    @property
    def speech_duration_sec(self) -> float:
        return round(sum(region.duration_sec for region in self.regions), 3)

    @property
    def noise_duration_sec(self) -> float:
        return round(self.noise_window_count * self.window_sec, 3)

    @property
    def leading_trim_sec(self) -> float:
        if not self.regions:
            return round(self.duration_sec, 3)
        return round(self.regions[0].start_sec, 3)

    @property
    def clip_timestamps(self) -> list[float]:
        timestamps: list[float] = []
        for region in self.regions:
            timestamps.extend([round(region.start_sec, 3), round(region.end_sec, 3)])
        return timestamps

    @property
    def speech_coverage(self) -> float:
        if self.duration_sec <= 0:
            return 0.0
        return self.speech_duration_sec / self.duration_sec

    def effective_clip_timestamps(
        self,
        *,
        coverage_skip_threshold: float = CLIP_COVERAGE_SKIP_THRESHOLD,
    ) -> list[float] | None:
        """Whisper에 안전하게 넘길 수 있는 clip_timestamps.

        - 음성이 전혀 없으면 None.
        - 음성이 거의 전부를 덮으면 None (clip 없이 전체 처리).
        - 그 외에는 region 목록을 그대로 반환한다.
        """
        if not self.regions:
            return None
        if self.speech_coverage >= coverage_skip_threshold:
            return None
        return self.clip_timestamps


def build_transcribe_options(
    *,
    language: str,
    fp16: bool,
    clip_timestamps: Iterable[float] | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "language": language if language != "auto" else None,
        "fp16": fp16,
        "verbose": False,
        # Disabling prompt carry reduces repetition loops on long files.
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.6,
        "compression_ratio_threshold": 2.4,
    }
    if clip_timestamps:
        options["clip_timestamps"] = list(clip_timestamps)
    return options


def empty_transcription_result(language: str) -> dict[str, Any]:
    detected = language if language != "auto" else "?"
    return {"text": "", "segments": [], "language": detected}


def normalize_transcription_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    segments: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for seg in result.get("segments", []):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        clean_seg = dict(seg)
        clean_seg["text"] = text
        segments.append(clean_seg)
        text_parts.append(text)

    normalized["segments"] = segments
    normalized["text"] = " ".join(text_parts).strip()
    if not normalized["text"]:
        normalized["text"] = str(result.get("text", "")).strip()
    return normalized


def format_activity_summary(report: AudioActivityReport) -> str:
    if not report.regions:
        return (
            "음성 패턴 없음 "
            f"(길이 {report.duration_sec:.1f}초, "
            f"비음성 잡음 {report.noise_duration_sec:.1f}초)"
        )

    summary = (
        f"음성 후보 {len(report.regions)}구간 / 총 {report.speech_duration_sec:.1f}초"
        f", 비음성 잡음 {report.noise_duration_sec:.1f}초"
    )
    if report.leading_trim_sec >= 2.0:
        summary += f", 앞 무음/잡음 {report.leading_trim_sec:.1f}초 제외"
    return summary


def analyze_audio_activity(
    audio_path: Path,
    *,
    window_sec: float = ANALYSIS_WINDOW_SEC,
    min_speech_region_sec: float = MIN_SPEECH_REGION_SEC,
    merge_gap_sec: float = MERGE_GAP_SEC,
    region_padding_sec: float = REGION_PADDING_SEC,
) -> AudioActivityReport:
    with wave.open(str(audio_path), "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        total_frames = wf.getnframes()

        if sample_width != 2:
            raise ValueError(f"Expected 16-bit PCM WAV, got sample width {sample_width}")

        duration_sec = total_frames / float(sample_rate) if sample_rate else 0.0
        window_samples = max(1, int(round(sample_rate * window_sec)))
        if window_samples <= 0:
            raise ValueError("window_sec produced an invalid window size")

        db_parts: list[np.ndarray] = []
        flatness_parts: list[np.ndarray] = []
        zcr_parts: list[np.ndarray] = []
        band_ratio_parts: list[np.ndarray] = []
        low_ratio_parts: list[np.ndarray] = []

        spectrum_window = np.hanning(window_samples).astype(np.float32)
        freqs = np.fft.rfftfreq(window_samples, d=1.0 / sample_rate)
        speech_band_mask = (freqs >= 120.0) & (freqs <= 4500.0)
        low_band_mask = freqs < 120.0

        carry = np.empty(0, dtype=np.float32)
        chunk_frames = window_samples * 120

        while True:
            raw = wf.readframes(chunk_frames)
            if not raw:
                break

            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if channels > 1:
                chunk = chunk.reshape(-1, channels).mean(axis=1)
            chunk /= 32768.0

            if carry.size:
                chunk = np.concatenate((carry, chunk))

            full_windows = chunk.size // window_samples
            if full_windows:
                windowed = chunk[: full_windows * window_samples].reshape(full_windows, window_samples)
                carry = chunk[full_windows * window_samples :]
                _append_window_features(
                    windowed,
                    spectrum_window,
                    speech_band_mask,
                    low_band_mask,
                    db_parts,
                    flatness_parts,
                    zcr_parts,
                    band_ratio_parts,
                    low_ratio_parts,
                )
            else:
                carry = chunk

        if carry.size >= int(window_samples * MIN_PARTIAL_WINDOW_RATIO):
            padded = np.pad(carry, (0, window_samples - carry.size))
            _append_window_features(
                padded.reshape(1, window_samples),
                spectrum_window,
                speech_band_mask,
                low_band_mask,
                db_parts,
                flatness_parts,
                zcr_parts,
                band_ratio_parts,
                low_ratio_parts,
            )

    if not db_parts:
        return AudioActivityReport(
            duration_sec=duration_sec,
            window_sec=window_sec,
            noise_floor_db=-120.0,
            activity_threshold_db=-52.0,
            active_window_count=0,
            speech_window_count=0,
            noise_window_count=0,
            regions=(),
        )

    db = np.concatenate(db_parts)
    flatness = np.concatenate(flatness_parts)
    zcr = np.concatenate(zcr_parts)
    band_ratio = np.concatenate(band_ratio_parts)
    low_ratio = np.concatenate(low_ratio_parts)

    noise_floor_db = float(np.percentile(db, 10))
    high_energy_db = float(np.percentile(db, 85))
    activity_threshold_db = max(min(noise_floor_db + 8.0, high_energy_db - 12.0), -52.0)

    active_mask = db >= activity_threshold_db
    speech_mask = (
        active_mask
        & (band_ratio >= 0.45)
        & (low_ratio <= 0.35)
        & (flatness <= 0.65)
        & ((flatness <= 0.25) | ((zcr >= 0.01) & (zcr <= 0.35)))
    )

    raw_regions = _find_regions(
        speech_mask,
        merge_gap_windows=max(1, int(round(merge_gap_sec / window_sec))),
    )
    min_region_windows = max(1, int(round(min_speech_region_sec / window_sec)))
    padding_windows = max(1, int(round(region_padding_sec / window_sec)))

    regions = _finalize_regions(
        raw_regions,
        db=db,
        flatness=flatness,
        band_ratio=band_ratio,
        zcr=zcr,
        min_region_windows=min_region_windows,
        padding_windows=padding_windows,
        total_windows=db.size,
        window_sec=window_sec,
        duration_sec=duration_sec,
        activity_threshold_db=activity_threshold_db,
    )

    return AudioActivityReport(
        duration_sec=duration_sec,
        window_sec=window_sec,
        noise_floor_db=noise_floor_db,
        activity_threshold_db=activity_threshold_db,
        active_window_count=int(active_mask.sum()),
        speech_window_count=int(speech_mask.sum()),
        noise_window_count=max(int(active_mask.sum() - speech_mask.sum()), 0),
        regions=tuple(regions),
    )


def _append_window_features(
    windowed: np.ndarray,
    spectrum_window: np.ndarray,
    speech_band_mask: np.ndarray,
    low_band_mask: np.ndarray,
    db_parts: list[np.ndarray],
    flatness_parts: list[np.ndarray],
    zcr_parts: list[np.ndarray],
    band_ratio_parts: list[np.ndarray],
    low_ratio_parts: list[np.ndarray],
) -> None:
    eps = 1e-12
    rms = np.sqrt(np.mean(windowed * windowed, axis=1) + eps)
    db_parts.append((20.0 * np.log10(rms + eps)).astype(np.float32))

    sign_changes = np.signbit(windowed[:, 1:]) != np.signbit(windowed[:, :-1])
    zcr_parts.append(np.mean(sign_changes, axis=1).astype(np.float32))

    spectrum = np.fft.rfft(windowed * spectrum_window, axis=1)
    power = (spectrum.real * spectrum.real + spectrum.imag * spectrum.imag).astype(np.float32)
    total_power = np.maximum(power.sum(axis=1), eps)
    band_ratio_parts.append((power[:, speech_band_mask].sum(axis=1) / total_power).astype(np.float32))
    low_ratio_parts.append((power[:, low_band_mask].sum(axis=1) / total_power).astype(np.float32))
    flatness_parts.append(
        (
            np.exp(np.mean(np.log(power + eps), axis=1))
            / np.maximum(np.mean(power + eps, axis=1), eps)
        ).astype(np.float32)
    )


def _find_regions(mask: np.ndarray, *, merge_gap_windows: int) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    gap = 0

    for idx, flag in enumerate(mask.tolist()):
        if flag:
            if start is None:
                start = idx
            end = idx + 1
            gap = 0
            continue

        if start is None:
            continue

        gap += 1
        if gap > merge_gap_windows:
            regions.append((start, end))
            start = None
            end = 0
            gap = 0

    if start is not None:
        regions.append((start, end))

    return regions


def _finalize_regions(
    raw_regions: list[tuple[int, int]],
    *,
    db: np.ndarray,
    flatness: np.ndarray,
    band_ratio: np.ndarray,
    zcr: np.ndarray,
    min_region_windows: int,
    padding_windows: int,
    total_windows: int,
    window_sec: float,
    duration_sec: float,
    activity_threshold_db: float,
) -> list[AudioRegion]:
    kept: list[tuple[int, int]] = []

    for start, end in raw_regions:
        if end - start < min_region_windows:
            continue

        region_band = float(np.mean(band_ratio[start:end]))
        region_flatness = float(np.mean(flatness[start:end]))
        region_peak_db = float(np.max(db[start:end]))
        region_median_zcr = float(np.median(zcr[start:end]))

        if region_band < 0.50:
            continue
        if region_flatness > 0.50:
            continue
        if region_peak_db < activity_threshold_db:
            continue
        if region_median_zcr < 0.005 or region_median_zcr > 0.42:
            continue

        kept.append((max(0, start - padding_windows), min(total_windows, end + padding_windows)))

    if not kept:
        return []

    merged: list[tuple[int, int]] = [kept[0]]
    for start, end in kept[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    max_sec = max(0.0, duration_sec - CLIP_SAFETY_MARGIN_SEC)
    finalized: list[AudioRegion] = []
    for start, end in merged:
        start_sec = min(start * window_sec, max_sec)
        end_sec = min(end * window_sec, max_sec)
        if end_sec - start_sec < window_sec:
            continue
        finalized.append(
            AudioRegion(
                start_sec=round(start_sec, 3),
                end_sec=round(end_sec, 3),
            )
        )
    return finalized
