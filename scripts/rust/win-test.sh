#!/usr/bin/env bash
# 交叉建置並經 WSL interop 執行 Windows GNU Cargo 測試。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
RUNNER="$ROOT/scripts/rust/win-runner.sh"
COLLECTOR="$ROOT/scripts/rust/cargo_l1_results.py"
CAPS=()
PACKAGES=()
CARGO_ARGS=()
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --caps)
      [[ $# -ge 2 ]] || { echo "win-test.sh: --caps 缺少值" >&2; exit 2; }
      IFS=, read -r -a parsed_caps <<< "$2"
      CAPS+=("${parsed_caps[@]}")
      shift 2
      ;;
    --package)
      [[ $# -ge 2 ]] || { echo "win-test.sh: --package 缺少值" >&2; exit 2; }
      PACKAGES+=(--package "$2")
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    --)
      shift
      CARGO_ARGS=("$@")
      break
      ;;
    *) echo "win-test.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

is_wsl=0
if [[ -n "${WSL_DISTRO_NAME:-}" || -e /proc/sys/fs/binfmt_misc/WSLInterop ]]; then
  is_wsl=1
elif command -v wslpath >/dev/null 2>&1 && command -v cmd.exe >/dev/null 2>&1 && [[ -e /mnt/c/Windows/System32/cmd.exe ]]; then
  # 某些受控 WSL 環境會隱藏 binfmt_misc，但仍提供 Windows 掛載與 interop 工具。
  is_wsl=1
fi

if [[ "$is_wsl" == "1" ]]; then
  export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER="$RUNNER"
  export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER="x86_64-w64-mingw32-gcc"
else
  unset CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER || true
fi

collector_args=(--platform windows)
if [[ "$is_wsl" == "1" ]]; then
  collector_args+=(--runner "$RUNNER")
fi
for cap in "${CAPS[@]}"; do
  [[ -n "$cap" ]] && collector_args+=(--caps "$cap")
done
collector_args+=("${PACKAGES[@]}")
collector_args+=("${CARGO_ARGS[@]}")

if [[ "$DRY_RUN" == "1" ]]; then
  if [[ "$is_wsl" == "1" ]]; then
    printf 'CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER=%s\n' "$CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER"
    printf 'CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=%s\n' "$CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER"
  else
    echo "Windows native host：不注入 Cargo runner/linker。"
  fi
  printf 'cargo test --no-run --message-format=json --target x86_64-pc-windows-gnu'
  dry_run_args=("${PACKAGES[@]}" "${CARGO_ARGS[@]}")
  if ((${#dry_run_args[@]} > 0)); then
    printf ' %q' "${dry_run_args[@]}"
  fi
  printf '\n'
  exit 0
fi

cd "$ROOT"
python3 "$ROOT/scripts/rust/check_cfg_boundary.py"

run_id="$(date +%y%m%d-%H%M%S)-$$"
report_dir="$ROOT/target/win-test-report"
mkdir -p "$report_dir"
l1_name="l1-results-$run_id.json"
summary_name="summary-$run_id.json"
report_name="$run_id.json"
l1_path="$report_dir/$l1_name"
summary_path="$report_dir/$summary_name"
report_path="$report_dir/$report_name"

set +e
python3 "$COLLECTOR" --out "$l1_path" --summary-out "$summary_path" "${collector_args[@]}"
collector_rc=$?
set -e

python3 - "$ROOT" "$l1_path" "$summary_path" "$report_path" "$run_id" "$collector_rc" "${CAPS[*]}" <<'PY'
from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

root, l1_path, summary_path, report_path, run_id, collector_rc, caps_raw = sys.argv[1:]
l1 = json.loads(Path(l1_path).read_text(encoding="utf-8")) if Path(l1_path).exists() else {}
summary = json.loads(Path(summary_path).read_text(encoding="utf-8")) if Path(summary_path).exists() else {}
exes = summary.get("exes", [])
evidence = set(summary.get("evidence_insufficient", []))
l1_verdict = "EVIDENCE-INSUFFICIENT"
l1_reasons = []
selected = {cap for cap in caps_raw.split() if cap}
for resource in ("windows_com", "interactive_desktop"):
    if resource not in selected:
        evidence.add(resource)
acceptance = Path(root) / "tests/acceptance"
judge_path = acceptance / "l1_judge.py"
lock_path = acceptance / "l1_required.lock.json"
if not judge_path.is_file():
    l1_reasons.append("l1_judge_missing")
if not lock_path.is_file():
    l1_reasons.append("l1_required_lock_missing")
if judge_path.is_file() and lock_path.is_file():
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        required = lock.get("sets", {}).get("s1a_cargo", {}).get("windows")
        if not isinstance(required, dict) or not isinstance(required.get("nodeids"), list):
            l1_reasons.append("s1a_cargo_windows_lock_missing")
        elif required.get("selector", {}).get("argv") != l1.get("argv"):
            l1_reasons.append("s1a_cargo_windows_selector_mismatch")
        else:
            spec = importlib.util.spec_from_file_location("l1_judge", judge_path)
            if spec is None or spec.loader is None:
                l1_reasons.append("l1_judge_load_failed")
            else:
                judge_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(judge_module)
                judged = judge_module.judge(l1, required["nodeids"], platform="windows")
                l1_verdict = str(judged.get("verdict") or "INVALID")
                l1_reasons.extend(str(reason) for reason in judged.get("reasons", []))
                if l1_verdict != "PASS" and not l1_reasons:
                    l1_reasons.append(f"l1_{l1_verdict.lower()}")
    except Exception as exc:
        l1_reasons.append(f"l1_judge_error:{type(exc).__name__}")
# 本報告的 overall 僅表示要求的 Cargo 執行是否完整成功；Stage 1A 的凍結清單判定由 l1_verdict 單獨保留，出口 gate 必須同時檢查兩者。
run_ok = (collector_rc == "0" and bool(exes) and l1.get("finished") is True
          and l1.get("exit_code") == 0
          and all(item.get("failed") == 0 and item.get("exit") == 0 for item in exes))
overall = "pass" if run_ok else "fail"
report = {
    "run_id": run_id,
    "exes": exes,
    "overall": overall,
    "evidence_insufficient": sorted(evidence),
    "l1_results": Path(l1_path).name,
    "l1_verdict": l1_verdict,
    "l1_reasons": sorted(set(l1_reasons)),
}
Path(report_path).write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
print(report_path)
sys.exit(0 if overall == "pass" else 1)
PY

if [[ "$collector_rc" -ne 0 ]]; then
  exit 1
fi
