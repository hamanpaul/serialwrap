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
