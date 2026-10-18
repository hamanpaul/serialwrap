"""realhw.wal_oracle：WAL 活性判定 oracle（seq／marker／CRC）。

依據：docs/rust-refactor/plans/02-stage0-python-bc.md Task T17
規格 §8.3、§10.4、§15.10；OD-13。
獨立性要求：不得 import sw_core（測試工具獨立性，stdlib only）。
"""
from __future__ import annotations

import base64
import errno
import io
import json
import os
import re
import stat
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WalCursor:
    """WAL 讀取游標，記錄檔案身分與起始偏移量。"""

    path: str
    identity: tuple[int, int]  # (st_dev, st_ino)
    offset: int  # 起始時檔案大小（只從此之後找新紀錄）
    last_seq: int | None  # 起始時最後一筆完整紀錄的 seq
    tail_crc: int | None = None  # offset 之前至多 64 bytes 的 CRC32：偵測同 inode 號被重用的新檔
    mtime_ns: int | None = None  # open_cursor 時的 st_mtime_ns，供 diagnostics 比對


@dataclass(frozen=True)
class OracleResult:
    """Oracle 判定結果。"""

    ok: bool
    reason_code: str  # "ok" | "wal_unreadable" | "wal_record_corrupt" | "wal_marker_not_found" | "wal_seq_regressed" | "wal_seq_discontinuous" | "wal_evidence_incomplete"
    matched: dict[str, Any] | None  # 命中的原始紀錄
    raw_lines: tuple[str, ...]  # 讀到的新增完整行（證據，最多 200 行）
    diagnostics: dict[str, Any]  # size_before/after、mtime_before/after、archives_scanned

    def to_dict(self) -> dict[str, Any]:
        """轉為字典格式以利 JSON 序列化。"""
        import dataclasses

        return dataclasses.asdict(self)


def reason_category(reason_code: str) -> str:
    """依 reason_code 判定測試分類（environment 或 test）。"""
    return "environment" if reason_code in ("wal_unreadable", "wal_evidence_incomplete") else "test"


def read_shared(path: str, offset: int) -> bytes:
    """以共用讀取模式讀取檔案（Windows 帶 FILE_SHARE_READ|WRITE|DELETE，POSIX 走 open）。"""
    if os.name == "nt":
        import ctypes
        import msvcrt

        generic_read = 0x80000000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        file_share_all = file_share_read | file_share_write | file_share_delete
        open_existing = 3
        file_attribute_normal = 0x00000080
        invalid_handle_value = ctypes.c_void_p(-1).value

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.CreateFileW(
            path,
            generic_read,
            file_share_all,
            None,
            open_existing,
            file_attribute_normal,
            None,
        )
        if handle == invalid_handle_value or handle == -1 or handle is None:
            err = ctypes.get_last_error()
            if err in (2, 3):
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
            raise ctypes.WinError(err)

        try:
            fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except Exception:
            kernel32.CloseHandle(handle)
            raise

        with io.open(fd, "rb", closefd=True) as fp:
            if offset > 0:
                fp.seek(offset)
            return fp.read()
    else:
        with open(path, "rb") as fp:
            if offset > 0:
                fp.seek(offset)
            return fp.read()


def verify_record(rec: dict[str, Any]) -> str | None:
    """驗證單筆 WAL 紀錄之完整性與 CRC。

    回傳 None 表示合法；否則回傳 "schema" | "b64_invalid" | "len_mismatch" | "crc_mismatch"。
    """
    if not isinstance(rec, dict):
        return "schema"

    required_keys = (
        "seq",
        "mono_ts_ns",
        "wall_ts",
        "com",
        "dir",
        "source",
        "len",
        "crc32",
        "payload_b64",
        "loss_flag",
        "meta",
    )
    for k in required_keys:
        if k not in rec:
            return "schema"

    if not isinstance(rec["seq"], int) or isinstance(rec["seq"], bool):
        return "schema"
    if not isinstance(rec["len"], int) or isinstance(rec["len"], bool):
        return "schema"
    if not isinstance(rec["crc32"], str):
        return "schema"
    if not isinstance(rec["payload_b64"], str):
        return "schema"
    if not isinstance(rec["loss_flag"], bool):
        return "schema"
    if not isinstance(rec["meta"], dict):
        return "schema"
    if not isinstance(rec["dir"], str):
        return "schema"
    if not isinstance(rec["source"], str):
        return "schema"

    try:
        payload = base64.b64decode(rec["payload_b64"], validate=True)
    except Exception:
        return "b64_invalid"

    if len(payload) != rec["len"]:
        return "len_mismatch"

    expected_crc = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
    if rec["crc32"].lower() != expected_crc:
        return "crc_mismatch"

    return None


