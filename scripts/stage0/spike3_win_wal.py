#!/usr/bin/env python3
"""Stage 0 Spike 3：Windows 原生 WAL 與端點防毒（EDR）實機驗證腳本（規格 §13；P2 case_id 指派給本計畫）。

entry: py -3.12 scripts/stage0/spike3_win_wal.py --case <case-id> --evidence-dir <dir> [--unlink-mode classic|posix]
       [--edr-product <名稱>] [--edr-status <狀態>] [--edr-interceptions <N|unknown>]
       py -3.12 scripts/stage0/spike3_win_wal.py --list
       執行前請以 win-pytest.sh 相同方式將 sw_core/ 與 scripts/、realhw/ 一併 staging 到 Windows 本機磁碟（或於 Windows 端 clone repo）。
revision: 本檔 git blob sha1（執行時由腳本計算並寫入證據 artifact.revision；等同 git rev-parse HEAD:scripts/stage0/spike3_win_wal.py）；
  被測 sw_core 以 artifact.tree_sha256（sw_core.release_manifest.tree_sha256）與 artifact.git_head（git rev-parse HEAD）記錄。
expected: win-wal-av-rotate：常駐 handle 下 rotate 成功、總筆數與 seq 連續（1..N）；win-wal-av-unlink：classic／POSIX delete 後
  WAL_RECREATED_AFTER_EXTERNAL_UNLINK META 正確、range_known 依實況（未知時 potentially_lost_seq_range 為 null）；
  win-wal-av-reader-handle：無 FILE_SHARE_DELETE 的外部 reader 期間 rotation_failed 為真、write_failures 為 0 且不遺失紀錄；
  win-wal-f12-progress：T17 oracle（wait_for_marker，常駐 handle 存活中讀取，跨 rotate）判定 PASS。
  每次執行必寫一份 spike3_evidence.schema.json 合格的證據 JSON（verdict PASS／FAIL／INSUFFICIENT_EVIDENCE）。
  EDR 產品或狀態為 unknown 時 PASS 一律降為 INSUFFICIENT_EVIDENCE（§13 結果只對指定環境有效；§11.3 證據不足不得 PASS）。
owner: 擁有者（Windows host 實機；EDR 主控台查詢權限；OD-08）。EDR 攔截查詢結果由擁有者以 --edr-* 參數帶入，查不到寫 unknown。
evidence_dir: ~/b-log/rust-refactor/p1/<case-id>/<YYMMDD-HHMMSS>/（證據 JSON 寫為 <case-id>.json）
rollback: 腳本建立的暫存目錄為 %TEMP%\\swspike3-<case-id>-*。verdict 為 PASS 時刪除本次建立的該目錄；FAIL／INSUFFICIENT_EVIDENCE 保留供檢視。
  只刪除本腳本以 mkdtemp 建立且名稱以 swspike3- 開頭的目錄，不得刪除既有 WAL 或 %USERPROFILE% 下任何檔案。
  EDR 誤攔截或 handle 卡住時：記錄 EDR 事件 ID 與 verdict，停止本 case；不得關閉防護或變更 EDR 排除清單後宣稱符合。
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import glob
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 以 `py -3.12 scripts/stage0/...` 執行時 sys.path[0] 為本檔目錄
    sys.path.insert(0, str(REPO_ROOT))
CASE_IDS: tuple[str, ...] = (
    "win-wal-av-rotate",
    "win-wal-av-unlink",
    "win-wal-av-reader-handle",
    "win-wal-f12-progress",
)
SCHEMA_PATH = Path(__file__).resolve().with_name("spike3_evidence.schema.json")
EXIT_NOT_WINDOWS = 3
RECORD_PAYLOAD = b"x" * 1024
SMALL_ROTATE_BYTES = 256 * 1024  # 測試用小門檻；正式 R=64 MiB 的實機行為由 drain／短測另行覆蓋
ROTATE_CASE_RECORDS = 400
UNLINK_META_TIMEOUT_S = 10.0  # 外部 unlink 後 WAL 身分檢查受 WAL_IDENTITY_CHECK_INTERVAL_S 節流，需有界輪詢
UNLINK_POLL_S = 0.2
WORKDIR_PREFIX = "swspike3-"


class Recorder:
    """收集 steps 與 observations；每一步附時間戳。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, str]] = []
        self.observations: dict[str, Any] = {}

    def step(self, op: str, result: str) -> None:
        self.steps.append({"op": op, "ts": _now(), "result": result})


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_blob_sha1(path: Path) -> str:
    """與 `git hash-object` 相同的 blob sha1（不依賴 git 可用性）。"""
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _git_head() -> str:
    try:
        cp = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = (cp.stdout or "").strip()
    return out if cp.returncode == 0 and out else "unknown"


