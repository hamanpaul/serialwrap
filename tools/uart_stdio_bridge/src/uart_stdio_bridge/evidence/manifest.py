"""Manifest 與私有目錄輔助函式。"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import tempfile
from typing import Any


def ensure_private_dir(path: pathlib.Path) -> None:
    """建立私有目錄，POSIX 下設權限 0o700。"""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def write_json_atomic(path: pathlib.Path, obj: dict[str, Any]) -> None:
    """原子化寫入 JSON 檔，POSIX 下設權限 0o600。"""
    content = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target_dir = path.parent
    ensure_private_dir(target_dir)
    fd, tmp_path = tempfile.mkstemp(dir=str(target_dir), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(path))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def platform_info() -> dict[str, Any]:
    """收集平台環境資訊。"""
    return {
        "os": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "evidence_insufficient": [],
    }


def bridge_manifest(
    *,
    run_id: str,
    case_id: str,
    tool: dict[str, Any],
    transport: dict[str, Any],
    dut: dict[str, Any],
    evidence: dict[str, Any],
    platform_info: dict[str, Any],
    started_at: str,
    closed_at: str | None,
    closed_cleanly: bool,
    failure: str | None,
) -> dict[str, Any]:
    """組裝 bridge-manifest.v1 根物件。"""
    return {
        "schema_version": "bridge-manifest.v1",
        "run_id": run_id,
        "case_id": case_id,
        "tool": tool,
        "transport": transport,
        "dut": dut,
        "evidence": evidence,
        "platform_info": platform_info,
        "started_at": started_at,
        "closed_at": closed_at,
        "closed_cleanly": closed_cleanly,
        "failure": failure,
    }
