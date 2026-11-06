"""OD-25（2026-10-27）：`scripts/rust/win-startup-100.ps1` 只做隔離環境的回歸測試。

規格依據 serialwrap-rust-spec-final.md:132「連續 100 次 CLI 執行，不因 CLI 啟動新增解壓執行檔或解壓目錄」，
判定對象是 CLI 造成的新增：
- 100 次執行的子行程 TEMP／TMP／LOCALAPPDATA 指向本次專用、執行前為空的隔離目錄；
  隔離目錄內出現任何新增項目（檔案或目錄，不限副檔名）即 FAIL。
- exe 所在目錄出現任何新增目錄／exe／dll 即 FAIL（既有條件）。
- 真實 %LOCALAPPDATA%（含其下 %TEMP%）只記錄於 ambient_changes，不參與 ok 判定。
- 100 次 exit 0 且 stdout 一致的既有條件不變。

以 mingw 編譯的假 CLI 驅動；腳本經 WSL interop 的 powershell.exe 原生執行。為了不碰使用者真實的
%LOCALAPPDATA%，呼叫腳本前把 powershell 行程的 LOCALAPPDATA／TEMP／TMP 指向本測試專用的 Windows 本機目錄，
該目錄即腳本眼中的「真實」環境（ambient）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "win-startup-100.ps1"

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.process_rpc,
    pytest.mark.skipif(
        shutil.which("powershell.exe") is None or shutil.which("wslpath") is None
        or shutil.which("x86_64-w64-mingw32-gcc") is None,
        reason="需要 WSL interop（powershell.exe／wslpath）與 mingw（證據不足，非 PASS）",
    ),
]

FAKE_CLI_C = r"""
#include <windows.h>
#include <stdio.h>

int main(void) {
    char tmp[MAX_PATH];
    char path[MAX_PATH];
    GetTempPathA(MAX_PATH, tmp);
#ifdef SW_TEMP_EXTRACT_DIR
    /* 模擬 onefile 解壓器：在 %TEMP% 建立 _MEI<pid> 目錄並放入 dll */
    snprintf(path, sizeof path, "%s_MEI%lu", tmp, (unsigned long)GetCurrentProcessId());
    CreateDirectoryA(path, NULL);
    snprintf(path, sizeof path, "%s_MEI%lu\\python312.dll", tmp, (unsigned long)GetCurrentProcessId());
    CloseHandle(CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL));
#endif
#ifdef SW_TEMP_PLAIN_FILE
    /* 隔離目錄內任何新增（不限副檔名）都算 */
    snprintf(path, sizeof path, "%ssw-cache.dat", tmp);
    CloseHandle(CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL));
#endif
#ifdef SW_LOCALAPPDATA_DIR
    {
        DWORD n = GetEnvironmentVariableA("LOCALAPPDATA", path, MAX_PATH);
        if (n > 0 && n < MAX_PATH - 16) {
            lstrcatA(path, "\\sw-unpack");
            CreateDirectoryA(path, NULL);
        }
    }
#endif
#ifdef SW_AMBIENT_DIR
    /* 模擬隔離外的其他桌面程式：在真實 LOCALAPPDATA 建立目錄（絕對路徑於編譯時嵌入） */
    CreateDirectoryA(SW_AMBIENT_DIR, NULL);
#endif
#ifdef SW_EXE_DIR_EXTRACT
    {
        DWORD n = GetModuleFileNameA(NULL, path, MAX_PATH);
        char *slash = strrchr(path, '\\');
        if (n > 0 && slash != NULL) {
            lstrcpyA(slash, "\\_unpacked");
            CreateDirectoryA(path, NULL);
        }
    }
#endif
#ifdef SW_VARYING_STDOUT
    printf("serialwrap-fake 0.0.0 pid=%lu\n", (unsigned long)GetCurrentProcessId());
#else
    fputs("serialwrap-fake 0.0.0\n", stdout);
