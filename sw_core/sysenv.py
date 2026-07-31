"""可注入的系統效果介面（Effects boundary）。

供 service、setup、doctor 等指令在不直接散佈 subprocess/os 呼叫的情況下
與底層系統互動，並讓單元測試可用 FakeEffects 代換真實 I/O。

公開介面（Protocol：Effects）：
    run(cmd)             — 執行外部指令，回傳 (returncode, stdout, stderr)
    has_systemd()        — 判斷 systemd 是否為 init
    user_in_group(g)     — 判斷目前使用者是否屬於群組 g
    is_wsl()             — 判斷是否執行於 WSL 環境
    which(name)          — 回傳二進位完整路徑或 None
    which_all(name)      — 回傳 PATH 上所有相符可執行檔的完整路徑清單（#154）
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Dict, FrozenSet, List, Optional, Protocol, Tuple


# ---------------------------------------------------------------------------
# Protocol（型別提示用；不強制繼承）
# ---------------------------------------------------------------------------

class Effects(Protocol):
    """所有 effects 實作須滿足的介面。"""

    def run(self, cmd: List[str], *, timeout_s: Optional[float] = None) -> Tuple[int, str, str]:
        """執行外部指令，回傳 (returncode, stdout, stderr)。

        ``timeout_s``（#154）：``None``（預設）與修改前行為逐位元組相同（不設限）；
        逾時回 ``(-1, "", "TIMEOUT")`` 而非拋例外。
        """
        ...

    def has_systemd(self) -> bool:
        """回傳 True 若 systemd 是目前的 init。"""
        ...

    def user_in_group(self, group: str) -> bool:
        """回傳 True 若目前使用者屬於指定群組。"""
        ...

    def is_wsl(self) -> bool:
        """回傳 True 若執行環境為 WSL。"""
        ...

    def which(self, name: str) -> Optional[str]:
        """回傳 `name` 二進位的完整路徑，找不到則回傳 None。"""
        ...

    def which_all(self, name: str) -> List[str]:
        """回傳 PATH 上所有相符可執行檔的完整路徑清單，去重（#154；`which()` 只回第一個）。"""
        ...


# ---------------------------------------------------------------------------
# SystemEffects — 真實實作
# ---------------------------------------------------------------------------

class SystemEffects:
    """呼叫真實系統 API 的 effects 實作。

    探測方法（has_systemd、user_in_group、is_wsl、which）一律不拋例外，
    發生錯誤時回傳安全預設值（False / None）。
    run() 以 check=False 執行子行程，不拋 CalledProcessError。
    """

    def run(self, cmd: List[str], *, timeout_s: Optional[float] = None) -> Tuple[int, str, str]:
        """執行 cmd，回傳 (returncode, stdout, stderr)；非零返回碼不拋例外。

        ``timeout_s``（#154）：``None``（預設）不設限，與修改前逐位元組相同；逾時
        （``subprocess.TimeoutExpired``）回 ``(-1, "", "TIMEOUT")`` 而非讓例外穿越
        呼叫端——doctor 對 PATH 上未知來源的可執行檔跑 ``--version`` 時需要這道
        「永不卡住」防線。
        """
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return -1, "", "TIMEOUT"
        return result.returncode, result.stdout, result.stderr

    def has_systemd(self) -> bool:
        """若 /run/systemd/system 存在則判定 systemd 為 init，回傳 True。"""
        try:
            return os.path.isdir("/run/systemd/system")
        except Exception:
            return False

    def user_in_group(self, group: str) -> bool:
        """回傳 True 若目前使用者（含補充群組）屬於 group；群組不存在回傳 False。

        Windows 無 POSIX 群組概念（grp/os.getgroups 均不存在），一律回傳 False（#84 PORT-4）。
        """
        if sys.platform == "win32":
            # Windows 無 POSIX 群組概念（無 grp/os.getgroups）；一律回 False（#84 PORT-4）
            return False
        try:
            import grp  # noqa: PLC0415 — POSIX-only，延遲 import 使本模組在 Windows 可載入
            try:
                gid = grp.getgrnam(group).gr_gid
            except KeyError:
                return False
            # 取得補充群組 GID 清單（含主群組）
            supplementary = set(os.getgroups())
            supplementary.add(os.getegid())
            return gid in supplementary
        except Exception:
            return False

    def is_wsl(self) -> bool:
        """若 kernel release 字串含 'microsoft' 則判定為 WSL，回傳 True。"""
        try:
            return "microsoft" in platform.uname().release.lower()
        except Exception:
            return False

    def which(self, name: str) -> Optional[str]:
        """回傳 name 二進位的完整路徑；找不到或發生錯誤回傳 None。"""
        try:
            return shutil.which(name)
        except Exception:
            return None

    def which_all(self, name: str) -> List[str]:
        """回傳 PATH 上所有相符、可執行、名為 `name` 的完整路徑清單（#154）。

        `shutil.which()` 只回傳 PATH 掃描到的第一筆，本方法用於「同機是否有多份
        不同來源安裝」的診斷，需要看見全部相符項；依 PATH 目錄掃描順序去重。
        Windows 另掃 `PATHEXT`（`.EXE`/`.BAT`/...）副檔名，POSIX 僅比對可執行權限
        （`os.X_OK`）。發生錯誤（PATH 目錄不可讀等）安全略過該目錄，不拋例外。
        """
        try:
            path_env = os.environ.get("PATH", "")
            dirs = path_env.split(os.pathsep) if path_env else []
            if sys.platform == "win32":
                pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
                suffixes = [""] if os.path.splitext(name)[1] else pathext
            else:
                suffixes = [""]
            seen: set = set()
            out: List[str] = []
            for d in dirs:
                if not d:
                    continue
                for suf in suffixes:
                    candidate = os.path.join(d, name + suf)
                    try:
                        if not os.path.isfile(candidate):
                            continue
                        if sys.platform != "win32" and not os.access(candidate, os.X_OK):
                            continue
                    except OSError:
                        continue
                    if candidate not in seen:
                        seen.add(candidate)
                        out.append(candidate)
            return out
        except Exception:
            return []


