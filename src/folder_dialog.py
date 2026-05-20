"""네이티브 폴더 선택 대화상자 (Windows).

브라우저는 보안상 OS 폴더 경로를 서버에 돌려주지 못하므로, 서버가 PowerShell로
폴더 대화상자를 띄워 선택된 절대경로를 받는다.

구식 WinForms FolderBrowserDialog는 트리뷰 모양이라, 파일 탐색기 형식의 모던
대화상자(IFileOpenDialog)를 쓴다. PowerShell(.NET Framework)에는 이 모던
폴더 피커가 기본 제공되지 않으므로, Add-Type으로 IFileOpenDialog를 직접
호출하는 C# 헬퍼를 컴파일해 쓴다. subprocess로 띄우므로 COM 오류가 나도
서버 프로세스는 안전하다.
"""
from __future__ import annotations

import subprocess

# STA 스레드에서 IFileOpenDialog(파일 탐색기 형식 폴더 피커)를 띄우고 선택 경로를
# stdout에 raw로 출력한다. 취소하면 빈 문자열. 한글 경로를 위해 출력 인코딩 UTF-8.
#
# C# 인터페이스 정의의 _VtblGapN_M 메서드는 CLR COM 상호운용이 인식하는
# "쓰지 않는 vtable 슬롯 M개 건너뛰기" 표식이다. 덕분에 IFileOpenDialog의
# 27개 메서드를 전부 선언하지 않고 실제 쓰는 4개만 올바른 슬롯에 맞춘다.
_PS_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class FolderPicker {
    public static string Pick() {
        try {
            var dialog = (IFileOpenDialog)new FileOpenDialogClass();
            uint opts;
            dialog.GetOptions(out opts);
            dialog.SetOptions(opts | 0x20u | 0x40u); // FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
            if (dialog.Show(IntPtr.Zero) != 0) return ""; // 취소 또는 오류
            IShellItem item;
            dialog.GetResult(out item);
            string path;
            item.GetDisplayName(0x80058000u, out path); // SIGDN_FILESYSPATH
            return path ?? "";
        } catch {
            return "";
        }
    }

    [ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
    private class FileOpenDialogClass { }

    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileOpenDialog {
        [PreserveSig] int Show(IntPtr parent);            // IModalWindow::Show
        void _VtblGap1_5();                               // SetFileTypes..Unadvise
        void SetOptions(uint fos);
        void GetOptions(out uint fos);
        void _VtblGap2_9();                               // SetDefaultFolder..SetFileNameLabel
        void GetResult(out IShellItem ppsi);
    }

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem {
        void _VtblGap1_2();                               // BindToHandler, GetParent
        void GetDisplayName(uint sigdnName,
                            [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
    }
}
'@
[Console]::Out.Write([FolderPicker]::Pick())
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
