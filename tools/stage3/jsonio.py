"""JSON、NDJSON 與檔案寫入的共用工具（stdlib only）。

輸出格式依 CLAUDE.md 慣例：CLI 輸出使用 `dumps_compact`，持久化報告使用 `dumps_report`，
兩者皆 `sort_keys=True` 以維持 diff 穩定。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """回傳帶微秒與 `+00:00` 的 UTC 時間字串。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(ts: str) -> datetime:
    """解析 ISO 8601 時間；接受 `Z` 與 `+00:00`，naive 時間一律拒絕。"""
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp rejected: {ts!r}")
    return dt


def dumps_compact(obj: Any) -> str:
    """緊湊、穩定排序的 JSON 字串（非 ASCII 原樣輸出）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def dumps_report(obj: Any) -> str:
    """持久化報告用的 JSON 字串：縮排、排序、尾端換行。"""
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def append_ndjson(path: Path, obj: dict) -> None:
    """以 append 模式寫入一行 NDJSON（一律 LF），flush 並 fsync（Windows 亦然）。

    前次寫入若在中途中斷而留下沒有結尾換行的殘行，先補一個 `\n` 把殘行隔離成單獨的壞行，
    避免新紀錄黏在殘行後面一起失效。
    """
    path = Path(path)
    needs_sep = False
    try:
        with path.open("rb") as probe:
            probe.seek(0, os.SEEK_END)
            if probe.tell() > 0:
                probe.seek(-1, os.SEEK_END)
                needs_sep = probe.read(1) != b"\n"
    except FileNotFoundError:
        pass
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(("\n" if needs_sep else "") + dumps_compact(obj) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_ndjson(path: Path) -> tuple[list[dict], list[int]]:
    """讀取 NDJSON，回傳 `(合法物件列表, 壞行行號)`；壞行不丟例外。

    以位元組逐行讀取並嚴格解碼為 UTF-8：`dumps_compact` 只會寫出合法 UTF-8，解碼失敗的行即為損毀，
    不以替代字元掩蓋。只以 `\n` 切行（`dumps_compact` 的 `ensure_ascii=False` 會原樣輸出
    U+2028／U+2029／U+0085，`str.splitlines()` 會把它們誤切成兩行）。空白行略過；非物件的合法 JSON 視為壞行。
    """
    records: list[dict] = []
    malformed: list[int] = []
    with Path(path).open("rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                # UnicodeDecodeError、JSONDecodeError 皆為 ValueError 子類；另含超過 int 位數上限的整數字面量
                # （ValueError: Exceeds the limit）與過深巢狀（RecursionError）。這些都必須以壞行回報，不得逸出
                malformed.append(lineno)
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                malformed.append(lineno)
    return records, malformed


def _new_file_mode() -> int:
    """新檔權限與一般 `open()` 相同：`0o666 & ~umask`（`mkstemp` 固定 0600，需明確補上）。

    `os.umask` 只能整體讀寫，會短暫改動程序全域值；本工具的寫入皆在單一執行緒內完成，可接受。
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def atomic_write_text(path: Path, text: str) -> None:
    """同目錄暫存檔寫入後以 `os.replace` 取代，避免讀到半寫的檔案。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp 固定 0600：覆寫既有檔沿用原權限（避免 0644 的報告被悄悄收緊）；新檔套用一般 umask，
        # 否則 root 或 docker 產生的報告對宿主使用者與 CI 不可讀
        try:
            os.chmod(tmp_name, os.stat(path).st_mode & 0o7777)
        except FileNotFoundError:
            os.chmod(tmp_name, _new_file_mode())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def record_id(obj: dict) -> str:
    """以去掉 `record_id` 欄位後的內容 sha256 取前 20 hex 作為紀錄識別碼。"""
    body = {k: v for k, v in obj.items() if k != "record_id"}
    return sha256_bytes(dumps_compact(body).encode("utf-8"))[:20]
