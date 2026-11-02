"""serialwrap 版本解析（#154：從 cli.py 抽成 daemon／CLI 皆可用的共用小模組）。

daemon 端（``sw_core/service.py``／``sw_core/daemon.py``）需要在 ``health.status``
回應內帶版本欄位，但架構慣例是「daemon 不依賴 cli.py 這層 client」——``cli.py`` 原本
自帶的 ``_resolve_version()``（#131）因此不能被 daemon 端直接 import，改搬到本模組，
CLI 與 daemon 兩側各自 import 使用同一份邏輯。
"""
from __future__ import annotations

import functools
import hashlib
import os


def _repo_version_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "VERSION"))


def resolve_version() -> str:
    """解析 serialwrap 版本字串（#131 補強：CLI 原本沒有 --version；#154 搬移供 daemon 共用）。

    順序：repo checkout 的 VERSION（原始碼執行最真實）→ 已安裝套件 metadata
    （pip/pipx）→ PyInstaller 內嵌 assets/VERSION（release exe，serialwrap.spec
    datas 於打包時帶入）→ "unknown"。
    """
    try:
        with open(_repo_version_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        pass
    try:
        import importlib.metadata  # noqa: PLC0415

        return importlib.metadata.version("serialwrap")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .assets import read_text  # noqa: PLC0415

        return read_text("VERSION").strip()
    except Exception:  # noqa: BLE001
        return "unknown"


# ───────────────────────── 程式碼指紋（build fingerprint）─────────────────────────
#
# 動機：版本字串只來自 ``VERSION``，release、分支 build、main 全印同一個 0.3.2，
# ``pipx install --force`` 之後 systemd 服務仍在跑的 daemon 也不會換成新碼，兩者
# 都從外面看不出來。指紋是「套件自身 .py 原始碼」的 sha256 前 N 碼，daemon 啟動時
# 算一次放進每個 RPC 回應，CLI 用同一函式算自己載入的那份，兩邊一比就知道是不是
# 同一份程式碼。
#
# 範圍刻意只含 ``sw_core/**/*.py``（含 ``event_engine``、``assets/__init__.py``）：
# - 不含 ``sw_core/assets/`` 下的 skill／profile／shell wrapper：改文件或範本不需要
#   重啟 daemon，納入只會製造假警報；
# - 不含 ``VERSION``：版本另有比對，且 ``resolve_version()`` 在 checkout／安裝間來源不同；
# - 輸入同時混入相對路徑與長度，改檔名、搬檔、檔間內容平移都會改變指紋。
# wheel 安裝與 git checkout 讀到的是同樣的 .py 位元組，所以同一個 commit 的指紋在
# 兩處一致，可用 checkout 的 ``./serialwrap --version`` 對照已安裝的版本。
FINGERPRINT_HEX_LEN = 12


def _package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def compute_code_fingerprint(root: str | None = None) -> str | None:
    """計算 *root*（預設為 ``sw_core`` 套件目錄）下所有 ``.py`` 的指紋。

    回傳 ``FINGERPRINT_HEX_LEN`` 碼十六進位字串；找不到任何 ``.py``（PyInstaller
    凍結包內沒有原始碼檔）或有檔案讀不到（安裝損壞、競態）時回 ``None``——呼叫端
    一律把 ``None`` 當成「無從比對」而靜默略過，不當作不一致。永不拋例外。
    """
    base = root if root is not None else _package_dir()
    try:
        found: list[tuple[str, str]] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    full = os.path.join(dirpath, name)
                    found.append((os.path.relpath(full, base).replace(os.sep, "/"), full))
        if not found:
            return None
        found.sort()
        digest = hashlib.sha256()
        for rel, full in found:
            with open(full, "rb") as fh:
                data = fh.read()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
        return digest.hexdigest()[:FINGERPRINT_HEX_LEN]
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def code_fingerprint() -> str | None:
    """本行程所載入那份 ``sw_core`` 的指紋（每個行程只算一次並快取）。

    daemon 在建立 ``SerialwrapService`` 時第一次呼叫，之後常駐——所以回報的是
    「daemon 啟動當下磁碟上的程式碼」，升級覆蓋檔案後與 CLI 現算的值就會分歧。
    """
    return compute_code_fingerprint()


def code_dir() -> str:
    """本行程載入的 ``sw_core`` 套件目錄（``daemon status`` 的 ``code_path``，供肉眼辨認是哪個安裝）。"""
    return _package_dir()


def format_version_with_fingerprint() -> str:
    """``--version`` 用的字串：``0.3.2 (code 1a2b3c4d5e6f)``；無指紋時退回純版本。"""
    version = resolve_version()
    fp = code_fingerprint()
    return f"{version} (code {fp})" if fp else version
