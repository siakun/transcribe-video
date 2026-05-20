# 영상 소스 선택 개선 & 유튜브 파이프라인 — 설계

작성일: 2026-05-20

## 1. 배경

`transcribe-video`는 로컬 FastAPI 서버(`localhost:8765`)와 단일 `index.html`로 된
음성인식 앱이다. 서버와 브라우저는 항상 같은 PC에서 실행되며, PyInstaller로
exe 패키징된다.

현재 영상 소스 선택의 문제:

- 폴더 경로가 `index.html`에 하드코딩된 텍스트 입력칸 하나(`Z:\Library\Education\...`)뿐이다.
- 폴더를 바꾸려면 경로를 직접 타이핑해야 한다.
- 유튜브 영상은 다룰 수 없다.

## 2. 목표

1. 네이티브 탐색기 폴더 대화상자로 폴더를 쉽게 선택한다.
2. 경로 하드코딩을 없애고, 최근 사용 폴더를 브라우저 `localStorage`에 최신 순으로 저장·표시한다.
3. 유튜브 URL을 넣으면 영상을 다운로드하고 곧바로 whisper 전사까지 진행한다(단일 영상 + 재생목록).

## 3. 비목표 (YAGNI)

- 오디오만 다운로드 — 요청이 "동영상 다운로드"이므로 영상 파일을 그대로 보존한다.
- 유튜브 외 사이트.
- 로그인/쿠키가 필요한 비공개·멤버십 영상.

## 4. 통합 개념 — "작업 폴더"

세 기능은 **작업 폴더 하나**를 축으로 묶인다:

- 스캔 → 그 폴더의 로컬 영상 목록
- 유튜브 다운로드 → 그 폴더 안에 저장
- 전사 결과(.txt/.srt) → 영상 파일 옆에 생성

`index.html`의 폴더 입력칸 하나가 로컬 스캔 위치이자 유튜브 저장 위치다.

## 5. 기능 1 — 네이티브 폴더 선택

### UI

- 폴더 입력칸 옆에 **📁 버튼** 추가.

### 서버

- `POST /api/pick-folder` 신설.
- 서버가 PowerShell을 `-STA -NoProfile`로 실행해 `System.Windows.Forms.FolderBrowserDialog`를 띄운다.
- 선택 시 절대경로를, 취소 시 빈 문자열을 JSON으로 반환.
- 대화상자 호출은 블로킹이므로 `run_in_executor`로 이벤트 루프를 막지 않는다.
- 한글 폴더명이 깨지지 않도록 PowerShell 출력 인코딩과 Python 디코딩을 UTF-8로 고정한다.

### 프론트

- 📁 클릭 → `/api/pick-folder` 호출 → 경로를 받으면 입력칸 채움 + 최근 목록에 추가 + 스캔.
  빈 문자열(취소)이면 아무 동작 안 함.

### 방식 선택

PowerShell `FolderBrowserDialog`를 쓴다. Python GUI 의존성이 없어 PyInstaller
번들링 위험이 0이고, `powershell.exe`는 Windows에 항상 있다(앱은 이미 ffmpeg도
subprocess로 호출). 이 앱은 Windows 전용이라 플랫폼 제약은 문제되지 않는다.
대안인 `tkinter`(Tcl/Tk 번들 추가·패키징 위험)와 `ctypes` Win32 `IFileDialog`
(코드량 최다)는 채택하지 않는다.

## 6. 기능 2 — 최근 폴더 (localStorage)

- `index.html`의 하드코딩 `value="Z:\..."` 제거. 첫 실행 시 입력칸은 비어 있고 placeholder만 표시.
- `localStorage` 키 `transcribe.recentFolders` — 폴더 경로 문자열 배열, 최신 순, 중복 제거, 최대 8개.
- 스캔 성공 또는 폴더 선택 성공 시 해당 폴더를 목록 맨 앞으로 이동.
- 폴더 입력칸 아래에 **최근 폴더 드롭다운** — 각 항목 클릭 시 입력칸 채움 + 스캔,
  항목 우측 ✕로 개별 삭제.

## 7. 기능 3 — 유튜브 파이프라인

### UI

- 좌측 패널에 **유튜브** 섹션 신설: URL 입력칸 + "⬇ 받아서 전사" 버튼.
- 작업 폴더가 설정되지 않았으면 버튼은 비활성 또는 "먼저 폴더를 선택하세요" 안내.

### yt-dlp.exe

- 유튜브 다운로드는 **`yt-dlp.exe` 단독 실행파일**을 subprocess로 호출한다(ffmpeg와 동일한 패턴).
- pip 패키지 대신 exe를 쓰는 이유: 유튜브 변경으로 yt-dlp는 자주 깨지는데, exe는
  whisper_server 재빌드 없이 독립적으로 교체·자동업데이트할 수 있다. PyInstaller가
  yt-dlp의 lazy extractor를 번들링하는 위험도 피한다.
- `yt-dlp.exe`는 저장소 `bin/yt-dlp.exe`로 포함하고, `build.ps1`에서
  `--add-binary "bin\yt-dlp.exe;."`로 앱에 함께 배포한다.
- 서버는 frozen일 때 번들 경로, 개발 실행 시 저장소 `bin/`, 둘 다 없으면 PATH
  순으로 `yt-dlp.exe`를 찾는다.