# ---------------------------------------------------------------------------
# FakeEffects — 測試替身
# ---------------------------------------------------------------------------

class FakeEffects:
    """可配置的 effects 測試替身，紀錄所有 run() 呼叫以供斷言。

    參數：
        systemd    — has_systemd() 的回傳值（預設 False）
        in_groups  — user_in_group() 判斷為 True 的群組名稱集合
        wsl        — is_wsl() 的回傳值（預設 False）
        which      — {name: path} 映射，供 which() 查詢
        which_all  — {name: [path, ...]} 映射，供 which_all() 查詢（#154）；
                    未登記名稱回傳空清單
        commands   — {tuple(cmd): (rc, out, err)} 映射，供 run() 查詢；
                    未登記的指令預設回傳 (0, "", "")
    """

    def __init__(
        self,
        *,
        systemd: bool = False,
        in_groups: FrozenSet[str] | set[str] = frozenset(),
        wsl: bool = False,
        which: Optional[Dict[str, str]] = None,
        which_all: Optional[Dict[str, List[str]]] = None,
        commands: Optional[Dict[Tuple[str, ...], Tuple[int, str, str]]] = None,
    ) -> None:
        self._systemd = systemd
        self._in_groups: FrozenSet[str] = frozenset(in_groups)
        self._wsl = wsl
        self._which: Dict[str, str] = dict(which) if which else {}
        self._which_all: Dict[str, List[str]] = (
            {k: list(v) for k, v in which_all.items()} if which_all else {}
        )
        self._commands: Dict[Tuple[str, ...], Tuple[int, str, str]] = (
            dict(commands) if commands else {}
        )
        self.calls: List[List[str]] = []
        # 與 self.calls 同索引，記錄該次呼叫收到的 timeout_s（供測試選擇性斷言）。
        self.timeouts: List[Optional[float]] = []

    def run(self, cmd: List[str], *, timeout_s: Optional[float] = None) -> Tuple[int, str, str]:
        """紀錄呼叫並回傳映射結果；未登記指令回傳預設 (0, "", "")。

        ``timeout_s``（#154）：僅記錄供測試斷言，不影響查詢結果（假替身不會真的逾時）。
        """
        self.calls.append(list(cmd))
        self.timeouts.append(timeout_s)
        return self._commands.get(tuple(cmd), (0, "", ""))

    def has_systemd(self) -> bool:
        """回傳建構時設定的 systemd 旗標。"""
        return self._systemd

    def user_in_group(self, group: str) -> bool:
        """回傳 group 是否在 in_groups 中。"""
        return group in self._in_groups

    def is_wsl(self) -> bool:
        """回傳建構時設定的 wsl 旗標。"""
        return self._wsl

    def which(self, name: str) -> Optional[str]:
        """從 which 映射查詢 name；不存在回傳 None。"""
        return self._which.get(name)

    def which_all(self, name: str) -> List[str]:
        """從 which_all 映射查詢 name；不存在回傳空清單（#154）。"""
        return list(self._which_all.get(name, []))


# ---------------------------------------------------------------------------
# Windows console 編碼修正（#118）
# ---------------------------------------------------------------------------

def force_utf8_stdio() -> None:
    """在 Windows console（預設 cp1252）將 stdout/stderr 重設為 UTF-8。

    PyInstaller 打包的 exe 在 Windows 印含非 ASCII（繁中）的 argparse ``--help``
    等輸出時，預設 cp1252 會 ``UnicodeEncodeError``（#118）。非 Windows 為 no-op；
    stream 無 ``reconfigure``（被重導/包裝）或 reconfigure 失敗時安全略過，
    絕不讓編碼修正本身成為新的失敗點。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            # 本函式唯一目的就是防崩：任何 reconfigure 失敗都不得成為新失敗點。
            # 含非標準 wrapper 的 reconfigure 不吃 encoding= 而拋 TypeError（#118 review）、
            # detached buffer 的 ValueError、底層 OSError 等，一律安全略過。
            pass