def open_cursor(wal_path: str) -> WalCursor:
    """開啟指定 wal_path 的游標：offset／last_seq 取自同一份讀取快照。"""
    data = b""
    identity: tuple[int, int] = (0, 0)
    st1 = None
    for _ in range(3):
        st0 = os.stat(wal_path)
        if stat.S_ISDIR(st0.st_mode):
            raise IsADirectoryError(errno.EISDIR, "Is a directory", wal_path)
        data = read_shared(wal_path, 0)  # size 0 也讀：順便驗證可讀
        st1 = os.stat(wal_path)
        identity = (st1.st_dev, st1.st_ino)
        if (st0.st_dev, st0.st_ino) == identity:
            break
    offset = len(data)  # 以快照長度為準，而非 stat().st_size
    last_seq: int | None = None
    last_nl = data.rfind(b"\n")
    if last_nl != -1:
        for line_bytes in reversed(data[: last_nl + 1].split(b"\n")):
            line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
            if not line_str.strip():
                continue
            try:
                obj = json.loads(line_str)
            except Exception:
                continue
            if isinstance(obj, dict) and verify_record(obj) is None:
                last_seq = obj.get("seq")
                break
    tail_len = min(offset, 512)
    tail_crc = zlib.crc32(data[offset - tail_len:offset]) if offset > 0 else None
    mtime_ns = st1.st_mtime_ns if st1 is not None else None
    return WalCursor(
        path=wal_path,
        identity=identity,
        offset=offset,
        last_seq=last_seq,
        tail_crc=tail_crc,
        mtime_ns=mtime_ns,
    )


def _read_tail(path: str, offset: int) -> bytes:
    """讀 offset 之後的位元組；offset 若落在行中，丟棄該行殘段（它屬於 cursor 之前的行）。"""
    if offset <= 0:
        return read_shared(path, 0)
    data = read_shared(path, offset - 1)
    if data[:1] == b"\n":
        return data[1:]
    nl = data.find(b"\n")
    return b"" if nl == -1 else data[nl + 1:]


_ARCHIVE_SUFFIX_RE = re.compile(r"^(\d{8}-\d{6})(?:\.(\d+))?$")


def _archive_key(entry: str, base: str) -> tuple[str, int] | None:
    """解析歸檔後綴為 (時間戳字串, 序號整數)，非歸檔格式回傳 None。"""
    m = _ARCHIVE_SUFFIX_RE.match(entry[len(base) + 1:])
    return None if m is None else (m.group(1), int(m.group(2) or 0))


