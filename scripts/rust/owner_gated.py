#!/usr/bin/env python3
"""擁有者機台／權限相依的 cargo 測試登記與掃描（`stage1a_gate.py` 與 `check_p4_cases.py` 共用）。

用途（計畫 04 §3 B3、05 附錄 B／B-02、規格 §11.3、§11.4）：
- 名稱以 `cap_` 開頭的測試是資源相依測試（`#[ignore = "cap:<resource>"]`）；它們在一般 CI／WSL 上為 skipped，
  代表「擁有者機台尚未執行」。這類 skipped 列必須以 BLOCKED-OWNER 列出，不得因為不在凍結鎖內就被靜默忽略。
- `OWNER_GATED` 另登記不以 `cap_` 開頭、但同屬擁有者機台的 ignored 測試（`windows_switch_running_exe_rename`）。
- 登記表內的列，若在目前 selector 涵蓋的 crate 中、且該平台會編譯進來，卻在報告的 results 與 collected 皆缺席，
  代表收集失效（例如 selector 寫錯或測試被刪），判 INSUFFICIENT，不得以 BLOCKED-OWNER 帶過。

本模組只做「列舉與分類」，不做 PASS 判定；判定仍由 `tests/acceptance/l1_judge.py` 負責（不改其規則）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 名稱 → {crate：測試所在套件；platforms：該測試會在哪些平台被編譯（cfg 結果）}。
# 來源：plan 04 §3 B3（cap_interactive_desktop__）、plan 05 附錄 B B-02（cap_supervisor_runtime__）、
# plan 05 Global 驗收列（cap_windows_native__launcher_*、windows_switch_running_exe_rename）。
OWNER_GATED: dict[str, dict[str, Any]] = {
    "cap_interactive_desktop__exec_or_wait_ctrl_c": {
        "crate": "sw-pal", "platforms": ("linux", "windows"),
    },
    "cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure": {
        "crate": "sw-pal", "platforms": ("linux",),
    },
    "cap_supervisor_runtime__windows_task_roundtrip": {
        "crate": "sw-pal", "platforms": ("windows",),
    },
    "cap_windows_native__launcher_parent_kill_kills_child": {
        "crate": "sw-daemon", "platforms": ("linux", "windows"),
    },
    "cap_windows_native__launcher_gui_subsystem": {
        "crate": "sw-daemon", "platforms": ("linux", "windows"),
    },
    "windows_switch_running_exe_rename": {
        "crate": "sw-cli", "platforms": ("linux", "windows"),
    },
}

OWNER_GATED_PREFIX = "cap_"
_SKIP_OUTCOMES = frozenset({"skipped", "xfailed"})


def name_of_nodeid(nodeid: str) -> str:
    """`cargo:<crate>::<target>::<name>` 的最後一段即測試函式名。"""
    return str(nodeid).rsplit("::", 1)[-1]


def crate_of(nodeid: str) -> str | None:
    """`cargo:<crate>::...` 取 crate；格式不符回 None。"""
    text = str(nodeid)
    if not text.startswith("cargo:"):
        return None
    head = text[len("cargo:"):].split("::", 1)[0]
    return head or None


def is_owner_gated_name(name: str) -> bool:
    return name.startswith(OWNER_GATED_PREFIX) or name in OWNER_GATED


def covered_crates(argv: Sequence[str]) -> set[str] | None:
    """cargo selector 涵蓋的 crate；`--workspace` 回 None（表示全部）。"""
    tokens = [str(x) for x in argv]
    if "--workspace" in tokens:
        return None
    crates: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token in ("--package", "-p"):
            crates.add(tokens[index + 1])
    return crates


def expected_names(platform: str, argv: Sequence[str]) -> list[tuple[str, str]]:
    """本平台、本 selector 下「必須出現在報告中」的登記列，回傳 (crate, name)。"""
    covered = covered_crates(argv)
    rows: list[tuple[str, str]] = []
    for name, meta in OWNER_GATED.items():
        if platform not in meta["platforms"]:
            continue
        if covered is not None and meta["crate"] not in covered:
            continue
        rows.append((str(meta["crate"]), name))
    return sorted(rows)


def scan_cargo_report(report: Mapping[str, Any], platform: str) -> dict[str, Any]:
    """掃描一份 `l1-results.v1`（runner=cargo）報告。

    回傳：
    - `owner_gated_skipped`：報告中 skipped（或 xfailed）且屬擁有者機台相依的 nodeid（已排序）。
    - `absent_expected`：登記中應存在卻在 results 與 collected 皆缺席的 `crate::name`。
    """
    argv = [str(x) for x in (report.get("argv") or [])]
    results = [row for row in (report.get("results") or []) if isinstance(row, Mapping)]
    collected = [str(x) for x in (report.get("collected") or [])]
    present = {str(row.get("nodeid")) for row in results} | set(collected)

    owner_skipped = sorted({
        str(row.get("nodeid"))
        for row in results
        if row.get("outcome") in _SKIP_OUTCOMES and is_owner_gated_name(name_of_nodeid(str(row.get("nodeid"))))
    })

    absent: list[str] = []
    for crate, name in expected_names(platform, argv):
        if not any(crate_of(nid) == crate and name_of_nodeid(nid) == name for nid in present):
            absent.append(f"{crate}::{name}")
    return {"owner_gated_skipped": owner_skipped, "absent_expected": absent}


def insufficiency_owner_only(judged: Mapping[str, Any], owner_skipped: Sequence[str]) -> bool:
    """judge 的 INSUFFICIENT 是否「只」由擁有者机台列未執行造成（其餘缺口一律不算）。

    judged：`l1_judge.judge` 的回傳；owner_skipped：本報告中屬擁有者機台相依的 skipped nodeid。
    只有 required_skipped 全部落在 owner_skipped 內，且無 missing／not_run／deselected／duplicate，
    且 reasons 只剩「required skipped」與「required case not satisfied」時才回 True。
    """
    if judged.get("missing") or judged.get("not_run") or judged.get("deselected_required") or judged.get("duplicates"):
        return False
    skipped = {str(row.get("nodeid")) for row in judged.get("skipped_required", [])}
    if not skipped or not skipped <= set(owner_skipped):
        return False
    extra = [
        reason for reason in judged.get("reasons", [])
        if not (str(reason).startswith("required skipped:") or reason == "required case not satisfied")
    ]
    return not extra
