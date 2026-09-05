from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import zlib
from collections import deque
from collections.abc import Iterator
from typing import Any

from .constants import DEFAULT_WAL_ROTATE_BYTES, WAL_DIR
from .util import dumps_stable, monotonic_ns, now_iso, to_printable

# 模組層 logger：append 的頻率等同 UART RX，而 logging.getLogger() 每次呼叫都會
# 進 logging.Manager 取全域鎖——不可放在這條熱路徑上（#189）。
_LOG = logging.getLogger("serialwrap")


class WalWriter:
    def __init__(self, wal_dir: str | None = None, rotate_bytes: int = DEFAULT_WAL_ROTATE_BYTES) -> None:
        # None-sentinel：於建構時解析模組層 WAL_DIR（#120）——def-time default 會在類別定義時
        # 凍結 import 當下的值，使 conftest env 隔離與 setattr(wal, "WAL_DIR", ...) 全部失效。
        self._wal_dir = WAL_DIR if wal_dir is None else wal_dir
        self._rotate_bytes = rotate_bytes
        self._wal_path = os.path.join(self._wal_dir, "raw.wal.ndjson")
        self._mirror_path = os.path.join(self._wal_dir, "raw.mirror.log")
        self._lock = threading.Lock()
        self._seq = 0
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

    def _load_last_seq(self) -> None:
        if not os.path.exists(self._wal_path):
            self._seq = 0
            return
        last = 0
        with open(self._wal_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
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
        self._seq = last

    def _rotate_if_needed(self) -> bool:
        """檔案超過上限時輪替。回傳 True 表示輪替過程發生 OSError 已被 best-effort 含住（degrade）。

        輪替只是「限制檔案大小」的最佳化，不是資料平面必要步驟。原本此方法在 ``append`` 的 try
        之外執行，rotation path 的 ``getsize``/``replace``/``open(dir)``/``fsync(dir)`` 因 EIO/權限/
        fd 耗盡拋 OSError 會逃出 ``append`` → 殺死 RX reader thread（正是 #79 想避免的長壽 thread
        死亡，含 console fan-out 一併中止）。改為自身含住例外：失敗則告警並續用既有（可能超大的）
        檔案，遠優於殺 reader（#79 Codex 必修）。
        """
        rotation_failed = False
        for path in (self._wal_path, self._mirror_path):
            try:
                if not os.path.exists(path):
                    continue
                if os.path.getsize(path) < self._rotate_bytes:
                    continue
                ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                dst = f"{path}.{ts}"
                os.replace(path, dst)
                dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                rotation_failed = True
                import logging
                logging.getLogger("serialwrap").warning(
                    "WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：%s：%s", path, exc
                )
        return rotation_failed

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
            rotation_failed = self._rotate_if_needed()
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
            try:
                self._write_record_locked(record, payload)
            except OSError as exc:
                # #189：稽核目錄可能在 daemon 運行期間被外部工具整個刪掉（實地事故：
                # testpilot 的 clean_wal() rmtree 硬編路徑 /tmp/serialwrap/wal）。append
                # 每次都重開檔，故下一筆寫入立刻撞到 ENOENT——在這裡自癒重建目錄並重試
                # 一次，而不是連續六天安靜地把稽核紀錄寫進虛空、seq 一路累加到 126 萬。
                try:
                    os.makedirs(self._wal_dir, exist_ok=True)
                    self._write_record_locked(record, payload)
                except OSError as retry_exc:
                    # 重建也失敗（ENOSPC/EROFS/權限/fd 耗盡）不得讓資料平面（RX reader
                    # thread）崩潰（#79 STA-1）。best-effort：標記 loss、告警，仍回傳
                    # record 讓上游照常 fan-out 給 console、續處理後續 RX。
                    record["loss_flag"] = True
                    self._write_failures += 1
                    self._last_write_error = f"{type(retry_exc).__name__}: {retry_exc}"
                    _LOG.warning("WAL append 寫入失敗（best-effort 略過）：%s", retry_exc)
                else:
                    self._recreated_count += 1
                    self._last_write_error = f"{type(exc).__name__}: {exc}"
                    _LOG.warning(
                        "WAL 目錄 %s 消失，已重建並續寫（原因：%s）；消失期間的紀錄無法復原",
                        self._wal_dir, exc,
                    )
        return record

    def _write_record_locked(self, record: dict[str, Any], payload: bytes) -> None:
        """雙軌 append：權威 ndjson ＋ 人類可讀鏡像。須在 ``self._lock`` 內呼叫。"""
        with open(self._wal_path, "a", encoding="utf-8") as wal_fp:
            wal_fp.write(dumps_stable(record))
            wal_fp.write("\n")
        with open(self._mirror_path, "a", encoding="utf-8") as mirror_fp:
            mirror_fp.write(to_printable(payload))

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
        wal_dir_writable = wal_dir_exists and os.access(self._wal_dir, os.W_OK)
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
            ),
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
            for path in (self._wal_path, self._mirror_path):
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    dst = f"{path}.{ts}"
                    os.replace(path, dst)
            dir_fd = os.open(self._wal_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            self._seq = 0
            return {"ok": True, "previous_seq": prev_seq, "rotated_suffix": ts}

    def _iter_matching(self, com: str | None) -> Iterator[dict[str, Any]]:
        """逐行掃**現行** WAL 檔（``raw.wal.ndjson``），產出通過解析與 com 過濾的紀錄（seq 為 int 才算有效）。

        注意：輪替（rotation）歸檔的 ``raw.wal.ndjson.<ts>`` 檔不在掃描範圍內（#124 review）。
        """
        with open(self._wal_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
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
