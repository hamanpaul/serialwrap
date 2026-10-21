"""增量收割 Canary WAL，驗證完整性並產生提交觀測與安全訊號。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from tools.stage3 import jsonio
from tools.stage3.canary_policy import CanaryPolicy

WAL_CURRENT = "raw.wal.ndjson"
HEAD_BYTES = 4096
READ_BYTES = 64 * 1024
_TWO_DAYS_S = 2 * 24 * 60 * 60
_FLASH_INTERVAL_S = 60


@dataclass
class FileCursor:
    identity: str
    head_sha256: str
    head_len: int
    path_last_seen: str
    offset: int
    size_last_seen: int
    # 增量掃描跨呼叫仍須偵測 seq 回退；舊 cursor 缺欄位時從 None 起算。
    last_seq: int | None = None


@dataclass
class Cursor:
    files: dict[str, FileCursor]
    tx_window: dict[str, list]
    human_window: dict[str, str]
    human_buckets: dict[str, int]
    # Canary WAL 每次只讀新增區段，因此 flash 的 60 秒限流時間需跨呼叫保存。
    flash_window: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HarvestRules:
    exclude_source_prefixes: tuple[str, ...]
    cmd_id_regex: str
    allowed_tx_source_regex: str
    resend_window_s: int
    human_replay_min_len: int
    coexist_bucket_s: int

    @staticmethod
    def from_policy(policy: CanaryPolicy) -> "HarvestRules":
        return HarvestRules(
            exclude_source_prefixes=tuple(policy.exclude_source_prefixes),
            cmd_id_regex=policy.cmd_id_regex,
            allowed_tx_source_regex=policy.allowed_tx_source_regex,
            resend_window_s=policy.resend_window_s,
            human_replay_min_len=policy.human_replay_min_len,
            coexist_bucket_s=policy.coexist_bucket_s,
        )


@dataclass(frozen=True)
class SubmitObservation:
    cmd_id: str
    com: str | None
    source: str
    wall_ts: str
    seq: int
    wal_file: str


@dataclass(frozen=True)
class WalSignal:
    kind: str
    com: str | None
    wall_ts: str
    seq: int
    source: str
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class HarvestResult:
    submits: tuple[SubmitObservation, ...]
    signals: tuple[WalSignal, ...]
    anomalies: tuple[str, ...]
    cursor: Cursor


def open_shared(path: Path) -> BinaryIO:
    """以允許同時 rename／unlink 的方式唯讀開啟 WAL。"""
    path = Path(path)
    if os.name != "nt":
        return path.open("rb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ：一般讀取
        0x00000001 | 0x00000002 | 0x00000004,  # 允許讀取、寫入與刪除分享
        None,
        3,  # OPEN_EXISTING：只開啟現有檔案
        0x00000080,  # FILE_ATTRIBUTE_NORMAL：一般檔案屬性
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _head(path: Path, length: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    read_len = 0
    with open_shared(path) as stream:
        remaining = max(0, length)
        while remaining:
            chunk = stream.read(min(READ_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            read_len += len(chunk)
            remaining -= len(chunk)
    return digest.hexdigest(), read_len


def file_identity(path: Path) -> tuple[str, str, int]:
    """回傳檔案裝置／inode、前 4 KiB 雜湊與實際雜湊長度。"""
    path = Path(path)
    stat = os.stat(path)
    head_len = min(stat.st_size, HEAD_BYTES)
    head_sha256, actual_len = _head(path, head_len)
    return f"{stat.st_dev}:{stat.st_ino}", head_sha256, actual_len


def wal_files(wal_dir: Path) -> list[Path]:
    """列出現行 WAL 與所有輪替檔；排除暫存檔並依修改時間排序。"""
    wal_dir = Path(wal_dir)
    paths: list[Path] = []
    current = wal_dir / WAL_CURRENT
    candidates = [current, *wal_dir.glob(WAL_CURRENT + ".*")]
    for path in candidates:
        if path.name.endswith(".tmp") or not path.is_file():
            continue
        paths.append(path)
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _stable_wal_files(wal_dir: Path) -> list[Path]:
    """重讀短暫變動的檔案清單，避免輪替期間漏掉已改名的 WAL。"""
    previous: tuple[Path, ...] | None = None
    latest: list[Path] = []
    for _ in range(3):
        try:
            latest = wal_files(wal_dir)
        except FileNotFoundError:
            latest = []
        snapshot = tuple(latest)
        if snapshot == previous:
            return latest
        previous = snapshot
    return latest


def _empty_cursor() -> Cursor:
    return Cursor(files={}, tx_window={}, human_window={}, human_buckets={})


def _copy_cursor(cursor: Cursor) -> Cursor:
    return Cursor(
        files={key: FileCursor(**asdict(value)) for key, value in cursor.files.items()},
        tx_window={key: list(value) for key, value in cursor.tx_window.items()},
        human_window=dict(cursor.human_window),
        human_buckets=dict(cursor.human_buckets),
        flash_window=dict(getattr(cursor, "flash_window", {})),
    )


def _parse_epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return jsonio.parse_iso(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_window(previous: Any, now_epoch: float, window_s: int) -> bool:
    if not isinstance(previous, (list, tuple)) or len(previous) != 3:
        return False
    previous_epoch = _parse_epoch(previous[2])
    if previous_epoch is None:
        return False
    delta = now_epoch - previous_epoch
    return 0 <= delta <= window_s


def _valid_duplicate(previous_ts: Any, now_epoch: float, window_s: int) -> bool:
    previous_epoch = _parse_epoch(previous_ts)
    if previous_epoch is None:
        return False
    delta = now_epoch - previous_epoch
    return 0 <= delta <= window_s


def _signal(
    kind: str,
    record: dict[str, Any],
    detail: Mapping[str, Any] | None = None,
) -> WalSignal:
    return WalSignal(
        kind=kind,
        com=record.get("com") if isinstance(record.get("com"), str) else None,
        wall_ts=record["wall_ts"],
        seq=record["seq"],
        source=record["source"],
        detail=dict(detail or {}),
    )


def _is_excluded(source: str, prefixes: tuple[str, ...]) -> bool:
    return any(source.startswith(prefix) for prefix in prefixes)


def _record_signal(
    record: dict[str, Any],
    *,
    rules: HarvestRules,
    cursor: Cursor,
    allowed_source: re.Pattern[str],
    cmd_id_pattern: re.Pattern[str],
    now_epoch: float | None,
) -> WalSignal | None:
    direction = record["dir"]
    source = record["source"]
    metadata = record.get("meta")
    event = metadata.get("event") if isinstance(metadata, dict) else None

    if direction == "META":
        if event == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK":
            return _signal("wal_recreated", record, {"event": event})
        if record.get("loss_flag") is True:
            return _signal("wal_loss", record)
        if event == "credentials_unresolved":
            return _signal("credentials_unresolved", record, {"event": event})
        if event == "transport_stall_suspected":
            return _signal("transport_stall", record, {"event": event})
        return None

    if record.get("loss_flag") is True:
        return _signal("wal_loss", record)
    if direction != "TX":
        return None
    if allowed_source.fullmatch(source) is None:
        return _signal("unauthorized_tx", record)
    if source == "system:recover":
        return _signal("recover_tx", record)
    if source == "flash":
        com_key = record.get("com") if isinstance(record.get("com"), str) else "null"
        previous = cursor.flash_window.get(com_key)
        if now_epoch is None or not _valid_duplicate(previous, now_epoch, _FLASH_INTERVAL_S - 1):
            cursor.flash_window[com_key] = record["wall_ts"]
            return _signal("flash_tx", record)
        return None
    if source.startswith("human:"):
        com = record.get("com") if isinstance(record.get("com"), str) else None
        com_key = "null" if com is None else com
        bucket_signal = False
        if now_epoch is not None:
            bucket = int(now_epoch // rules.coexist_bucket_s)
            bucket_key = f"{com_key}|{bucket}"
            bucket_signal = bucket_key not in cursor.human_buckets
            cursor.human_buckets[bucket_key] = 1

        crc = record["crc32"]
        length = record["len"]
        replay_key = f"{com_key}|{crc}|{length}"
        previous_ts = cursor.human_window.get(replay_key)
        replay = (
            length >= rules.human_replay_min_len
            and now_epoch is not None
            and _valid_duplicate(previous_ts, now_epoch, rules.resend_window_s)
        )
        if now_epoch is not None and length >= rules.human_replay_min_len:
            cursor.human_window[replay_key] = record["wall_ts"]

        detail = {"crc32": crc, "len": length}
        if bucket_signal:
            return _signal("human_tx", record, detail)
        if replay:
            return _signal("human_replay_suspect", record, {**detail, "previous_wall_ts": previous_ts})
        return None

    cmd_id = record.get("cmd_id")
    if isinstance(cmd_id, str) and cmd_id_pattern.fullmatch(cmd_id) and now_epoch is not None:
        previous = cursor.tx_window.get(cmd_id)
        same_payload = (
            isinstance(previous, (list, tuple))
            and len(previous) == 3
            and previous[0] == record["crc32"]
            and previous[1] == record["len"]
        )
        if same_payload and _valid_window(previous, now_epoch, rules.resend_window_s):
            cursor.tx_window[cmd_id] = [record["crc32"], record["len"], record["wall_ts"]]
            return _signal(
                "resend_suspect",
                record,
                {
                    "crc32": record["crc32"],
                    "len": record["len"],
                    "previous_wall_ts": previous[2],
                },
            )
        cursor.tx_window[cmd_id] = [record["crc32"], record["len"], record["wall_ts"]]
    return None


def _valid_record(raw_line: bytes, identity: str, offset: int, anomalies: list[str]) -> dict[str, Any] | None:
    try:
        record = json.loads(raw_line.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeError):
        anomalies.append(f"malformed_line:{identity}:{offset}")
        return None
    if not isinstance(record, dict):
        anomalies.append(f"malformed_line:{identity}:{offset}")
        return None

    seq = record.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        anomalies.append(f"malformed_line:{identity}:{offset}")
        return None
    if not isinstance(record.get("dir"), str) or not isinstance(record.get("source"), str):
        anomalies.append(f"malformed_line:{identity}:{offset}")
        return None
    if not isinstance(record.get("wall_ts"), str):
        anomalies.append(f"malformed_line:{identity}:{offset}")
        return None

    encoded = record.get("payload_b64")
    crc = record.get("crc32")
    length = record.get("len")
    payload: bytes | None = None
    integrity_valid = True
    try:
        if not isinstance(encoded, str):
            raise ValueError("payload_b64 is not a string")
        payload = base64.b64decode(encoded, validate=True)
        if type(length) is not int or length != len(payload):
            raise ValueError("length mismatch")
        if not isinstance(crc, str) or re.fullmatch(r"[0-9a-f]{8}", crc) is None:
            raise ValueError("invalid crc32")
        if int(crc, 16) != (zlib.crc32(payload) & 0xFFFFFFFF):
            raise ValueError("crc mismatch")
    except (ValueError, binascii.Error):
        anomalies.append(f"crc_mismatch:{identity}:{seq}")
        integrity_valid = False
    if not integrity_valid:
        record["_integrity_valid"] = False
    return record


def _new_file_cursor(path: Path, identity: str, size: int) -> tuple[str, FileCursor]:
    head_len = min(size, HEAD_BYTES)
    head_sha256, actual_len = _head(path, head_len)
    key = f"{identity}|{head_sha256}"
    return key, FileCursor(
        identity=identity,
        head_sha256=head_sha256,
        head_len=actual_len,
        path_last_seen=str(path),
        offset=0,
        size_last_seen=size,
    )


def _find_file_cursor(
    path: Path,
    stat: os.stat_result,
    cursor: Cursor,
    anomalies: list[str],
) -> tuple[str, FileCursor, bool]:
    identity = f"{stat.st_dev}:{stat.st_ino}"
    candidates = [(key, value) for key, value in cursor.files.items() if value.identity == identity]

    for key, previous in candidates:
        if stat.st_size < previous.offset:
            anomalies.append(f"truncated:{identity}")
            cursor.files.pop(key, None)
            new_key, fresh = _new_file_cursor(path, identity, stat.st_size)
            return new_key, fresh, True

    for key, previous in candidates:
        if stat.st_size < previous.head_len:
            continue
        head_sha256, actual_len = _head(path, previous.head_len)
        if actual_len == previous.head_len and head_sha256 == previous.head_sha256:
            previous.path_last_seen = str(path)
            previous.size_last_seen = stat.st_size
            return key, previous, False

    if candidates:
        anomalies.append(f"replaced:{identity}")
        for key, _ in candidates:
            cursor.files.pop(key, None)
    key, fresh = _new_file_cursor(path, identity, stat.st_size)
    return key, fresh, True


def _prune_windows(cursor: Cursor, rules: HarvestRules, latest_epoch: float | None) -> None:
    if latest_epoch is None:
        return
    for cmd_id, value in list(cursor.tx_window.items()):
        ts = value[2] if isinstance(value, (list, tuple)) and len(value) == 3 else None
        epoch = _parse_epoch(ts)
        if epoch is None or latest_epoch - epoch > rules.resend_window_s:
            cursor.tx_window.pop(cmd_id, None)
    for key, ts in list(cursor.human_window.items()):
        epoch = _parse_epoch(ts)
        if epoch is None or latest_epoch - epoch > rules.resend_window_s:
            cursor.human_window.pop(key, None)
    cutoff = latest_epoch - _TWO_DAYS_S
    for key in list(cursor.human_buckets):
        try:
            bucket = int(key.rsplit("|", 1)[1])
        except (IndexError, ValueError):
            cursor.human_buckets.pop(key, None)
            continue
        if bucket * rules.coexist_bucket_s < cutoff:
            cursor.human_buckets.pop(key, None)
    for com, ts in list(cursor.flash_window.items()):
        epoch = _parse_epoch(ts)
        if epoch is None or latest_epoch - epoch >= _FLASH_INTERVAL_S:
            cursor.flash_window.pop(com, None)


def harvest(wal_dir: Path, cursor: Cursor, *, rules: HarvestRules) -> HarvestResult:
    """增量收割完整 WAL 行；無效或不完整紀錄不得形成提交或訊號。"""
    working = _copy_cursor(cursor)
    anomalies: list[str] = []
    signals: list[WalSignal] = []
    submits_by_id: dict[str, SubmitObservation] = {}
    present_keys: set[str] = set()
    latest_epoch: float | None = None
    cmd_id_pattern = re.compile(rules.cmd_id_regex)
    allowed_source = re.compile(rules.allowed_tx_source_regex)

    for path in _stable_wal_files(Path(wal_dir)):
        stat = os.stat(path)
        identity = f"{stat.st_dev}:{stat.st_ino}"
        key, file_cursor, newly_seen = _find_file_cursor(path, stat, working, anomalies)
        present_keys.add(key)
        working.files[key] = file_cursor
        start_offset = 0 if newly_seen else file_cursor.offset
        if stat.st_size < start_offset:
            anomalies.append(f"truncated:{identity}")
            start_offset = 0
            file_cursor.last_seq = None

        line_start = start_offset
        pending = bytearray()
        with open_shared(path) as stream:
            stream.seek(start_offset)
            while True:
                chunk = stream.read(READ_BYTES)
                if not chunk:
                    break
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    raw_line = bytes(pending[:newline])
                    consumed = newline + 1
                    del pending[:consumed]
                    record_offset = line_start
                    line_start += consumed

                    record = _valid_record(raw_line, identity, record_offset, anomalies)
                    if record is None:
                        continue
                    seq = record["seq"]
                    if file_cursor.last_seq is not None and seq <= file_cursor.last_seq:
                        anomalies.append(f"seq_regress:{identity}:{seq}")
                    file_cursor.last_seq = seq

                    wall_ts = record["wall_ts"]
                    now_epoch = _parse_epoch(wall_ts)
                    if record.get("_integrity_valid") is False:
                        continue
                    if now_epoch is not None:
                        latest_epoch = now_epoch if latest_epoch is None else max(latest_epoch, now_epoch)

                    signal = _record_signal(
                        record,
                        rules=rules,
                        cursor=working,
                        allowed_source=allowed_source,
                        cmd_id_pattern=cmd_id_pattern,
                        now_epoch=now_epoch,
                    )
                    if signal is not None:
                        signals.append(signal)

                    source = record["source"]
                    cmd_id = record.get("cmd_id")
                    if (
                        record["dir"] == "TX"
                        and isinstance(cmd_id, str)
                        and cmd_id_pattern.fullmatch(cmd_id) is not None
                        and not _is_excluded(source, rules.exclude_source_prefixes)
                    ):
                        observation = SubmitObservation(
                            cmd_id=cmd_id,
                            com=record.get("com") if isinstance(record.get("com"), str) else None,
                            source=source,
                            wall_ts=wall_ts,
                            seq=seq,
                            wal_file=path.name,
                        )
                        previous = submits_by_id.get(cmd_id)
                        if previous is None or (wall_ts, seq) < (previous.wall_ts, previous.seq):
                            submits_by_id[cmd_id] = observation
            file_cursor.offset = line_start
            try:
                file_cursor.size_last_seen = os.fstat(stream.fileno()).st_size
            except OSError:
                file_cursor.size_last_seen = stat.st_size

    for key, file_cursor in list(working.files.items()):
        if key not in present_keys:
            anomalies.append(f"vanished:{file_cursor.identity}")
            working.files.pop(key, None)

    _prune_windows(working, rules, latest_epoch)
    submits = tuple(
        sorted(
            submits_by_id.values(),
            key=lambda row: (row.wall_ts, row.seq, row.cmd_id),
        )
    )
    return HarvestResult(
        submits=submits,
        signals=tuple(signals),
        anomalies=tuple(anomalies),
        cursor=working,
    )


def load_cursor(path: Path) -> Cursor:
    """讀取增量 cursor；首次執行且檔案不存在時回傳空狀態。"""
    path = Path(path)
    if not path.exists():
        return _empty_cursor()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("cursor root must be an object")
    raw_files = value.get("files", {})
    if not isinstance(raw_files, dict):
        raise ValueError("cursor files must be an object")
    files: dict[str, FileCursor] = {}
    for key, raw in raw_files.items():
        if not isinstance(raw, dict):
            raise ValueError("cursor file entry must be an object")
        files[str(key)] = FileCursor(
            identity=str(raw["identity"]),
            head_sha256=str(raw["head_sha256"]),
            head_len=int(raw["head_len"]),
            path_last_seen=str(raw["path_last_seen"]),
            offset=int(raw["offset"]),
            size_last_seen=int(raw["size_last_seen"]),
            last_seq=int(raw["last_seq"]) if raw.get("last_seq") is not None else None,
        )

    raw_tx = value.get("tx_window", {})
    raw_human = value.get("human_window", {})
    raw_buckets = value.get("human_buckets", {})
    raw_flash = value.get("flash_window", {})
    for name, raw in (
        ("tx_window", raw_tx),
        ("human_window", raw_human),
        ("human_buckets", raw_buckets),
        ("flash_window", raw_flash),
    ):
        if not isinstance(raw, dict):
            raise ValueError(f"cursor {name} must be an object")
    return Cursor(
        files=files,
        tx_window={str(key): list(item) for key, item in raw_tx.items()},
        human_window={str(key): str(item) for key, item in raw_human.items()},
        human_buckets={str(key): int(item) for key, item in raw_buckets.items()},
        flash_window={str(key): str(item) for key, item in raw_flash.items()},
    )


def save_cursor(path: Path, cursor: Cursor) -> None:
    """以同目錄原子取代方式持久化 cursor。"""
    jsonio.atomic_write_text(Path(path), jsonio.dumps_report(asdict(cursor)))

