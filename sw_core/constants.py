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
DEFAULT_WAL_ROTATE_BYTES = 64 * 1024 * 1024


def ensure_runtime_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(WAL_DIR, exist_ok=True)
