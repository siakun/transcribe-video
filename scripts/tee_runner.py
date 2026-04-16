"""서브프로세스 출력을 콘솔과 로그 파일 양쪽으로 흘려보내는 래퍼.

사용법:
    python scripts/tee_runner.py <log_path> <command> [args...]

종료 코드는 하위 프로세스의 exit code를 그대로 돌려준다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: tee_runner.py <log_path> <command> [args...]", file=sys.stderr)
        return 2

    log_path = Path(sys.argv[1])
    cmd = sys.argv[2:]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
        finally:
            proc.stdout.close()
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
