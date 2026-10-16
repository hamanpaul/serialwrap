from __future__ import annotations

import base64
import errno
import json
import logging
import os
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

from .constants import DEFAULT_WAL_ROTATE_BYTES, WAL_DIR
from .util import dumps_stable, monotonic_ns, now_iso, to_printable
from .wal_fs import PartialWriteError, WalFile, WalFs, default_wal_fs

# 模組層 logger：append 的頻率等同 UART RX，而 logging.getLogger() 每次呼叫都會
# 進 logging.Manager 取全域鎖——不可放在這條熱路徑上（#189）。
_LOG = logging.getLogger("serialwrap")


class WalWriter:
    def __init__(
        self,
        wal_dir: str | None = None,
        rotate_bytes: int = DEFAULT_WAL_ROTATE_BYTES,
        *,
        fs: WalFs | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # None-sentinel：於建構時解析模組層 WAL_DIR（#120）——def-time default 會在類別定義時
        # 凍結 import 當下的值，使 conftest env 隔離與 setattr(wal, "WAL_DIR", ...) 全部失效。
        self._wal_dir = WAL_DIR if wal_dir is None else wal_dir
        self._rotate_bytes = rotate_bytes
        self._wal_path = os.path.join(self._wal_dir, "raw.wal.ndjson")
        self._mirror_path = os.path.join(self._wal_dir, "raw.mirror.log")
        self._fs = fs if fs is not None else default_wal_fs()
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._seq = 0
        self._wal_fp: WalFile | None = None
        self._mirror_fp: WalFile | None = None
        self._closed = False
        self._dirty_tail = {"wal": False, "mirror": False}
        self._unhealthy = False
        self._unhealthy_reason: str | None = None
        # 稽核健康計數（#189）：WAL 目錄可能被外部工具整個刪掉，而服務先前對此毫無所覺。
        self._write_failures = 0
        self._last_write_error: str | None = None
        self._recreated_count = 0
        os.makedirs(self._wal_dir, exist_ok=True)
        self._load_last_seq()

    @property
    def wal_path(self) -> str:
        return self._wal_path

    @property
    def mirror_path(self) -> str:
        return self._mirror_path

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def unhealthy(self) -> bool:
        return self._unhealthy

    def _load_last_seq(self) -> None:
        if not os.path.exists(self._wal_path):
            self._seq = 0
            return
        last = 0
        with open(self._wal_path, "rb") as fp:
            for raw_line in fp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    seq = obj.get("seq")
                    if isinstance(seq, int) and seq > last:
                        last = seq
            fp.seek(0, os.SEEK_END)
            if fp.tell() > 0:
                fp.seek(-1, os.SEEK_END)
                self._dirty_tail["wal"] = fp.read(1) != b"\n"
        self._seq = last

    def _rotate_if_needed(self, sizes: dict[str, int | None] | None = None) -> bool:
        """檔案超過上限時輪替。回傳 True 表示輪替過程發生 OSError 已被 best-effort 含住（degrade）。

        輪替只是「限制檔案大小」的最佳化，不是資料平面必要步驟。原本此方法在 ``append`` 的 try
        之外執行，rotation path 的 ``fs.size``/``rename_noreplace_or_suffix``/``open(dir)``/``fsync(dir)`` 因 EIO/權限/
        fd 耗盡拋 OSError 會逃出 ``append`` → 殺死 RX reader thread（正是 #79 想避免的長壽 thread
        死亡，含 console fan-out 一併中止）。改為自身含住例外：失敗則告警並續用既有（可能超大的）
        檔案，遠優於殺 reader（#79 Codex 必修）。
        """
        rotation_failed = False
        observed_sizes = sizes if sizes is not None else {}
        for path in (self._wal_path, self._mirror_path):
            try:
                size = self._fs.size(path)
                observed_sizes[path] = size
                if size is None:
                    continue
                if size < self._rotate_bytes:
                    continue
                which = "wal" if path == self._wal_path else "mirror"
                close_error = self._close_handle_locked(which, sync=True)
                if close_error is not None:
                    raise close_error
                ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                self._fs.rename_noreplace_or_suffix(path, f"{path}.{ts}")
                self._dirty_tail[which] = False
                self._fs.sync_dir(os.path.dirname(path))
            except OSError as exc:
                rotation_failed = True
                _LOG.warning("WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：%s：%s", path, exc)
        return rotation_failed

    def _handle_for(self, which: str) -> WalFile | None:
        return self._wal_fp if which == "wal" else self._mirror_fp

    def _set_handle(self, which: str, handle: WalFile | None) -> None:
        if which == "wal":
            self._wal_fp = handle
        else:
            self._mirror_fp = handle

    def _path_for(self, which: str) -> str:
        return self._wal_path if which == "wal" else self._mirror_path

    def _close_handle_locked(self, which: str, *, sync: bool = False) -> Exception | None:
        handle = self._handle_for(which)
        if handle is None:
            return None
        # 先解除所有權，再執行可能失敗的 close；任何例外路徑都不會再關閉同一 handle。
        self._set_handle(which, None)
        first_error: Exception | None = None
        if sync:
            try:
                handle.sync()
            except Exception as exc:
                first_error = exc
        try:
            handle.close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        return first_error

    def _open_handle_locked(self, which: str) -> WalFile:
        handle = self._handle_for(which)
        if handle is None:
            handle = self._fs.open_append(self._path_for(which))
            self._set_handle(which, handle)
        return handle

    def _write_once_locked(self, which: str, data: bytes) -> None:
        self._open_handle_locked(which).write_all_flush(data)

    @staticmethod
    def _sent(exc: OSError, attempted_length: int) -> int:
        if not isinstance(exc, PartialWriteError):
            return 0
        try:
            sent = int(exc.sent)
        except (AttributeError, TypeError, ValueError):
            return 0
        return min(max(sent, 0), attempted_length)

    @staticmethod
    def _tail_is_dirty(sent: int, data_length: int, prefix_length: int) -> bool:
        if sent >= data_length:
            return False
        if prefix_length:
            # sent=0 時舊殘行仍未終結；只寫出換行時則尚未開始新紀錄。
            return sent == 0 or sent > prefix_length
        return sent > 0

    @staticmethod
    def _write_error_text(exc: OSError) -> str:
        reported = exc.__cause__
        if isinstance(reported, OSError):
            exc = reported
        return f"{type(exc).__name__}: {exc}"

    def _note_retry_succeeded(self, exc: OSError) -> None:
        self._last_write_error = self._write_error_text(exc)
        if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
            self._record_recreated(exc)

    def _record_recreated(self, exc: OSError) -> None:
        self._recreated_count += 1
        self._last_write_error = self._write_error_text(exc)
        _LOG.warning(
            "WAL 目錄 %s 消失，已重建並續寫（原因：%s）；消失期間的紀錄無法復原",
            self._wal_dir,
            exc,
        )

    def _check_missing_names_locked(self, sizes: dict[str, int | None]) -> None:
        directory_missing = not os.path.isdir(self._wal_dir)
        stale: list[str] = []
        for which in ("wal", "mirror"):
            if self._handle_for(which) is None:
                continue
            if directory_missing:
                stale.append(which)
                continue
            path = self._path_for(which)
            if path not in sizes:
                # rotate 的 size 探測失敗時保留 handle；實際寫入路徑會回報可操作的錯誤。
                continue
            if sizes[path] is None:
                stale.append(which)
        if not stale:
            return

        for which in stale:
            self._close_handle_locked(which)
            # 遺失名稱代表原 inode 上的殘行已不再是目前檔案的一部分。
            self._dirty_tail[which] = False
        try:
            self._fs.ensure_dir(self._wal_dir)
        except OSError:
            return
        self._record_recreated(
            FileNotFoundError(errno.ENOENT, "WAL 檔案路徑消失", self._wal_dir)
        )

    def _retry_write_locked(
        self,
        which: str,
        data: bytes,
        first_exc: OSError,
        *,
        first_attempt: bytes,
        prefix_length: int = 0,
    ) -> OSError | None:
        sent = self._sent(first_exc, len(first_attempt))
        if sent > 0:
            if sent >= len(first_attempt):
                self._dirty_tail[which] = False
                self._note_retry_succeeded(first_exc)
                return None
            try:
                self._write_once_locked(which, first_attempt[sent:])
            except OSError as retry_exc:
                total_sent = sent + self._sent(retry_exc, len(first_attempt) - sent)
                self._dirty_tail[which] = self._tail_is_dirty(
                    total_sent, len(first_attempt), prefix_length
                )
                return retry_exc
            self._dirty_tail[which] = False
            self._note_retry_succeeded(first_exc)
            return None

        close_error = self._close_handle_locked(which)
        if close_error is not None:
            self._dirty_tail[which] = bool(prefix_length)
            return close_error if isinstance(close_error, OSError) else OSError(str(close_error))
        try:
            self._fs.ensure_dir(self._wal_dir)
            self._write_once_locked(which, data)
        except OSError as retry_exc:
            retry_sent = self._sent(retry_exc, len(data))
            self._dirty_tail[which] = self._tail_is_dirty(
                retry_sent, len(data), prefix_length
            )
            return retry_exc
        self._dirty_tail[which] = False
        self._note_retry_succeeded(first_exc)
        return None

    def _write_one_locked(self, which: str, data: bytes) -> OSError | None:
        if self._dirty_tail[which]:
            try:
                self._write_once_locked(which, b"\n")
            except OSError as exc:
                # 換行也寫不進去時重開 handle，將終結符與本筆資料合併重試。
                return self._retry_write_locked(
                    which,
                    b"\n" + data,
                    exc,
                    first_attempt=b"\n",
                    prefix_length=1,
                )
            self._dirty_tail[which] = False

        try:
            self._write_once_locked(which, data)
        except OSError as exc:
            return self._retry_write_locked(
                which,
                data,
                exc,
                first_attempt=data,
            )
        self._dirty_tail[which] = False
        return None

    def append(
        self,
        *,
        com: str,
        direction: str,
        source: str,
        payload: bytes,
        cmd_id: str | None = None,
        loss_flag: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_b64 = base64.b64encode(payload).decode("ascii")
        crc32 = zlib.crc32(payload) & 0xFFFFFFFF
        with self._lock:
            rotation_failed = False
            if not self._closed:
                sizes: dict[str, int | None] = {}
                rotation_failed = self._rotate_if_needed(sizes)
                self._check_missing_names_locked(sizes)
            self._seq += 1
            record = {
                "seq": self._seq,
                "mono_ts_ns": monotonic_ns(),
                "wall_ts": now_iso(),
                "com": com,
                "dir": direction,
                "source": source,
                "cmd_id": cmd_id,
                "len": len(payload),
                "crc32": f"{crc32:08x}",
                "payload_b64": payload_b64,
                "loss_flag": bool(loss_flag),
                "meta": meta or {},
            }
            if rotation_failed:
                # 輪替失敗不丟資料（仍寫入既有檔），但標記供觀測；conditional key 維持常態 record 向後相容。
                record["rotation_failed"] = True
            if self._closed:
                record["loss_flag"] = True
                self._write_failures += 1
                self._last_write_error = "WalClosed: writer closed"
                return record

            wal_error, mirror_error = self._write_record_locked(record, payload)
            if wal_error is None:
                self._unhealthy = False
                self._unhealthy_reason = None
            else:
                record["loss_flag"] = True
                self._write_failures += 1
                self._last_write_error = self._write_error_text(wal_error)
                self._unhealthy = True
                self._unhealthy_reason = "write_failed"
                _LOG.warning("WAL append 寫入失敗（best-effort 略過）：%s", wal_error)
            if mirror_error is not None:
                self._write_failures += 1
                self._last_write_error = self._write_error_text(mirror_error)
                _LOG.warning("WAL append 寫入失敗（best-effort 略過）：%s", mirror_error)
        return record

    def _write_record_locked(
        self, record: dict[str, Any], payload: bytes
    ) -> tuple[OSError | None, OSError | None]:
        """雙軌 append：權威 ndjson ＋ 人類可讀鏡像。須在 ``self._lock`` 內呼叫。"""
        line = (dumps_stable(record) + "\n").encode("utf-8")
        printable = to_printable(payload).encode("utf-8")
        wal_error = self._write_one_locked("wal", line)
        if printable:
            mirror_error = self._write_one_locked("mirror", printable)
        else:
            try:
                self._open_handle_locked("mirror")
            except OSError:
                # 空 META 不寫 mirror，也不因其 best-effort 開檔失敗影響權威 WAL。
                pass
            mirror_error = None
        return wal_error, mirror_error

    def close(self) -> None:
        """同步並關閉目前持有的 WAL 與 mirror handle；重複呼叫不重複關閉。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            first_error: Exception | None = None
            for which in ("wal", "mirror"):
                error = self._close_handle_locked(which, sync=True)
                if first_error is None and error is not None:
                    first_error = error
            if first_error is not None:
                raise first_error

    def health(self) -> dict[str, Any]:
        """稽核紀錄的活體健康（#189）。

        先前唯一能問的只有 ``current_seq``，而它在 WAL 目錄被刪掉之後仍會繼續累加——
        於是 ``current_seq=1261000`` 與 ``records=[]`` 可以同時出現在同一個回應裡，
        沒有任何欄位指出檔案不見了。這裡把檔案系統的實況一次攤開。

        ``healthy`` 的判準：目錄存在且可寫，且不出現「已寫過紀錄但現行檔不存在」。
        ``current_seq == 0`` 時檔案尚未建立是正常的（全新 daemon、還沒有 UART 流量），
        不算故障。
        """
        wal_dir_exists = os.path.isdir(self._wal_dir)
        wal_file_exists = os.path.exists(self._wal_path)
        # 目錄要能建立/開啟檔案，需同時具備寫入（W_OK）與 traverse（X_OK）權限——
        # 只驗 W_OK 會把 0600 這類「寫得進 inode 但進不去」的目錄誤判為可寫，
        # 使 healthy / doctor 的 wal_writable 漏報（Copilot review）。
        wal_dir_writable = wal_dir_exists and os.access(self._wal_dir, os.W_OK | os.X_OK)
        # 刻意**不取 self._lock**：該鎖在每一筆 append 的檔案寫入全程持有，而 append
        # 的頻率等同 UART RX。health() 由 health.status / log.tail_* / wal.range 呼叫，
        # 這些都在 daemon 單執行緒 asyncio dispatcher 內同步執行——在此等 WAL 寫鎖等於
        # 讓診斷查詢被資料平面的寫入節奏拖住，凍結整個 RPC loop。這裡讀的是四個
        # int/str 欄位的診斷快照，GIL 下各自的讀取為原子；快照內部略有時間差對
        # 「稽核檔在不在」的判定無影響。
        seq = self._seq
        failures = self._write_failures
        last_error = self._last_write_error
        recreated = self._recreated_count
        unhealthy = self._unhealthy
        unhealthy_reason = self._unhealthy_reason
        return {
            "wal_dir": self._wal_dir,
            "wal_path": self._wal_path,
            "wal_dir_exists": wal_dir_exists,
            "wal_file_exists": wal_file_exists,
            "wal_dir_writable": wal_dir_writable,
            "current_seq": seq,
            "write_failures": failures,
            "last_write_error": last_error,
            "recreated_count": recreated,
            "healthy": bool(
                wal_dir_exists
                and wal_dir_writable
                and not (seq > 0 and not wal_file_exists)
                and not unhealthy
            ),
            "unhealthy_reason": unhealthy_reason,
        }

    def available_from_seq(self) -> int | None:
        """現行 WAL 檔中最小的 seq；檔案不存在或無有效紀錄時回 ``None``（#189）。

        用來分辨「這個區間本來就沒有紀錄」與「這個區間曾經存在，但已被輪替掉」——
        對呼叫端是完全不同的結論，先前兩者都只是空陣列 ＋ ``ok:true``。
        """
        if not os.path.exists(self._wal_path):
            return None
        try:
            for obj in self._iter_matching(None):
                return int(obj["seq"])
        except OSError:
            return None
        return None

    def reset(self) -> dict[str, Any]:
        """輪替現有 WAL 檔案並重設 seq 計數器。不需重啟 daemon。"""
        with self._lock:
            prev_seq = self._seq
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            rotated_suffix = ts
            for which, path in (("wal", self._wal_path), ("mirror", self._mirror_path)):
                size = self._fs.size(path)
                if size is not None and size > 0:
                    close_error = self._close_handle_locked(which, sync=True)
                    if close_error is not None:
                        raise close_error
                    dst = self._fs.rename_noreplace_or_suffix(path, f"{path}.{ts}")
                    if which == "wal":
                        archive_prefix = f"{os.path.basename(self._wal_path)}."
                        rotated_suffix = os.path.basename(dst)[len(archive_prefix):]
                    self._dirty_tail[which] = False
            self._fs.sync_dir(self._wal_dir)
            self._seq = 0
            return {"ok": True, "previous_seq": prev_seq, "rotated_suffix": rotated_suffix}

    def _iter_matching(self, com: str | None) -> Iterator[dict[str, Any]]:
        """逐行掃**現行** WAL 檔（``raw.wal.ndjson``），產出通過解析與 com 過濾的紀錄（seq 為 int 才算有效）。

        注意：輪替（rotation）歸檔的 ``raw.wal.ndjson.<ts>`` 檔不在掃描範圍內（#124 review）。
        """
        with open(self._wal_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if not isinstance(obj.get("seq"), int):
                    continue
                if com and obj.get("com") != com:
                    continue
                yield obj

    def tail_raw(
        self, *, from_seq: int | None = None, com: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """回傳 WAL 紀錄列（seq 升冪）。

        - ``from_seq=None``（預設）：**latest 模式**——回傳符合條件的最新 ``limit`` 筆（#124）。
        - ``from_seq=N``（int，含 0）：**range 模式**——回傳 ``seq > N`` 起最舊的
          ``limit`` 筆（舊語意，供增量讀取與老 client 相容）。

        兩種模式皆**僅掃描現行 WAL 檔**；輪替歸檔的更舊紀錄不列入（詳見
        ``tail_raw_with_meta`` 的 scope 說明）。
        """
        rows, _ = self.tail_raw_with_meta(from_seq=from_seq, com=com, limit=limit)
        return rows

    def tail_raw_with_meta(
        self, *, from_seq: int | None = None, com: str | None = None, limit: int = 200
    ) -> tuple[list[dict[str, Any]], bool]:
        """同 ``tail_raw``，另回傳 ``truncated``：是否還有符合條件但被 limit 截掉的紀錄。

        truncated 語意：

        - latest 模式（``from_seq=None``）：True 表示回傳視窗**之前**還有更舊的符合紀錄。
        - range 模式（``from_seq=N``）：True 表示回傳視窗**之後**還有更新的符合紀錄。

        scope（#124 review）：查詢與 truncated 判定**僅涵蓋現行 WAL 檔**（``raw.wal.ndjson``）。
        輪替（rotation）後更舊紀錄保存在 ``raw.wal.ndjson.<ts>`` 歸檔檔，不在掃描範圍內——
        rotation 剛發生時 latest 模式可能回不足 ``limit`` 筆且 ``truncated=False``；
        需要歸檔紀錄請直接讀取歸檔檔（``log tail-*`` 與 ``wal export`` 皆僅讀現行檔）。
        """
        if not os.path.exists(self._wal_path):
            return [], False
        if from_seq is None:
            # latest 模式：deque(maxlen=limit) 只留掃描到的最後 limit 筆，天然維持 seq 升冪。
            window: deque[dict[str, Any]] = deque(maxlen=max(limit, 0))
            total = 0
            for obj in self._iter_matching(com):
                total += 1
                window.append(obj)
            rows = list(window)
            return rows, total > len(rows)
        out: list[dict[str, Any]] = []
        truncated = False
        for obj in self._iter_matching(com):
            if obj["seq"] <= from_seq:
                continue
            if len(out) >= limit:
                # 已收滿又遇到符合紀錄 → 視窗之後還有資料。
                truncated = True
                break
            out.append(obj)
        return out, truncated

    def tail_text(
        self, *, from_seq: int | None = None, com: str | None = None, limit: int = 200
    ) -> list[str]:
        """同 ``tail_raw`` 的兩種模式（``limit`` 計 WAL 紀錄筆數，非文字行數），輸出可讀文字行。"""
        rows = self.tail_raw(from_seq=from_seq, com=com, limit=limit)
        return rows_to_text_lines(rows)


def rows_to_text_lines(rows: list[dict[str, Any]]) -> list[str]:
    """把 WAL 紀錄列解碼為人類可讀文字行；無換行結尾的 partial 尾段保留為最後一行。"""
    chunks: list[str] = []
    for row in rows:
        payload = base64.b64decode(row.get("payload_b64", ""), validate=False)
        chunks.append(to_printable(payload))
    text = "".join(chunks)
    if not text:
        return []
    lines = text.splitlines()
    if text.endswith("\n"):
        return lines
    if not lines:
        return [text]
    consumed = sum(len(line) for line in lines) + max(len(lines) - 1, 0)
    if consumed < len(text):
        lines.append(text[consumed:])
    return lines
