# transcribe-video

로컬 Whisper(faster-whisper) 기반 동영상 음성인식 도구. FastAPI 서버와 브라우저
UI(`localhost:8765`)로 로컬 폴더 또는 유튜브 영상을 전사한다.

![demo](images/demo.png)

## 구조

소스, 빌드 스크립트, 가상환경은 모두 `transcribe-video/` 프로젝트 폴더 아래에 둔다.
저장소 메타(README, 문서, 이미지)는 repo 루트에 둔다.

```
transcribe-video/        repo 루트
├── README.md
├── images/              README용 이미지
├── docs/                개발 참고 자료
└── transcribe-video/    프로젝트 (소스/빌드/venv)
    ├── pyproject.toml
    ├── uv.lock
    ├── src/             서버, CLI, 공용 모듈
    ├── tests/
    ├── scripts/         build.ps1, build.bat
    └── bin/             yt-dlp.exe
```

## 개발

모든 명령은 프로젝트 폴더에서 실행한다.

```
cd transcribe-video
uv venv
uv sync --group dev
```

- 서버: `uv run python src/server.py` (브라우저에서 http://localhost:8765 접속)
- CLI: `uv run python src/transcribe_video.py <영상 파일 또는 폴더>`
- 테스트: `uv run pytest`

## 빌드 (Windows, PyInstaller)

```
cd transcribe-video
scripts\build.bat
```

산출물은 `transcribe-video/build/<타임스탬프>/whisper_server/`에 생성된다. 실행 시점에
PATH에 `ffmpeg`가 설치되어 있어야 한다.