def _git_clean() -> bool:
    """工作樹（含未追蹤檔、排除 target/）乾淨才為 True；無法判定一律 False（gate fail-closed）。"""
    try:
        cp = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=normal",
             "--", ".", ":(exclude)target"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0 and cp.stdout.strip() == ""


def _tree_sha256() -> str:
    """被測 sw_core 的部署樹 tree_sha256（規格 §9.5）；失敗寫 unknown。"""
    try:
        from sw_core.release_manifest import tree_sha256

        return tree_sha256(str(REPO_ROOT / "sw_core"))
    except Exception:  # noqa: BLE001 — 證據欄位查不到一律 unknown，不得中斷證據寫出
        return "unknown"


def _windows_probe(env: dict[str, Any], wal_dir: Path) -> None:
    """Windows 專屬探測：記憶體、檔案系統、電源；查不到的欄位維持 unknown（不得填 0 或空字串）。

    僅於 os.name == "nt" 呼叫。透過 ctypes 呼叫 kernel32，任何失敗皆吞掉並保留 unknown。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _PowerStatus(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    try:
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        ms = _MemoryStatusEx()
        ms.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)) and ms.ullTotalPhys > 0:
            env["memory_bytes"] = int(ms.ullTotalPhys)
    except Exception:  # noqa: BLE001
        pass

    root = os.path.splitdrive(str(wal_dir.resolve()))[0] + "\\"
    try:
        fs_name = ctypes.create_unicode_buffer(256)
        kernel32.GetVolumeInformationW.restype = wintypes.BOOL
        if kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fs_name, 256) and fs_name.value:
            env["fs_type"] = fs_name.value
    except Exception:  # noqa: BLE001
        pass
    try:
        vol = ctypes.create_unicode_buffer(260)
        kernel32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
        if kernel32.GetVolumeNameForVolumeMountPointW(root, vol, 260) and vol.value:
            env["storage_device"] = vol.value
    except Exception:  # noqa: BLE001
        pass

    try:
        sps = _PowerStatus()
        kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_PowerStatus)]
        kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
        if kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            env["power"] = {0: "battery", 1: "AC"}.get(int(sps.ACLineStatus), "unknown")
    except Exception:  # noqa: BLE001
        pass


def gather_env(wal_dir: Path, edr_product: str) -> dict[str, Any]:
    """T19 env（沿用 scripts/stage0/wal_microbench.py::collect_env）加 Windows 探測；查不到的欄位一律 unknown。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from wal_microbench import collect_env  # noqa: E402 — 同目錄 T19 工具，延遲 import 以免測試收集時載入

    env: dict[str, Any] = dict(collect_env(wal_dir))
    if os.name == "nt":
        _windows_probe(env, wal_dir)
    env["endpoint_av"] = edr_product or "unknown"
    return env


