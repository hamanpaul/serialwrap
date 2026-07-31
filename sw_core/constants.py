from __future__ import annotations

import os
import sys


def _env_path(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default
    return os.path.expanduser(raw)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _xdg(name: str, fallback_rel: str) -> str:
    return os.environ.get(name) or os.path.join(os.path.expanduser("~"), fallback_rel)


_CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")
_STATE_HOME = _xdg("XDG_STATE_HOME", ".local/state")
_DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
_RUNTIME_HOME = os.environ.get("XDG_RUNTIME_DIR")  # 可能 None

CONFIG_DIR = _env_path("SERIALWRAP_CONFIG_DIR", os.path.join(_CONFIG_HOME, "serialwrap"))
STATE_DIR = _env_path("SERIALWRAP_STATE_DIR", os.path.join(_STATE_HOME, "serialwrap"))
def _run_dir_default() -> str:
    # 若呼叫端只設 SERIALWRAP_STATE_DIR（常見 CI / throwaway daemon 隔離），socket/lock 必須
    # 落在 STATE_DIR 之下以維持向後相容（舊行為 RUN_DIR 預設＝STATE_DIR，即使 XDG_RUNTIME_DIR 存在）。
    if os.environ.get("SERIALWRAP_STATE_DIR", "").strip():
        return STATE_DIR
    if _RUNTIME_HOME:
        return os.path.join(_RUNTIME_HOME, "serialwrap")
    return os.path.join(STATE_DIR, "run")


RUN_DIR = _env_path("SERIALWRAP_RUN_DIR", _run_dir_default())
DATA_DIR = _env_path("SERIALWRAP_DATA_DIR", os.path.join(_DATA_HOME, "serialwrap"))

LOCK_PATH = os.path.join(RUN_DIR, "serialwrapd.lock")
SOCKET_PATH = os.path.join(RUN_DIR, "serialwrapd.sock")

# 平台感知 RPC endpoint 預設（#84 PORT-4）
# Windows：走 TCP loopback；POSIX：沿用 AF_UNIX SOCKET_PATH。
# tcp:// URL 不可過 os.path.expanduser，故 Windows 分支直接組字串。
DEFAULT_TCP_PORT: int = int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))
# RPC TCP 僅允許 loopback（#84 PORT-4 server bind 白名單；#131 daemon start 亦共用）。
LOOPBACK_TCP_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
if os.name == "nt" or sys.platform.startswith("win"):
    DEFAULT_ENDPOINT: str = (
        os.environ.get("SERIALWRAP_ENDPOINT")
        or f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}"
    )
else:
    DEFAULT_ENDPOINT = os.environ.get("SERIALWRAP_ENDPOINT") or SOCKET_PATH

TTYMCU_PATH = _env_path("SERIALWRAP_TTYMCU_PATH", os.path.join(RUN_DIR, "dev", "ttyMCU"))
STATE_PATH = os.path.join(STATE_DIR, "state.json")
WAL_DIR = _env_path("SERIALWRAP_WAL_DIR", os.path.join(STATE_DIR, "wal"))
PROFILE_DIR = _env_path("SERIALWRAP_PROFILE_DIR", os.path.join(CONFIG_DIR, "profiles"))
DEVICE_BY_ID_DIR = _env_path("SERIALWRAP_BY_ID_DIR", "/dev/serial/by-id")
DEVICE_BY_PATH_DIR = _env_path("SERIALWRAP_BY_PATH_DIR", "/dev/serial/by-path")
LOG_DIR = _env_path("SERIALWRAP_LOG_DIR", os.path.join(os.path.expanduser("~"), "b-log"))
DEFAULT_WAL_ROTATE_BYTES = 64 * 1024 * 1024
EVENTS_DIR = _env_path(
    "SERIALWRAP_EVENTS_DIR",
    os.path.join(os.path.expanduser("~"), ".serialwrap", "events.d"),
)
EVENTS_RUNTIME_DIR = _env_path(
    "SERIALWRAP_EVENTS_RUNTIME_DIR",
    os.path.join(STATE_DIR, "events"),
)
EVENTS_LOG_PATH = _env_path(
    "SERIALWRAP_EVENTS_LOG_PATH",
    os.path.join(EVENTS_RUNTIME_DIR, "events.ndjson"),
)
EVENTS_LOG_ROTATE_BYTES = 10 * 1024 * 1024
EVENTS_LOG_BACKUP_COUNT = 3


# 帳密解析終態（#140）：profile 宣告帳密來源但解析為空時，session 進此終態，
# login 流程不對 Login:/Password: 送空字串、自動 reprobe 亦不再重試；需操作者補
# 帳密後手動 attach/recover 才重試。與「板子尚未到 login prompt」的 LOGIN_REQUIRED 區分。
ERROR_CREDENTIALS_UNRESOLVED: str = "CREDENTIALS_UNRESOLVED"

