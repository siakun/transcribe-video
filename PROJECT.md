# PROJECT: transcribe-video 설계 참고 문서

이 문서는 `transcribe-video`의 구조와 설계 의도를 한곳에 모은 개발 참고 자료다.
코드를 읽기 전에 전체 그림을 잡거나, 변경 시 영향 범위를 가늠하는 용도로 쓴다.
파일 단위 세부보다 "왜 이렇게 되어 있는가"에 무게를 둔다.

관련 문서
- 루트 구조 재편 설계: [docs/superpowers/specs/2026-06-18-project-layout-restructure-design.md](docs/superpowers/specs/2026-06-18-project-layout-restructure-design.md)
- 소스 선택/유튜브 파이프라인 설계: [docs/superpowers/specs/2026-05-20-source-selection-design.md](docs/superpowers/specs/2026-05-20-source-selection-design.md)

## 1. 개요

로컬에서 도는 동영상 음성인식 도구다. faster-whisper(CTranslate2 백엔드)로 GPU
가속 전사를 수행하고, 결과를 원본 영상 옆에 `.txt`와 선택적 `.srt`로 저장한다.

두 가지 사용 진입점이 있다.

- **서버 + 브라우저 UI**: FastAPI 서버(`localhost:8765`)가 단일 `index.html`을
  제공하고, WebSocket으로 실시간 진행률을 스트리밍한다. 로컬 폴더 일괄 전사와
  유튜브 다운로드 전사를 지원한다.
- **CLI**: `transcribe_video.py`를 직접 실행해 단일 파일 또는 폴더를 배치 전사한다.

배포물은 PyInstaller로 만든 frozen exe(`whisper_server.exe`)다. 서버와 브라우저는
항상 같은 PC에서 돈다는 전제로 설계됐다(인증, 멀티유저, 원격 접근을 고려하지 않음).

## 2. 저장소 구조

repo 루트는 저장소 메타만 두고, 프로젝트 본체는 한 겹 아래 `transcribe-video/`에
모은다. .NET의 솔루션/프로젝트 2단계에 대응한다. 근거와 결정 과정은 구조 재편
spec 문서에 있다.

```
transcribe-video/            repo 루트 (저장소 메타)
├── README.md
├── PROJECT.md               이 문서
├── images/                  README용 이미지
├── docs/                    설계 문서, 개발 참고 자료
└── transcribe-video/        프로젝트 루트
    ├── pyproject.toml        의존성, pytest 설정
    ├── uv.lock               잠금 파일 (커밋 대상)
    ├── .venv/                가상환경 (gitignore)
    ├── src/                  소스 (flat 모듈)
    ├── tests/
    ├── scripts/              build.ps1, build.bat
    └── bin/                  yt-dlp.exe (vendored)
```

`uv`, `pytest`, 빌드는 모두 프로젝트 폴더(`transcribe-video/`)에서 실행한다.
`src/`는 패키지로 묶지 않은 flat 모듈 구성이며, 모듈 간 import는 같은 디렉터리
기준의 직접 import다(`import youtube`, `from audio_activity import ...`). 실행 시
스크립트 디렉터리(`src/`)가 `sys.path`에 들어가고, 테스트는 `pyproject.toml`의
`[tool.pytest.ini_options] pythonpath = ["src"]`로 같은 경로를 얻는다.

## 3. 아키텍처

```
            [브라우저: index.html]
                  |  HTTP / WebSocket
                  v
        [FastAPI 서버: server.py]
          |            |            |
   audio_activity   runtime_paths   youtube
   (음성 분석)      (경로 모델)     (yt-dlp 래퍼)
          |                            |
   faster-whisper                  yt-dlp.exe (subprocess)
   (+ torch, CUDA)
          |
        ffmpeg (subprocess, PATH)

        [CLI: transcribe_video.py]  --- audio_activity, runtime_paths 공유
```

핵심 의존 관계
- `server.py`와 `transcribe_video.py`는 서로를 import하지 않는 독립 진입점이다.
  공통 로직은 `audio_activity`(분석/필터)와 `runtime_paths`(경로)로 공유한다.
- 무거운 외부 도구는 pip 패키지가 아니라 subprocess로 호출한다: `ffmpeg`(오디오
  추출), `yt-dlp.exe`(유튜브), PowerShell(폴더 대화상자). 프로세스 분리 덕분에
  외부 도구가 실패해도 서버 프로세스 자체는 안전하다.

## 4. 모듈 책임

- **server.py**: FastAPI 앱, HTTP/WebSocket 엔드포인트, 모델 로딩/캐시, 파일 1개
  전사 오케스트레이션, 진행률 스트리밍. 로깅/진단 훅 설정도 여기서 한다.
