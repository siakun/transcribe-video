# 영상 소스 선택 개선 & 유튜브 파이프라인 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 폴더를 네이티브 대화상자로 선택하고, 최근 폴더를 localStorage에 저장하며, 유튜브 URL을 받아 다운로드 후 자동 전사하는 기능을 추가한다.

**Architecture:** 로컬 FastAPI 서버 + 단일 `index.html`. 서버는 PowerShell(폴더 대화상자)과 `yt-dlp.exe`(유튜브)를 subprocess로 호출한다 — 기존 ffmpeg 호출과 동일한 패턴. 전사 로직은 `transcribe_file` 함수로 추출해 로컬 파일 경로와 유튜브 경로가 공유한다.

**Tech Stack:** Python 3.12, FastAPI, faster-whisper, PowerShell `FolderBrowserDialog`, `yt-dlp.exe` 단독 실행파일, PyInstaller.

**설계 문서:** `docs/superpowers/specs/2026-05-20-source-selection-design.md`

**테스트 방침:** 이 저장소에는 테스트 인프라가 없고, 기능 대부분이 UI·subprocess·네트워크에 묶여 있다. 따라서 대부분의 Task는 **수동 검증 단계**(서버 실행 → 동작 → 관찰)를 쓴다. 순수 로직인 `youtube.py`의 JSON 파싱만 pytest 단위 테스트를 둔다(Task 4).

---

## File Structure

| 파일 | 책임 |
|---|---|
| `src/folder_dialog.py` (신규) | PowerShell `FolderBrowserDialog`를 띄워 폴더 절대경로를 반환 |
| `src/youtube.py` (신규) | `yt-dlp.exe` 래퍼 — URL 해석(`resolve`)과 영상 다운로드(`download`) |
| `src/server.py` (수정) | `/api/pick-folder`·`/ws/youtube` 추가, 전사 로직을 `ensure_model`/`transcribe_file`로 추출 |
| `src/index.html` (수정) | 📁 버튼, 최근 폴더 드롭다운, 유튜브 섹션 + JS |
| `bin/yt-dlp.exe` (신규) | yt-dlp 공식 릴리스 실행파일(저장소에 포함) |
| `build.ps1` (수정) | `--add-binary`로 yt-dlp.exe를 앱에 번들 |
| `tests/test_youtube.py` (신규) | `youtube.py` 파싱 로직 단위 테스트 |
| `conftest.py` (신규) | pytest가 `src/`를 import 경로에 넣도록 함 |
| `pyproject.toml` (수정) | dev 의존성에 `pytest` 추가 |

---

## Task 1: 폴더 선택 대화상자 (`folder_dialog.py` + `/api/pick-folder`)

**Files:**
- Create: `src/folder_dialog.py`
- Modify: `src/server.py` (import 추가, 엔드포인트 추가)

- [ ] **Step 1: `src/folder_dialog.py` 생성**

```python
"""네이티브 폴더 선택 대화상자 (Windows).

브라우저는 보안상 OS 폴더 경로를 서버에 돌려주지 못하므로, 서버가 PowerShell로
System.Windows.Forms.FolderBrowserDialog를 띄워 선택된 절대경로를 받는다.
"""
from __future__ import annotations

import subprocess

# STA 스레드에서 폴더 대화상자를 띄우고 선택 경로를 stdout에 raw로 출력한다.
# 취소하면 아무것도 출력하지 않는다. 한글 경로를 위해 출력 인코딩을 UTF-8로 고정.
_PS_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select the folder with videos to transcribe'
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.SelectedPath)
}
"""


def pick_folder() -> str:
    """폴더 대화상자를 띄우고 선택된 절대경로를 반환한다. 취소 시 빈 문자열.

    블로킹 호출이므로 호출 측에서 run_in_executor 등으로 감싸야 한다.
    """
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command", _PS_SCRIPT,
            ],
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.decode("utf-8", errors="replace").strip()
```

- [ ] **Step 2: `src/server.py`에 import 추가**

`from runtime_paths import (...)` 블록(현재 25-30행) 바로 아래에 추가:

```python
from folder_dialog import pick_folder
```

- [ ] **Step 3: `src/server.py`에 엔드포인트 추가**

`/api/cancel` 엔드포인트(현재 224-229행) 바로 아래에 추가:

```python
# ─────────────────────────────────────────────
# 폴더 선택 대화상자 API
# ─────────────────────────────────────────────
@app.post("/api/pick-folder")
async def pick_folder_endpoint():
    """네이티브 폴더 대화상자를 띄우고 선택된 절대경로를 반환한다."""
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, pick_folder)
    return {"path": path}
```

- [ ] **Step 4: 수동 검증**

```
python src/server.py
```

다른 터미널에서:
```
curl -X POST http://localhost:8765/api/pick-folder
```

기대: 폴더 선택 대화상자가 화면에 뜬다. 폴더를 고르면 `{"path":"C:\\..."}`,
취소하면 `{"path":""}`. 한글 폴더명을 골라도 경로가 깨지지 않는지 확인.

- [ ] **Step 5: 커밋**

```bash
git add src/folder_dialog.py src/server.py
git commit -m "feat: 네이티브 폴더 선택 대화상자 API 추가"
```

커밋 메시지는 제목 + 한글 한 문단 설명 형식으로 작성한다(이하 모든 커밋 동일).

---

## Task 2: 폴더 선택 버튼 + 최근 폴더 (`index.html`)

**Files:**
- Modify: `src/index.html`

- [ ] **Step 1: CSS 추가**

`<style>` 안, `.folder-input:focus { ... }`(현재 75행) 바로 아래에 추가:

```css
  /* 최근 폴더 드롭다운 */
  .recent-box { margin-top: 6px; display: flex; flex-direction: column; gap: 2px; }
  .recent-item {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 8px; border-radius: 5px; cursor: pointer;
    font-size: 12px; color: #94a3b8;
  }
  .recent-item:hover { background: #1a1d2e; }
  .recent-path { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .recent-del { color: #64748b; flex-shrink: 0; padding: 0 4px; }
  .recent-del:hover { color: #f87171; }
```

- [ ] **Step 2: 폴더 입력 영역의 HTML 교체**

현재 폴더 섹션(현재 250-281행, `<div class="section">`부터 그 `</div>`까지)에서
`<input class="folder-input" ...>`의 하드코딩된 `value="Z:\..."`를 제거하고
📁 버튼과 최근 폴더 컨테이너를 추가한다. `.folder-row` 블록을 다음으로 교체:

```html
      <div class="folder-row">
        <input class="folder-input" id="folderPath" type="text"
          placeholder="폴더 경로를 입력하거나 📁로 선택" />
        <button class="btn btn-secondary" onclick="pickFolder()">📁</button>
        <button class="btn btn-secondary" onclick="scanFolder()">🔍 스캔</button>
      </div>
      <div class="recent-box" id="recentBox" style="display:none"></div>
```

`.settings-row` 블록(모델/언어/SRT)은 그대로 둔다.

- [ ] **Step 3: 최근 폴더 + 폴더 선택 JS 추가**

`<script>` 안, 상태 변수 블록(현재 329-333행 `let allFiles = []; ...`) 바로 아래에 추가:

```javascript
// ─────────────────────────────
// 최근 폴더 (localStorage)
// ─────────────────────────────
const RECENT_KEY = 'transcribe.recentFolders';
const RECENT_MAX = 8;

function loadRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; }
  catch { return []; }
}
function saveRecent(list) {
  localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
}
function addRecent(folder) {
  if (!folder) return;
  const list = loadRecent().filter(f => f !== folder);
  list.unshift(folder);
  saveRecent(list);
  renderRecent();
}
function removeRecent(folder) {
  saveRecent(loadRecent().filter(f => f !== folder));
  renderRecent();
}
function renderRecent() {
  const box = document.getElementById('recentBox');
  const list = loadRecent();
  if (!list.length) { box.innerHTML = ''; box.style.display = 'none'; return; }
  box.style.display = '';
  box.innerHTML = list.map(f => `
    <div class="recent-item">
      <span class="recent-path" title="${f}">${f}</span>
      <span class="recent-del" data-folder="${f}">✕</span>
    </div>`).join('');
  box.querySelectorAll('.recent-item').forEach(item => {
    const folder = item.querySelector('.recent-del').dataset.folder;
    item.querySelector('.recent-path').onclick = () => {
      document.getElementById('folderPath').value = folder;
      scanFolder();
    };
    item.querySelector('.recent-del').onclick = (e) => {
      e.stopPropagation();
      removeRecent(folder);
    };
  });
}

// ─────────────────────────────
// 폴더 선택 대화상자
// ─────────────────────────────
async function pickFolder() {
  let data;
  try {
    const res = await fetch('/api/pick-folder', { method: 'POST' });
    data = await res.json();
  } catch (e) {
    logMsg('❌ 폴더 대화상자 호출 실패', 'error');
    return;
  }
  if (data.path) {
    document.getElementById('folderPath').value = data.path;
    scanFolder();
  }
}
```

