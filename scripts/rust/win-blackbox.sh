#!/usr/bin/env bash
# 以 Windows 本機磁碟執行 Stage 1A A/B 黑箱與 100 次啟動檢查。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT"
NO_COMPARE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-compare) NO_COMPARE=1; shift ;;
    -h|--help)
      echo "用法：scripts/rust/win-blackbox.sh [--no-compare]";
      exit 0
      ;;
    *) echo "win-blackbox.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

run_id="$(date +%y%m%d-%H%M%S)-$$"
out_dir="$ROOT/target/win-blackbox/$run_id"
release_exe="$ROOT/target/x86_64-pc-windows-gnu/release/serialwrap.exe"
mkdir -p "$out_dir"

export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER="x86_64-w64-mingw32-gcc"
if ! cargo build -p sw-cli --release --target x86_64-pc-windows-gnu; then
  echo "win-blackbox.sh: Windows GNU release 交叉建置失敗" >&2
  exit 1
fi

set +e
python3 "$ROOT/scripts/rust/check_win_imports.py" "$release_exe" > "$out_dir/imports.json"
imports_rc=$?
local_appdata="$(cmd.exe /c echo %LOCALAPPDATA% 2>/dev/null | tr -d '\r' | tail -n 1)"
interop_rc=$?
set -e

if [[ "$interop_rc" -ne 0 || -z "$local_appdata" || "$local_appdata" == '%LOCALAPPDATA%' ]]; then
  python3 - "$out_dir" "$run_id" "$imports_rc" <<'PY'
import json
import sys
from pathlib import Path

root, run_id, imports_rc = sys.argv[1:]
out = Path(root)
imports = json.loads((out / "imports.json").read_text(encoding="utf-8")) if (out / "imports.json").exists() else {}
report = {
    "run_id": run_id,
    "result": "evidence-insufficient",
    "reason": "WSL interop 無法啟動 cmd.exe；未執行 Windows binary",
    "imports": imports,
    "imports_exit": int(imports_rc),
    "windows_com": "BLOCKED-OWNER",
}
(out / "report.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
print(out / "report.json")
PY
  exit 2
fi

local_appdata_unix="$(wslpath -u "$local_appdata")"
stage_root="$local_appdata_unix/Temp/sw-rust-test/$run_id-wrapper"
stage_exe="$stage_root/serialwrap.exe"
stage_exe_win="$(wslpath -w "$stage_exe")"
cleanup_stage() {
  rm -rf -- "$stage_root"
}
trap cleanup_stage EXIT
mkdir -p "$stage_root"
cp -- "$release_exe" "$stage_exe"
chmod +x "$stage_exe"

rust_win="$stage_exe_win"
repo_win="$(wslpath -w "$ROOT")"
out_win="$(wslpath -w "$out_dir")"
git_revision="$(git -C "$ROOT" rev-parse HEAD)"
contract_revision="$(git -C "$ROOT" rev-parse HEAD:contracts)"
python3123="$local_appdata\sw-rust-test\py-3.12.3"
ps_args=(-NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w "$ROOT/scripts/rust/win-blackbox.ps1")"
  -RustExe "$rust_win" -RepoSnapshot "$repo_win" -OutDir "$out_win"
  -GitRevision "$git_revision" -ContractRevision "$contract_revision")
if [[ -d "$(wslpath -u "$python3123" 2>/dev/null)" ]]; then
  ps_args+=(-Python3123 "$python3123")
fi
if [[ -n "${SW_TEST_COM_PAIR:-}" ]]; then
  ps_args+=(-ComPair "$SW_TEST_COM_PAIR")
fi

set +e
powershell.exe "${ps_args[@]}"
blackbox_rc=$?
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w "$ROOT/scripts/rust/win-startup-100.ps1")" \
  -Exe "$stage_exe_win" -OutDir "$(wslpath -w "$out_dir/startup")"
startup_rc=$?
set -e

set +e
if [[ "$NO_COMPARE" -eq 0 && -f "$out_dir/a.xml" && -f "$out_dir/b.xml" ]]; then
  python3 "$ROOT/scripts/rust/ab_compare.py" \
    --a "$out_dir/a.xml" \
    --b "$out_dir/b.xml" \
    --out "$out_dir/ab-report.json"
fi

python3 - "$out_dir" "$run_id" "$imports_rc" "$blackbox_rc" "$startup_rc" "$NO_COMPARE" <<'PY'
import json
import sys
from pathlib import Path

root, run_id, imports_rc, blackbox_rc, startup_rc, no_compare = sys.argv[1:]
out = Path(root)
def read(name):
    path = out / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

blackbox = read("report.json")
startup = read("startup/startup-100.json")
imports = read("imports.json")
evidence = list((blackbox or {}).get("evidence_insufficient", []))
if not startup:
    evidence.append("startup-100 未產生報告")
if not imports or imports.get("ok") is not True:
    evidence.append("Windows 匯入 DLL 檢查未通過")
if (blackbox or {}).get("dialect_runtime_missing"):
    evidence.append("CPython 3.12.3 方言基線缺失")
if not blackbox:
    evidence.append("Windows 黑箱報告不存在")
if (blackbox or {}).get("windows_com") != "PASS":
    evidence.append("Windows COM 證據尚未完成")
release_check = (blackbox or {}).get("release_check") or {}
if not release_check.get("ok"):
    evidence.append("A 輪後資源釋放檢查未通過")

for name in ("a.xml", "l1/windows/blackbox/junit.xml", "l1/windows/blackbox/resource-report.json"):
    path = out / name
    if not path.is_file() or path.stat().st_size == 0:
        evidence.append(f"必要黑箱/L1 證據不存在或為空：{name}")

l1_path = out / "l1/windows/blackbox/resource-report.json"
l1 = None
if l1_path.is_file():
    try:
        l1 = json.loads(l1_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
if not isinstance(l1, dict) or l1.get("format") != "l1-results.v1" or l1.get("platform") != "windows" \
        or l1.get("finished") is not True or not isinstance(l1.get("collected"), list) \
        or not isinstance(l1.get("results"), list):
    evidence.append("Windows Cargo blackbox L1 報告格式不完整")

python_run = (blackbox or {}).get("python") or {}
rust_run = (blackbox or {}).get("rust") or {}
execution_failed = bool(
    (imports and imports.get("ok") is False and int(imports_rc) == 1)
    or (startup and startup.get("ok") is False)
    or (python_run and python_run.get("exit") not in (None, 0))
    or (rust_run and rust_run.get("exit") not in (None, 0))
    or (release_check and not release_check.get("ok"))
)
ab_report = read("ab-report.json")
if ab_report:
    comparison = ab_report.get("result", "fail")
elif no_compare == "1":
    comparison = "skipped"
else:
    comparison = "not-run"

if execution_failed:
    result = "fail"
elif evidence:
    result = "evidence-insufficient"
elif comparison not in ("pass", "skipped"):
    result = "fail" if comparison == "fail" else "not-compared"
else:
    result = "pass"
report = {
    "run_id": run_id,
    "result": result,
    "comparison": comparison,
    "imports": imports,
    "blackbox": blackbox,
    "startup_100": startup,
    "windows_com": (blackbox or {}).get("windows_com", "BLOCKED-OWNER"),
    "evidence_insufficient": sorted(set(evidence)),
}
(out / "report.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
print(out / "report.json")
sys.exit(0 if result == "pass" else 1)
PY
report_rc=$?
set -e

if [[ "$imports_rc" -ne 0 || "$blackbox_rc" -ne 0 || "$startup_rc" -ne 0 || "$report_rc" -ne 0 ]]; then
  exit 1
fi
