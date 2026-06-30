from __future__ import annotations
import yaml
from pathlib import Path


class RuntimeConfig:
    """讀寫 config.yaml：記錄 supervision_mode 與有效 socket 路徑（單一事實來源）。"""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._data: dict = {}
        if self._path.exists():
            self._data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}

    def mode(self) -> str | None:
        return self._data.get("supervision_mode")

    def socket_path(self) -> str | None:
        return self._data.get("socket_path")

    def set_mode(self, mode: str, *, socket_path: str | None = None) -> None:
        self._data["supervision_mode"] = mode
        if socket_path is not None:
            self._data["socket_path"] = socket_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(yaml.safe_dump(self._data, allow_unicode=True), encoding="utf-8")

    def set_socket(self, endpoint: str) -> None:
        """更新 config.yaml 的 socket_path，不改動 supervision_mode（#84 PORT-4）。

        daemon 啟動成功後寫入當前 endpoint，使 CLI ``_resolve_endpoint`` 連得上。
        """
        self._data["socket_path"] = endpoint
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(yaml.safe_dump(self._data, allow_unicode=True), encoding="utf-8")