- [ ] **Step 4: `scanFolder()`가 성공 시 최근 목록에 추가하도록 수정**

현재 `scanFolder()`의 성공 처리부(현재 351-353행)에서 `allFiles = data.files;` 다음에
`addRecent(data.folder || folder);`를 추가:

```javascript
  allFiles = data.files;
  addRecent(data.folder || folder);
  renderFileList();
  logMsg(`✅ ${data.count}개 파일 발견 (완료: ${allFiles.filter(f=>f.done).length}개)`, 'success');
```

(`/api/scan` 응답의 `data.folder`는 정규화된 절대경로다. 없으면 입력값 `folder`를 쓴다.)

- [ ] **Step 5: 초기 로드 동작 수정**

현재 마지막 줄 `window.addEventListener('load', () => scanFolder());`을 다음으로 교체:

```javascript
// 초기 로드: 최근 폴더 렌더링, 가장 최근 폴더가 있으면 자동 스캔
window.addEventListener('load', () => {
  renderRecent();
  const recent = loadRecent();
  if (recent.length) {
    document.getElementById('folderPath').value = recent[0];
    scanFolder();
  }
});
```

- [ ] **Step 6: 수동 검증**

```
python src/server.py
```

브라우저 `http://localhost:8765`:
1. 첫 실행 — 입력칸 비어 있고 placeholder 표시, 최근 폴더 없음.
2. 📁 클릭 → 폴더 선택 → 입력칸 채워지고 스캔됨, 최근 폴더에 1개 생김.
3. 다른 폴더를 📁로 선택 → 최근 목록 맨 앞에 추가됨.
4. 페이지 새로고침 → 최근 목록 유지, 가장 최근 폴더 자동 스캔.
5. 최근 항목 클릭 → 그 폴더로 스캔. ✕ 클릭 → 항목 삭제.

- [ ] **Step 7: 커밋**

```bash
git add src/index.html
git commit -m "feat: 폴더 선택 버튼과 최근 폴더(localStorage) UI 추가"
```

---

## Task 3: 전사 로직을 `ensure_model` / `transcribe_file`로 추출 (리팩터링)

기존 `ws_transcribe` 핸들러는 모델 로딩과 파일별 전사를 인라인으로 한다. 유튜브
경로가 같은 전사 로직을 재사용하도록 두 함수로 추출한다. **동작 변화 없음** — 순수 추출.

**Files:**
- Modify: `src/server.py`

- [ ] **Step 1: `ensure_model` 함수 추가**

`_run_transcribe_with_progress` 함수 정의(현재 243행)의 바로 위에 추가. 본문은 현재
`ws_transcribe`의 모델 로딩 코드(현재 388-431행)를 그대로 옮긴 것이다:

```python
async def ensure_model(websocket: WebSocket, model_name: str,
                       loop: asyncio.AbstractEventLoop):
    """모델을 로딩(또는 캐시 재사용)하고 wmodel을 반환한다. 로딩 로그를 전송한다."""
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
```

- [ ] **Step 2: `transcribe_file` 함수 추가**

`ensure_model` 바로 아래에 추가. 본문은 현재 `ws_transcribe`의 for 루프 안쪽
(현재 440행 `p = Path(file_path)`부터 585행 `})` 까지 — `file_start` 전송, 이미
완료 시 건너뛰기, 오디오 추출, 음성 분석, 전사, txt/srt 저장, `file_done` 전송)을
그대로 옮긴 것이다. 루프 변수였던 `idx`/`len(files)`는 파라미터로 받는다:

```python
async def transcribe_file(websocket: WebSocket, wmodel, idx: int, total: int,
                          file_path, language: str, make_srt: bool,
                          loop: asyncio.AbstractEventLoop):
    """파일 1개를 전사하고 txt/srt를 저장한다. file_start ~ file_done 메시지 전송.

    호출 측에서 cancel_flag와 모델 로딩을 책임진다.
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
    # ... 현재 server.py 449-585행의 루프 본문을 그대로 옮긴다 ...
    # (이미 완료 건너뛰기 → 오디오 추출 → 음성 구간 분석 → 빈 결과 처리 →
    #  전사 → txt 저장 → srt 저장 → 임시파일 삭제 → file_done 전송)
    # 단, 옮길 때 `len(files)`는 `total`로, `idx`는 파라미터 그대로 사용한다.
    # `import time`은 위로 끌어올렸으므로 본문 안의 `import time`(현재 433행)은 제거.
```

> 구현 시: 현재 449-585행을 그대로 복사해 위 `# ...` 자리에 붙여넣고, 그 안의
> `len(files)` → `total` 치환만 한다. ffmpeg 추출·`analyze_audio_activity`·
> `_run_transcribe_with_progress` 호출·txt/srt 저장 로직은 변경하지 않는다.

- [ ] **Step 3: `ws_transcribe`를 추출한 함수 호출로 교체**

현재 `ws_transcribe`의 모델 로딩 + for 루프(현재 388-585행)를 다음으로 교체.
`start`/`done`/`cancelled` 전송과 `is_running`·`cancel_flag` 처리는 유지:

```python
        loop = asyncio.get_event_loop()
        wmodel = await ensure_model(websocket, model_name, loop)

        for idx, file_path in enumerate(files, 1):
            if cancel_flag.is_set():
                await websocket.send_json({"type": "cancelled", "msg": "사용자가 취소했습니다."})
                break
            await transcribe_file(
                websocket, wmodel, idx, len(files), file_path, language, make_srt, loop,
            )

        if not cancel_flag.is_set():
            await websocket.send_json({"type": "done", "msg": "모든 파일 처리 완료!"})
            LOGGER.info("세션 종료: 전체 파일 처리 완료 (%d개)", len(files))
        else:
            LOGGER.info("세션 종료: 사용자 취소")
```

(현재 386행 `await websocket.send_json({"type": "start", ...})`는 그대로 둔다.)

- [ ] **Step 4: 수동 검증 — 로컬 전사가 그대로 동작하는지**

```
python src/server.py
```

브라우저에서 영상이 든 폴더를 스캔 → 파일 1개 선택 → ▶ 변환 시작.
기대: 모델 로딩 로그 → 오디오 추출 → 진행바 → `.txt`/`.srt` 저장 → 완료.
리팩터링 전과 동일하게 동작해야 한다(콘솔 로그, 진행바, 배지 모두).

- [ ] **Step 5: 커밋**

```bash
git add src/server.py
git commit -m "refactor: 전사 로직을 ensure_model/transcribe_file 함수로 추출"
```

---

## Task 4: yt-dlp.exe 추가 + `youtube.py` + 단위 테스트

**Files:**
- Create: `bin/yt-dlp.exe`
- Create: `src/youtube.py`
- Create: `conftest.py`, `tests/test_youtube.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: yt-dlp.exe 내려받기**

저장소 루트에서:
```bash
mkdir bin
curl -L -o bin/yt-dlp.exe https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe
```

확인:
```bash
./bin/yt-dlp.exe --version
```
기대: 버전 문자열(예: `2026.05.xx`)이 출력된다.

- [ ] **Step 2: `src/youtube.py` 생성**

```python
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

    frozen 빌드: server.py와 같은 번들 디렉터리(_internal)에 함께 들어 있다.
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


def download(video_url: str, dest_dir: Path,
             progress_cb: Callable[[float], None]) -> Path:
    """영상을 dest_dir에 mp4로 다운로드한다. 다운로드된 파일 경로를 반환.

    progress_cb는 0~100 사이 퍼센트로 호출된다. 블로킹 함수다.
    """
    ytdlp = find_ytdlp()
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dest_dir / "%(title).180B [%(id)s].%(ext)s")
    cmd = [
        str(ytdlp),
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-overwrites",
        "--windows-filenames",
        "--newline",
        "--progress-template", "DLPCT %(progress._percent_str)s",
        "--print", "after_move:DLPATH %(filepath)s",
        "-o", out_template,
        video_url,
    ]
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
```

- [ ] **Step 3: `pyproject.toml`에 pytest 추가**

`[dependency-groups]`의 `dev` 리스트(현재 14-16행)를 다음으로 교체:

```toml
[dependency-groups]
dev = [
  "pyinstaller>=6.14,<7",
  "pytest>=8,<9",
]
```

그 다음:
```bash
uv sync --group dev
```

- [ ] **Step 4: `conftest.py` 생성 (저장소 루트)**

```python
import sys
from pathlib import Path