def _work_dir(case_id: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{WORKDIR_PREFIX}{case_id}-"))


def _cleanup_pass_dir(wal_dir: str, verdict: str) -> None:
    """PASS 時刪除本次 mkdtemp 建立的 swspike3-* 目錄；其他 verdict 保留供檢視。"""
    if verdict != "PASS":
        return
    path = Path(wal_dir)
    if not path.name.startswith(WORKDIR_PREFIX):
        return
    shutil.rmtree(path, ignore_errors=True)


def _read_records(wal_dir: Path) -> list[dict[str, Any]]:
    """讀取 active WAL 與所有歸檔（僅 NDJSON 行），並依 seq 排序。

    檔名字典序不可信：active 檔 ``raw.wal.ndjson`` 排在歸檔 ``…ndjson.<ts>`` 之前，
    但 active 內含最新 seq；故讀完後一律以 seq 排序再判斷連續性。
    """
    out: list[dict[str, Any]] = []
    for path in glob.glob(str(wal_dir / "raw.wal.ndjson*")):
        with open(path, "rb") as fp:
            for line in fp:
                line = line.strip()
                if line.startswith(b"{"):
                    out.append(json.loads(line))
    out.sort(key=lambda r: int(r.get("seq", 0)) if isinstance(r.get("seq"), int) else -1)
    return out


def _seqs(records: list[dict[str, Any]]) -> list[int]:
    return sorted(int(r["seq"]) for r in records if isinstance(r.get("seq"), int))


def _seq_continuous(records: list[dict[str, Any]]) -> bool:
    """seq 必須非空且逐一連續（無缺號、無重複）。空集合不得判為連續。"""
    seqs = _seqs(records)
    return bool(seqs) and seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def _safe_read_records(wal_dir: Path, rec: Recorder) -> list[dict[str, Any]]:
    """外部 unlink 後 classic delete 可能暫時無法開檔（DeletePending）；讀失敗記錄後回空集合，不得拋出。"""
    try:
        return _read_records(wal_dir)
    except OSError as exc:
        rec.observations.setdefault("read_errors", []).append(f"{type(exc).__name__}: {exc}")
        return []


def case_rotate(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    from sw_core.wal import WalWriter

    wal_dir = _work_dir("win-wal-av-rotate")
    rec.observations["wal_dir"] = str(wal_dir)
    writer = WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES)
    try:
        for _ in range(ROTATE_CASE_RECORDS):
            writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
        rec.step("append_until_rotate", "ok")
    finally:
        writer.close()
    archives = [p for p in glob.glob(str(wal_dir / "raw.wal.ndjson*")) if not p.endswith("raw.wal.ndjson")]
    records = _read_records(wal_dir)
    seqs = _seqs(records)
    continuous = _seq_continuous(records)
    complete = len(records) == ROTATE_CASE_RECORDS and seqs[:1] == [1] and seqs[-1:] == [ROTATE_CASE_RECORDS]
    rec.observations.update({
        "archives": len(archives), "records": len(records), "expected_records": ROTATE_CASE_RECORDS,
        "first_seq": seqs[0] if seqs else None, "last_seq": seqs[-1] if seqs else None,
        "seq_continuous": continuous,
    })
    rec.step("verify_seq_continuity", "ok" if continuous and complete else "gap_or_count_mismatch")
    if archives and continuous and complete:
        return "PASS", "none"
    return "FAIL", "改以 --unlink-mode 或縮小 rotate 門檻重跑；仍失敗則記錄 EDR 事件後停止"


def _find_recreate_meta(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("dir") == "META"
            and (r.get("meta") or {}).get("event") == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"]


def case_unlink(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    from sw_core import win32_files
    from sw_core.wal import WalWriter

    wal_dir = _work_dir("win-wal-av-unlink")
    rec.observations["wal_dir"] = str(wal_dir)
    writer = WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES)
    metas: list[dict[str, Any]] = []
    try:
        for _ in range(10):
            writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
        rec.step("prefill", "ok")
        wal_path = str(wal_dir / "raw.wal.ndjson")
        if args.unlink_mode == "posix":
            win32_files.delete_posix_semantics(wal_path)
        else:
            win32_files.delete_classic(wal_path)
        rec.step(f"external_unlink:{args.unlink_mode}", "ok")
        # 身分檢查受 WAL_IDENTITY_CHECK_INTERVAL_S 節流（且 classic 刪除為 DeletePending），
        # 故 unlink 後不能只 append 一次就斷言；改為有界輪詢直到 META 出現或逾時。
        time.sleep(UNLINK_POLL_S)
        attempts = 0
        deadline = time.monotonic() + UNLINK_META_TIMEOUT_S
        while True:
            writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
            attempts += 1
            metas = _find_recreate_meta(_safe_read_records(wal_dir, rec))
            if metas or time.monotonic() >= deadline:
                break
            time.sleep(UNLINK_POLL_S)
        rec.observations["append_attempts_until_meta"] = attempts
        rec.step("poll_recreate_meta", "found" if metas else "timeout")
    finally:
        writer.close()
    rec.observations["meta_count"] = len(metas)
    if not metas:
        rec.step("verify_meta", "missing")
        return "FAIL", "未見 WAL_RECREATED_AFTER_EXTERNAL_UNLINK META；記錄 EDR 攔截與 unlink 錯誤後停止"
    meta = metas[0]
    loss = bool(meta.get("loss_flag"))
    rng = (meta.get("meta") or {}).get("potentially_lost_seq_range")
    range_known = bool((meta.get("meta") or {}).get("range_known"))
    rec.observations.update({"loss_flag": loss, "potentially_lost_seq_range": rng, "range_known": range_known})
    rec.step("verify_meta", "ok" if loss else "loss_flag_false")
    consistent = loss and ((range_known and rng is not None) or (not range_known and rng is None))
    return ("PASS", "none") if consistent else ("FAIL", "range_known 與 potentially_lost_seq_range 不一致，停止自動 TX 並保留證據")


def case_reader_handle(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    import ctypes
    from ctypes import wintypes

    from sw_core.wal import WalWriter

    if os.name != "nt":
        raise RuntimeError("reader-handle 需 Windows CreateFileW")
    wal_dir = _work_dir("win-wal-av-reader-handle")
    rec.observations["wal_dir"] = str(wal_dir)
    wal_path = str(wal_dir / "raw.wal.ndjson")
    writer = WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002  # 與常駐 writer 共存；刻意不給 FILE_SHARE_DELETE
    OPEN_EXISTING = 3
    invalid_handle = ctypes.c_void_p(-1).value
    reader: Any = None
    appended = 0
    seen_rotation_failed = False
    write_failures = -1
    try:
        # WalWriter.__init__ 不開檔，第一次 append 才建立 raw.wal.ndjson；必須先寫一筆，reader 才有檔可開。
        writer.append(com="COM0", direction="TX", source="spike3", payload=b"prefill", cmd_id="spike3-prefill")
        appended += 1
        rec.step("prefill_create_resident_wal", "ok")
        reader = kernel32.CreateFileW(wal_path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                      None, OPEN_EXISTING, 0, None)
        if reader in (None, invalid_handle):
            raise RuntimeError(f"外部 reader 開檔失敗（WinError {ctypes.get_last_error()}）")
        rec.step("open_external_reader_no_share_delete", "ok")
        for _ in range(ROTATE_CASE_RECORDS):
            out = writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
            appended += 1
            if out.get("rotation_failed"):
                seen_rotation_failed = True
        rec.step("append_during_reader_hold", "ok")
    finally:
        try:
            if reader not in (None, invalid_handle):
                kernel32.CloseHandle(reader)
            write_failures = int(writer.health().get("write_failures", -1))
        finally:
            writer.close()
    records = _read_records(wal_dir)
    seqs = _seqs(records)
    continuous = _seq_continuous(records)
    complete = len(records) == appended
    rec.observations.update({
        "rotation_failed_seen": seen_rotation_failed, "appended": appended, "records": len(records),
        "seq_continuous": continuous, "write_failures": write_failures,
    })
    rec.step("verify_no_loss", "ok" if continuous and complete else "gap_or_count_mismatch")
    if seen_rotation_failed and continuous and complete and write_failures == 0:
        return "PASS", "none"
    return "FAIL", "rotation_failed 未出現、紀錄不連續、筆數不符或 write_failures 非 0；保留 WAL 目錄並記錄 EDR 攔截後停止"


def case_f12_progress(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    from realhw import wal_oracle
    from realhw.wal_oracle import verify_record
    from sw_core.wal import WalWriter

    wal_dir = _work_dir("win-wal-f12-progress")
    rec.observations["wal_dir"] = str(wal_dir)
    marker_pre = b"SPIKE3-F12-MARKER-PRE"
    marker_post = b"SPIKE3-F12-MARKER-POST"
    wal_path = str(wal_dir / "raw.wal.ndjson")
    writer = WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES)
    res_pre = res_post = None
    try:
        # 常駐 writer 不關閉：先寫一筆使檔案與 handle 存在，再以外部游標讀取（與 F12 實機路徑相同）。
        writer.append(com="COM0", direction="TX", source="spike3", payload=b"prefill", cmd_id="spike3-f12-prefill")
        cursor = wal_oracle.open_cursor(wal_path)
        rec.step("open_cursor_while_writer_resident", "ok")
        writer.append(com="COM0", direction="TX", source="spike3", payload=marker_pre, cmd_id="spike3-f12")
        res_pre = wal_oracle.wait_for_marker(cursor, marker=marker_pre, cmd_id="spike3-f12", timeout_s=10.0)
        rec.step("wait_marker_pre_rotate", "ok" if res_pre.ok else res_pre.reason_code)
        for _ in range(ROTATE_CASE_RECORDS):
            writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3-f12-bulk")
        writer.append(com="COM0", direction="TX", source="spike3", payload=marker_post, cmd_id="spike3-f12-post")
        res_post = wal_oracle.wait_for_marker(cursor, marker=marker_post, cmd_id="spike3-f12-post", timeout_s=10.0)
        rec.step("wait_marker_post_rotate", "ok" if res_post.ok else res_post.reason_code)
    finally:
        writer.close()
    records = _read_records(wal_dir)
    crc_errors = [e for e in (verify_record(r) for r in records) if e is not None]
    rec.observations.update({
        "pre_rotate": {"ok": res_pre.ok if res_pre else None,
                       "reason_code": res_pre.reason_code if res_pre else None,
                       "diagnostics": res_pre.diagnostics if res_pre else None},
        "post_rotate": {"ok": res_post.ok if res_post else None,
                        "reason_code": res_post.reason_code if res_post else None,
                        "diagnostics": res_post.diagnostics if res_post else None},
        "crc_errors": crc_errors,
    })
    if res_pre is not None and res_post is not None and res_pre.ok and res_post.ok and not crc_errors:
        return "PASS", "none"
    return "FAIL", "T17 oracle（常駐 handle 存活中讀取、跨 rotate）未通過；保留 WAL 目錄，回報 observations 與 EDR 紀錄"


CASE_FUNCS: dict[str, Callable[[Recorder, argparse.Namespace], tuple[str, str]]] = {
    "win-wal-av-rotate": case_rotate,
    "win-wal-av-unlink": case_unlink,
    "win-wal-av-reader-handle": case_reader_handle,
    "win-wal-f12-progress": case_f12_progress,
}


def _validate(evidence: dict[str, Any]) -> None:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=evidence, schema=schema)


def run_case(args: argparse.Namespace) -> int:
    case_id = args.case
    rec = Recorder()
    run_at = _now()
    edr_product = args.edr_product or "unknown"
    edr_status = args.edr_status or "unknown"
    edr = {
        "product": edr_product,
        "status": edr_status,
        "interceptions": (int(args.edr_interceptions) if str(args.edr_interceptions).isdigit()
                          else "unknown"),
    }
    artifact = {
        "binary": "sw_core (Python 基線)",
        "revision": _git_blob_sha1(Path(__file__).resolve()),
        "tree_sha256": _tree_sha256(),
        "git_head": _git_head(),
        "git_clean": _git_clean(),
    }
    fallback = "none"
    if os.name != "nt":
        verdict, fallback, code = "INSUFFICIENT_EVIDENCE", "在 Windows host 以 py -3.12 重跑", EXIT_NOT_WINDOWS
        rec.observations["reason"] = f"host os={os.name}，非 Windows，不得作為 Spike 3 證據"
        env_dir = Path(tempfile.gettempdir())
    else:
        env_dir = Path(tempfile.gettempdir())
        try:
            verdict, fallback = CASE_FUNCS[case_id](rec, args)
            code = 0 if verdict == "PASS" else 1
        except Exception as exc:  # noqa: BLE001 — 實機腳本：任何異常都必須落證據，不得靜默
            verdict, fallback, code = "INSUFFICIENT_EVIDENCE", "檢視 observations.exception 後重跑", 1
            rec.observations["exception"] = f"{type(exc).__name__}: {exc}"
    if verdict == "PASS" and (edr_product == "unknown" or edr_status == "unknown"):
        # 未記錄 EDR 產品或狀態時，結果不足以代表端點防護環境（§13、§11.3）。
        verdict, fallback, code = (
            "INSUFFICIENT_EVIDENCE",
            "EDR 產品或狀態為 unknown：於擁有者 EDR 主控台查得後以 --edr-product／--edr-status 重跑",
            1,
        )
        rec.observations["edr_downgrade"] = True
    wal_dir_str = str(rec.observations.get("wal_dir", ""))
    env = gather_env(Path(wal_dir_str) if wal_dir_str else env_dir, edr_product)
    evidence = {
        "case_id": case_id,
        "run_at": run_at,
        "env": env,
        "artifact": artifact,
        "steps": rec.steps or [{"op": "host_check", "ts": run_at, "result": "no_case_steps_executed"}],
        "observations": rec.observations,
        "edr": edr,
        "verdict": verdict,
        "fallback": fallback,
    }
    _validate(evidence)
    out_dir = Path(args.evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{case_id}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"case_id": case_id, "verdict": verdict, "evidence": str(out_dir / f"{case_id}.json")},
                     ensure_ascii=False))
    _cleanup_pass_dir(wal_dir_str, verdict)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage 0 Spike 3：Windows WAL／EDR 實機證據腳本")
    p.add_argument("--list", action="store_true", help="列出 case_id（JSON 陣列）後結束")
    p.add_argument("--case", choices=CASE_IDS, help="要執行的 case_id")
    p.add_argument("--evidence-dir", help="證據輸出目錄")
    p.add_argument("--unlink-mode", choices=("classic", "posix"), default="classic")
    p.add_argument("--edr-product", default="", help="EDR 產品名（主控台查得；查不到留空即 unknown）")
    p.add_argument("--edr-status", default="", help="EDR 防護狀態（查不到留空即 unknown，PASS 會降級）")
    p.add_argument("--edr-interceptions", default="unknown", help="攔截次數（整數）或 unknown")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        print(json.dumps(list(CASE_IDS), ensure_ascii=False))
        return 0
    if not args.case or not args.evidence_dir:
        build_parser().error("--case 與 --evidence-dir 必填（或使用 --list）")
    return run_case(args)


if __name__ == "__main__":
    sys.exit(main())
