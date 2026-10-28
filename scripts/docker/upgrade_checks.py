"""OD-33 R2(b)：0.3.2 → 0.4.0 升級驗證的判定純函式（容器內 driver 與 pytest 共用）。

只用標準函式庫：容器內 driver 以系統 python3 執行，不依賴 repo 套件。
每個檢查列為 {"id", "verdict": PASS|FAIL|INSUFFICIENT, "reason", "detail"}；整體 verdict 規則見 overall_verdict。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

# 每種監管模式都必須產出的檢查（順序即 driver 執行順序）。缺任何一項 → INSUFFICIENT。
REQUIRED_CHECKS: tuple[str, ...] = (
    "old_install_0_3_2",
    "seed_user_data",
    "doctor_green_old",
    "upgrade_with_old_daemon_running",
    "upgrade_after_stop",
    "entries_replaced",
    "config_preserved_upgrade",
    "new_daemon_started",
    "supervision_reconciled",
    "state_compatible_upgrade",
    "doctor_green_new",
    "rollback_command",
    "downgrade_manual",
    "config_preserved_downgrade",
    "state_compatible_downgrade",
    "doctor_green_downgrade",
    "reupgrade",
    "no_two_reader",
)

# 容器內沒有序列硬體（OD-15／OD-16 禁止把真實裝置帶進容器），doctor 的 devices advisory 必然為 0；其餘 advisory 須全綠。
ALLOWED_ADVISORY_FAILURES = frozenset({"devices"})

_DAEMON_NAMES = frozenset({"serialwrapd", "serialwrapd.exe", "serialwrapd.py"})


def row(check_id: str, verdict: str, reason: str | None = None, **detail: Any) -> dict[str, Any]:
    return {"id": check_id, "verdict": verdict, "reason": reason, "detail": detail}


def is_daemon_argv(argv: list[str]) -> bool:
    """argv 是否為 serialwrapd 行程本身（launcher、python 直譯器執行 serialwrapd(.py) 或 -m sw_core.daemon）。

    只看 argv[0] 與直譯器的第一個非選項參數；shell、runuser、grep 等命令字串中出現 serialwrapd 不算。
    """
    if not argv:
        return False
    base0 = os.path.basename(argv[0])
    if base0 in _DAEMON_NAMES:
        return True
    if not base0.startswith("python"):
        return False
    args = iter(argv[1:])
    for arg in args:
        if arg == "-m":
            return next(args, "") == "sw_core.daemon"
        if arg in ("-c", "-"):
            return False
        if arg.startswith("-"):
            continue
        return os.path.basename(arg) in _DAEMON_NAMES
    return False


def _read_argv(pid_dir: Path) -> list[str] | None:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _read_ppid(pid_dir: Path) -> int | None:
    try:
        text = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm 以括號包住且可能含空白：取最後一個 ')' 之後的欄位
    fields = text[text.rfind(")") + 1:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def scan_daemon_instances(proc_root: Path = Path("/proc")) -> list[int]:
    """回傳 daemon 實例的 PID（排序）。祖先鏈上另有 daemon 行程者（launcher 的子行程）併入祖先，不另計。"""
    daemons: dict[int, int | None] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        argv = _read_argv(entry)
        if argv and is_daemon_argv(argv):
            daemons[int(entry.name)] = _read_ppid(entry)
    roots = []
    for pid in daemons:
        seen = set()
        parent = daemons.get(pid)
        nested = False
        while parent and parent not in seen and parent != pid:
            if parent in daemons:
                nested = True
                break
            seen.add(parent)
            parent = _read_ppid(proc_root / str(parent))
        if not nested:
            roots.append(pid)
    return sorted(roots)


def judge_two_reader(samples: Iterable[tuple[float, list[int]]], *, min_samples: int) -> dict[str, Any]:
    """任何取樣同時有兩個以上 daemon 實例 → FAIL；取樣數不足 → INSUFFICIENT。"""
    samples = list(samples)
    overlaps = [{"t": t, "pids": sorted(pids)} for t, pids in samples if len(pids) > 1]
    seen: list[int] = []
    for _, pids in samples:
        for pid in pids:
            if pid not in seen:
                seen.append(pid)
    detail = {"samples": len(samples), "overlaps": overlaps[:20], "overlap_count": len(overlaps),
              "instances_seen": seen}
    if overlaps:
        return row("no_two_reader", "FAIL", "同一時間出現兩個以上 serialwrapd 實例（two-reader）", **detail)
    if len(samples) < min_samples:
        return row("no_two_reader", "INSUFFICIENT", f"取樣數 {len(samples)} < {min_samples}", **detail)
    return row("no_two_reader", "PASS", None, **detail)


def snapshot_files(root: Path) -> dict[str, str]:
    """root 下所有一般檔案的相對路徑 → sha256（不跟隨 symlink 目錄）。"""
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def judge_files_preserved(check_id: str, before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    missing = sorted(set(before) - set(after))
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    added = sorted(set(after) - set(before))
    if missing or changed:
        return row(check_id, "FAIL", "既有設定或 profile 遺失／被改寫", missing=missing, changed=changed, added=added)
    return row(check_id, "PASS", None, files=len(before), added=added)


def _alias_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(entry.get("alias")), str(entry.get("session_id") or ""), str(entry.get("device_by_id") or ""),
            str(entry.get("profile") or ""))


def judge_state_compatible(base: dict[str, Any], now: dict[str, Any], check_id: str = "state_compatible") -> dict[str, Any]:
    """alias（名稱、session_id、by-id、profile；不含時間戳）與 session 的 by-id 綁定須與基線相同。"""
    base_aliases = {_alias_key(a) for a in base.get("aliases") or []}
    now_aliases = {_alias_key(a) for a in now.get("aliases") or []}
    base_bind = {s.get("session_id"): s.get("device_by_id") for s in base.get("sessions") or []}
    now_bind = {s.get("session_id"): s.get("device_by_id") for s in now.get("sessions") or []}
    aliases_missing = sorted(base_aliases - now_aliases)
    bindings_changed = sorted(str(sid) for sid, dev in base_bind.items() if now_bind.get(sid) != dev)
    detail = {"aliases_missing": [list(a) for a in aliases_missing], "bindings_changed": bindings_changed,
              "aliases_added": [list(a) for a in sorted(now_aliases - base_aliases)]}
    if aliases_missing or bindings_changed:
        return row(check_id, "FAIL", "alias 或 binding 與升級前不一致", **detail)
    return row(check_id, "PASS", None, **detail)


def judge_doctor(check_id: str, doctor: dict[str, Any] | None, *, require_pairing: bool) -> dict[str, Any]:
    if not isinstance(doctor, dict) or not isinstance(doctor.get("checks"), list):
        return row(check_id, "FAIL", "doctor 輸出不可解析")
    checks = [c for c in doctor["checks"] if isinstance(c, dict)]
    hard = sorted(c.get("check") for c in checks if not c.get("ok") and not c.get("advisory"))
    advisory = sorted(c.get("check") for c in checks if not c.get("ok") and c.get("advisory"))
    extra_adv = sorted(set(advisory) - ALLOWED_ADVISORY_FAILURES)
    pairing = next((c for c in checks if c.get("check") == "version_pairing"), None)
    detail = {"ok": doctor.get("ok"), "failed": hard, "advisory_failed": advisory,
              "version_pairing": None if pairing is None else pairing.get("ok")}
    if hard or doctor.get("ok") is not True:
        return row(check_id, "FAIL", "doctor 有非 advisory 檢查失敗", **detail)
    if extra_adv:
        return row(check_id, "FAIL", f"doctor advisory 失敗超出容器允許集合 {sorted(ALLOWED_ADVISORY_FAILURES)}", **detail)
    if require_pairing and (pairing is None or pairing.get("ok") is not True):
        return row(check_id, "FAIL", "release 安裝的 doctor 缺 version_pairing 或未通過", **detail)
    return row(check_id, "PASS", None, **detail)


def judge_refusal_untouched(check_id: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """setup 拒絕時：daemon PID、入口 symlink 目標與設定檔雜湊都必須與拒絕前相同。"""
    diffs = [key for key in ("daemon_pids", "entries", "files") if before.get(key) != after.get(key)]
    if not before.get("daemon_pids"):
        return row(check_id, "INSUFFICIENT", "拒絕前沒有執行中的舊 daemon，無法驗證", before=before)
    if diffs:
        return row(check_id, "FAIL", "setup 拒絕後安裝現場已被改動", changed=diffs, before=before, after=after)
    return row(check_id, "PASS", None, kept=["daemon_pids", "entries", "files"])


def overall_verdict(rows: list[dict[str, Any]]) -> str:
    """任一 FAIL 或檢查 id 重複 → FAIL；缺必要檢查或任一 INSUFFICIENT → INSUFFICIENT；否則 PASS。"""
    ids = [r.get("id") for r in rows]
    if len(ids) != len(set(ids)) or any(r.get("verdict") == "FAIL" for r in rows):
        return "FAIL"
    if set(REQUIRED_CHECKS) - set(ids) or any(r.get("verdict") != "PASS" for r in rows):
        return "INSUFFICIENT"
    return "PASS"
