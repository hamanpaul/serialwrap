"""MCU A/B 測試觀察記錄與逐 case/node/field 差異報告工具（只用標準函式庫）。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROUNDS = ("python032", "python_main", "rust")
_MISSING = object()
_COMMIT_LINE = re.compile(r"\b[0-9a-f]{7,40}\b\s+[^\s:]+:\d+(?:-\d+)?\b")
_RULING = re.compile(r"\b(?:N\d+|DF-\d+|OD-\d+|BC-\d+|SC-\d+)\b")

# 僅兩輪模式使用；六個欄位及值逐項取自
# ~/b-log/rust-refactor/exec/s2/gate/evidence/mcu-flash-parity-0.3.2.md:175,180,215-216,247-248（OD-91）。
_TWO_ROUND_KNOWN_DIFFERENCES: dict[str, dict[str, tuple[Any, Any]]] = {
    "s24-ho-windows-com::tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown": {
        "attach_attempt": (
            {
                "error_code": None,
                "exit": 0,
                "holder_unknown": None,
                "ok": True,
                "pids": None,
                "selector": None,
                "session_state": "ATTACHING",
            },
            {
                "error_code": "DEVICE_STILL_HELD",
                "exit": 2,
                "holder_unknown": True,
                "ok": False,
                "pids": [],
                "selector": "COM0",
                "session_state": None,
            },
        ),
    },
    "s24-ho-windows-com::tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_selftest_released_holder_unknown": {
        "self_test": (
            {
                "classification": "RELEASED",
                "external_holder": "none",
                "reclaimable": True,
                "recommended_action": "device_attach",
            },
            {
                "classification": "RELEASED",
                "external_holder": "none",
                "reclaimable": False,
                "recommended_action": "wait_external_flash",
            },
        ),
    },
    "s24-upd-guards::tests/contract_blackbox/test_s24_update_rollback.py::test_rollback_refuses_while_push[after_probe]": {
        "busy": ([], [{"com": "COM0", "foreground_busy": True, "interactive": False, "ops": ["file.push"]}]),
        "error_code": ("METHOD_NOT_FOUND", "SESSION_BUSY"),
    },
    "s24-upd-guards::tests/contract_blackbox/test_s24_update_rollback.py::test_rollback_force_refuses_while_push[rollback-after-probe]": {
        "busy": ([], [{"com": "COM0", "foreground_busy": True, "interactive": False, "ops": ["file.push"]}]),
        "error_code": ("METHOD_NOT_FOUND", "SESSION_BUSY"),
    },
}


def record_observation(case_id: str, nodeid: str, **fields: Any) -> None:
    """若設定觀察檔，追加一筆實際測試觀察；未設定時不影響一般測試。"""
    path = os.environ.get("SW_MCU_FLASH_OBSERVATIONS")
    if not path:
        return
    row = {
        "round": os.environ.get("SW_MCU_FLASH_ROUND", "unlabeled"),
        "case_id": case_id,
        "nodeid": nodeid,
        "fields": fields,
    }
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _load_rows(path: Path, expected_round: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: JSON 格式錯誤：{exc}") from exc
        if not isinstance(row, dict) or row.get("round") != expected_round:
            raise ValueError(f"{path}:{number}: round 必須是 {expected_round!r}")
        case_id, nodeid, fields = row.get("case_id"), row.get("nodeid"), row.get("fields")
        if (not isinstance(case_id, str) or not case_id or not isinstance(nodeid, str) or not nodeid
                or not isinstance(fields, dict)):
            raise ValueError(f"{path}:{number}: 需要非空 case_id、nodeid 與 fields object")
        merged = result.setdefault(f"{case_id}::{nodeid}", {})
        for key, value in fields.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path}:{number}: fields 鍵必須是非空字串")
            if key in merged and merged[key] != value:
                raise ValueError(f"{path}:{number}: {case_id}.{key} 有衝突觀察值")
            merged[key] = value
    return result


def _display(value: Any) -> str:
    if value is _MISSING:
        return "（缺少）"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _case_node(row_key: str) -> tuple[str, str]:
    """解讀載入後的 case/node key；直接呼叫 compare 的舊式 fixture 可省略 node。"""
    if "::" not in row_key:
        return row_key, ""
    case_id, nodeid = row_key.split("::", 1)
    return case_id, nodeid


def _attribution_status(key: str, v032: Any, main: Any, rust: Any, attribution: Any) -> tuple[str, str | None]:
    if v032 == main == rust:
        return "三輪一致", None
    if not isinstance(attribution, dict):
        return "未歸因", None
    kind = attribution.get("kind")
    reference = attribution.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        return "未歸因", None
    if kind == "python_post_032" and rust == main and main != v032 and _COMMIT_LINE.search(reference):
        return "Python 0.3.2 之後修正", reference
    if kind == "registered_df_or_ruling" and _RULING.search(reference):
        return "已登記 DF／裁決", reference
    if kind == "rust_defect_fixed" and rust == main and _COMMIT_LINE.search(reference):
        return "Rust 缺陷已修正", reference
    if kind == "v032_no_windows_daemon" and reference.strip() == "OD-91":
        return "0.3.2 無 Windows daemon", reference
    return "未歸因", None


def compare(data: dict[str, Any]) -> tuple[str, bool]:
    if "python_main" not in data:
        return _compare_two_round(data)

    runs = {name: data.get(name) for name in ROUNDS}
    for name, rows in runs.items():
        if not isinstance(rows, dict) or not rows:
            raise ValueError(f"{name} 輪觀察資料必須是非空 object")
        for row_key, fields in rows.items():
            if not isinstance(row_key, str) or not isinstance(fields, dict):
                raise ValueError(f"{name} 輪每案必須是 case/node → fields object")

    expected_rows = set(runs["python032"])
    for name in ROUNDS[1:]:
        actual_rows = set(runs[name])
        if actual_rows != expected_rows:
            missing = sorted(expected_rows - actual_rows)
            extra = sorted(actual_rows - expected_rows)
            raise ValueError(f"{name} 輪 observation 案例不齊：缺少={missing!r}、額外={extra!r}")
    for row_key in sorted(expected_rows):
        expected_fields = set(runs["python032"][row_key])
        if not expected_fields:
            raise ValueError(f"{row_key} observation fields 不得為空")
        for name in ROUNDS[1:]:
            actual_fields = set(runs[name][row_key])
            if actual_fields != expected_fields:
                missing = sorted(expected_fields - actual_fields)
                extra = sorted(actual_fields - expected_fields)
                raise ValueError(f"{name} 輪 {row_key} fields 不齊：缺少={missing!r}、額外={extra!r}")

    attributions = data.get("attributions", {})
    if not isinstance(attributions, dict):
        raise ValueError("attributions 必須是 object")
    row_keys = sorted(set().union(*(set(rows) for rows in runs.values())))
    rows_out: list[list[str]] = []
    unattributed = False
    for row_key in row_keys:
        case_id, nodeid = _case_node(row_key)
        fields = sorted(set().union(*(set(runs[name].get(row_key, {})) for name in ROUNDS)))
        for field in fields:
            values = [runs[name].get(row_key, {}).get(field, _MISSING) for name in ROUNDS]
            key = f"{row_key}.{field}" if nodeid else f"{case_id}.{field}"
            status, reference = _attribution_status(key, *values, attributions.get(key))
            if status == "未歸因":
                unattributed = True
            rows_out.append([case_id, nodeid, field, *(_display(value) for value in values), status, reference or ""])

    lines = [
        "# MCU 燒錄 A/B 逐欄差異",
        "",
        "依序比較 Python 0.3.2、Python origin/main、Rust。任一欄缺值或有差異而沒有可驗證歸因時，工具以 exit 2 結束。",
        "",
        "| 案例 | 測試節點 | 欄位 | Python 0.3.2 | Python origin/main | Rust | 判定 | 歸因依據 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows_out:
        lines.append("| " + " | ".join(value.replace("|", "\\|").replace("\n", " ") for value in row) + " |")
    lines.extend(("", "三輪一致。" if not unattributed and all(row[6] == "三輪一致" for row in rows_out)
                  else ("發現未歸因差異。" if unattributed else "差異皆有登記歸因。"), ""))
    return "\n".join(lines), unattributed


def _compare_two_round(data: dict[str, Any]) -> tuple[str, bool]:
    """比較 Python 0.3.2 與 Rust；只承認 OD-91 報告列出的六個精確欄值。"""
    runs = {name: data.get(name) for name in ("python032", "rust")}
    for name, rows in runs.items():
        if not isinstance(rows, dict) or not rows:
            raise ValueError(f"{name} 輪觀察資料必須是非空 object")
        for row_key, fields in rows.items():
            if not isinstance(row_key, str) or not isinstance(fields, dict):
                raise ValueError(f"{name} 輪每案必須是 case/node → fields object")

    expected_rows = set(runs["python032"])
    actual_rows = set(runs["rust"])
    if actual_rows != expected_rows:
        missing = sorted(expected_rows - actual_rows)
        extra = sorted(actual_rows - expected_rows)
        raise ValueError(f"rust 輪 observation 案例不齊：缺少={missing!r}、額外={extra!r}")

    attributions = data.get("attributions", {})
    if not isinstance(attributions, dict):
        raise ValueError("attributions 必須是 object")
    rows_out: list[list[str]] = []
    failed = False
    for row_key in sorted(expected_rows):
        py_fields = runs["python032"][row_key]
        rust_fields = runs["rust"][row_key]
        expected_fields = set(py_fields)
        actual_fields = set(rust_fields)
        if expected_fields != actual_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            raise ValueError(f"rust 輪 {row_key} fields 不齊：缺少={missing!r}、額外={extra!r}")
        if not expected_fields:
            raise ValueError(f"{row_key} observation fields 不得為空")

        case_id, nodeid = _case_node(row_key)
        for field in sorted(expected_fields):
            py_value = py_fields[field]
            rust_value = rust_fields[field]
            if py_value == rust_value:
                status = "兩輪一致"
                reference = ""
            elif _TWO_ROUND_KNOWN_DIFFERENCES.get(row_key, {}).get(field) == (py_value, rust_value):
                status = "OD-91 六欄已知差異"
                reference = "mcu-flash-parity-0.3.2.md:175,180,215-216,247-248; OD-91"
            else:
                status = "未歸因"
                reference = ""
                failed = True
            rows_out.append([
                case_id,
                nodeid,
                field,
                _display(py_value),
                _display(rust_value),
                status,
                reference,
            ])

    lines = [
        "# MCU 燒錄兩輪逐欄差異",
        "",
        "依序比較 Python 0.3.2 與 Rust。缺少 observation 或非 OD-91 報告中六個精確差異時，以 exit 2 結束。",
        "",
        "| 案例 | 測試節點 | 欄位 | Python 0.3.2 | Rust | 判定 | 歸因依據 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows_out:
        lines.append("| " + " | ".join(value.replace("|", "\\|").replace("\n", " ") for value in row) + " |")
    lines.extend(("", "發現未歸因差異。" if failed else "差異皆限於 OD-91 已歸因六欄。", ""))
    return "\n".join(lines), failed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是 object：{path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bundle = sub.add_parser("bundle", help="將兩輪或三輪 NDJSON observations 合併為比較輸入")
    bundle.add_argument("--python032", type=Path, required=True)
    bundle.add_argument("--python-main", type=Path)
    bundle.add_argument("--rust", type=Path, required=True)
    bundle.add_argument("--attributions", type=Path)
    bundle.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("compare", help="產生逐案逐欄 Markdown 差異表")
    check.add_argument("--input", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "bundle":
            data: dict[str, Any] = {
                "python032": _load_rows(args.python032, "python032"),
                "rust": _load_rows(args.rust, "rust"),
                "attributions": _read_json(args.attributions) if args.attributions else {},
            }
            if args.python_main is not None:
                data["python_main"] = _load_rows(args.python_main, "python_main")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            return 0
        report, failed = compare(_read_json(args.input))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        return 2 if failed else 0
    except ValueError as exc:
        print(f"mcu_flash_parity: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