# RX 洪水分類（#153）：probe 失敗且 RX 速率超閾時，把 PROMPT_UNAVAILABLE 類錯誤
# 反分類為 RX_FLOOD——「console 被灌爆」與「console 死了」不再擠同一個錯誤碼。
# 語意：等排空（daemon 於 RX 閒置後自動重探接手），勿重建 session。
ERROR_RX_FLOOD: str = "RX_FLOOD"
RX_RATE_WINDOW_S: float = 10.0
"""UARTBridge RX 速率統計視窗（秒）；rx_stats() 回報此視窗內的 raw bytes。"""
RX_FLOOD_BYTES_PER_10S: int = 20_000
"""視窗內 raw RX bytes 達此值即視為洪水（≈2KB/s 持續，115200 線速的 ~17%）。
僅在 probe 失敗後才據此反分類；正常 idle 板視窗內近 0，誤判面窄。"""

# transport stall 分類（#150）：probe 全程零 raw RX（連 echo 都無）、且該 session 曾有
# RX 但已凍結逾閾時，把 PROMPT_UNAVAILABLE／*_PROMPT_TIMEOUT 精煉為 TRANSPORT_STALL
# （疑似 USB/usbip read-endpoint stall，dmesg 常見 `urb stopped: -32`）——serialwrap 的
# recover/release+attach 無法自復，需 host 層 USB re-enumeration，勿誤導去 power-cycle DUT。
ERROR_TRANSPORT_STALL: str = "TRANSPORT_STALL"
TRANSPORT_STALL_MIN_RX_AGE_S: float = 30.0
"""last_rx 距今至少此秒數才允許翻轉為 TRANSPORT_STALL。reprobe backoff 上限 15s、
最多 10 次≈2 分鐘，30s 門檻保證 exhausted 前必有多次 probe 落在門檻後、能完成翻轉。"""

# bootloader recovery 用常數
MAX_RECOVERY_LEASE_S: float = 120.0
"""recovery interactive lease 最長持續秒數。"""
BOOTLOADER_RX_TAIL_BYTES: int = 512
"""self_test 讀取 RX tail 的位元組數，用於 bootloader prompt 比對。"""
HUMAN_ACTIVE_WINDOW_S: float = 60.0
"""human interactive lease 視為仍在使用的最後鍵入時間窗（秒）。"""
_HUMAN_PEER_GRACE_S: float = 3.0
"""reconcile tick 的孤兒 console 回收節流間隔（秒）；亦作 Task 5 peer-loss grace 基準值（#76）。"""
REPROBE_RX_IDLE_S: float = 3.0
"""readiness 自動重探前，RX 需先維持閒置的秒數。"""
REPROBE_BACKOFF_S: float = 2.0
"""readiness 自動重探的初始 backoff 秒數。"""
REPROBE_MAX_INTERVAL_S: float = 15.0
"""readiness 自動重探的最大 backoff 秒數。"""
REPROBE_MAX_ATTEMPTS: int = 10
"""readiness 自動重探的最大嘗試次數。"""

# U-Boot autoboot 保護（#130）：boot quiet window
BOOT_BANNER_PATTERNS: tuple[str, ...] = (
    "U-Boot",
    "Hit any key to stop autoboot",
)
"""boot banner 偵測樣式（**大小寫敏感的 substring 比對，非 regex**）。

選 substring 而非 regex：樣式為固定字面字串、比對成本低，且配合呼叫端的
rolling tail 對 RX chunk 任意切割位置最穩健（見 ``login_fsm.detect_boot_banner``）。"""
BOOT_QUIET_WINDOW_S: float = 180.0
"""偵測到 boot banner（或 agent 送出 reboot 命令）後，system probe TX 的靜默秒數。

實測 prpl 平台目標板完整開機約 150s，加裕度取 180s。RX 見到該 session 的 login/prompt
（開機完成訊號）會提前解除；quiet window 只擋 source=system 的自動 probe，
不擋 human console bytes、interactive lease TX 與 agent 顯式命令。"""
BOOT_BANNER_TAIL_CHARS: int = 256
"""banner 偵測用 rolling RX tail 的長度（字元）；跨 chunk 邊界拼接比對。"""

# 記憶體上限（#81）：防止長壽 daemon 下無界 in-memory 結構成長至 OOM。
BG_CAPTURE_MAX_BYTES: int = 4 * 1024 * 1024
"""單一 background capture 在記憶體保留的 chunk 總位元組上限；超過則丟最舊（環形），dropped_chunks 累計。"""
BG_CAPTURE_MAX_COUNT: int = 64
"""保留的 background capture 數上限；超過則淘汰最舊的「已終結（非 active）」capture。"""
CMD_HISTORY_MAX: int = 512
"""arbiter 保留的命令記錄數上限；超過則淘汰最舊的「已完成（有 done_at）」命令。"""
CMD_PENDING_MAX: int = 256
"""arbiter 每個 session「進行中（accepted/running，done_at 為 None）」命令的硬上限（admission control）。
超過即拒絕新 submit（SESSION_QUEUE_FULL backpressure），而非接受後排隊——eviction 只能淘汰已完成命令，
無法回收尚未執行者；少了 admission control，client 比 UART worker 快時 _commands 與 PriorityQueue 會
持續累積 accepted/running records 而 OOM（#81 Codex 必修）。"""
DEFERRED_INPUT_MAX_BYTES: int = 256 * 1024
"""agent 命令期間單一 human console 的 deferred 輸入緩衝上限；超過則丟最舊位元組。"""


def ensure_runtime_dirs() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(WAL_DIR, exist_ok=True)
    os.makedirs(EVENTS_DIR, exist_ok=True)
    os.makedirs(EVENTS_RUNTIME_DIR, exist_ok=True)
