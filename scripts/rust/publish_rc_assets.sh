#!/usr/bin/env bash
# 在擁有者確認後，封裝 gate 候選 Stage 2 bundle 並建立 prerelease；不建置執行檔。
set -euo pipefail

usage() {
  cat <<'EOF'
用法：
  scripts/rust/publish_rc_assets.sh --owner-confirmed --tag v0.5.0-rc.N \
    --shared-evidence <gate_shared.sh 輸出目錄> \
    --linux-bundle <共用證據記錄的 Linux 候選 bundle> \
    --p7-run <P7 gate run 目錄：含 results.json、p7-gate.md、windows-ev01/bundle> \
    --windows-bundle <P7 ev-01 驗證的原始 Windows bundle> \
    --slice21-run <Slice 2.1 正式 gate run 目錄> \
    --slice22-report <Slice 2.2 正式 s22-gate.json> \
    --slice24-run <Slice 2.4 正式 gate run 目錄> \
    --out <新的輸出目錄> [--repo OWNER/REPO]

只有在擁有者確認建立該 RC tag 與 GitHub prerelease 後才可執行。
本工具只封裝 gate 已驗證的 Linux／Windows 候選 bundle，不會建置或修改執行檔；輸出目錄不得已存在。
EOF
}

OWNER_CONFIRMED=0
TAG=""
SHARED_EVIDENCE=""
LINUX_BUNDLE=""
WINDOWS_BUNDLE=""
P7_RUN=""
SLICE21_RUN=""
SLICE22_REPORT=""
SLICE24_RUN=""
OUT=""
REPO=""
while (($#)); do
  case "$1" in
    --owner-confirmed) OWNER_CONFIRMED=1; shift ;;
    --tag) (($# >= 2)) || { usage >&2; exit 2; }; TAG="$2"; shift 2 ;;
    --shared-evidence) (($# >= 2)) || { usage >&2; exit 2; }; SHARED_EVIDENCE="$2"; shift 2 ;;
    --linux-bundle) (($# >= 2)) || { usage >&2; exit 2; }; LINUX_BUNDLE="$2"; shift 2 ;;
    --windows-bundle) (($# >= 2)) || { usage >&2; exit 2; }; WINDOWS_BUNDLE="$2"; shift 2 ;;
    --p7-run) (($# >= 2)) || { usage >&2; exit 2; }; P7_RUN="$2"; shift 2 ;;
    --slice21-run) (($# >= 2)) || { usage >&2; exit 2; }; SLICE21_RUN="$2"; shift 2 ;;
    --slice22-report) (($# >= 2)) || { usage >&2; exit 2; }; SLICE22_REPORT="$2"; shift 2 ;;
    --slice24-run) (($# >= 2)) || { usage >&2; exit 2; }; SLICE24_RUN="$2"; shift 2 ;;
    --out) (($# >= 2)) || { usage >&2; exit 2; }; OUT="$2"; shift 2 ;;
    --repo) (($# >= 2)) || { usage >&2; exit 2; }; REPO="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "publish_rc_assets.sh: 未知參數：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$OWNER_CONFIRMED" -ne 1 ]]; then
  echo "publish_rc_assets.sh: 必須在擁有者確認後明確帶入 --owner-confirmed" >&2
  exit 2
fi
if [[ -z "$TAG" || -z "$SHARED_EVIDENCE" || -z "$LINUX_BUNDLE" || -z "$P7_RUN" || -z "$WINDOWS_BUNDLE" || -z "$SLICE21_RUN" || -z "$SLICE22_REPORT" || -z "$SLICE24_RUN" || -z "$OUT" ]]; then
  usage >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
OUT="$(realpath -m -- "$OUT")"
if [[ -e "$OUT" ]]; then
  echo "publish_rc_assets.sh: 輸出路徑已存在，拒絕覆寫：$OUT" >&2
  exit 2
fi

TAG_COMMIT="$(git -C "$ROOT" rev-parse --verify "refs/tags/${TAG}^{commit}")"
# 以共用證據的候選身分記錄驗證 Linux bundle 位元組，拒絕同 HEAD 重建但非 gate 實測的 bundle。
python3 "$ROOT/scripts/rust/gate_shared_evidence.py" check-candidate-bundle \
  --shared-evidence "$SHARED_EVIDENCE" --candidate-bundle "$LINUX_BUNDLE"
P7_RUN="$(realpath -e -- "$P7_RUN")"
SLICE21_RUN="$(realpath -e -- "$SLICE21_RUN")"
SLICE22_REPORT="$(realpath -e -- "$SLICE22_REPORT")"
SLICE24_RUN="$(realpath -e -- "$SLICE24_RUN")"
python3 - "$ROOT" "$P7_RUN" "$WINDOWS_BUNDLE" "$TAG_COMMIT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
run_dir = Path(sys.argv[2]).resolve(strict=True)
supplied_bundle = Path(sys.argv[3]).resolve(strict=True)
tag_commit = sys.argv[4]
sys.path.insert(0, str(root / "scripts" / "rust"))
import p7_gate_report  # noqa: E402

results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
if results.get("git_head") != tag_commit or results.get("worktree_clean") is not True:
    raise SystemExit("P7 gate run 必須綁定同一個 tag commit，且記錄的工作樹須乾淨")
report_path = run_dir / "p7-gate.md"
problems = p7_gate_report.verify_report(report_path, run_dir)
if problems:
    raise SystemExit("P7 gate report 驗證失敗：" + "; ".join(problems))
report = p7_gate_report.build_report(run_dir)
if "結論：PASS（含 BLOCKED-OWNER）" not in report.splitlines():
    raise SystemExit("P7 gate 必須為 PASS（含 BLOCKED-OWNER）")
rows = [row for row in results.get("rows", []) if row.get("name") == p7_gate_report.WINDOWS_EV01_ROW]
if len(rows) != 1:
    raise SystemExit("P7 results.json 必須恰有一筆 Windows ev-01 gate evidence")
status, reason = p7_gate_report._windows_ev01_row_status(run_dir, rows[0], results.get("git_head"))
if status != "PASS":
    raise SystemExit(f"Windows Stage 2 bundle 未通過 gate ev-01：{status}：{reason}")
evidence = rows[0].get("windows_ev01")
raw_bundle_path = evidence.get("bundle_path") if isinstance(evidence, dict) else None
if not isinstance(raw_bundle_path, str):
    raise SystemExit("P7 gate 缺少 Windows ev-01 bundle 路徑")
recorded_bundle = (run_dir / raw_bundle_path).resolve(strict=True)
try:
    recorded_bundle.relative_to(run_dir)
except ValueError as exc:
    raise SystemExit("P7 Windows bundle evidence path 逸出 gate run 目錄") from exc
recorded_identity = p7_gate_report.windows_bundle_identity(recorded_bundle)
supplied_identity = p7_gate_report.windows_bundle_identity(supplied_bundle)
if recorded_identity != supplied_identity:
    raise SystemExit("指定 Windows bundle 與 P7 ev-01 gate 實際驗證的 bundle bytes 不同")
print(f"P7 Windows ev-01 bundle verified: {supplied_bundle}")
PY
python3 "$ROOT/scripts/rust/release_gate_evidence.py" check \
  --repo "$ROOT" --head "$TAG_COMMIT" \
  --slice21-run "$SLICE21_RUN" --slice22-report "$SLICE22_REPORT" --slice24-run "$SLICE24_RUN"
python3 "$ROOT/scripts/rust/release_assets.py" prepare-rc-assets \
  --tag "$TAG" \
  --version-file "$ROOT/VERSION" \
  --tag-commit "$TAG_COMMIT" \
  --linux-bundle "$LINUX_BUNDLE" \
  --windows-bundle "$WINDOWS_BUNDLE" \
  --guide "$ROOT/docs/rust-refactor/stage2-rc-install.md" \
  --out-dir "$OUT"

GH_ARGS=()
if [[ -n "$REPO" ]]; then GH_ARGS+=(--repo "$REPO"); fi
if gh release view "$TAG" "${GH_ARGS[@]}" >/dev/null 2>&1; then
  echo "publish_rc_assets.sh: release $TAG 已存在，拒絕覆寫" >&2
  exit 1
fi

# gh release create 接收檔案參數並上傳附件後才發布，避免先發布空 release 觸發 RC 驗證。
# 不使用 --clobber；重複資產名稱一律失敗。
VERSION="$(tr -d ' \n' < "$ROOT/VERSION")"
gh release create "$TAG" "${GH_ARGS[@]}" \
  --verify-tag --prerelease --title "$TAG" \
  --notes-file "$OUT/RC-INSTALL.md" \
  "$OUT/serialwrap-${VERSION}-linux-x86_64.tar.gz" \
  "$OUT/serialwrap-${VERSION}-windows-x86_64.zip" \
  "$OUT/SHA256SUMS" "$OUT/RC-INSTALL.md"
printf 'RC assets prepared and published: %s\n' "$TAG"
