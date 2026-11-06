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
from dataclasses import dataclass
from typing import Any

from .constants import (
    DEFAULT_WAL_ROTATE_BYTES,
    WAL_DIR,
    WAL_IDENTITY_CHECK_INTERVAL_S,
    WAL_RECREATE_BACKOFF_S,
    WAL_RECREATE_RETRY_INTERVALS_S,
)
from .util import dumps_stable, monotonic_ns, now_iso, to_printable
from .wal_fs import PartialWriteError, WalFile, WalFs, default_wal_fs

# 模組層 logger：append 的頻率等同 UART RX，而 logging.getLogger() 每次呼叫都會
# 進 logging.Manager 取全域鎖——不可放在這條熱路徑上（#189）。
_LOG = logging.getLogger("serialwrap")

# OD-19（2026-10-23）：rotate 遇共用違規（ERROR_SHARING_VIOLATION）或存取拒絕（ERROR_ACCESS_DENIED）
# 屬防毒等外部程式暫時佔用的干擾，不是資料錯誤。立即退避的次數與間隔為有上限的常數；超過上限即
# 轉入延遲佇列，之後以 WAL_DEFERRED_DELETE_INTERVAL_S 非阻塞重試，直到輪替完成。寫入路徑不持鎖睡眠：
# 退避以「下一次嘗試的時間戳」判定，於後續 append 或 tick() 檢查到期。Rust sw-pal wal_fs 須採相同語意。
WAL_ROTATE_RETRY_BACKOFF_S: tuple[float, ...] = (0.01, 0.05, 0.2)
WAL_DEFERRED_DELETE_INTERVAL_S: float = 1.0
WAL_DEFERRED_WARNING_INTERVAL_S: float = 60.0
_WINERROR_ACCESS_DENIED = 5
_WINERROR_SHARING_VIOLATION = 32


@dataclass
class _RotatePending:
    """某個 WAL 檔（現行 WAL 或 mirror）因干擾而尚未完成的輪替。"""

    failures: int = 0
    next_at: float = 0.0
    deferred: bool = False
    last_warning_at: float | None = None


