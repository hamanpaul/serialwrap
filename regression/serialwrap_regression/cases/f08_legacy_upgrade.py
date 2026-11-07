"""F8 daemon 單一性：0.3.x → 0.4.x 執行中直接升級的 two-reader 防線（SC-39 D5.2，PR 250）。

0.3.x daemon 以 systemd --user 監管且 attach 實體 UART、同使用者有真實 ``systemd --user`` 與 ``(sd-pam)`` 時，候選 bundle 的
``setup`` 必須在有界時間內切換到新 daemon，且全程不出現 two-reader。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False, pending_issue=False):
    def deco(fn):
        register(Case(id=id, family="F8", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints),
                      pending_issue=pending_issue))
        return fn
    return deco


# entry: TestPilot ``serialwrap_regression --case f8-legacy-attached-upgrade``（``allow_destructive: true``；單獨執行）
# expected: 0.3.x daemon 以 systemd --user 監管、已 attach 實體 UART，且同使用者有真實 ``systemd --user`` 與 ``(sd-pam)`` 時，
#   候選 bundle 的 ``setup --user`` 在 120 s 內成功（不得等滿 330 s 回 ``TIMEOUT``／``legacy_stop_pending``），報告帶
#   ``legacy_stop``；舊行程樹（pid＋start_ticks）結束、之後只剩一個 serialwrapd（無 two-reader）、新 daemon 為候選版本，
#   升級前已 attach 的每個 COM 由新 daemon 重新 attach 同一個 tty。
# owner: 擁有者實機（接板的 Linux／WSL 主機，有 systemd user session；非 root 執行）
# precondition（擁有者事先備妥，case 不自動降版）：pinned CLI 與 daemon 為 0.3.x（pipx 安裝、``serialwrap setup --user``），
#   至少一個 session 已 attach 實體 UART；testbed.yaml 設 ``legacy_upgrade.bundle``（候選 0.4.x release bundle 目錄）。
#   條件不符時 SKIP（configuration／environment），不改動任何安裝。
# evidence_dir: TestPilot report 目錄下的 case 目錄（setup 輸出、升級前後 daemon status／session list、行程快照）
# rollback: case 不還原 0.3.x。PASS 後主機即為候選版本；要回到升級前狀態，以候選 CLI 執行 ``serialwrap setup --rollback``，
#   或依 0.3.x 安裝程序重新以 pipx 安裝。setup 失敗時依回應的 ``restart_hint`` 手動重啟舊 daemon（OD-48：失敗不自動重啟）。
# issue: #257（PR 250 的缺陷追蹤 issue）。Docker ``attached-legacy`` 情境是自動化部分，
#   用 PTY 假裝置，不能取代本 case 的真實 UART 與真實登入 session。

_LEGACY_UPGRADE_MAX_S = 120.0  # 與 Docker attached-legacy 判準一致（scripts/docker/upgrade_checks.py ATTACHED_LEGACY_MAX_SETUP_S）
_LEGACY_SETUP_TIMEOUT_S = 420.0  # 大於產品等待上限 330 s：缺陷再現時讓 setup 自己回 legacy_stop_pending
_ATTACHED_STATES = ("READY", "ATTACHED")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def is_legacy_version(text: str | None) -> bool:
    """0.3.x 以前（< 0.4.0）；無法解析 → False（不得把未知版本當成前置條件成立）。"""
    version = _parse_version(text)
    return version is not None and version < (0, 4, 0)


def _proc_start_ticks(proc_root: Path, pid: int) -> int | None:
    try:
        raw = (proc_root / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    fields = raw[raw.rfind(b")") + 1:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def same_uid_processes(proc_root: Path = Path("/proc"), uid: int | None = None) -> list[dict]:
    """四個 uid 都等於 ``uid`` 的行程：pid、comm、argv、start_ticks。"""
    me = os.getuid() if uid is None else uid
    out = []
    for entry in proc_root.iterdir() if proc_root.is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            argv = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue
        line = next((row for row in status.splitlines() if row.startswith("Uid:")), "")
        uids = line.split()[1:5]
        if len(uids) != 4 or any(value != str(me) for value in uids):
            continue
        pid = int(entry.name)
        out.append({"pid": pid, "comm": comm, "argv": argv, "start_ticks": _proc_start_ticks(proc_root, pid)})
    return sorted(out, key=lambda item: item["pid"])


def has_systemd_user_session(procs: list[dict]) -> bool:
    """同使用者同時有 ``systemd --user`` 與 ``(sd-pam)``（缺陷的觸發條件）。"""
    user_manager = any(p["comm"] == "systemd" and "--user" in p["argv"] for p in procs)
    sd_pam = any(p["comm"] == "(sd-pam)" for p in procs)
    return user_manager and sd_pam


def serialwrapd_pids(procs: list[dict]) -> list[int]:
    """serialwrapd 實例（原生執行檔、Python script 或 ``-m sw_core.daemon``）；子行程不另計。"""
    def is_daemon(argv: list[str]) -> bool:
        if not argv:
            return False
        if os.path.basename(argv[0]) == "serialwrapd":
            return True
        if any(part.endswith("serialwrapd.py") for part in argv):
            return True
        if any("/" in part and os.path.basename(part) == "serialwrapd" for part in argv[1:]):
            return True
        return any(a == "-m" and b == "sw_core.daemon" for a, b in zip(argv, argv[1:]))
    return [p["pid"] for p in procs if is_daemon(p["argv"])]


def _find_key(value, key: str):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found is not None:
            return found
    return None


def evaluate_legacy_attached_upgrade(obs: dict) -> CaseResult:
    """``f8-legacy-attached-upgrade`` 的純判定（pytest 覆蓋）。

    obs 欄位：``setup_rc``（逾時為 ``None``）、``body``（setup 的 JSON 回應或 ``None``）、``elapsed_s``、
    ``old_tree_alive``（升級後仍存活的舊行程 pid）、``daemons_after``（升級後的 serialwrapd pid）、``expected_version``
    （候選 CLI 版本）、``version_after``（升級後 daemon 版本）、``attached_before``（{COM: tty}）、``sessions_after``
    （升級後 ``session list`` 的 sessions）。
    """
    body = obs.get("body") or {}
    elapsed = float(obs.get("elapsed_s") or 0.0)
    if obs.get("setup_rc") is None:
        return CaseResult("FAIL", reason=f"setup {elapsed:.0f} s 未返回（PR 250 回歸：等待舊 daemon 停止卡住）",
                          category="test", reason_code="setup_hung")
    if body.get("error_code") == "TIMEOUT" and body.get("reason") == "legacy_stop_pending":
        return CaseResult(
            "FAIL",
            reason=f"setup 等 {elapsed:.0f} s 後回 TIMEOUT／legacy_stop_pending（PR 250 回歸：holder 掃描被無法檢查的"
            f"同使用者行程擋住；holders_status={body.get('holders_status')!r}、last_unstable={body.get('last_unstable')!r}）",
            category="test", reason_code="legacy_stop_pending")
    if obs.get("setup_rc") != 0 or body.get("ok") is not True:
        return CaseResult("FAIL", reason=f"setup 失敗：rc={obs.get('setup_rc')} error_code={body.get('error_code')!r}"
                          f" reason={body.get('reason')!r}", category="test", reason_code="setup_failed")
    if elapsed >= _LEGACY_UPGRADE_MAX_S:
        return CaseResult("FAIL", reason=f"setup 成功但耗時 {elapsed:.1f} s（上限 {_LEGACY_UPGRADE_MAX_S:.0f} s）",
                          category="test", reason_code="setup_too_slow")
    legacy_stop = _find_key(body, "legacy_stop")
    if not isinstance(legacy_stop, dict):
        return CaseResult("FAIL", reason="setup 成功但報告沒有 legacy_stop（未走 0.3.x 停止路徑，無法驗證本缺陷）",
                          category="test", reason_code="legacy_stop_missing")
    reported = legacy_stop.get("uninspectable_procs") or []
    if reported and legacy_stop.get("holders_status") != "partial":
        return CaseResult("FAIL", reason=f"legacy_stop 列出無法檢查的行程卻未標 holders_status partial：{legacy_stop!r}",
                          category="test", reason_code="uninspectable_not_marked_partial")
    if obs.get("old_tree_alive"):
        return CaseResult("FAIL", reason=f"setup 成功但舊 daemon 行程仍存活：{obs.get('old_tree_alive')}",
                          category="test", reason_code="legacy_tree_alive")
    daemons = list(obs.get("daemons_after") or [])
    if len(daemons) != 1:
        return CaseResult("FAIL", reason=f"升級後 serialwrapd 實例數 {len(daemons)}（{daemons}），應恰為 1（two-reader）",
                          category="test", reason_code="two_reader")
    expected = _parse_version(obs.get("expected_version"))
    if expected is None or _parse_version(obs.get("version_after")) != expected:
        return CaseResult("FAIL", reason=f"新 daemon 版本 {obs.get('version_after')!r} ≠ 候選 {obs.get('expected_version')!r}",
                          category="test", reason_code="new_daemon_version_mismatch")
    after = {s.get("com"): s for s in obs.get("sessions_after") or []}
    missing = sorted(
        com for com, tty in (obs.get("attached_before") or {}).items()
        if (after.get(com) or {}).get("attached_real_path") != tty
        or (after.get(com) or {}).get("state") not in _ATTACHED_STATES
    )
    if missing:
        return CaseResult("FAIL", reason=f"升級前已 attach 的 {missing} 未由新 daemon 重新 attach 同一個 tty",
                          category="test", reason_code="uart_not_reattached")
    return CaseResult("PASS")


def _json_or_none(text: str):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@_case(
    "f8-legacy-attached-upgrade",
    "0.3.x daemon attach 實體 UART 且有 systemd user session 時，0.4.x setup 在有界時間內切換、無 two-reader",
    issues=("#257",),
    destructive=True,
    requires=("supervisor_runtime",),
    hints=(
        "缺陷（#257，PR 250）：同使用者的 (sd-pam) 與 systemd --user 整個無法檢查 fd，0.4.0 把 holder 掃描判為讀取失敗，"
        "setup 等滿 330 s 回 legacy_stop_pending（alive_pids、holder_pids 皆空），舊 daemon 已停且不重啟。",
        "Docker attached-legacy 情境用 PTY 假裝置自動化同一路徑；本 case 補真實 UART、真實登入 session 與外部工具的實機證據。",
        "前置條件由擁有者備妥（0.3.x pipx＋setup --user、session 已 attach）；條件不符一律 SKIP，不改動安裝。",
    ),
)
def f8_legacy_attached_upgrade(ctx):
    """0.3.x 執行中且 attach 實體 UART 時，候選 bundle 的 setup 必須在有界時間內完成切換（PR 250 回歸防線）。"""
    settings = ctx.cfg.get("legacy_upgrade") or {}
    bundle = str(settings.get("bundle") or "").strip()
    if not bundle:
        return CaseResult("SKIP", reason="testbed 未設定 legacy_upgrade.bundle（候選 0.4.x bundle 目錄）",
                          category="configuration", reason_code="legacy_upgrade_bundle_unset")
    cli = Path(bundle).expanduser() / "bin" / "serialwrap"
    if not cli.is_file():
        return CaseResult("SKIP", reason=f"候選 bundle 缺 {cli}", category="configuration",
                          reason_code="legacy_upgrade_bundle_missing")
    if os.getuid() == 0:
        return CaseResult("SKIP", reason="以 root 執行：無法重現同使用者行程 fd 讀不到的情境", category="environment",
                          reason_code="running_as_root")
    status = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status-before.json", json.dumps(status, ensure_ascii=False, sort_keys=True))
    if not is_legacy_version(status.get("version")):
        return CaseResult("SKIP", reason=f"daemon 版本 {status.get('version')!r} 不是 0.3.x；請先依前置條件備妥舊版",
                          category="environment", reason_code="daemon_not_legacy")
    proc_root = Path("/proc")
    before = same_uid_processes(proc_root)
    ctx.note("procs-before.json", json.dumps(before, ensure_ascii=False, sort_keys=True))
    if not has_systemd_user_session(before):
        return CaseResult("SKIP", reason="同使用者缺 systemd --user 或 (sd-pam)（沒有 systemd user session）",
                          category="environment", reason_code="no_systemd_user_session")
    sessions = (ctx.sw.run("session", "list") or {}).get("sessions") or []
    ctx.note("sessions-before.json", json.dumps(sessions, ensure_ascii=False, sort_keys=True))
    attached = {s["com"]: s["attached_real_path"] for s in sessions
                if s.get("com") and str(s.get("attached_real_path") or "").startswith("/dev/")}
    if not attached:
        return CaseResult("SKIP", reason="0.3.x daemon 沒有 attach 任何實體 UART（attached_real_path 皆空）",
                          category="environment", reason_code="no_attached_uart")
    legacy_tree = [(pid, _proc_start_ticks(proc_root, pid)) for pid in serialwrapd_pids(before)]
    expected_version = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    setup_args = [str(arg) for arg in (settings.get("setup_args") or ["--user"])]
    started = time.monotonic()
    try:
        proc = subprocess.run([str(cli), "setup", *setup_args], capture_output=True, text=True,
                              timeout=float(settings.get("setup_timeout_s") or _LEGACY_SETUP_TIMEOUT_S))
        setup_rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        setup_rc, stdout, stderr = None, str(exc.stdout or ""), str(exc.stderr or "")
    elapsed = time.monotonic() - started
    ctx.note("setup-stdout.json", stdout)
    ctx.note("setup-stderr.txt", stderr)
    version_after = None
    sessions_after: list = []
    if setup_rc == 0:
        version_after = (_json_or_none(subprocess.run([str(cli), "daemon", "status"], capture_output=True, text=True,
                                                      timeout=30).stdout) or {}).get("version")
        deadline = time.monotonic() + float(ctx.cfg["timeouts"]["ready_wait_s"])
        while True:
            listed = _json_or_none(subprocess.run([str(cli), "session", "list"], capture_output=True, text=True,
                                                  timeout=30).stdout) or {}
            sessions_after = listed.get("sessions") or []
            by_com = {s.get("com"): s for s in sessions_after}
            if all((by_com.get(com) or {}).get("state") in _ATTACHED_STATES for com in attached):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(2)
        ctx.note("sessions-after.json", json.dumps(sessions_after, ensure_ascii=False, sort_keys=True))
    after = same_uid_processes(proc_root)
    ctx.note("procs-after.json", json.dumps(after, ensure_ascii=False, sort_keys=True))
    obs = {
        "setup_rc": setup_rc,
        "body": _json_or_none(stdout),
        "elapsed_s": elapsed,
        "old_tree_alive": [pid for pid, ticks in legacy_tree if ticks is not None and _proc_start_ticks(proc_root, pid) == ticks],
        "daemons_after": serialwrapd_pids(after),
        "expected_version": expected_version,
        "version_after": version_after,
        "attached_before": attached,
        "sessions_after": sessions_after,
    }
    return evaluate_legacy_attached_upgrade(obs)
