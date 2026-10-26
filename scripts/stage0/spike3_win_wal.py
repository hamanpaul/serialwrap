#!/usr/bin/env python3
"""Stage 0 Spike 3：Windows 原生 WAL 與端點防護（OD-19，與防毒產品無關）實機驗證腳本（規格 §13；P2 case_id 指派給本計畫）。

entry: py -3.12 scripts/stage0/spike3_win_wal.py --case <case-id> --evidence-dir <dir> [--unlink-mode classic|posix]
       py -3.12 scripts/stage0/spike3_win_wal.py --list
       執行前請以 win-pytest.sh 相同方式將 sw_core/ 與 scripts/、realhw/ 一併 staging 到 Windows 本機磁碟（或於 Windows 端 clone repo）。
revision: 本檔 git blob sha1（執行時由腳本計算並寫入證據 artifact.revision；等同 git rev-parse HEAD:scripts/stage0/spike3_win_wal.py）；
  被測 sw_core 以 artifact.tree_sha256（sw_core.release_manifest.tree_sha256）與 artifact.git_head（git rev-parse HEAD）記錄。
expected: 各案主測皆由 scripts/stage0/av_sim.py helper 以不含 FILE_SHARE_DELETE 的共用模式
  （seeded 持有 10–500 ms）注入；另在相同 WAL 目錄執行獨立的 FILE_SHARE_DELETE + RH oplock 變體：
  每次 WalWriter.append 實際耗時（毫秒）記入 observations.append_latency_ms；gate 以最大值比對 owner 核准上限，上限未裁決前維持 BLOCKED-OWNER；
  win-wal-av-rotate：輪替最終完成、總筆數與 seq 連續（1..N）、干擾實際觀察到（grabs≥1、rotate_retry≥1）、延後佇列清空；
  win-wal-av-unlink：外部 unlink 有界重試至成功，WAL_RECREATED_AFTER_EXTERNAL_UNLINK META 正確、range_known 依實況；
  win-wal-av-reader-handle：無 FILE_SHARE_DELETE 的外部 reader 期間 rotation_failed 為真、紀錄不遺失、write_failures 為 0、延後佇列清空；
  win-wal-f12-progress：T17 oracle（wait_for_marker，常駐 handle 存活中讀取，跨 rotate）於 av_sim 持有中判定 PASS。
  每次執行必寫一份 spike3_evidence.schema.json 合格的證據 JSON（verdict PASS／FAIL／INSUFFICIENT_EVIDENCE）。
  PASS 另須 binary_integrity 成立：測試前後直譯器、av_sim、本工具的存在與 sha256 不變，且 helper 啟動未被拒；否則降為 FAIL。
edr: 防毒產品名與狀態由工具以 PowerShell Get-CimInstance -Namespace root/SecurityCenter2 AntiVirusProduct 唯讀查詢，
  只寫入 edr 資訊欄位；查不到寫 unknown，**不影響 verdict**（OD-19 (3)：驗收與防毒產品無關，不需要攔截次數或主控台資料）。
owner: 擁有者（Windows host 實機；OD-08）。不再接受 --edr-* 自填參數。
evidence_dir: ~/b-log/rust-refactor/p1/<case-id>/<YYMMDD-HHMMSS>/（證據 JSON 寫為 <case-id>.json）
rollback: 腳本建立的暫存目錄為 %TEMP%\\swspike3-<case-id>-*。verdict 為 PASS 時刪除本次建立的該目錄；FAIL／INSUFFICIENT_EVIDENCE 保留供檢視。
  只刪除本腳本以 mkdtemp 建立且名稱以 swspike3- 開頭的目錄，不得刪除既有 WAL 或 %USERPROFILE% 下任何檔案。
  干擾導致 handle 卡住時：保留 av_sim 日誌與 verdict 後停止本 case；不得關閉防護或變更排除清單後宣稱符合。
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
SPIKE3_SEED = 20261023  # av_sim 持有時長序列的種子（同 seed 可重現）
AV_SIM_PATH = Path(__file__).resolve().with_name("av_sim.py")
AV_HELPER_DURATION_S = 900.0
AV_HELPER_READY_TIMEOUT_S = 15.0
AV_HELPER_STOP_TIMEOUT_S = 60.0
DRAIN_TIMEOUT_S = 20.0
DRAIN_SETTLE_S = 0.5
CONTINUITY_META_EVENT = "WAL_ROTATED_AFTER_DEFERRAL"
# 與 daemon 的 DeviceWatcher 預設 poll_interval_s（sw_core/device_watcher.py，1.0 s）相同節奏；
# 不得以更密的 tick 替代 daemon 的服務頻率，否則 PASS 會高估延後輪替的實際完成速度。
DAEMON_TICK_INTERVAL_S = 1.0
UNLINK_RETRY_TIMEOUT_S = 20.0
EDR_QUERY_PS = (
    "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct "
    "| Select-Object displayName,productState | ConvertTo-Json -Compress"
)


class Recorder:
    """收集 steps 與 observations；每一步附時間戳。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, str]] = []
        self.observations: dict[str, Any] = {}

    def step(self, op: str, result: str) -> None:
        self.steps.append({"op": op, "ts": _now(), "result": result})


