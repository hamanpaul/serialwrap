"""可注入的系統效果介面（Effects boundary）。

供 service、setup、doctor 等指令在不直接散佈 subprocess/os 呼叫的情況下
與底層系統互動，並讓單元測試可用 FakeEffects 代換真實 I/O。

公開介面（Protocol：Effects）：
    run(cmd)            — 執行外部指令，回傳 (returncode, stdout, stderr)
    has_systemd()       — 判斷 systemd 是否為 init
    user_in_group(g)    — 判斷目前使用者是否屬於群組 g
    is_wsl()            — 判斷是否執行於 WSL 環境
    which(name)         — 回傳二進位完整路徑或 None
"""

from __future__ import annotations

import grp
import os
import platform
import shutil
import subprocess
from typing import Dict, FrozenSet, List, Optional, Protocol, Tuple


# ---------------------------------------------------------------------------
# Protocol（型別提示用；不強制繼承）
# ---------------------------------------------------------------------------

class Effects(Protocol):
    """所有 effects 實作須滿足的介面。"""

    def run(self, cmd: List[str]) -> Tuple[int, str, str]:
        """執行外部指令，回傳 (returncode, stdout, stderr)。"""
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


# ---------------------------------------------------------------------------
# SystemEffects — 真實實作
# ---------------------------------------------------------------------------

class SystemEffects:
    """呼叫真實系統 API 的 effects 實作。

    探測方法（has_systemd、user_in_group、is_wsl、which）一律不拋例外，
    發生錯誤時回傳安全預設值（False / None）。
    run() 以 check=False 執行子行程，不拋 CalledProcessError。
    """

    def run(self, cmd: List[str]) -> Tuple[int, str, str]:
        """執行 cmd，回傳 (returncode, stdout, stderr)；非零返回碼不拋例外。"""
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.returncode, result.stdout, result.stderr

    def has_systemd(self) -> bool:
        """若 /run/systemd/system 存在則判定 systemd 為 init，回傳 True。"""
        try:
            return os.path.isdir("/run/systemd/system")
        except Exception:
            return False

    def user_in_group(self, group: str) -> bool:
        """回傳 True 若目前使用者（含補充群組）屬於 group；群組不存在回傳 False。"""
        try:
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


# ---------------------------------------------------------------------------
# FakeEffects — 測試替身
# ---------------------------------------------------------------------------

class FakeEffects:
    """可配置的 effects 測試替身，紀錄所有 run() 呼叫以供斷言。

    參數：
        systemd   — has_systemd() 的回傳值（預設 False）
        in_groups — user_in_group() 判斷為 True 的群組名稱集合
        wsl       — is_wsl() 的回傳值（預設 False）
        which     — {name: path} 映射，供 which() 查詢
        commands  — {tuple(cmd): (rc, out, err)} 映射，供 run() 查詢；
                    未登記的指令預設回傳 (0, "", "")
    """

    def __init__(
        self,
        *,
        systemd: bool = False,
        in_groups: FrozenSet[str] | set[str] = frozenset(),
        wsl: bool = False,
        which: Optional[Dict[str, str]] = None,
        commands: Optional[Dict[Tuple[str, ...], Tuple[int, str, str]]] = None,
    ) -> None:
        self._systemd = systemd
        self._in_groups: FrozenSet[str] = frozenset(in_groups)
        self._wsl = wsl
        self._which: Dict[str, str] = dict(which) if which else {}
        self._commands: Dict[Tuple[str, ...], Tuple[int, str, str]] = (
            dict(commands) if commands else {}
        )
        self.calls: List[List[str]] = []

    def run(self, cmd: List[str]) -> Tuple[int, str, str]:
        """紀錄呼叫並回傳映射結果；未登記指令回傳預設 (0, "", "")。"""
        self.calls.append(list(cmd))
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