# 테스트가 src/ 의 모듈을 import할 수 있도록 경로 추가.
sys.path.insert(0, str(Path(__file__).parent / "src"))
```

- [ ] **Step 5: 실패하는 테스트 작성 — `tests/test_youtube.py`**

```python
from youtube import _parse_resolve_json, sanitize_filename


def test_sanitize_filename_replaces_reserved_chars():
    assert sanitize_filename('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"


def test_sanitize_filename_empty_fallback():
    assert sanitize_filename("   ") == "untitled"


def test_parse_single_video():
    data = {"id": "abc123", "title": "파이썬 강의 1강",
            "webpage_url": "https://www.youtube.com/watch?v=abc123"}
    result = _parse_resolve_json(data)
    assert result.is_playlist is False
    assert result.playlist_title == ""
    assert len(result.entries) == 1
    assert result.entries[0].id == "abc123"
    assert result.entries[0].title == "파이썬 강의 1강"
    assert result.entries[0].url == "https://www.youtube.com/watch?v=abc123"


def test_parse_playlist():
    data = {
        "_type": "playlist",
        "title": "파이썬: 기초/심화",
        "entries": [
            {"id": "v1", "title": "1강", "url": "https://youtube.com/watch?v=v1"},
            {"id": "v2", "title": "2강", "url": "https://youtube.com/watch?v=v2"},
            None,  # yt-dlp가 None 항목을 끼워넣는 경우
        ],
    }
    result = _parse_resolve_json(data)
    assert result.is_playlist is True
    assert result.playlist_title == "파이썬_ 기초_심화"  # : 와 / 가 _ 로 치환
    assert len(result.entries) == 2
    assert [e.id for e in result.entries] == ["v1", "v2"]
```

- [ ] **Step 6: 테스트 실행 — 실패 확인**

Run: `uv run pytest tests/test_youtube.py -v`
Expected: import 단계 통과 시 모두 PASS여야 정상이다(구현이 Step 2에서 이미 완료됨).
만약 FAIL이면 `youtube.py`를 수정한다. 이 Task는 구현과 테스트를 함께 두므로,
Step 2의 코드가 위 테스트를 통과하는지 확인하는 것이 목적이다.

- [ ] **Step 7: 테스트 통과 확인**

Run: `uv run pytest tests/test_youtube.py -v`
Expected: `4 passed`

- [ ] **Step 8: 실제 yt-dlp 동작 수동 확인**

짧은 유튜브 영상으로 `resolve`/`download`를 직접 확인:
```bash
uv run python -c "import sys; sys.path.insert(0,'src'); import youtube; r=youtube.resolve('https://www.youtube.com/watch?v=<짧은영상ID>'); print(r.is_playlist, r.entries[0].title)"
```
기대: `False <영상 제목>`.

> yt-dlp의 CLI 플래그(`--progress-template`, `--print after_move:`,
> 포맷 셀렉터)는 버전에 따라 다를 수 있다. 이 단계에서 `download`가 실제로
> mp4를 받고 경로를 반환하는지 확인하고, 어긋나면 `bin/yt-dlp.exe --help`로
> 플래그를 맞춘다.

- [ ] **Step 9: 커밋**

```bash
git add bin/yt-dlp.exe src/youtube.py conftest.py tests/test_youtube.py pyproject.toml uv.lock
git commit -m "feat: yt-dlp.exe 기반 유튜브 다운로드 모듈 추가"
```

(`uv.lock`은 .gitignore 대상이므로 실제로는 스테이징되지 않는다 — 경고가 나면 무시.)

---

## Task 5: `/ws/youtube` 엔드포인트 (`server.py`)

**Files:**
- Modify: `src/server.py`

- [ ] **Step 1: import 추가**

`from folder_dialog import pick_folder`(Task 1에서 추가) 아래에:

```python
import youtube
```

- [ ] **Step 2: `/ws/youtube` 핸들러 추가**

`ws_transcribe` 핸들러 정의가 끝난 직후(현재 파일 기준 600행 부근, `finally` 블록
다음)에 추가:

```python
# ─────────────────────────────────────────────
# WebSocket - 유튜브 다운로드 + 전사
# ─────────────────────────────────────────────
@app.websocket("/ws/youtube")
async def ws_youtube(websocket: WebSocket):
    global is_running
    await websocket.accept()

    try:
        data = await websocket.receive_json()
        url: str = (data.get("url") or "").strip()
        dest_folder: str = (data.get("folder") or "").strip()
        model_name: str = data.get("model", "turbo")
        language: str = data.get("language", "ko")
        make_srt: bool = data.get("srt", True)

        if not url:
            await websocket.send_json({"type": "error", "msg": "유튜브 URL이 비었습니다."})
            return
        if not dest_folder:
            await websocket.send_json({"type": "error", "msg": "먼저 폴더를 선택하세요."})
            return
        if is_running:
            await websocket.send_json({"type": "error", "msg": "이미 변환이 실행 중입니다."})
            return

        is_running = True
        cancel_flag.clear()
        loop = asyncio.get_event_loop()

        # URL 해석
        await websocket.send_json({"type": "log", "msg": "[유튜브] URL 해석 중..."})
        try:
            resolved = await loop.run_in_executor(None, lambda: youtube.resolve(url))
        except youtube.YoutubeError as e:
            await websocket.send_json({"type": "error", "msg": str(e)})
            return

        dest = Path(dest_folder)
        if resolved.is_playlist:
            dest = dest / resolved.playlist_title
        await websocket.send_json({
            "type": "yt_resolve",
            "count": len(resolved.entries),
            "playlist_title": resolved.playlist_title,
        })
        await websocket.send_json({"type": "start", "total": len(resolved.entries)})

        wmodel = await ensure_model(websocket, model_name, loop)

        total = len(resolved.entries)
        for idx, entry in enumerate(resolved.entries, 1):
            if cancel_flag.is_set():
                await websocket.send_json({"type": "cancelled", "msg": "사용자가 취소했습니다."})
                break

            await websocket.send_json({"type": "log", "msg": f"\n[{idx}/{total}] ⬇ {entry.title}"})

            # 다운로드 — 진행률은 큐를 통해 메인 코루틴으로 넘긴다.
            q: asyncio.Queue = asyncio.Queue()

            def progress_cb(pct: float):
                loop.call_soon_threadsafe(q.put_nowait, pct)

            fut = loop.run_in_executor(
                None, lambda e=entry: youtube.download(e.url, dest, progress_cb),
            )
            while not fut.done() or not q.empty():
                try:
                    pct = await asyncio.wait_for(q.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    continue
                await websocket.send_json({
                    "type": "yt_download",
                    "idx": idx, "total": total,
                    "name": entry.title, "pct": pct,
                })
            try:
                video_path = await fut
            except youtube.YoutubeError as e:
                await websocket.send_json({"type": "log", "msg": f"  ❌ 다운로드 실패: {e}"})
                await websocket.send_json({"type": "file_error", "idx": idx, "name": entry.title})
                continue

            await websocket.send_json({"type": "log", "msg": "  ✓ 다운로드 완료, 전사 시작"})
            # 전사 — 로컬 파일과 동일한 경로 재사용
            await transcribe_file(
                websocket, wmodel, idx, total, video_path, language, make_srt, loop,
            )

        if not cancel_flag.is_set():
            await websocket.send_json({"type": "done", "msg": "유튜브 다운로드 + 전사 완료!"})
            LOGGER.info("유튜브 세션 종료: %d개 처리", total)
        else:
            LOGGER.info("유튜브 세션 종료: 사용자 취소")

    except WebSocketDisconnect:
        LOGGER.info("유튜브 세션 종료: WebSocket 연결 끊김")
    except Exception as e:
        LOGGER.exception("ws_youtube failed")
        await websocket.send_json({"type": "error", "msg": str(e)})
    finally:
        is_running = False
        cancel_flag.clear()
```

- [ ] **Step 3: 구문 검증**

Run: `uv run python -c "import sys; sys.path.insert(0,'src'); import server"`
Expected: import 오류 없이 끝난다(서버는 뜨지 않음 — `__main__`이 아니므로).

- [ ] **Step 4: 커밋**

```bash
git add src/server.py
git commit -m "feat: 유튜브 다운로드+전사 WebSocket 엔드포인트 추가"
```

(엔드투엔드 동작 검증은 Task 6의 UI까지 끝난 뒤 수행한다.)

---

## Task 6: 유튜브 섹션 UI (`index.html`)

**Files:**
- Modify: `src/index.html`

- [ ] **Step 1: 유튜브 섹션 HTML 추가**

폴더 섹션 `<div class="section">...</div>`이 끝난 직후(파일 목록 헤더
`<div class="file-list-header">` 바로 위)에 새 섹션을 추가:

```html
    <!-- 유튜브 -->
    <div class="section">
      <div class="section-title">유튜브</div>
      <div class="folder-row">
        <input class="folder-input" id="ytUrl" type="text"
          placeholder="유튜브 영상 또는 재생목록 URL" />
        <button class="btn btn-primary" id="btnYoutube" onclick="startYoutube()">⬇ 받아서 전사</button>
      </div>
    </div>
```

- [ ] **Step 2: 유튜브 JS 추가**

`<script>` 안, `startTranscribe()` 함수 정의 바로 위에 추가:

```javascript
// ─────────────────────────────
// 유튜브 다운로드 + 전사
// ─────────────────────────────
function startYoutube() {
  const url = document.getElementById('ytUrl').value.trim();
  if (!url) return;
  const folder = document.getElementById('folderPath').value.trim();
  if (!folder) {
    logMsg('❌ 먼저 폴더를 선택하세요 (📁).', 'error');
    return;
  }

  const model = document.getElementById('modelSelect').value;
  const lang  = document.getElementById('langSelect').value;
  const srt   = document.getElementById('srtCheck').checked;

  totalFiles = 0;
  doneFiles  = 0;
  sessionFinished = false;
  updateProgress(0, 0);

  setRunning(true);
  logMsg(`▶ 유튜브 다운로드 + 전사 시작`, 'file');
  logMsg(`  URL: ${url}`, 'info');

  ws = new WebSocket(`ws://${location.host}/ws/youtube`);

  ws.onopen = () => {
    document.getElementById('statusDot').classList.add('active');
    ws.send(JSON.stringify({ url, folder, model, language: lang, srt }));
  };
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  ws.onclose = () => {
    document.getElementById('statusDot').classList.remove('active');
    setRunning(false);
  };
  ws.onerror = () => {
    logMsg('❌ WebSocket 연결 오류', 'error');
    setRunning(false);
  };
}
```

- [ ] **Step 3: `handleMessage`에 유튜브 메시지 처리 추가**

`handleMessage`의 `switch (msg.type)` 안, `case 'progress':` 블록 앞에 두 case 추가:

```javascript
    case 'yt_resolve':
      logMsg(
        `📋 ${msg.count}개 영상` +
        (msg.playlist_title ? ` (재생목록: ${msg.playlist_title})` : ''),
        'success');
      totalFiles = msg.count;
      doneFiles = 0;
      updateProgress(0, totalFiles);
      break;

    case 'yt_download': {
      const cDl = document.getElementById('console');
      let dEl = document.getElementById('curr-progress');
      if (!dEl) {
        dEl = document.createElement('div');
        dEl.id = 'curr-progress';
        dEl.className = 'log-progress-bar';
        dEl.innerHTML = '<div class="log-progress-bar-fill"></div><div class="log-progress-bar-text"></div>';
        cDl.appendChild(dEl);
      }
      const dpct = Math.min(100, Math.max(0, msg.pct || 0));
      dEl.querySelector('.log-progress-bar-fill').style.width = dpct + '%';
      dEl.querySelector('.log-progress-bar-text').textContent =
        `⬇ ${msg.name}  ${dpct.toFixed(1)}%`;
      break;
    }
```

(`file_start`/`progress`/`file_done`/`done`/`cancelled`/`error` case는 로컬 전사와
공유되므로 추가 작업 없음. `yt_download`는 다운로드가 끝나고 `file_start`가 오면
기존 로직대로 `curr-progress` id가 해제되어 줄바꿈된다.)

- [ ] **Step 4: 수동 검증 — 단일 영상**

```
python src/server.py
```

브라우저에서 폴더 선택(📁) → 유튜브 입력칸에 짧은 단일 영상 URL → ⬇ 받아서 전사.
기대: `URL 해석 중` → `1개 영상` → 다운로드 진행바 → `다운로드 완료, 전사 시작`
→ 전사 진행바 → `.txt`/`.srt`가 작업 폴더에 생성 → `완료`.

- [ ] **Step 5: 수동 검증 — 재생목록**

짧은 재생목록 URL로 동일 동작. 기대: `N개 영상 (재생목록: 제목)` → `작업폴더/제목/`
하위 폴더가 생기고 그 안에 영상별 mp4 + txt/srt가 쌓인다. 영상별로 다운로드 →
전사가 순차 진행된다. 중간에 ⏹ 중지를 누르면 현재 영상까지 마치고 멈춘다.

- [ ] **Step 6: 수동 검증 — 오류 처리**

잘못된 URL을 넣고 ⬇ → 콘솔에 `URL 해석 실패` 오류가 뜨고 실행 상태가 풀리는지 확인.

- [ ] **Step 7: 커밋**

```bash
git add src/index.html
git commit -m "feat: 유튜브 URL 입력 섹션과 다운로드 진행 UI 추가"
```

---

## Task 7: 빌드 통합 (`build.ps1`)

**Files:**
- Modify: `build.ps1`

- [ ] **Step 1: `--add-binary`로 yt-dlp.exe 번들**

`build.ps1`의 `$pyiArgs` 배열에서 `--add-data` 줄
(`'--add-data', "$IndexHtmlAbs;.",`) 바로 아래에 추가:

```powershell
        '--add-binary', "$ScriptRoot\bin\yt-dlp.exe;.",
```

(`$ScriptRoot`는 build.ps1 상단에서 이미 정의돼 있다 — 저장소 루트.)

- [ ] **Step 2: 빌드 실행**

```
.\build.bat
```
기대: `[2/2] Build complete.` — 빌드 성공, temp 정리 프롬프트 없이 종료.

- [ ] **Step 3: 빌드된 exe 수동 검증**

`build\<타임스탬프>\whisper_server\whisper_server.exe` 실행 →
브라우저 `http://localhost:8765`:
1. 📁 폴더 선택 동작.
2. 최근 폴더 저장·표시.
3. 짧은 유튜브 단일 영상 URL → 다운로드 + 전사 완료.

기대: 개발 실행(`python src/server.py`)과 동일하게 동작. yt-dlp.exe가 번들에서
정상 실행되는지가 핵심 확인 포인트.

- [ ] **Step 4: 커밋**

```bash
git add build.ps1
git commit -m "build: yt-dlp.exe를 PyInstaller 번들에 포함"
```

---

## Self-Review

**Spec 커버리지 (spec §2 목표 대비):**
- 목표 1(네이티브 폴더 선택) → Task 1, 2 ✓
- 목표 2(하드코딩 제거 + 최근 폴더 localStorage) → Task 2 ✓
- 목표 3(유튜브 다운로드 후 자동 전사, 단일+재생목록) → Task 4, 5, 6 ✓
- spec §8 `transcribe_file` 추출 → Task 3 ✓
- spec §10 빌드(`--add-binary`) → Task 7 ✓

**타입 일관성:** `VideoEntry`/`ResolveResult`/`YoutubeError`는 Task 4에서 정의하고
Task 5에서 `youtube.resolve`/`youtube.download`/`youtube.YoutubeError`로 사용 — 일치.
`ensure_model`/`transcribe_file` 시그니처는 Task 3에서 정의하고 Task 5에서 동일하게
호출 — 일치. WebSocket 메시지 타입(`yt_resolve`/`yt_download`)은 Task 5(서버 전송)와
Task 6(클라이언트 수신)에서 동일.

**알려진 검증 포인트(구현 중 확인 필요):**
- `folder_dialog.py`의 PowerShell `[Console]::OutputEncoding`/`Out.Write` 조합이
  한글 경로를 깨지지 않게 반환하는지 (Task 1 Step 4).
- `youtube.py`의 yt-dlp CLI 플래그(`--progress-template`, `--print after_move:`,
  포맷 셀렉터)가 내려받은 yt-dlp.exe 버전과 맞는지 (Task 4 Step 8).
