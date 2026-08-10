"""同機多開（two-reader）被動偵測 helper（#101）。

掃 ``/proc`` 找其他 ``serialwrapd`` 程序，best-effort 讀 ``/proc/<pid>/fd`` 找哪個
程序持有目標 tty。**純偵測 + 回報，不終止任何 daemon、不退讓、無背景週期掃描**，
全 on-demand（由 doctor 與 daemon status 各自呼叫）。

module-level 函式（非 SessionManager method）：不複用 ``_probe_external_holder``——
後者綁 SessionManager、只回 pid，且語意是「某 real_path 被誰持有」；本 helper 語意是
「同機是否另有 serialwrapd、各持哪條 tty」，且須能在 daemon-less 的 doctor 程序內獨立執行。

跨 uid 讀不到 fd symlink 時**明確降級**為 ``permission`` / ``unknown`` 狀態（此時只能
確認「另有 serialwrapd 存在」，無法判定持有哪條 tty）。此降級資訊本身即為輸出契約的一部分。
"""

from __future__ import annotations

import os


def _iter_pids(proc_root: str):
    """列舉 proc_root 下的數字 pid 目錄。"""
    for name in os.listdir(proc_root):
        if name.isdigit():
            yield int(name)


def _is_serialwrapd(proc_root: str, pid: int) -> bool:
    """判定 ``/proc/<pid>`` 是否為 serialwrapd daemon 程序（#101 5a）。

    需涵蓋三種真實啟動形式，同時排除 ``grep serialwrapd`` 這類「裸字串含 serialwrapd」誤判：

    - argv0 的 basename 為 ``serialwrapd``（直接 exec console_script，或 pipx 入口），或
    - 某個**路徑型**參數（含 ``/``）的 basename 為 ``serialwrapd``——pipx/venv console_script
      被 python 執行的形式 ``python /path/bin/serialwrapd --socket ...``（argv0=python、
      argv1=可執行檔路徑、basename 無 ``.py``；**這是實機 prod daemon 的形式**），或
    - 某參數以 ``serialwrapd.py`` 結尾（薄 shim ``python serialwrapd.py``）。

    關鍵：對裸名 ``serialwrapd`` 要求含路徑分隔 ``/`` 才算，以排除 ``grep serialwrapd``
    （arg 為裸字串、無路徑）的誤判；``serialwrapd.py`` 因副檔名特異性高，放寬不要求路徑。

    不在此處做 TGID 去重：``/proc`` 頂層只列 thread group leader（TGID）、不列同 group 的子
    thread，故同一 daemon 不會在頂層重複出現，無需去重。
    """
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    parts = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]
    if not parts:
        return False
    if os.path.basename(parts[0]) == "serialwrapd":
        return True
    # 任一參數（含 argv0）以 serialwrapd.py 結尾：薄 shim，無論直接 exec 或 python 執行。
    if any(p.endswith("serialwrapd.py") for p in parts):
        return True
    # console_script 路徑形式（python /path/bin/serialwrapd）：某路徑型參數 basename 為
    # serialwrapd 且含 '/' 才算，避免 `grep serialwrapd` 裸字串誤判。
    for p in parts[1:]:
        if "/" in p and os.path.basename(p) == "serialwrapd":
            return True
    return False


def _extract_socket_arg(proc_root: str, pid: int) -> str | None:
    """讀 ``/proc/<pid>/cmdline`` 擷取 ``--socket`` 參數值（#173）。

    支援 ``--socket X``（分開兩個 argv）與 ``--socket=X``（單一 argv）兩種形式，
    對應 :func:`sw_core.daemon.build_parser` 用 argparse 產生的 argv。讀不到 cmdline
    或找不到 ``--socket`` 一律回傳 ``None``（呼叫端須視為「無法判定，保守當作衝突」）。
    """
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    parts = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]
    for i, part in enumerate(parts):
        if part == "--socket" and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith("--socket="):
            return part[len("--socket="):]
    return None


def detect_multi_open(proc_root: str = "/proc", tty_paths: list[str] | None = None) -> dict:
    """偵測同機多開與 tty 持有者。

    Args:
        proc_root: procfs 根（測試可注入 fake /proc）。
        tty_paths: 要比對持有者的 tty real_path 清單（daemon status 帶入已 attach 的裝置）。

    Returns:
        ``{multi_open, daemons:[{pid, socket}], holders:{tty:pid}, holders_status}``。
        每個 daemon 的 ``socket``（#173）為從其 cmdline 擷取的 ``--socket`` 參數值，
        擷取不到時為 ``None``（呼叫端如 ``serialwrap daemon start`` 的 spawn 防線、
        doctor 的 ``endpoint_reachable`` 須視為「無法判定，保守處理」）。
        ``holders_status``：

        - ``ok``：所有 daemon 的 fd 都讀得到。
        - ``permission``：至少一個 daemon 的 fd 因權限讀不到（仍確認多開存在）。
        - ``unknown``：連 proc_root 都列不出（procfs 不可用）。
    """
    tty_paths = tty_paths or []
    try:
        pids = list(_iter_pids(proc_root))
    except OSError:
        return {"multi_open": False, "daemons": [], "holders": {}, "holders_status": "unknown"}

    daemons = [
        {"pid": pid, "socket": _extract_socket_arg(proc_root, pid)}
        for pid in pids
        if _is_serialwrapd(proc_root, pid)
    ]

    holders: dict[str, int] = {}
    status = "ok"
    tty_set = set(tty_paths)
    for info in daemons:
        pid = info["pid"]
        fd_dir = f"{proc_root}/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            status = "permission"
            continue
        except OSError:
            if status == "ok":
                status = "unknown"
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target in tty_set:
                holders[target] = pid

    return {
        "multi_open": len(daemons) > 1,
        "daemons": sorted(daemons, key=lambda d: d["pid"]),
        "holders": holders,
        "holders_status": status,
    }