- yt-dlp.exe는 스트림 병합에 ffmpeg를 쓰며, 앱은 이미 PATH의 ffmpeg를 요구하므로 추가 의존성 없음.

### 흐름 — `ws/youtube` WebSocket

1. URL 해석: `yt-dlp.exe --flat-playlist --dump-single-json <url>` → 단일 영상이면
   항목 1개, 재생목록이면 N개 + 재생목록 제목.
2. 재생목록이면 `작업폴더/<재생목록 제목>/` 하위 폴더 생성. 단일 영상이면 작업 폴더 루트에 저장.
3. 영상별로 **다운로드 → 곧바로 전사 → 다음 영상** 순서(인터리브). 중간에 실패해도 앞 영상 결과는 남는다.
4. 다운로드는 mp4로, 화질 상한 1080p(전사가 목적이므로 디스크 절약).
   `--no-overwrites`로 재실행 시 중복 다운로드 방지.
5. 전사는 기존 파이프라인 재사용(오디오 추출 → faster-whisper → .txt/.srt).
6. 진행률: 다운로드는 yt-dlp `--newline --progress-template` 출력을 파싱, 전사는 기존 progress 메시지.

### 진행 메시지 (WebSocket)

- 기존: `start`, `log`, `file_start`, `progress`, `file_done`, `file_error`, `done`, `cancelled`, `error`.
- 신설: `yt_resolve` {count, playlist_title?}, `yt_download` {idx, total, name, pct}.

### 오류 처리

- 잘못된 URL·삭제된 영상·비공개·연령제한: 콘솔에 오류 표시 후, 재생목록이면 다음
  영상으로, 단일 영상이면 중단.
- 네트워크 오류: 콘솔에 표시.
- 재생목록 제목·영상 제목에 Windows 금지문자(`\ / : * ? " < > |`)가 있으면 yt-dlp
  출력 템플릿(`--windows-filenames`)으로 안전하게 정리한다.
- 기존 `cancel_flag`를 유튜브 작업에도 적용 — 중지 시 현재 영상까지 마치고 멈춘다.

## 8. 아키텍처 / 컴포넌트

| 파일 | 변경 |
|---|---|
| `src/server.py` | `POST /api/pick-folder`, `ws/youtube` 추가. `ws_transcribe` 루프의 "파일 1개 전사 → txt/srt 저장" 본문을 `async def transcribe_file(websocket, ...)` 함수로 추출해 두 경로가 공유. |
| `src/youtube.py` (신규) | `yt-dlp.exe` 래퍼 — `resolve(url)`(JSON 해석), `download(entry, dest, progress_cb)`. yt-dlp.exe 경로 탐색 포함. |
| `src/folder_dialog.py` (신규) | PowerShell `FolderBrowserDialog` 래퍼 — `pick_folder() -> str`. |
| `src/index.html` | 📁 버튼, 최근 폴더 드롭다운, 유튜브 섹션 + JS(localStorage, ws/youtube 처리). |
| `build.ps1` | `--add-binary "bin\yt-dlp.exe;."` 추가. |
| `bin/yt-dlp.exe` (신규) | yt-dlp 공식 릴리스 실행파일을 저장소에 포함. |

`transcribe_file` 함수는 `websocket`, 파일 경로, 모델/언어/SRT 설정, 인덱스/총개수를
받아 추출·분석·전사·저장과 진행 메시지 전송을 담당한다. `ws_transcribe`(로컬)와
`ws/youtube`(다운로드 후) 양쪽이 호출한다. 모델 캐시 전역(`_cached_model`)은 그대로 둔다.

## 9. 데이터 흐름

**폴더 선택:** 📁 클릭 → `POST /api/pick-folder` → PowerShell 대화상자 → 경로 →
입력칸 + 최근목록 + 스캔.

**유튜브:** "받아서 전사" 클릭 → `ws/youtube` 연결 → `{url}` 전송 → 서버 해석
(`yt_resolve`) → 영상별 [다운로드(`yt_download`) → 전사(`file_start`/`progress`/
`file_done`)] → `done`.

## 10. 빌드 / 패키징

- `build.ps1`에 `--add-binary "bin\yt-dlp.exe;."` 추가 — yt-dlp.exe가 앱 디렉터리에 함께 배포된다.
- pip `yt-dlp` 의존성이나 `--collect-all yt_dlp`는 쓰지 않는다.
- frozen 빌드에서 yt-dlp.exe가 정상 실행되는지 검증한다.

## 11. 테스트 / 검증

- `youtube.py`의 `resolve()` JSON 파싱은 저장된 샘플 JSON으로 단위 테스트 가능.
- 폴더 대화상자는 상호작용이라 수동 검증.
- 최근 폴더 localStorage 로직은 소규모 JS — 수동 검증.
- 통합 검증(수동): 폴더 선택 → 스캔, 최근 폴더 재선택, 단일 영상 URL →
  다운로드+전사, 재생목록 URL → 하위폴더+일괄, 잘못된 URL 오류 처리,
  빌드된 exe에서 동일 동작.

## 12. 미해결 / 추후

- yt-dlp.exe 업데이트는 `yt-dlp.exe -U` 자동 업데이트 또는 수동 교체 —
  whisper_server 빌드와 무관하게 가능.