- **transcribe_video.py**: CLI 진입점. argparse로 단일/배치 처리, 모델 정보 표,
  진행/요약 출력. 서버와 같은 분석/필터 로직을 쓴다.
- **audio_activity.py**: 음성 구간 분석(VAD 성격)과 전사 옵션 생성, 그리고 전사
  결과의 환각(hallucination) 필터링. 이 프로젝트에서 설계 밀도가 가장 높은 모듈.
- **youtube.py**: `yt-dlp.exe` 래퍼. URL 해석(단일/재생목록)과 영상 다운로드.
- **folder_dialog.py**: 서버측 네이티브 폴더 선택 대화상자(PowerShell + C# COM).
- **runtime_paths.py**: frozen/개발 환경에 따른 읽기 전용 리소스 기준점과 쓰기
  가능한 상태 디렉터리(logs/temp) 결정.
- **index.html**: 단일 파일 브라우저 UI. 서버 API와 WebSocket을 호출한다.

## 5. 전사 파이프라인

세 진입 경로(로컬 폴더 WS, 유튜브 WS, CLI)가 파일 1개 단위로는 같은 흐름을 탄다.

1. **건너뛰기 검사**: 같은 이름의 `.txt`가 이미 있으면 건너뛴다(중단 후 이어하기).
2. **오디오 추출**: ffmpeg로 16kHz 모노 WAV를 만든다. 서버 경로는 영상 파일을
   Python에서 열어 ffmpeg stdin(`pipe:0`)으로 흘려보낸다. ffmpeg가 한글 경로를
   못 여는 버그를 우회하기 위함이다.
3. **음성 구간 분석**: `analyze_audio_activity`가 스펙트럼 특징으로 음성 후보
   구간(regions)을 찾고, Whisper에 넘길 `clip_timestamps`를 만든다.
4. **전사**: `WhisperModel.transcribe`를 `build_transcribe_options`로 호출한다.
   반환된 segments는 generator라 반복이 실제 추론을 트리거한다. 서버는 이 반복을
   워커 스레드에서 돌리고 `asyncio.Queue`로 메인 코루틴에 넘겨 WebSocket으로
   진행률(`seg.end / info.duration`)을 실시간 전송한다.
5. **결과 정규화**: `normalize_transcription_result`가 환각 세그먼트를 거르고
   겹침을 제거한 뒤 본문 텍스트를 합친다.
6. **저장**: 원본 옆에 `.txt`(+ 선택 `.srt`)를 쓰고, 임시 WAV를 지운다.

음성 후보가 전혀 없으면 빈 결과를 저장하고 전사를 건너뛴다.

## 6. 음성 활동 분석과 환각 필터 (audio_activity)

이 모듈은 두 가지를 한다: 전사 전에 음성 구간을 추려 `clip_timestamps`로 넘기고,
전사 후에 환각 세그먼트를 거른다.

### 6.1 음성 구간 분석

0.5초 윈도로 오디오를 훑으며 윈도마다 다음 특징을 뽑는다.

- RMS dB(에너지), zero-crossing rate(ZCR)
- 스펙트럼 평탄도(flatness, 잡음일수록 높음)
- 음성 대역(120~4500Hz) 파워 비율, 저역(<120Hz) 파워 비율

10퍼센타일을 노이즈 플로어로 잡아 활동 임계값을 정하고, 대역 비율/저역 비율/
평탄도/ZCR 조건을 결합해 음성 윈도 마스크를 만든다. 마스크를 구간으로 묶을 때
최소 길이, 병합 간격, 패딩을 적용하고, 구간별로 대역/평탄도/피크/ZCR을 다시
검증해 잡음 구간을 떨군다.

`clip_timestamps`로 넘길 때 두 가지 안전장치가 있다.

- **무한 루프 방지**: 마지막 구간 end를 오디오 끝에서 `CLIP_SAFETY_MARGIN_SEC`
  (0.05초)만큼 당긴다. clip이 content_frames를 넘으면 Whisper가 무한 루프에
  빠지는 사례가 있기 때문이다.
- **이득 없으면 생략**: 음성 커버리지가 0.95 이상이면 clip 없이 전체를 처리한다.
  거의 전부가 음성이면 clip은 이득 없이 엣지케이스만 키운다.

### 6.2 환각 필터링 (다층)

Whisper는 무음/잡음 구간에서 그럴듯한 가짜 문장을 만든다. 이를 여러 층으로 막는다.

1. **Whisper 내장**: `word_timestamps=True` + `hallucination_silence_threshold=2.0`
   으로 언어 무관 환각 탐지를 켠다. `condition_on_previous_text=False`로 긴 파일의
   반복 루프를 줄인다.
2. **관측 문자열 정확 일치**: 실제로 본 환각 문구를 `OBSERVED_HALLUCINATIONS`
   집합에 그대로 모아 정확 일치로 막는다. 정규식으로 일반화하지 않는다. 관측되지
   않은 문구를 임의로 막으면 진짜 발화를 오삭제할 위험이 더 크기 때문이다. 새
   문구를 만날 때마다 문자열을 그대로 추가하는 방식이다.
3. **지표 기반**: `avg_logprob`, `no_speech_prob`, `compression_ratio`가 극단적으로
   나쁘거나, 긴 세그먼트인데 글자 밀도가 낮으면(전형적 "30초 창 + 짧은 환각 문장")
   버린다.
4. **반복 루프 탐지**: 같은 텍스트가 3회 이상 나오는데 반복 사이 간격이 모두 5초
   이상이면 환각 루프로 본다. 환각은 같은 입력에 같은 출력을 내는 결정론적 특성이
   있어 멀리 떨어져 반복된다. 진짜 발화 중 filler 반복과는 "간격이 모두 크다"로
   구분한다.
5. **겹침 제거**: 정렬 후 앞 세그먼트 end보다 시작이 이르면 잘라 겹침을 없앤다.

## 7. 서버 API 표면

HTTP
- `GET /`: `index.html` 반환.
- `GET /api/scan?folder=<경로>`: 폴더를 재귀 스캔해 영상 목록을 반환한다. 숫자
  자연 정렬(1강 < 2강 < ... < 10강)을 쓰고, 각 항목에 `.txt`/`.srt` 존재 여부를
  포함한다.
- `POST /api/cancel`: 진행 중인 작업에 취소 플래그를 세운다.
- `POST /api/pick-folder`: 네이티브 폴더 대화상자를 띄워 선택 절대경로를 반환한다.

WebSocket(메시지는 모두 JSON, `type` 필드로 구분)
- `/ws/transcribe`: 클라이언트가 `{files, model, language, srt}`를 보내면 파일들을
  순차 전사한다.
- `/ws/youtube`: `{url, folder, model, language, srt}`를 받아 yt-dlp로 해석/다운로드
  후 곧바로 전사한다. 재생목록이면 폴더 하위에 재생목록 제목 폴더를 만든다.

서버가 보내는 주요 메시지 타입: `start`, `file_start`, `progress`, `yt_resolve`,
`yt_download`, `log`, `file_done`, `file_error`, `cancelled`, `done`, `error`.

동시 실행은 전역 `is_running` 플래그로 1건만 허용한다. 취소는 `threading.Event`
(`cancel_flag`)로 전달한다.

## 8. 런타임 경로 모델 (runtime_paths)

frozen 여부에 따라 두 종류의 기준을 나눈다.

- **읽기 전용 리소스 기준(`runtime_base_dir`)**: frozen이면 exe가 놓인 디렉터리,
  개발이면 프로젝트 루트. 번들된 리소스(index.html, yt-dlp.exe 등)의 기준점이다.
- **쓰기 가능한 상태 디렉터리(`runtime_state_dir`)**: frozen이면 exe 옆을 건드리지
  않고 `LOCALAPPDATA/transcribe-video`(없으면 임시폴더)를 쓴다. 개발이면 프로젝트
  루트다. `logs/`, `temp/`가 이 아래에 생긴다.

`make_temp_audio_path`는 원본 파일명을 안전한 문자로 치환하고 UUID를 붙여 임시 WAV
경로를 만든다.

## 9. 빌드와 패키징

`scripts/build.ps1`이 PyInstaller onedir 빌드를 수행한다(`build.bat`은 더블클릭용
얇은 런처). 산출물은 `transcribe-video/build/<타임스탬프>/whisper_server/`에 생기고,
`build/run_latest.bat` 런처가 함께 만들어진다.

설계 요점
- **기준 경로**: build.ps1은 자신이 `scripts/` 안에 있다는 점을 반영해, 모든 상대
  경로를 스크립트 폴더의 부모(프로젝트 루트) 기준으로 해석한다.
- **번들 대상**: `index.html`과 `bin/yt-dlp.exe`를 add-data/add-binary로 함께
  넣는다. faster_whisper, ctranslate2, huggingface_hub, tokenizers, onnxruntime, av는
  collect-all, torch는 바이너리/데이터를 collect한다.
- **ffmpeg는 번들하지 않는다.** 실행 시점에 PATH에 있어야 한다.
- **yt-dlp.exe는 vendored 바이너리**다. pip 패키지가 아니라 단독 실행파일로 두어,
  유튜브가 깨질 때 whisper_server 재빌드 없이 exe만 교체할 수 있다.
- 첫 전사 때 Whisper 모델이 캐시에 없으면 다운로드가 일어날 수 있다.

### 9.1 사용자 진입점 (start)

루트의 `start.bat`/`start.ps1`이 사용자용 정문이다. 사용자는 리포지토리를 받은 뒤
`start.bat`만 실행하면 된다. `start.ps1`은 빌드 엔진(`scripts/build.ps1`) 위에서
"필요할 때만 빌드하고 실행"을 캡슐화한다.

- 소스 입력(`src/`, `pyproject.toml`, `uv.lock`, `scripts/build.ps1`)의 SHA256 내용
  지문을 계산해 마지막 빌드 기록(`build/.last_build.json`)과 비교한다. 같으면 마지막
  빌드를 그대로 실행하고(중복 빌드 방지), 다르거나 빌드가 없으면 새로 빌드한 뒤 실행한다.
- 타임스탬프나 git이 아니라 내용 해시를 쓴다. 다운로드 방식(ZIP 또는 clone)이나 파일
  수정시각에 흔들리지 않고 "코드 내용이 바뀌었는가"만 보기 위함이다.
- `.venv`가 없으면 `uv sync`로 환경을 먼저 준비하고, 서버 기동 후 포트가 열리면
  브라우저를 연다.
- `start.ps1`은 UTF-8 BOM으로 저장한다. Windows PowerShell 5.1은 BOM 없는 스크립트를
  시스템 코드페이지(한국어 Windows면 cp949)로 읽어 한글 문자열 리터럴이 깨진다.

## 10. 개발 워크플로

모든 명령은 프로젝트 폴더에서 실행한다.

```
cd transcribe-video
uv venv
uv sync --group dev
```

- 서버: `uv run python src/server.py` (브라우저에서 http://localhost:8765)
- CLI: `uv run python src/transcribe_video.py <영상 또는 폴더> [--batch] [--srt] [--model ...]`
- 테스트: `uv run pytest`

CLI 기본값은 `--model large-v3`, `--language ko`다. 모델별 크기/VRAM/속도는
`transcribe_video.py`의 `MODEL_INFO` 표를 참고한다.

## 11. 주요 설계 결정 (요약)

- **모델 캐시를 모듈 전역에 1개**: 함수 로컬에 두면 핸들러 리턴 시 GC가 CTranslate2
  모델을 해제하다가 Windows frozen 빌드에서 "Fatal Python error: Aborted"가 났다.
  전역 캐시로 이를 피하고 재로드 비용도 줄인다. 모델 키가 바뀌면 기존 캐시를 먼저
  비워 두 모델이 동시에 GPU 메모리를 점유하지 않게 한다.
- **OpenMP 환경변수 선설정**: faster-whisper(CTranslate2)와 torch가 각자 OpenMP
  런타임을 들고 있어, frozen에서 libomp 중복 로드 시 종료 시점 네이티브 abort가
  관측됐다. 진입 전에 `KMP_DUPLICATE_LIB_OK=TRUE`, `OMP_NUM_THREADS=1`로 고정한다.
- **한글 경로 대응**: 오디오 추출은 ffmpeg stdin 파이프로, yt-dlp 다운로드는
  `--encoding UTF-8`로 파이프 출력을 UTF-8에 맞춰 한글 경로 깨짐을 막는다.
- **환각 필터는 일반화하지 않는다**: 관측된 문구만 정확 일치로 막고, 원리 기반
  필터(지표/반복 루프)는 결정론적 특성에만 의존한다. 오삭제 위험을 일반화보다
  우선한 보수적 선택이다.
- **외부 도구는 subprocess로 격리**: ffmpeg, yt-dlp, 폴더 대화상자 모두 별도
  프로세스라 실패가 서버로 전파되지 않는다.

## 12. 외부 의존성

- **ffmpeg**: 런타임 PATH 필수. 오디오 추출에 쓴다.
- **yt-dlp.exe**: `bin/`에 vendored. 유튜브 해석/다운로드에 쓴다.
- **CUDA + torch**: GPU가 있으면 `cuda`/`float16`, 없으면 `cpu`/`int8`로 자동 전환.
  torch는 Windows에서 cu128 인덱스를 쓴다(`pyproject.toml` 참고).
- **PowerShell**: 폴더 선택 대화상자(Windows 전용).
