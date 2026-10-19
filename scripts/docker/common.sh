#!/usr/bin/env bash

sw_require_p1_oc() {
    local repo_root="$1"
    local bc_path="${SW_BC_PATH:-$repo_root/contracts/behavior_changes.yaml}"
    local error_path="${SW_ERROR_CODES_PATH:-$repo_root/contracts/error_codes.yaml}"
    python3 - "$bc_path" "$error_path" <<'PY'
import sys

import yaml

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        changes = yaml.safe_load(stream) or {}
    with open(sys.argv[2], encoding="utf-8") as stream:
        codes = yaml.safe_load(stream) or {}
except (OSError, yaml.YAMLError) as exc:
    print(f"P1/P1-OC gate 無法讀取契約：{exc}", file=sys.stderr)
    raise SystemExit(3)

statuses = {
    str(row.get("id")): row.get("status")
    for row in changes.get("changes", [])
    if isinstance(row, dict)
}
for change_id in ("BC-01", "BC-02", "BC-03", "BC-04", "BC-05"):
    if statuses.get(change_id) != "IMPLEMENTED":
        print(f"P1 未合併：{change_id} 非 IMPLEMENTED", file=sys.stderr)
        raise SystemExit(3)

codes_present = {
    str(row.get("code"))
    for row in codes.get("codes", [])
    if isinstance(row, dict)
}
missing = [change_id for change_id in ("SC-34", "SC-35") if statuses.get(change_id) != "IMPLEMENTED"]
if missing or "DRAIN_STALLED" not in codes_present:
    print("P1-OC 未合併：OD-14 契約尚未完整登記並實作", file=sys.stderr)
    raise SystemExit(3)
PY
}

sw_prepare_empty_out() {
    local out="$1"
    if [[ -e "$out" && ! -d "$out" ]]; then
        echo "--out 必須是目錄：$out" >&2
        return 2
    fi
    if [[ -d "$out" ]] && [[ -n "$(find "$out" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        echo "拒絕使用非空輸出目錄：$out" >&2
        return 2
    fi
    mkdir -p "$out"
}
