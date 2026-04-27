from __future__ import annotations

import os


def _env_path(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default
    return os.path.expanduser(raw)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = _env_path("SERIALWRAP_STATE_DIR", "/tmp/serialwrap")
RUN_DIR = _env_path("SERIALWRAP_RUN_DIR", STATE_DIR)
LOCK_PATH = os.path.join(RUN_DIR, "serialwrapd.lock")
SOCKET_PATH = os.path.join(RUN_DIR, "serialwrapd.sock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
WAL_DIR = _env_path("SERIALWRAP_WAL_DIR", os.path.join(STATE_DIR, "wal"))
PROFILE_DIR = _env_path("SERIALWRAP_PROFILE_DIR", os.path.join(BASE_DIR, "profiles"))
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


def ensure_runtime_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(WAL_DIR, exist_ok=True)
    os.makedirs(EVENTS_DIR, exist_ok=True)
    os.makedirs(EVENTS_RUNTIME_DIR, exist_ok=True)
