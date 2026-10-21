#!/usr/bin/env bash
# 解析 P3／P4 bundle 旗標並先執行 Stage 3 退役計畫守門。
set -euo pipefail

usage() {
  cat <<'EOF'
用法：mkbundle.sh [--dev] [--out DIR] [--target TRIPLE] [--profile debug|release]
                  [--delegate-sentinel] [--print-dir] [--version-override VERSION]
                  [--stage STAGE] [--plan-only]
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dev=0
out="${repo_root}/dist/serialwrap-bundle"
target=""
profile="release"
delegate_sentinel=0
print_dir=0
version_override=""
stage="1A"
plan_only=0

while (($#)); do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --dev)
      dev=1
      shift
      ;;
    --out)
      (($# >= 2)) || { echo "缺少 --out 路徑" >&2; exit 2; }
      out="$2"
      shift 2
      ;;
    --target)
      (($# >= 2)) || { echo "缺少 --target triple" >&2; exit 2; }
      target="$2"
      shift 2
      ;;
    --profile)
      (($# >= 2)) || { echo "缺少 --profile 值" >&2; exit 2; }
      profile="$2"
      shift 2
      ;;
    --delegate-sentinel)
      delegate_sentinel=1
      shift
      ;;
    --print-dir)
      print_dir=1
      shift
      ;;
    --version-override)
      (($# >= 2)) || { echo "缺少 --version-override 值" >&2; exit 2; }
      version_override="$2"
      shift 2
      ;;
    --stage)
      (($# >= 2)) || { echo "缺少 --stage 值" >&2; exit 2; }
      stage="$2"
      shift 2
      ;;
    --plan-only)
      plan_only=1
      shift
      ;;
    *)
      echo "未知參數：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$profile" != "debug" && "$profile" != "release" ]]; then
  echo "--profile 只接受 debug 或 release" >&2
  exit 2
fi
if [[ -n "$version_override" && "$dev" -ne 1 ]]; then
  echo "--version-override 僅限 --dev" >&2
  exit 2
fi

cd "$repo_root"
plan_args=(retirement bundle-plan --stage "$stage")
if [[ "$dev" -eq 1 ]]; then
  plan_args+=(--dev)
fi
if plan_json="$(python3 -m tools.stage3 "${plan_args[@]}")"; then
  :
else
  printf '%s\n' "$plan_json" >&2
  exit 2
fi
plan_candidate="$(python3 -c 'import json,sys; print(str(json.load(sys.stdin)["candidate"]).lower())' <<<"$plan_json")"

if [[ "$plan_only" -eq 1 ]]; then
  printf '%s\n' "$plan_json"
  exit 0
fi

if [[ "$dev" -eq 1 && "$plan_candidate" != "true" ]]; then
  out="${out}-dev"
fi

# P3／P4 的 Rust binary build 與 bundle 組裝器尚未合入此 worktree；不可輸出假 bundle。
echo "HARNESS_BUILD_UNAVAILABLE: 此 worktree 缺少 P3／P4 Rust bundle builder；目前僅提供退役計畫守門" >&2
exit 2
