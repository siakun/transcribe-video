# 프로젝트 루트 구조 재편: 설계

작성일: 2026-06-18

## 1. 배경

`transcribe-video` repo의 루트에 프로젝트 소스(`src/`, `tests/`, 빌드 스크립트)와
저장소 메타 문서(`docs/`, `images/`, `README.md`), 그리고 자동 생성물(`.venv/`,
`build/`, 캐시)이 한 층에 섞여 있다. git이 추적하는 항목 자체는 많지 않지만,
소스와 문서가 같은 층에 놓여 "무엇이 코드이고 무엇이 저장소 메타인지" 경계가
흐리다는 점이 정리의 동기다.

## 2. 목표

1. repo 루트를 "저장소 메타 층"으로, 그 아래 프로젝트 폴더를 "프로젝트 층"으로
   분리해, C#/.NET의 솔루션과 프로젝트 2단계 구조에 대응시킨다.
2. 소스, 테스트, 빌드 스크립트, 실행 의존 바이너리, 가상환경을 모두 프로젝트
   폴더 한 곳으로 모은다.
3. 저장소 메타(`README.md`, `docs/`, `images/`)는 루트에 남겨, 소스와의 혼재를
   해소한다.
4. 위 이동이 기존 동작(서버 기동, CLI, 빌드, 테스트, yt-dlp 탐색)을 깨지 않는다.

## 3. 비목표 (YAGNI)

- `src/` 내부를 named package(`src/transcribe_video/`)로 패키징하지 않는다. 이유는
  4절 결정 근거 참고. 실행/import 방식을 바꾸지 않는다.
- 빌드 산출물을 repo 밖으로 빼지 않는다. `build/`는 gitignore라 git 단위 뷰에
  애초에 잡히지 않으므로 프로젝트 폴더 안(`transcribe-video/build/`)에 둔다.
- `docs/`, `images/` 이동. 이 둘은 루트에 그대로 둔다.

## 4. 구조 결정과 근거

### 4.1 2단계 구조 채택

`pyproject.toml` 하나가 .NET의 솔루션(.sln)과 프로젝트(.csproj) 역할을 겸하므로,
Python 표준은 "repo 루트 = 프로젝트 루트"인 1단계다. 그러나 본 프로젝트는 시각적
경계 분리를 우선해, 의도적으로 2단계를 채택한다. 프로젝트 루트를 repo 아래
`transcribe-video/` 폴더로 한 겹 내린다.

채택에 따른 마찰: `uv`, `pytest`, 빌드는 `pyproject.toml`이 있는 프로젝트 폴더에서
실행해야 한다. 도구가 상위로만 설정을 탐색하기 때문이다. 이 마찰은
`.vscode/settings.json`으로 인터프리터를 프로젝트 폴더의 `.venv`로 지정해 완화한다.

### 4.2 src 내부는 flat 유지 (named package 미채택)

named package는 (a) PyPI 배포, (b) 외부 코드가 `import transcribe_video`로 사용,
(c) 여러 패키지 간 코드 공유 중 하나라도 있을 때 값을 한다. 본 프로젝트는 셋 다
아니고 `pyproject.toml`에 `package = false`이며 실제 배포물은 frozen exe다.

또한 현재 코드의 경로 로직이 전부 프로젝트 루트 기준 상대경로라, src를 한 겹
내려도 src와 bin, 프로젝트 루트의 상대 관계가 보존되어 코드가 깨지지 않는다.
flat 유지 시 src 안 코드는 한 줄도 바뀌지 않는다. 반대로 named package는 import
다수, 진입점, 빌드 진입점, 그리고 `parent.parent.parent` 형태의 깊이 하드코딩을
늘려 frozen 빌드의 버그 표면을 키운다. 따라서 flat이 비용 대비 우월하다.

프로젝트 폴더 `transcribe-video/` 자체가 .NET의 named project 단위 역할을 하므로,
그 안의 `src/`를 다시 패키지 이름으로 감싸면 이름 계층이 중복된다.

## 5. 목표 구조

```
transcribe-video/                 (repo 루트, 저장소 메타 층)
├── .gitignore                    프로젝트 폴더 아래 .venv, build 등을 그대로 무시
├── README.md                     새 구조와 실행법으로 갱신
├── images/                       README용, 그대로 유지
├── docs/                         개발 참고 자료, 그대로 유지
│   └── superpowers/
└── transcribe-video/            (프로젝트 루트, 프로젝트 층)
    ├── pyproject.toml            + [tool.pytest.ini_options] pythonpath = ["src"]
    ├── uv.lock                   이제 커밋 대상
    ├── .venv/                    gitignore, 프로젝트 폴더에서 재생성
    ├── src/                      소스, flat 유지 (내부 변경 없음)
    │   ├── server.py  transcribe_video.py  youtube.py
    │   ├── audio_activity.py  folder_dialog.py  runtime_paths.py
    │   └── index.html
    ├── tests/
    │   └── test_youtube.py
    ├── scripts/
    │   ├── build.ps1             루트 기준을 프로젝트 루트로 보정 (유일한 코드 수정)
    │   └── build.bat
    └── bin/
        └── yt-dlp.exe
```