def _scan_new_records(
    cursor: WalCursor, *, allow_reset: bool = False
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    """掃描自游標以來的全部新增紀錄，回傳 (records, raw_lines, problems, archives_scanned)。"""
    problems: list[str] = []
    archives_scanned: list[str] = []
    parent = os.path.dirname(os.path.abspath(cursor.path)) or "."
    base = os.path.basename(cursor.path)

    try:
        cur_st = os.stat(cursor.path)
        if stat.S_ISDIR(cur_st.st_mode):
            raise IsADirectoryError(errno.EISDIR, "Is a directory", cursor.path)
        cur_identity = (cur_st.st_dev, cur_st.st_ino)
        cur_size = cur_st.st_size
        path_exists = True
    except (FileNotFoundError, OSError):
        path_exists = False
        cur_identity = None
        cur_size = 0

    rotated = (not path_exists) or (cur_identity != cursor.identity) or (cur_size < cursor.offset)
    if not rotated and cursor.tail_crc is not None and cursor.offset > 0:
        tail_len = min(cursor.offset, 512)
        try:
            head = read_shared(cursor.path, cursor.offset - tail_len)[:tail_len]
            if zlib.crc32(head) != cursor.tail_crc:
                rotated = True  # inode 號相同但 offset 前的內容已不同＝被替換的新檔
        except OSError:
            problems.append("wal_unreadable")

    files_to_read: list[tuple[str, int, bool]] = []  # (path, offset, is_archive)
    continuation = True  # 第一個讀取的檔案是否為 cursor 所在檔（無論現行或歸檔）

    if not rotated:
        files_to_read = [(cursor.path, cursor.offset, False)]
    else:
        candidates: list[tuple[tuple[str, int], str, tuple[int, int]]] = []
        try:
            entries = os.listdir(parent)
        except OSError:
            entries = []
        for entry in entries:
            key = _archive_key(entry, base) if entry.startswith(base + ".") else None
            if key is None:
                continue  # 非歸檔後綴（.lock/.bak/.gz…）不是證據
            try:
                cand_st = os.stat(os.path.join(parent, entry))
            except OSError:
                continue
            candidates.append((key, entry, (cand_st.st_dev, cand_st.st_ino)))
        candidates.sort()  # (時間, 數字後綴) 升冪；.2 < .10

        matching = [c for c in candidates if c[2] == cursor.identity]
        if len(matching) != 1:
            problems.append("archive_missing")  # 找不到或不唯一
            continuation = False
            if path_exists:
                files_to_read.append((cursor.path, 0, False))
            else:
                problems.append("wal_unreadable")
        else:
            initial = matching[0]
            archives_scanned = [initial[1]] + [c[1] for c in candidates if c[0] > initial[0]]
            files_to_read.append((os.path.join(parent, initial[1]), cursor.offset, True))
            for c in candidates:
                if c[0] > initial[0]:
                    files_to_read.append((os.path.join(parent, c[1]), 0, True))
            if path_exists:
                files_to_read.append((cursor.path, 0, False))

    all_records: list[dict[str, Any]] = []
    all_raw_lines: list[str] = []
    prev_seq: int | None = cursor.last_seq if continuation else None

    for idx, (fpath, foffset, is_arch) in enumerate(files_to_read):
        try:
            data = _read_tail(fpath, foffset)
        except OSError:
            problems.append("archive_unreadable" if is_arch else "wal_unreadable")
            continue

        last_nl = data.rfind(b"\n")
        complete_bytes = b"" if last_nl == -1 else data[: last_nl + 1]

        file_records: list[dict[str, Any]] = []
        for line_bytes in complete_bytes.split(b"\n"):
            line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
            if not line_str.strip():
                continue
            all_raw_lines.append(line_str)
            try:
                rec = json.loads(line_str)
            except Exception:
                problems.append("wal_record_corrupt")
                continue
            if verify_record(rec) is not None:
                problems.append("wal_record_corrupt")
                continue
            file_records.append(rec)
            all_records.append(rec)

        if file_records:
            first_s = file_records[0]["seq"]
            if prev_seq is not None:
                if idx == 0 and continuation:
                    if first_s <= prev_seq:
                        problems.append("wal_seq_regressed")
                elif allow_reset and first_s == 1:
                    pass
                elif first_s == prev_seq + 1:
                    pass
                elif first_s > prev_seq + 1:
                    problems.append("intermediate_archive_missing")
                else:
                    problems.append("wal_seq_discontinuous")
            for i in range(1, len(file_records)):
                if file_records[i]["seq"] <= file_records[i - 1]["seq"]:
                    problems.append("wal_seq_regressed")
            prev_seq = file_records[-1]["seq"]

    return all_records, all_raw_lines, problems, archives_scanned


def scan_new_records(
    cursor: WalCursor, *, allow_reset: bool = False
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """掃描自游標以來的全部新增紀錄，回傳 (records, raw_lines, problems)。"""
    records, raw_lines, problems, _ = _scan_new_records(cursor, allow_reset=allow_reset)
    return records, raw_lines, problems


def _is_match(rec: dict[str, Any], marker: bytes, cmd_id: str | None) -> bool:
    try:
        payload = base64.b64decode(rec.get("payload_b64", ""))
    except Exception:
        return False
    if marker not in payload:
        return False
    if cmd_id is not None:
        return rec.get("dir") == "TX" and rec.get("cmd_id") == cmd_id
    else:
        return rec.get("dir") == "RX"


def wait_for_marker(
    cursor: WalCursor,
    *,
    marker: bytes,
    cmd_id: str | None,
    timeout_s: float,
    allow_reset: bool = False,
    poll_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> OracleResult:
    """等待 WAL 出現含指定 marker 與 cmd_id 之紀錄。"""
    marker_bytes = marker.encode("utf-8") if isinstance(marker, str) else marker
    if cursor.mtime_ns is not None:
        mtime_before = cursor.mtime_ns / 1e9
    else:
        mtime_before = 0.0
        try:
            if os.path.exists(cursor.path):
                mtime_before = os.path.getmtime(cursor.path)
        except OSError:
            pass

    def _diag(archives: list[str]) -> dict[str, Any]:
        size_after = 0
        mtime_after = 0.0
        try:
            if os.path.exists(cursor.path):
                size_after = os.path.getsize(cursor.path)
                mtime_after = os.path.getmtime(cursor.path)
        except OSError:
            pass
        return {
            "size_before": cursor.offset,
            "size_after": size_after,
            "mtime_before": mtime_before,
            "mtime_after": mtime_after,
            "archives_scanned": archives,
        }

    start = monotonic()
    last_problems: list[str] = []
    last_raw_lines: list[str] = []
    last_archives: list[str] = []

    while True:
        try:
            records, raw_lines, problems, archives = _scan_new_records(cursor, allow_reset=allow_reset)
        except OSError:
            records, raw_lines, problems, archives = [], [], ["wal_unreadable"], []

        last_problems = problems
        last_raw_lines = raw_lines
        last_archives = archives

        if "wal_record_corrupt" in problems:
            return OracleResult(
                ok=False,
                reason_code="wal_record_corrupt",
                matched=None,
                raw_lines=tuple(raw_lines[:200]),
                diagnostics=_diag(archives),
            )

        if "wal_seq_regressed" in problems:
            return OracleResult(
                ok=False,
                reason_code="wal_seq_regressed",
                matched=None,
                raw_lines=tuple(raw_lines[:200]),
                diagnostics=_diag(archives),
            )

        if "wal_seq_discontinuous" in problems:
            return OracleResult(
                ok=False,
                reason_code="wal_seq_discontinuous",
                matched=None,
                raw_lines=tuple(raw_lines[:200]),
                diagnostics=_diag(archives),
            )

        matched_rec = None
        for rec in records:
            if _is_match(rec, marker_bytes, cmd_id):
                matched_rec = rec
                break

        has_transient_error = any(p in ("wal_unreadable", "archive_unreadable") for p in problems)

        if matched_rec is not None and not has_transient_error:
            if any(p in ("archive_missing", "intermediate_archive_missing") for p in problems):
                return OracleResult(
                    ok=False,
                    reason_code="wal_evidence_incomplete",
                    matched=None,
                    raw_lines=tuple(raw_lines[:200]),
                    diagnostics=_diag(archives),
                )
            return OracleResult(
                ok=True,
                reason_code="ok",
                matched=matched_rec,
                raw_lines=tuple(raw_lines[:200]),
                diagnostics=_diag(archives),
            )

        elapsed = monotonic() - start
        if elapsed >= timeout_s:
            if "wal_unreadable" in last_problems:
                return OracleResult(
                    ok=False,
                    reason_code="wal_unreadable",
                    matched=None,
                    raw_lines=tuple(last_raw_lines[:200]),
                    diagnostics=_diag(last_archives),
                )
            if any(p in ("archive_missing", "intermediate_archive_missing", "archive_unreadable") for p in last_problems):
                return OracleResult(
                    ok=False,
                    reason_code="wal_evidence_incomplete",
                    matched=None,
                    raw_lines=tuple(last_raw_lines[:200]),
                    diagnostics=_diag(last_archives),
                )
            return OracleResult(
                ok=False,
                reason_code="wal_marker_not_found",
                matched=None,
                raw_lines=tuple(last_raw_lines[:200]),
                diagnostics=_diag(last_archives),
            )

        remaining = timeout_s - elapsed
        to_sleep = min(poll_s, remaining)
        if to_sleep <= 0:
            to_sleep = poll_s
        sleep(to_sleep)
