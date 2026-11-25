"""Stage 3 證據推導結果的共用型別。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Derived:
    """不同證據來源共用的正規化結果；不執行任何推導或 I/O。"""

    result: str
    candidate_context: Mapping[str, Any] | None
    artifact_id: str | None
    contract_revision: str | None
    tool_revision: str | None
    case_list_sha256: str | None
    required_cases_sha256: str | None
    workload_sha256: str | None
    bundle_tree_sha256: str | None
    runtime_tree_sha256: str | None
    xfail_cases: tuple[str, ...]
    started_at: str
    files: tuple[Path, ...]
    ledger_through: str | None
    ledger_records: int | None
    l1_set_sha256: str | None
    l1_verdict: Mapping[str, Any] | None
    note: str
    detail: tuple[str, ...] = ()
