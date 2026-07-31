"""serialwrap 版本解析（#154：從 cli.py 抽成 daemon／CLI 皆可用的共用小模組）。

daemon 端（``sw_core/service.py``／``sw_core/daemon.py``）需要在 ``health.status``
回應內帶版本欄位，但架構慣例是「daemon 不依賴 cli.py 這層 client」——``cli.py`` 原本
自帶的 ``_resolve_version()``（#131）因此不能被 daemon 端直接 import，改搬到本模組，
CLI 與 daemon 兩側各自 import 使用同一份邏輯。
"""
from __future__ import annotations

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