#endif
    return 0;
}
"""


def _wslpath(path: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=True).stdout.strip()


def _ps(command: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False)


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@pytest.fixture(scope="module")
def win_root() -> tuple[Path, str]:
    """Windows 本機（非 9P）測試根目錄：%LOCALAPPDATA%\\Temp\\sw-startup100-test-<uuid>。"""
    out = _ps("[System.IO.Path]::GetTempPath()", timeout=120)
    temp_win = out.stdout.strip()
    assert out.returncode == 0 and temp_win, out.stderr
    name = f"sw-startup100-test-{uuid.uuid4().hex[:12]}"
    root_win = temp_win.rstrip("\\") + "\\" + name
    root_wsl = Path(subprocess.run(["wslpath", "-u", root_win], capture_output=True, text=True, check=True).stdout.strip())
    root_wsl.mkdir(parents=True)
    try:
        yield root_wsl, root_win
    finally:
        _ps(f"Remove-Item -LiteralPath {_ps_quote(root_win)} -Recurse -Force -ErrorAction SilentlyContinue", timeout=120)
        shutil.rmtree(root_wsl, ignore_errors=True)


def _run_case(win_root: tuple[Path, str], tmp_path: Path, case: str, defines: list[str]) -> tuple[int, dict]:
    root_wsl, root_win = win_root
    case_wsl = root_wsl / case
    ambient_local = case_wsl / "ambient-localappdata"
    (ambient_local / "Temp").mkdir(parents=True)
    (case_wsl / "exe").mkdir()
    (case_wsl / "out").mkdir()
    ambient_dir_win = root_win + "\\" + case + "\\ambient-localappdata\\OtherDesktopApp"
    src = tmp_path / "fake_cli.c"
    src.write_text(FAKE_CLI_C, encoding="utf-8")
    exe = case_wsl / "exe" / "serialwrap.exe"
    flags = [f"-D{d}" for d in defines]
    if "SW_AMBIENT_DIR" in defines:
        flags = [f for f in flags if f != "-DSW_AMBIENT_DIR"]
        flags.append(f'-DSW_AMBIENT_DIR="{ambient_dir_win.replace(chr(92), chr(92) * 2)}"')
    subprocess.run(["x86_64-w64-mingw32-gcc", "-O1", "-o", str(exe), str(src), *flags], check=True,
                   capture_output=True, text=True)
    case_win = root_win + "\\" + case
    local_win = case_win + "\\ambient-localappdata"
    temp_win = local_win + "\\Temp"
    exe_win = case_win + "\\exe\\serialwrap.exe"
    out_win = case_win + "\\out"
    command = (
        f"$env:LOCALAPPDATA={_ps_quote(local_win)};"
        f"$env:TEMP={_ps_quote(temp_win)};"
        f"$env:TMP={_ps_quote(temp_win)};"
        f"& {_ps_quote(_wslpath(SCRIPT))} -Exe {_ps_quote(exe_win)} -OutDir {_ps_quote(out_win)};"
        "exit $LASTEXITCODE"
    )
    res = _ps(command)
    report_path = case_wsl / "out" / "startup-100.json"
    assert report_path.is_file(), f"未產生 startup-100.json；rc={res.returncode} stdout={res.stdout[-2000:]} stderr={res.stderr[-2000:]}"
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    return res.returncode, report


def test_clean_cli_is_ok_with_isolated_environment(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "clean", [])
    assert rc == 0, report
    assert report["ok"] is True
    assert len(report["runs"]) == 100 and all(run["exit_code"] == 0 for run in report["runs"])
    assert report["isolated_new"] == [] and report["new_artifacts"] == []
    assert report["ambient_changes"] == []
    assert report["snapshot_errors"] == []
    # 子行程看到的是隔離目錄，且隔離目錄不在 ambient 快照內
    iso = report["isolated_root"]
    assert iso and report["isolated_env"]["TEMP"].startswith(iso) and report["isolated_env"]["LOCALAPPDATA"].startswith(iso)


def test_temp_extract_dir_fails(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "temp-extract", ["SW_TEMP_EXTRACT_DIR"])
    assert rc != 0
    assert report["ok"] is False
    assert any("_MEI" in path for path in report["isolated_new"]), report["isolated_new"]
    assert any(path.lower().endswith("python312.dll") for path in report["isolated_new"])
    assert report["ambient_changes"] == []


def test_temp_plain_file_fails_regardless_of_extension(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "temp-file", ["SW_TEMP_PLAIN_FILE"])
    assert rc != 0
    assert report["ok"] is False
    assert any(path.lower().endswith("sw-cache.dat") for path in report["isolated_new"]), report["isolated_new"]


def test_localappdata_unpack_dir_fails(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "localappdata", ["SW_LOCALAPPDATA_DIR"])
    assert rc != 0
    assert report["ok"] is False
    assert any(path.lower().endswith("sw-unpack") for path in report["isolated_new"]), report["isolated_new"]


def test_ambient_change_outside_isolation_is_recorded_not_failed(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "ambient", ["SW_AMBIENT_DIR"])
    assert rc == 0, report
    assert report["ok"] is True
    assert report["isolated_new"] == []
    assert any(path.endswith("OtherDesktopApp") for path in report["ambient_changes"]), report["ambient_changes"]


def test_exe_dir_extract_still_fails(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "exe-dir", ["SW_EXE_DIR_EXTRACT"])
    assert rc != 0
    assert report["ok"] is False
    assert any(path.endswith("_unpacked") for path in report["new_artifacts"]), report["new_artifacts"]


def test_varying_stdout_still_fails(win_root, tmp_path: Path) -> None:
    rc, report = _run_case(win_root, tmp_path, "varying", ["SW_VARYING_STDOUT"])
    assert rc != 0
    assert report["ok"] is False
    assert report["isolated_new"] == [] and report["new_artifacts"] == []
