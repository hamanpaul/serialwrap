"""讀取並驗證 Stage 3 執行前凍結的 context。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

_FIELDS = frozenset({
    "candidate_round_id",
    "rc_tag",
    "platform",
    "archive_sha256",
    "artifact_id",
    "required_cases_sha256",
    "run_id",
})
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RC_TAG = re.compile(r"^v1\.0\.0-rc\.[1-9][0-9]*$")
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def load_run_context(path: Path) -> Mapping[str, Any]:
    """載入精確的七欄位 context；不補值，也不依賴證據索引。"""
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"run context is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("run context must contain exactly the frozen identity fields")
    if not isinstance(value["candidate_round_id"], str) or not _HEX32.fullmatch(value["candidate_round_id"]):
        raise ValueError("candidate_round_id must be 32 lowercase hexadecimal characters")
    if not isinstance(value["rc_tag"], str) or not _RC_TAG.fullmatch(value["rc_tag"]):
        raise ValueError("rc_tag must match v1.0.0-rc.N")
    if value["platform"] not in ("linux", "windows"):
        raise ValueError("platform must be linux or windows")
    for field in ("archive_sha256", "artifact_id", "required_cases_sha256"):
        if not isinstance(value[field], str) or not _HEX64.fullmatch(value[field]):
            raise ValueError(f"{field} must be a 64-character lowercase SHA256")
    if not isinstance(value["run_id"], str) or not _RUN_ID.fullmatch(value["run_id"]):
        raise ValueError("run_id has an invalid format")
    return MappingProxyType(value)
