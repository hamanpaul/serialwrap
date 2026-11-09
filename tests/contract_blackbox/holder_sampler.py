"""tty 持有者取樣（P8 Task 0；計畫 09 Task 0 Interfaces）。

`scan_holders(path)` 掃 `/proc/*/fd`，回傳開著 `path`（解析 symlink 後的實體路徑）的行程 pid；
`HolderSampler` 以背景 thread 每 `interval_s` 秒取樣一次，記錄 `(t, pids)` 序列，供「交接期間不得同時有兩個持有者」
這類連續取樣證明（Review Focus 5：不以單次快照證明）。

POSIX（Linux `/proc`）限定；Windows 無 `/proc`，`scan_holders` 回空清單、`HolderSampler` 不得作為證據。
讀不到的 `/proc/<pid>/fd`（其他使用者、行程已結束）略過。stdlib only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Iterable

_PROC = "/proc"


def scan_holders(path: str, *, exclude: Iterable[int] = ()) -> list[int]:
    """回傳目前開著 `path` 的 pid（遞增排序、不重複）；`exclude` 中的 pid 不列入。"""
    real = os.path.realpath(path)
    skip = {int(p) for p in exclude}
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return []
    out: list[int] = []
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in skip:
            continue
        fd_dir = f"{_PROC}/{name}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") == real:
                    out.append(pid)
                    break
            except OSError:
                continue
    return sorted(out)


class HolderSampler:
    """每 `interval_s` 秒掃一次持有者並記錄 `(monotonic 秒, pids)`；以 `with` 區塊界定取樣期間。

    掃一次 `/proc` 可能比 `interval_s` 久（行程多時）；此時取樣以掃描能達到的最快速度進行，`samples()` 的時間戳為實際取樣時刻。
    """

    def __init__(self, path: str, interval_s: float = 0.01, *, exclude: Iterable[int] = ()) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s 必須為正數")
        self.path = path
        self.interval_s = interval_s
        self._exclude = tuple(int(p) for p in exclude)
        self._samples: list[tuple[float, list[int]]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "HolderSampler":
        self._stop.clear()
        self._sample_once()          # 進入區塊時至少有一筆，避免短區塊沒有任何樣本
        self._thread = threading.Thread(target=self._run, name="holder-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._sample_once()          # 離開區塊時再取一筆，反映區塊結束當下的狀態

    def _sample_once(self) -> None:
        pids = scan_holders(self.path, exclude=self._exclude)
        with self._lock:
            self._samples.append((time.monotonic(), pids))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._sample_once()

    def samples(self) -> list[tuple[float, list[int]]]:
        with self._lock:
            return [(t, list(p)) for t, p in self._samples]

    def max_concurrent(self) -> int:
        """全部樣本中同時持有者數量的最大值（無樣本為 0）。"""
        with self._lock:
            return max((len(p) for _t, p in self._samples), default=0)

    def holders_seen(self) -> set[int]:
        """取樣期間出現過的所有持有者 pid。"""
        with self._lock:
            return {pid for _t, pids in self._samples for pid in pids}


__all__ = ["HolderSampler", "scan_holders"]