def _is_transient_rotate_error(exc: OSError) -> bool:
    """共用違規或存取拒絕（含 POSIX 的 EACCES／EBUSY 與 Windows winerror 5／32）屬暫時性干擾。"""
    if exc.errno in (errno.EACCES, errno.EBUSY):
        return True
    return getattr(exc, "winerror", None) in (_WINERROR_ACCESS_DENIED, _WINERROR_SHARING_VIOLATION)


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
        # OD-19：輪替干擾的有界退避狀態與計數（health() 快照）；以路徑種類（"wal"／"mirror"）為鍵。
        self._rotate_pending: dict[str, _RotatePending] = {}
        self._rotate_retry_count = 0
        self._rotate_deferred_count = 0
        self._file_first_seq: int | None = None
        self._file_first_seq_pending = True
        self._last_identity_check: float | None = None
        self._recreate_failures = 0
        self._next_recreate_at: float | None = None
        self._wal_identity: tuple[int, int] | None = None
        self._pending_recreate_reason: str | None = None
        self._pending_previous_identity: str | None = None
        self._pending_file_first_seq: int | None = None
        self._wal_path_expected = False
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
            self._file_first_seq = None
            self._file_first_seq_pending = True
            self._wal_path_expected = False
            return
        self._wal_path_expected = True
        last = 0
        first: int | None = None
        first_nonempty_seen = False
        with open(self._wal_path, "rb") as fp:
            for raw_line in fp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not first_nonempty_seen:
                    first_nonempty_seen = True
                    try:
                        first_obj = json.loads(line)
                    except json.JSONDecodeError:
                        first_obj = None
                    if isinstance(first_obj, dict):
                        first_seq = first_obj.get("seq")
                        if isinstance(first_seq, int) and not isinstance(first_seq, bool):
                            first = first_seq
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
        self._file_first_seq = first
        self._file_first_seq_pending = not first_nonempty_seen

    def _rotate_if_needed(
        self,
        sizes: dict[str, int | None] | None = None,
        *,
        skip_paths: set[str] | None = None,
    ) -> bool:
        """檔案超過上限時輪替。回傳 True 表示本次呼叫有輪替嘗試失敗（已被 best-effort 含住，degrade）。

        輪替只是「限制檔案大小」的最佳化，不是資料平面必要步驟。原本此方法在 ``append`` 的 try
        之外執行，rotation path 的 ``fs.size``/``rename_noreplace_or_suffix``/``open(dir)``/``fsync(dir)`` 因 EIO/權限/
        fd 耗盡拋 OSError 會逃出 ``append`` → 殺死 RX reader thread（正是 #79 想避免的長壽 thread
        死亡，含 console fan-out 一併中止）。改為自身含住例外：失敗則告警並續用既有（可能超大的）
        檔案，遠優於殺 reader（#79 Codex 必修）。

        OD-19：共用違規／存取拒絕（``_is_transient_rotate_error``）改為有界退避：前
        ``WAL_ROTATE_RETRY_BACKOFF_S`` 段以下一次嘗試時間戳排程，之後轉入延遲佇列
        （``WAL_DEFERRED_DELETE_INTERVAL_S``）。未到期者不嘗試、也不計為本次失敗；寫入路徑從不睡眠。
        """
        rotation_failed = False
        observed_sizes = sizes if sizes is not None else {}
        paths_to_skip = skip_paths if skip_paths is not None else set()
        for path in (self._wal_path, self._mirror_path):
            if path in paths_to_skip:
                continue
            which = "wal" if path == self._wal_path else "mirror"
            pending = self._rotate_pending.get(which)
            try:
                if path in observed_sizes:
                    size = observed_sizes[path]
                else:
                    size = self._fs.size(path)
                    observed_sizes[path] = size
                if size is None or size < self._rotate_bytes:
                    # 檔案已不存在或已低於門檻（例如外部截短）：先前排程的輪替不再需要。
                    self._rotate_pending.pop(which, None)
                    continue
                if pending is not None and self._clock() < pending.next_at:
                    continue
                close_error = self._close_handle_locked(which, sync=True)
                if close_error is not None:
                    raise close_error
                ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                self._fs.rename_noreplace_or_suffix(path, f"{path}.{ts}")
                self._rotate_pending.pop(which, None)
                self._dirty_tail[which] = False
                if which == "wal":
                    self._wal_path_expected = False
                    self._wal_identity = None
                    self._file_first_seq = self._seq + 1
                    self._file_first_seq_pending = False
                self._fs.sync_dir(os.path.dirname(path))
            except OSError as exc:
                rotation_failed = True
                if _is_transient_rotate_error(exc):
                    newly_deferred = self._schedule_rotate_retry_locked(which)
                    pending = self._rotate_pending[which]
                    now = self._clock()
                    # 首次失敗與轉入延遲時告警；持續延遲則每分鐘最多一筆，保留永久拒絕的訊號而不刷屏。
                    warn_now = newly_deferred or pending.failures == 1
                    if (
                        pending.deferred
                        and pending.last_warning_at is not None
                        and now - pending.last_warning_at >= WAL_DEFERRED_WARNING_INTERVAL_S
                    ):
                        warn_now = True
                else:
                    self._rotate_pending.pop(which, None)
                    warn_now = True
                if warn_now:
                    if _is_transient_rotate_error(exc):
                        self._rotate_pending[which].last_warning_at = self._clock()
                    _LOG.warning("WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：%s：%s", path, exc)
        return rotation_failed

    def _schedule_rotate_retry_locked(self, which: str) -> bool:
        """記錄一次暫時性輪替失敗並排程下一次嘗試；回傳 True 表示本次剛轉入延遲佇列。須在 ``self._lock`` 內呼叫。"""
        self._rotate_retry_count += 1
        pending = self._rotate_pending.get(which)
        if pending is None:
            pending = _RotatePending()
            self._rotate_pending[which] = pending
        pending.failures += 1
        now = self._clock()
        if pending.failures <= len(WAL_ROTATE_RETRY_BACKOFF_S):
            pending.deferred = False
            pending.next_at = now + WAL_ROTATE_RETRY_BACKOFF_S[pending.failures - 1]
            return False
        newly_deferred = not pending.deferred
        pending.deferred = True
        pending.next_at = now + WAL_DEFERRED_DELETE_INTERVAL_S
        if newly_deferred:
            self._rotate_deferred_count += 1
        return newly_deferred

    def tick(self) -> bool:
        """非阻塞地服務到期的輪替重試（延遲佇列）；寫鎖被佔用時立即返回，不等待、不寫入紀錄。

        回傳本次是否有輪替嘗試失敗。append 路徑本身也會檢查到期項目；tick 讓沒有新寫入時
        仍能完成先前因干擾而延後的輪替。
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._closed or not self._rotate_pending:
                return False
            wal_expected_before = self._wal_path_expected
            failed = self._rotate_if_needed()
            if wal_expected_before and not self._wal_path_expected:
                # 延後的現行檔輪替在無新寫入的 tick 內完成：立即於新檔寫連續性 META，
                # 與 append 驅動輪替（觸發寫入即落在新檔）等價，維持現行檔存在與重啟後 seq 續接。
                self._write_rotation_continuity_meta_locked()
            return failed
        finally:
            self._lock.release()

    def _write_rotation_continuity_meta_locked(self) -> None:
        """tick 驅動的現行檔輪替完成後，於新現行檔寫一筆 WAL_ROTATED_AFTER_DEFERRAL META。

        只在輪替確實於本次 tick 完成（``_wal_path_expected`` 由 True 轉 False）且現行檔仍不存在時寫入，
        不與外部 unlink 的重建路徑（WAL_RECREATED_AFTER_EXTERNAL_UNLINK）重疊。寫入失敗依 append 同一語意
        計入寫入失敗並標示不健康，下一次 append 或 tick 以既有重建路徑處理。須在 ``self._lock`` 內呼叫。
        """
        try:
            if self._fs.size(self._wal_path) is not None:
                return
        except OSError:
            return
        archived_through = self._seq
        record = self._new_record_locked(
            com="WAL",
            direction="META",
            source="system",
            payload=b"",
            cmd_id=None,
            loss_flag=False,
            meta={"event": "WAL_ROTATED_AFTER_DEFERRAL", "archived_through_seq": archived_through},
        )
        wal_error, _ = self._write_record_locked(record, b"")
        if wal_error is not None:
            record["loss_flag"] = True
            self._write_failures += 1
            self._last_write_error = self._write_error_text(wal_error)
            self._unhealthy = True
            self._unhealthy_reason = "write_failed"
            return
        self._file_first_seq = record["seq"]
        self._file_first_seq_pending = False
        self._wal_path_expected = True
        self._unhealthy = False
        self._unhealthy_reason = None

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
            if which == "wal":
                self._wal_path_expected = True
                try:
                    self._wal_identity = handle.identity()
                except OSError:
                    # FileIdInfo 等 identity 查詢不支援時，保留 handle 並略過該次證據。
                    self._wal_identity = None
                self._last_identity_check = self._clock()
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

    @staticmethod
    def _identity_text(identity: tuple[int, int] | None) -> str | None:
        if identity is None:
            return None
        return f"{identity[0]}:{identity[1]}"

    def _identity_change_locked(self, which: str) -> tuple[str | None, bool]:
        handle = self._handle_for(which)
        if handle is None:
            return None, False
        path = self._path_for(which)
        try:
            path_identity = self._fs.path_identity(path)
        except OSError:
            return None, True
        if path_identity is None or path_identity == (0, 0):
            return "external_unlink", False
        try:
            handle_identity = handle.identity()
        except OSError:
            return None, True
        if which == "wal":
            self._wal_identity = handle_identity
        if path_identity != handle_identity:
            return "replaced", False
        try:
            if handle.unlinked():
                return "external_unlink", False
        except OSError:
            return None, True
        return None, False

    def _check_missing_names_locked(
        self,
        sizes: dict[str, int | None],
        *,
        force_identity: bool = False,
    ) -> tuple[str | None, set[str]]:
        """每筆確認名稱存在；檔案身分與 DeletePending 則依節流間隔查詢。"""
        wal_handle = self._wal_fp
        mirror_handle = self._mirror_fp
        wal_expected = self._wal_path_expected or wal_handle is not None
        wal_history_exists = wal_expected or mirror_handle is not None or self._seq > 0
        directory_missing = wal_history_exists and not os.path.isdir(self._wal_dir)
        wal_reason: str | None = "dir_missing" if directory_missing and wal_expected else None
        identity_unknown_paths: set[str] = set()

        if (
            wal_reason is None
            and wal_expected
            and not directory_missing
            and self._wal_path in sizes
            and sizes[self._wal_path] is None
        ):
            wal_reason = "external_unlink"

        for which, handle in (("wal", wal_handle), ("mirror", mirror_handle)):
            if handle is None:
                continue
            path = self._path_for(which)
            reason: str | None = None
            if directory_missing:
                reason = "dir_missing"
            elif path in sizes and sizes[path] is None:
                reason = "external_unlink"
            if reason is not None:
                if which == "wal":
                    wal_reason = reason
                else:
                    self._close_handle_locked("mirror")
                    self._dirty_tail["mirror"] = False

        now = self._clock()
        due = (
            force_identity
            or
            self._last_identity_check is None
            or now - self._last_identity_check >= WAL_IDENTITY_CHECK_INTERVAL_S
        )
        if due:
            self._last_identity_check = now
            for which, handle in (("wal", self._wal_fp), ("mirror", self._mirror_fp)):
                if handle is None or (which == "wal" and wal_reason is not None):
                    continue
                reason, identity_unknown = self._identity_change_locked(which)
                if identity_unknown:
                    identity_unknown_paths.add(self._path_for(which))
                if reason is None:
                    continue
                if which == "wal":
                    wal_reason = reason
                else:
                    self._close_handle_locked("mirror")
                    self._dirty_tail["mirror"] = False
        return wal_reason, identity_unknown_paths

    def _previous_wal_identity_locked(self) -> str | None:
        handle = self._wal_fp
        if handle is not None:
            try:
                identity = handle.identity()
            except OSError:
                identity = self._wal_identity
            else:
                self._wal_identity = identity
            return self._identity_text(identity)
        return self._identity_text(self._wal_identity)

    def _schedule_recreate_retry_locked(self, exc: OSError) -> None:
        self._recreate_failures += 1
        failure_number = self._recreate_failures
        if failure_number <= len(WAL_RECREATE_BACKOFF_S):
            delay = WAL_RECREATE_BACKOFF_S[failure_number - 1]
        else:
            retry_index = min(
                failure_number - len(WAL_RECREATE_BACKOFF_S) - 1,
                len(WAL_RECREATE_RETRY_INTERVALS_S) - 1,
            )
            delay = WAL_RECREATE_RETRY_INTERVALS_S[retry_index]
        self._next_recreate_at = self._clock() + delay
        self._unhealthy = True
        self._unhealthy_reason = "recreate_failed"
        self._last_write_error = self._write_error_text(exc)
        _LOG.warning("WAL 重建失敗（best-effort 延後重試）：%s", exc)

    def _attempt_recreate_locked(self) -> bool:
        if self._pending_recreate_reason is None:
            return False
        now = self._clock()
        if self._next_recreate_at is not None and now < self._next_recreate_at:
            return False
        try:
            self._fs.ensure_dir(self._wal_dir)
            handle = self._fs.open_append(self._wal_path)
        except OSError as exc:
            self._schedule_recreate_retry_locked(exc)
            return False

        self._set_handle("wal", handle)
        self._wal_path_expected = True
        try:
            self._wal_identity = handle.identity()
        except OSError:
            self._wal_identity = None
        self._last_identity_check = now

        try:
            self._dirty_tail["wal"] = self._fs.tail_needs_termination(self._wal_path)
            if self._dirty_tail["wal"]:
                handle.write_all_flush(b"\n")
                self._dirty_tail["wal"] = False
        except OSError as exc:
            self._close_handle_locked("wal")
            self._schedule_recreate_retry_locked(exc)
            return False

        first_seq = self._pending_file_first_seq
        last_seq = self._seq
        range_known = first_seq is not None and last_seq >= first_seq
        lost_range = [first_seq, last_seq] if range_known else None
        meta_record = self._new_record_locked(
            com="WAL",
            direction="META",
            source="system",
            payload=b"",
            cmd_id=None,
            loss_flag=True,
            meta={
                "event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK",
                "reason": self._pending_recreate_reason,
                "previous_identity": self._pending_previous_identity,
                "potentially_lost_seq_range": lost_range,
                "range_known": range_known,
            },
        )
        wal_error, _ = self._write_record_locked(meta_record, b"")
        if wal_error is not None:
            self._close_handle_locked("wal")
            self._schedule_recreate_retry_locked(wal_error)
            return False

        self._file_first_seq = meta_record["seq"]
        self._file_first_seq_pending = False
        self._record_recreated(
            FileNotFoundError(errno.ENOENT, "WAL 檔案路徑消失", self._wal_dir)
        )
        self._pending_recreate_reason = None
        self._pending_previous_identity = None
        self._pending_file_first_seq = None
        self._recreate_failures = 0
        self._next_recreate_at = None
        self._unhealthy = False
        self._unhealthy_reason = None
        return True

    def _begin_recreate_locked(self, reason: str, *, attempt: bool = True) -> bool:
        if self._pending_recreate_reason is None:
            self._pending_recreate_reason = reason
            self._pending_previous_identity = self._previous_wal_identity_locked()
            self._pending_file_first_seq = self._file_first_seq
            self._recreate_failures = 0
            self._next_recreate_at = None
            self._close_handle_locked("wal")
            self._dirty_tail["wal"] = False
        return self._attempt_recreate_locked() if attempt else True

    def _new_record_locked(
        self,
        *,
        com: str,
        direction: str,
        source: str,
        payload: bytes,
        payload_b64: str = "",
        crc32: str = "00000000",
        cmd_id: str | None = None,
        loss_flag: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._seq += 1
        return {
            "seq": self._seq,
            "mono_ts_ns": monotonic_ns(),
            "wall_ts": now_iso(),
            "com": com,
            "dir": direction,
            "source": source,
            "cmd_id": cmd_id,
            "len": len(payload),
            "crc32": crc32,
            "payload_b64": payload_b64,
            "loss_flag": bool(loss_flag),
            "meta": meta or {},
        }

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
        payload_crc32 = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
        with self._lock:
            rotation_failed = False
            if not self._closed:
                if self._pending_recreate_reason is None:
                    sizes: dict[str, int | None] = {}
                    for path in (self._wal_path, self._mirror_path):
                        try:
                            sizes[path] = self._fs.size(path)
                        except OSError:
                            pass
                    force_identity = any(
                        size is not None and size >= self._rotate_bytes
                        for size in sizes.values()
                    )
                    reason, identity_unknown_paths = self._check_missing_names_locked(
                        sizes, force_identity=force_identity
                    )
                    if reason is not None:
                        self._begin_recreate_locked(reason, attempt=False)
                        rotation_failed = self._rotate_if_needed(sizes)
                        self._attempt_recreate_locked()
                    else:
                        rotation_failed = self._rotate_if_needed(
                            sizes, skip_paths=identity_unknown_paths
                        )
                if self._pending_recreate_reason is not None:
                    if not self._attempt_recreate_locked():
                        record = self._new_record_locked(
                            com=com,
                            direction=direction,
                            source=source,
                            payload=payload,
                            payload_b64=payload_b64,
                            crc32=payload_crc32,
                            cmd_id=cmd_id,
                            loss_flag=True,
                            meta=meta,
                        )
                        if rotation_failed:
                            record["rotation_failed"] = True
                        self._write_failures += 1
                        if self._last_write_error is None:
                            self._last_write_error = "WAL recreation is pending"
                        return record
            record = self._new_record_locked(
                com=com,
                direction=direction,
                source=source,
                payload=payload,
                payload_b64=payload_b64,
                crc32=payload_crc32,
                cmd_id=cmd_id,
                loss_flag=loss_flag,
                meta=meta,
            )
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
                if self._file_first_seq_pending:
                    self._file_first_seq = record["seq"]
                    self._file_first_seq_pending = False
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

    def sync(self) -> None:
        """同步目前已開啟的 WAL 與 mirror handle，但保留 handle 開啟。"""
        with self._lock:
            if self._closed:
                return
            first_error: Exception | None = None
            for which in ("wal", "mirror"):
                handle = self._handle_for(which)
                if handle is None:
                    continue
                try:
                    handle.sync()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
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
        # OD-19：輪替干擾計數（防毒等外部程式佔用）；屬可觀測的干擾指標，不影響 healthy 判定。
        rotate_retry = self._rotate_retry_count
        rotate_deferred = self._rotate_deferred_count
        deferred_delete_pending = sum(1 for p in tuple(self._rotate_pending.values()) if p.deferred)
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
            "rotate_retry": rotate_retry,
            "rotate_deferred": rotate_deferred,
            "deferred_delete_pending": deferred_delete_pending,
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
                        self._wal_path_expected = False
                        self._wal_identity = None
                        self._file_first_seq = self._seq + 1
                        self._file_first_seq_pending = False
                    self._dirty_tail[which] = False
            self._fs.sync_dir(self._wal_dir)
            self._rotate_pending.clear()
            self._seq = 0
            self._wal_path_expected = False
            self._wal_identity = None
            self._file_first_seq = 1
            self._file_first_seq_pending = False
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