루트 `.vscode/settings.json`(선택)도 추가해 repo를 열어도 인터프리터를 인식하게 한다.

## 6. 변경 항목과 영향

### 6.1 파일 이동 (git mv로 이력 보존)

- `src/`, `tests/`, `bin/`, `pyproject.toml` -> `transcribe-video/` 아래로
- `build.ps1`, `build.bat` -> `transcribe-video/scripts/` 아래로
- `uv.lock`은 현재 gitignore라 미추적이므로 일반 이동 후 git add (6.4 참고)

### 6.2 conftest.py 제거

루트 `conftest.py`는 `sys.path`에 `src`를 끼워넣는 역할만 한다. 이를
`pyproject.toml`의 선언적 설정으로 대체한다.

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
```

pytest 8을 쓰므로 `pythonpath` 옵션을 지원한다. conftest.py는 삭제한다.

### 6.3 build.ps1 루트 기준 보정 (유일한 소스측 코드 수정)

build.ps1이 `transcribe-video/scripts/`로 내려가면 `$PSScriptRoot`가 스크립트
폴더(`scripts/`)를 가리킨다. 스크립트가 쓰는 모든 상대경로(`src\server.py`,
`bin\yt-dlp.exe`, `.venv`, `build`, `temp`, `logs`)는 프로젝트 루트 기준이므로,
기준 변수를 스크립트 폴더의 부모로 보정한다.

```powershell
$ScriptRoot = Split-Path -Parent $PSScriptRoot   # = 프로젝트 루트
```

이후 모든 `Join-Path $ScriptRoot ...` 호출이 프로젝트 루트로 정확히 해석된다.
build.bat은 `%~dp0build.ps1`로 같은 폴더의 build.ps1을 부르므로 추가 수정이 없다.

### 6.4 .gitignore 조정

- `uv.lock` 줄을 제거한다. 애플리케이션은 잠금 파일을 커밋해 재현 가능한 빌드를
  보장하는 것이 표준이다.
- 나머지 무시 패턴(`.venv/`, `__pycache__/`, `build/`, `logs/` 등)은 선행 슬래시가
  없어 임의 깊이에 매칭되므로, 프로젝트 폴더 아래로 이동해도 그대로 적용된다.
  `.gitignore`는 repo 루트에 유지한다.

### 6.5 환경 재생성

`.venv/`는 내부에 절대경로가 박혀 있어 이동하면 깨질 수 있다. gitignore이고
`uv.lock`에서 재현 가능하므로 옮기지 않고 재생성한다.

- 루트의 `.venv/`, `build/`, `__pycache__/`, `.pytest_cache/`를 제거한다.
- 프로젝트 폴더에서 `uv venv` 후 `uv sync --group dev`로 환경을 다시 만든다.

### 6.6 README.md 갱신

새 구조 설명, 실행/빌드 명령(프로젝트 폴더에서 실행), `images/` 참조를 반영한다.
실행 예: `cd transcribe-video` 후 `uv run python src/server.py`.

## 7. 검증

이동과 수정 후 다음을 확인한다.

1. 테스트: 프로젝트 폴더에서 `uv run pytest`가 통과한다.
2. 서버: `uv run python src/server.py`로 기동되고 `localhost:8765`가 응답한다.
3. CLI: `uv run python src/transcribe_video.py <파일>`이 동작한다.
4. yt-dlp 탐색: 개발 실행에서 `bin/yt-dlp.exe`를 찾는다.
5. 빌드: `scripts/build.bat` 실행으로 `build/<stamp>/whisper_server/`에 exe가 생성된다.
6. git: 이동이 `git mv`로 기록되어 이력이 보존되고, `uv.lock`이 추적 대상이 된다.

## 8. 위험과 대응

- 빌드 경로 오류: 6.3 보정을 빼먹으면 빌드가 실패한다. 7-5로 즉시 검출한다.
- 실행 위치 혼동: 프로젝트 폴더 밖에서 `uv`/`pytest`를 돌리면 설정을 못 찾는다.
  README 명시와 `.vscode/settings.json`으로 완화한다.
- 잠금 파일 정책 변경: `uv.lock` 커밋 전환은 의도된 표준화이며, 되돌리려면
  `.gitignore`에 줄을 복원하면 된다.
