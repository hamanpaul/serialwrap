from __future__ import annotations

import dataclasses
import os
import threading
import time
from typing import Callable


@dataclasses.dataclass(frozen=True)
class DeviceInfo:
    by_id: str
    real_path: str


class DeviceWatcher:
    def __init__(
        self,
        by_id_dir: str,
        on_change: Callable[[list[DeviceInfo], list[DeviceInfo]], None],
        poll_interval_s: float = 1.0,
    ) -> None:
        self._by_id_dir = by_id_dir
        self._on_change = on_change
        self._poll_interval_s = poll_interval_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._devices: dict[str, DeviceInfo] = {}

    @property
    def devices(self) -> dict[str, DeviceInfo]:
        return dict(self._devices)

    def _scan(self) -> dict[str, DeviceInfo]:
        out: dict[str, DeviceInfo] = {}
        if not os.path.isdir(self._by_id_dir):
            return out

        for name in sorted(os.listdir(self._by_id_dir)):
            path = os.path.join(self._by_id_dir, name)
            if not os.path.exists(path):
                continue
            real_path = os.path.realpath(path)
            out[path] = DeviceInfo(by_id=path, real_path=real_path)
        return out

    def poll_once(self) -> None:
        current = self._scan()
        prev = self._devices
        added_keys = sorted(set(current.keys()) - set(prev.keys()))
        removed_keys = sorted(set(prev.keys()) - set(current.keys()))
        changed_keys = sorted(
            key for key in set(current.keys()) & set(prev.keys())
            if current[key].real_path != prev[key].real_path
        )
        added = [current[k] for k in added_keys]
        removed = [prev[k] for k in removed_keys]
        for key in changed_keys:
            removed.append(prev[key])
            added.append(current[key])
        self._devices = current
        if added or removed:
            self._on_change(added, removed)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self._poll_interval_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="serialwrap-device-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