class _MeasuredWalWriter:
    """轉送 WAL writer 呼叫，並記錄每次 append 的實際耗時（毫秒）。"""

    def __init__(self, writer: Any, rec: Recorder) -> None:
        self._writer = writer
        self._rec = rec
        self._rec.observations["append_latency_ms"] = []
        self._rec.observations["append_count"] = 0

    def append(self, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter_ns()
        try:
            return self._writer.append(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            self._rec.observations["append_latency_ms"].append(elapsed_ms)
            self._rec.observations["append_count"] += 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


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


def _continuity_meta_count(records: list[dict[str, Any]]) -> int:
    """計算 tick 完成延後輪替時補入的連續性 META；此列不是資料 append。"""
    return sum(
        1 for row in records
        if row.get("dir") == "META" and (row.get("meta") or {}).get("event") == CONTINUITY_META_EVENT
    )


def _safe_read_records(wal_dir: Path, rec: Recorder) -> list[dict[str, Any]]:
    """外部 unlink 後 classic delete 可能暫時無法開檔（DeletePending）；讀失敗記錄後回空集合，不得拋出。"""
    try:
        return _read_records(wal_dir)
    except OSError as exc:
        rec.observations.setdefault("read_errors", []).append(f"{type(exc).__name__}: {exc}")
        return []


def _av_sim_module() -> Any:
    """延遲匯入 scripts/stage0/av_sim.py（同目錄，與 gather_env 匯入 wal_microbench 的方式相同）。"""
    sys.path.insert(0, str(AV_SIM_PATH.parent))
    try:
        import av_sim  # noqa: PLC0415 — 同目錄工具，延遲匯入
    finally:
        sys.path.pop(0)
    return av_sim


def decode_product_state(value: Any) -> str:
    """SecurityCenter2 productState 常見十六進位版面：第二個位元組 0x10／0x11 為啟用、0x00 為停用，其餘 unknown。

    僅供資訊欄位使用，不參與 verdict（OD-19 (3)）。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "unknown"
    middle = (number >> 8) & 0xFF
    if middle in (0x10, 0x11):
        return "enabled"
    if middle == 0x00:
        return "disabled"
    return "unknown"


def query_edr(runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """唯讀查詢 SecurityCenter2 的防毒產品名與狀態，只寫入 info 欄位；查不到一律 unknown，不影響 verdict。

    工具自動查詢，不接受呼叫者自填（OD-19 (3)）；擁有者主控台查詢不再是驗收前提。
    """
    result: dict[str, Any] = {
        "product": "unknown",
        "status": "unknown",
        "interceptions": "unknown",
        "source": "SecurityCenter2/AntiVirusProduct (read-only)",
        "product_state_raw": "unknown",
    }
    try:
        cp = runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", EDR_QUERY_PS],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return result
    if cp.returncode != 0 or not (cp.stdout or "").strip():
        return result
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        return result
    rows = data if isinstance(data, list) else [data]
    rows = [r for r in rows if isinstance(r, dict) and str(r.get("displayName") or "").strip()]
    if not rows:
        return result
    names = sorted({str(r["displayName"]).strip() for r in rows})
    first = rows[0]
    result.update(
        product="；".join(names),
        status=decode_product_state(first.get("productState")),
        product_state_raw=str(first.get("productState", "unknown")),
    )
    return result


def _file_sha256(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def binary_subject_paths() -> list[str]:
    """binary_integrity 檢查的執行檔：本次直譯器、av_sim helper 與本工具自身。"""
    return [sys.executable, str(AV_SIM_PATH), str(Path(__file__).resolve())]


def snapshot_binaries(paths: list[str]) -> dict[str, dict[str, Any]]:
    return {p: {"exists": os.path.isfile(p), "sha256": _file_sha256(p)} for p in paths}


def binary_integrity_report(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    launch_error: str | None,
) -> dict[str, Any]:
    """測試前後執行檔存在、sha256 不變、啟動未被拒（OD-19 (3)「執行檔被誤判」的可觀察事實判定）。"""
    subjects: list[dict[str, Any]] = []
    unchanged = True
    for path, snap in before.items():
        post = after.get(path, {"exists": False, "sha256": None})
        same = bool(snap["exists"]) and bool(post["exists"]) and snap["sha256"] is not None and snap["sha256"] == post["sha256"]
        unchanged = unchanged and same
        subjects.append({
            "path": path,
            "exists_before": bool(snap["exists"]),
            "exists_after": bool(post["exists"]),
            "sha256_before": snap["sha256"] or "unknown",
            "sha256_after": post["sha256"] or "unknown",
            "unchanged": same,
        })
    return {
        "subjects": subjects,
        "unchanged": unchanged,
        "launch_refused": launch_error != "none",
        "launch_error": launch_error or "none",
    }


class AvHelper:
    """以子行程啟動 scripts/stage0/av_sim.py（Windows helper）監看 WAL 目錄，對新檔套用持有模型（OD-19 注入）。

    旗標與日誌置於 WAL 目錄內，檔名不符 raw.wal.ndjson／raw.mirror.log 前綴，不會被 helper 自己持有；
    PASS 時隨 swspike3-* 目錄一併清除。
    """

    def __init__(self, wal_dir: Path, seed: int, rec: Recorder, *,
                 oplock: bool = False, variant: str | None = None) -> None:
        self._wal_dir = wal_dir
        self._seed = seed
        self._rec = rec
        self._oplock = oplock
        self._variant = variant
        suffix = f"-{variant}" if variant else ""
        self._ready = wal_dir / f"av-helper{suffix}.ready"
        self._stop = wal_dir / f"av-helper{suffix}.stop"
        self._go = wal_dir / f"av-helper{suffix}.go"
        self._ack = wal_dir / f"av-helper{suffix}.ack"
        self._log = wal_dir / f"av-helper{suffix}.jsonl"
        self._proc: subprocess.Popen[bytes] | None = None
        self._arm_serial = 0
        self.launch_error: str | None = None
        self._ready_files: frozenset[str] = frozenset()
        self._evidence_key = variant
        evidence = self._evidence()
        if variant is None:
            self._rec.observations["av_sim"] = evidence
        else:
            self._rec.observations.setdefault("av_sim", {}).setdefault("variants", {})[variant] = evidence

    def _evidence(self) -> dict[str, Any]:
        return {
            **_av_sim_module().describe(self._seed, oplock=self._oplock),
            "launch_error": "not_started",
            "grabs": None,
            "open_errors": None,
            "helper_exit": None,
        }

    def _payload(self) -> dict[str, Any]:
        root = self._rec.observations["av_sim"]
        return root if self._evidence_key is None else root["variants"][self._evidence_key]

    def _argv(self) -> list[str]:
        argv = [
            sys.executable, str(AV_SIM_PATH),
            "--watch-dir", str(self._wal_dir),
            "--seed", str(self._seed),
            "--duration-s", str(AV_HELPER_DURATION_S),
            "--ready-file", str(self._ready),
            "--stop-file", str(self._stop),
            "--log-file", str(self._log),
            "--go-file", str(self._go),
            "--ack-file", str(self._ack),
        ]
        if self._oplock:
            argv.append("--oplock")
        return argv

    def start(self) -> bool:
        try:
            self._proc = subprocess.Popen(self._argv())
        except OSError as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")
        deadline = time.monotonic() + AV_HELPER_READY_TIMEOUT_S
        ready_error: str | None = None
        while time.monotonic() < deadline:
            if self._ready.exists():
                try:
                    ready = json.loads(self._ready.read_text(encoding="utf-8"))
                    held_files = ready.get("held_files") if isinstance(ready, dict) else None
                    if (not isinstance(ready, dict) or ready.get("ready") is not True
                            or not isinstance(held_files, list)
                            or any(not isinstance(name, str) for name in held_files)):
                        ready_error = "helper ready 記錄格式錯誤"
                    else:
                        self._ready_files = frozenset(held_files)
                        self._payload()["launch_error"] = "none"
                        self._rec.step(f"av_sim_helper_ready{':' + self._variant if self._variant else ''}", "ok")
                        return True
                except (OSError, ValueError, TypeError) as exc:
                    ready_error = f"helper ready 記錄無法解析：{type(exc).__name__}"
            if self._proc.poll() is not None:
                if ready_error is not None:
                    return self._fail(ready_error)
                return self._fail(f"helper 提前結束（exit={self._proc.returncode}）")
            time.sleep(0.05)
        return self._fail(ready_error or "helper 就緒逾時")

    def ready_holds(self, name: str) -> bool:
        """首次目錄掃描完成時，確認指定 WAL 檔已取得持有 handle。"""
        return name in self._ready_files

    def arm_hold(self, name: str) -> bool:
        """以 go/ack 握手重新取得 seeded lease，避免首次 ready 到刺激間持有已到期。"""
        self._arm_serial += 1
        request_id = f"{os.getpid()}-{time.monotonic_ns()}-{self._arm_serial}"
        try:
            self._ack.unlink()
        except FileNotFoundError:
            pass
        try:
            self._go.write_text(json.dumps({"request_id": request_id, "name": name}, sort_keys=True) + "\n",
                                encoding="utf-8")
        except OSError as exc:
            return self._fail(f"helper rearm request 無法寫入：{type(exc).__name__}: {exc}")
        deadline = time.monotonic() + AV_HELPER_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._ack.exists():
                try:
                    response = json.loads(self._ack.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    response = None
                if isinstance(response, dict) and response.get("request_id") == request_id:
                    hold_ms = response.get("hold_ms")
                    if (response.get("held_file") == name and isinstance(hold_ms, int)
                            and not isinstance(hold_ms, bool) and 10 <= hold_ms <= 500):
                        self._rec.step(
                            f"av_sim_helper_rearmed{':' + self._variant if self._variant else ''}", "ok",
                        )
                        return True
                    return self._fail(f"helper rearm 未取得 {name} 的有效 seeded lease：{response!r}")
            if self._proc is not None and self._proc.poll() is not None:
                return self._fail(f"helper rearm 前提前結束（exit={self._proc.returncode}）")
            time.sleep(0.01)
        return self._fail("helper rearm 握手逾時")

    def _fail(self, message: str) -> bool:
        self.launch_error = message
        self._payload()["launch_error"] = message
        self._rec.step(f"av_sim_helper_ready{':' + self._variant if self._variant else ''}", "refused")
        return False

    def stop(self) -> None:
        try:
            self._stop.write_text("stop\n", encoding="utf-8")
        except OSError:
            pass
        exit_code: int | None = None
        if self._proc is not None:
            try:
                exit_code = self._proc.wait(timeout=AV_HELPER_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                exit_code = self._proc.wait()
        summary: dict[str, Any] = {}
        try:
            for line in self._log.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row.get("event") == "summary":
                        summary = row
        except (OSError, ValueError):
            summary = {}
        self._payload().update({
            "grabs": summary.get("grabs"),
            "open_errors": summary.get("open_errors"),
            "helper_exit": exit_code,
            "oplock": summary.get("oplock", "unavailable" if self._oplock else "not_requested"),
        })


def _start_av_helper_with_wal(
    wal_dir: Path, rec: Recorder, *, oplock: bool = False, variant: str | None = None,
) -> AvHelper:
    """只在現行 WAL 已存在、helper 首次掃描確認持有後回傳。"""
    if not (wal_dir / "raw.wal.ndjson").is_file():
        raise RuntimeError("Spike 3 helper 啟動前 WAL 必須先存在")
    helper = AvHelper(wal_dir, SPIKE3_SEED, rec, oplock=oplock, variant=variant)
    if not helper.start():
        helper.stop()
        raise RuntimeError(f"av_sim helper 無法啟動：{helper.launch_error}")
    if not helper.ready_holds("raw.wal.ndjson"):
        helper.stop()
        raise RuntimeError("av_sim helper 首次掃描未持有 raw.wal.ndjson；不得開始 Spike 3 操作")
    if not helper.arm_hold("raw.wal.ndjson"):
        helper.stop()
        raise RuntimeError(f"av_sim helper 未能在刺激前重新持有 raw.wal.ndjson：{helper.launch_error}")
    return helper


def _collect_oplock_variant(wal_dir: Path, rec: Recorder) -> bool:
    """在隔離 WAL 目錄執行獨立 append／rotate／F12 workload，結果與主案例分開記錄。"""
    from realhw import wal_oracle
    from realhw.wal_oracle import verify_record
    from sw_core.wal import WalWriter

    oplock_dir = wal_dir / "oplock-variant"
    oplock_dir.mkdir(parents=True, exist_ok=True)
    variant_rec = Recorder()
    writer: _MeasuredWalWriter | None = None
    helper: AvHelper | None = None
    res_pre = res_post = None
    try:
        writer = _MeasuredWalWriter(
            WalWriter(wal_dir=str(oplock_dir), rotate_bytes=SMALL_ROTATE_BYTES), variant_rec,
        )
        wal_path = str(oplock_dir / "raw.wal.ndjson")
        writer.append(com="COM0", direction="TX", source="spike3-oplock", payload=b"prefill",
                      cmd_id="spike3-oplock-prefill")
        cursor = wal_oracle.open_cursor(wal_path)
        helper = _start_av_helper_with_wal(oplock_dir, rec, oplock=True, variant="oplock")
        marker_pre = b"SPIKE3-OPLOCK-MARKER-PRE"
        marker_post = b"SPIKE3-OPLOCK-MARKER-POST"
        writer.append(com="COM0", direction="TX", source="spike3-oplock", payload=marker_pre,
                      cmd_id="spike3-oplock-f12")
        res_pre = wal_oracle.wait_for_marker(
            cursor, marker=marker_pre, cmd_id="spike3-oplock-f12", timeout_s=10.0,
        )
        for _ in range(ROTATE_CASE_RECORDS):
            writer.append(com="COM0", direction="TX", source="spike3-oplock", payload=RECORD_PAYLOAD,
                          cmd_id="spike3-oplock-bulk")
        writer.append(com="COM0", direction="TX", source="spike3-oplock", payload=marker_post,
                      cmd_id="spike3-oplock-f12-post")
        res_post = wal_oracle.wait_for_marker(
            cursor, marker=marker_post, cmd_id="spike3-oplock-f12-post", timeout_s=10.0,
        )
        _drain_writer(writer, variant_rec)
    except Exception as exc:  # noqa: BLE001 — 任一子變體失敗均保留主案例與不足診斷
        rec.observations["oplock_variant_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001 — 收尾錯誤同樣不能放行
                rec.observations["oplock_variant_close_error"] = f"{type(exc).__name__}: {exc}"
        if helper is not None:
            helper.stop()
    if writer is None:
        return False
    records = _read_records(oplock_dir)
    seqs = _seqs(records)
    continuity_meta_count = _continuity_meta_count(records)
    health = writer.health()
    archives = [p for p in glob.glob(str(oplock_dir / "raw.wal.ndjson*")) if not p.endswith("raw.wal.ndjson")]
    append_count = variant_rec.observations["append_count"]
    continuous = _seq_continuous(records)
    complete = (
        len(records) - continuity_meta_count == append_count
        and seqs[:1] == [1]
        and seqs[-1:] == [len(records)]
        and continuous
    )
    crc_errors = [error for error in (verify_record(record) for record in records) if error is not None]
    evidence = rec.observations["av_sim"]["variants"]["oplock"]
    workload = {
        "append_count": append_count,
        "append_latency_ms": variant_rec.observations["append_latency_ms"],
        "archives": len(archives), "records": len(records),
        "continuity_meta_count": continuity_meta_count,
        "first_seq": seqs[0] if seqs else None, "last_seq": seqs[-1] if seqs else None,
        "seq_continuous": continuous,
        "rotate_retry": health.get("rotate_retry"),
        "deferred_delete_pending": health.get("deferred_delete_pending"),
        "write_failures": health.get("write_failures"),
        "pre_rotate": {"ok": res_pre.ok if res_pre else None,
                       "reason_code": res_pre.reason_code if res_pre else None},
        "post_rotate": {"ok": res_post.ok if res_post else None,
                        "reason_code": res_post.reason_code if res_post else None},
        "crc_errors": crc_errors,
        "interference_observed": isinstance(evidence.get("grabs"), int)
        and not isinstance(evidence.get("grabs"), bool) and evidence["grabs"] >= 1,
    }
    rec.observations["oplock_variant"] = workload
    return (
        evidence.get("helper_exit") == 0
        and isinstance(evidence.get("grabs"), int) and not isinstance(evidence.get("grabs"), bool)
        and evidence["grabs"] >= 1 and evidence.get("oplock") in {"granted", "denied", "unavailable"}
        and append_count == ROTATE_CASE_RECORDS + 3 and len(archives) >= 1 and complete
        and res_pre is not None and res_pre.ok and res_post is not None and res_post.ok
        and health.get("deferred_delete_pending") == 0 and health.get("write_failures") == 0
        and not crc_errors and workload["interference_observed"]
    )


def _grabs(rec: Recorder) -> int:
    value = (rec.observations.get("av_sim") or {}).get("grabs")
    return value if isinstance(value, int) else 0


def _drain_writer(writer: Any, rec: Recorder | None = None) -> None:
    """干擾期間延後的輪替須於有界時間內完成：以 daemon 同節奏（DAEMON_TICK_INTERVAL_S）呼叫 tick，直到清空或逾時。

    驅動方式（tick 間隔、次數、是否清空）記入 observations["drain"]，供 gate 與人工核對。
    """
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    settle_until = time.monotonic() + DRAIN_SETTLE_S
    ticks = 0
    drained = False
    while time.monotonic() < deadline:
        writer.tick()
        ticks += 1
        if time.monotonic() >= settle_until and writer.health()["deferred_delete_pending"] == 0:
            drained = True
            break
        time.sleep(DAEMON_TICK_INTERVAL_S)
    if rec is not None:
        rec.observations["drain"] = {
            "tick_interval_s": DAEMON_TICK_INTERVAL_S,
            "ticks": ticks,
            "drained": drained,
        }


def case_rotate(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    from sw_core.wal import WalWriter

    wal_dir = _work_dir("win-wal-av-rotate")
    rec.observations["wal_dir"] = str(wal_dir)
    writer = _MeasuredWalWriter(WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES), rec)
    helper: AvHelper | None = None
    try:
        # WalWriter 延遲開檔；先建立現行 WAL，再確認 helper 已持有後才開始輪替壓力。
        writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
        rec.step("prefill_create_resident_wal", "ok")
        helper = _start_av_helper_with_wal(wal_dir, rec)
        for _ in range(ROTATE_CASE_RECORDS - 1):
            writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
        _drain_writer(writer, rec)
        rec.step("append_until_rotate_under_av_hold", "ok")
    finally:
        writer.close()
        if helper is not None:
            helper.stop()
    health = writer.health()
    archives = [p for p in glob.glob(str(wal_dir / "raw.wal.ndjson*")) if not p.endswith("raw.wal.ndjson")]
    records = _read_records(wal_dir)
    seqs = _seqs(records)
    continuity_meta_count = _continuity_meta_count(records)
    continuous = _seq_continuous(records)
    complete = (
        len(records) - continuity_meta_count == ROTATE_CASE_RECORDS
        and seqs[:1] == [1]
        and seqs[-1:] == [len(records)]
    )
    interference = _grabs(rec) >= 1 and health["rotate_retry"] >= 1
    rec.observations.update({
        "archives": len(archives), "records": len(records),
        "continuity_meta_count": continuity_meta_count, "expected_records": ROTATE_CASE_RECORDS,
        "first_seq": seqs[0] if seqs else None, "last_seq": seqs[-1] if seqs else None,
        "seq_continuous": continuous,
        "rotate_retry": health["rotate_retry"], "rotate_deferred": health["rotate_deferred"],
        "deferred_delete_pending": health["deferred_delete_pending"],
        "interference_observed": interference,
    })
    rec.step("verify_seq_continuity", "ok" if continuous and complete else "gap_or_count_mismatch")
    if archives and continuous and complete and interference and health["deferred_delete_pending"] == 0:
        return "PASS", "none"
    return "FAIL", "輪替未完成、紀錄不連續或未觀察到 av_sim 干擾；保留 WAL 目錄與 av_sim 日誌後停止"


def _find_recreate_meta(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("dir") == "META"
            and (r.get("meta") or {}).get("event") == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"]


def case_unlink(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    from sw_core import win32_files
    from sw_core.wal import WalWriter

    wal_dir = _work_dir("win-wal-av-unlink")
    rec.observations["wal_dir"] = str(wal_dir)
    writer = _MeasuredWalWriter(WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES), rec)
    helper: AvHelper | None = None
    metas: list[dict[str, Any]] = []
    unlinked = False
    unlink_attempts = 0
    sharing_violation_attempts = 0
    try:
        for _ in range(10):
            writer.append(com="COM0", direction="TX", source="spike3", payload=RECORD_PAYLOAD, cmd_id="spike3")
        rec.step("prefill", "ok")
        # 先建立 WAL，再啟動 helper；ready 只在首次掃描完成後寫入，並帶回已持有檔名。
        helper = _start_av_helper_with_wal(wal_dir, rec)
        wal_path = str(wal_dir / "raw.wal.ndjson")
        # 防毒持有期間外部 unlink 會因共用違規失敗；有界重試直到成功（unlink 須最終完成）。
        unlink_error: str | None = None
        deadline = time.monotonic() + UNLINK_RETRY_TIMEOUT_S
        while True:
            unlink_attempts += 1
            try:
                if args.unlink_mode == "posix":
                    win32_files.delete_posix_semantics(wal_path)
                else:
                    win32_files.delete_classic(wal_path)
                unlinked = True
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) == 32:  # ERROR_SHARING_VIOLATION
                    sharing_violation_attempts += 1
                unlink_error = f"{type(exc).__name__}: {exc}"
                if time.monotonic() >= deadline:
                    break
                time.sleep(UNLINK_POLL_S)
        rec.observations.update({
            "unlink_attempts": unlink_attempts,
            "sharing_violation_attempts": sharing_violation_attempts,
            "unlink_last_error": unlink_error or "none",
            "unlinked": unlinked,
        })
        rec.step(f"external_unlink:{args.unlink_mode}", "ok" if unlinked else "blocked_until_timeout")
        if unlinked:
            # 身分檢查受 WAL_IDENTITY_CHECK_INTERVAL_S 節流（且 classic 刪除為 DeletePending），
            # 故 unlink 後改為有界輪詢直到 META 出現或逾時。
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
        if helper is not None:
            helper.stop()
    interference = _grabs(rec) >= 1 and sharing_violation_attempts >= 1
    rec.observations["interference_observed"] = interference
    if not unlinked:
        return "FAIL", "外部 unlink 在有界時間內未完成（防毒持有期間）；保留 WAL 目錄與 av_sim 日誌後停止"
    if not interference:
        return "FAIL", "未觀察到 av_sim 持有期間的 ERROR_SHARING_VIOLATION 與重試；不得判為 PASS"
    rec.observations["meta_count"] = len(metas)
    if not metas:
        rec.step("verify_meta", "missing")
        return "FAIL", "未見 WAL_RECREATED_AFTER_EXTERNAL_UNLINK META；保留 WAL 目錄與 av_sim 日誌後停止"
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
    writer = _MeasuredWalWriter(WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES), rec)
    helper: AvHelper | None = None
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
    FILE_SHARE_WRITE = 0x00000002  # 與常駐 writer 共存；刻意不給 FILE_SHARE_DELETE（防毒同形）
    OPEN_EXISTING = 3
    invalid_handle = ctypes.c_void_p(-1).value
    reader: Any = None
    appended = 0
    seen_rotation_failed = False
    write_failures = -1
    health: dict[str, Any] = {}
    try:
        # WalWriter.__init__ 不開檔，第一次 append 才建立 raw.wal.ndjson；必須先寫一筆，reader 才有檔可開。
        writer.append(com="COM0", direction="TX", source="spike3", payload=b"prefill", cmd_id="spike3-prefill")
        appended += 1
        rec.step("prefill_create_resident_wal", "ok")
        helper = _start_av_helper_with_wal(wal_dir, rec)
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
            _drain_writer(writer, rec)
            health = writer.health()
            write_failures = int(health.get("write_failures", -1))
        finally:
            writer.close()
            if helper is not None:
                helper.stop()
    records = _read_records(wal_dir)
    seqs = _seqs(records)
    continuity_meta_count = _continuity_meta_count(records)
    continuous = _seq_continuous(records)
    complete = (
        len(records) - continuity_meta_count == appended
        and seqs[:1] == [1]
        and seqs[-1:] == [len(records)]
    )
    interference = _grabs(rec) >= 1 and seen_rotation_failed
    rec.observations.update({
        "rotation_failed_seen": seen_rotation_failed, "appended": appended, "records": len(records),
        "continuity_meta_count": continuity_meta_count,
        "first_seq": seqs[0] if seqs else None, "last_seq": seqs[-1] if seqs else None,
        "seq_continuous": continuous, "write_failures": write_failures,
        "rotate_retry": health.get("rotate_retry"), "deferred_delete_pending": health.get("deferred_delete_pending"),
        "interference_observed": interference,
    })
    rec.step("verify_no_loss", "ok" if continuous and complete else "gap_or_count_mismatch")
    if (interference and continuous and complete and write_failures == 0
            and health.get("deferred_delete_pending") == 0):
        return "PASS", "none"
    return "FAIL", "rotation_failed 未出現、紀錄不連續、筆數不符、write_failures 非 0 或延後佇列未清空；保留 WAL 目錄後停止"


def case_f12_progress(rec: Recorder, args: argparse.Namespace) -> tuple[str, str]:
    from realhw import wal_oracle
    from realhw.wal_oracle import verify_record
    from sw_core.wal import WalWriter

    wal_dir = _work_dir("win-wal-f12-progress")
    rec.observations["wal_dir"] = str(wal_dir)
    marker_pre = b"SPIKE3-F12-MARKER-PRE"
    marker_post = b"SPIKE3-F12-MARKER-POST"
    wal_path = str(wal_dir / "raw.wal.ndjson")
    writer = _MeasuredWalWriter(WalWriter(wal_dir=str(wal_dir), rotate_bytes=SMALL_ROTATE_BYTES), rec)
    helper: AvHelper | None = None
    res_pre = res_post = None
    try:
        # 先建立現行 WAL，再確認 helper 持有；常駐 writer 後續維持開啟供 F12 游標讀取。
        writer.append(com="COM0", direction="TX", source="spike3", payload=b"prefill", cmd_id="spike3-f12-prefill")
        helper = _start_av_helper_with_wal(wal_dir, rec)
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
        _drain_writer(writer, rec)
    finally:
        writer.close()
        if helper is not None:
            helper.stop()
    records = _read_records(wal_dir)
    seqs = _seqs(records)
    continuity_meta_count = _continuity_meta_count(records)
    health = writer.health()
    archives = [p for p in glob.glob(str(wal_dir / "raw.wal.ndjson*")) if not p.endswith("raw.wal.ndjson")]
    append_count = rec.observations["append_count"]
    complete = (
        len(records) - continuity_meta_count == append_count
        and seqs[:1] == [1]
        and seqs[-1:] == [len(records)]
        and _seq_continuous(records)
    )
    interference = _grabs(rec) >= 1 and health.get("rotate_retry", 0) >= 1
    crc_errors = [e for e in (verify_record(r) for r in records) if e is not None]
    rec.observations.update({
        "pre_rotate": {"ok": res_pre.ok if res_pre else None,
                       "reason_code": res_pre.reason_code if res_pre else None,
                       "diagnostics": res_pre.diagnostics if res_pre else None},
        "post_rotate": {"ok": res_post.ok if res_post else None,
                        "reason_code": res_post.reason_code if res_post else None,
                        "diagnostics": res_post.diagnostics if res_post else None},
        "archives": len(archives), "records": len(records),
        "continuity_meta_count": continuity_meta_count,
        "first_seq": seqs[0] if seqs else None, "last_seq": seqs[-1] if seqs else None,
        "seq_continuous": _seq_continuous(records),
        "rotate_retry": health.get("rotate_retry"),
        "deferred_delete_pending": health.get("deferred_delete_pending"),
        "crc_errors": crc_errors,
        "interference_observed": interference,
    })
    if (res_pre is not None and res_post is not None and res_pre.ok and res_post.ok
            and complete and archives and interference
            and health.get("deferred_delete_pending") == 0 and not crc_errors):
        return "PASS", "none"
    return "FAIL", "T17 oracle（常駐 handle 存活中讀取、跨 rotate、av_sim 持有中）未通過；保留 WAL 目錄與 av_sim 日誌"


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
    # OD-19 (3)：防毒名稱與狀態由工具唯讀查詢，僅作資訊欄位；非 Windows 主機不發出任何 Windows 端查詢。
    edr = query_edr() if os.name == "nt" else {"product": "unknown", "status": "unknown", "interceptions": "unknown"}
    artifact = {
        "binary": "sw_core (Python 基線)",
        "revision": _git_blob_sha1(Path(__file__).resolve()),
        "tree_sha256": _tree_sha256(),
        "git_head": _git_head(),
        "git_clean": _git_clean(),
    }
    subjects = binary_subject_paths()
    before = snapshot_binaries(subjects)
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
        wal_dir_value = rec.observations.get("wal_dir")
        if "exception" not in rec.observations and isinstance(wal_dir_value, str) and Path(wal_dir_value).is_dir():
            if not _collect_oplock_variant(Path(wal_dir_value), rec) and verdict == "PASS":
                verdict, fallback, code = (
                    "INSUFFICIENT_EVIDENCE", "獨立 oplock 變體未取得有效 helper 證據", 1,
                )
    after = snapshot_binaries(subjects)
    av_sim = dict(rec.observations.get("av_sim") or {
        **_av_sim_module().describe(SPIKE3_SEED),
        "launch_error": "not_started", "grabs": None, "open_errors": None, "helper_exit": None,
    })
    av_sim.setdefault("variants", {}).setdefault("oplock", {
        **_av_sim_module().describe(SPIKE3_SEED, oplock=True),
        "launch_error": "not_started", "grabs": None, "open_errors": None, "helper_exit": None,
    })
    integrity = binary_integrity_report(before, after, av_sim.get("launch_error"))
    if verdict == "PASS" and not (integrity["unchanged"] and not integrity["launch_refused"]):
        # 執行檔前後不一致或啟動被拒＝端點防護干預的可觀察事實；不得以 PASS 放行。
        verdict, fallback, code = "FAIL", "執行檔前後不一致或啟動被拒（端點防護干預）；保留證據，不得判為 PASS", 1
        rec.observations["binary_integrity_downgrade"] = True
    wal_dir_str = str(rec.observations.get("wal_dir", ""))
    env = gather_env(Path(wal_dir_str) if wal_dir_str else env_dir, edr["product"])
    observations = {k: v for k, v in rec.observations.items() if k != "av_sim"}
    evidence = {
        "case_id": case_id,
        "run_at": run_at,
        "env": env,
        "artifact": artifact,
        "steps": rec.steps or [{"op": "host_check", "ts": run_at, "result": "no_case_steps_executed"}],
        "observations": observations,
        "av_sim": av_sim,
        "binary_integrity": integrity,
        "edr": {"product": edr["product"], "status": edr["status"], "interceptions": edr["interceptions"]},
        "verdict": verdict,
        "fallback": fallback,
    }
    try:
        _validate(evidence)
    except Exception as exc:  # noqa: BLE001 - schema 錯誤也須留下不通過的診斷證據
        import jsonschema

        if not isinstance(exc, jsonschema.ValidationError):
            raise
        verdict = "INSUFFICIENT_EVIDENCE"
        fallback = "證據 schema 驗證失敗；檢視 observations.evidence_validation_error 後重跑"
        code = 1
        evidence["verdict"] = verdict
        evidence["fallback"] = fallback
        evidence.setdefault("observations", {})["evidence_validation_error"] = str(exc)
    out_dir = Path(args.evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{case_id}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"case_id": case_id, "verdict": verdict, "evidence": str(out_dir / f"{case_id}.json")},
                     ensure_ascii=False))
    _cleanup_pass_dir(wal_dir_str, verdict)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage 0 Spike 3：Windows WAL 與端點防護（OD-19）實機證據腳本")
    p.add_argument("--list", action="store_true", help="列出 case_id（JSON 陣列）後結束")
    p.add_argument("--case", choices=CASE_IDS, help="要執行的 case_id")
    p.add_argument("--evidence-dir", help="證據輸出目錄")
    p.add_argument("--unlink-mode", choices=("classic", "posix"), default="classic")
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
