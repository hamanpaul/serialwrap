"""證據 ingest 的共用推導結果型別（P9-T15 交付；Task 14B `evidence_ingest` 以 `from tools.stage3.derived import Derived` 再匯出）。

`Derived` 是所有 kind 推導器（含 `od09_consume.derive_freeze_report`）的統一回傳形狀。獨立成模組，
使 `od09_consume`（被 `evidence_ingest` 依賴）不必延後匯入 `evidence_ingest`，避免循環依賴。
欄位順序與型別依 `docs/rust-refactor/plans/10-stage3-canary.md` Task 14B；唯一新增的是末欄 `detail`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Derived:
    result: str                     # RESULTS 之一
    artifact_id: str | None         # 報告內記錄的受測 artifact（無則由 --release-manifest 提供並交叉核對）
    contract_revision: str | None
    tool_revision: str | None
    case_list_sha256: str | None
    workload_sha256: str | None
    bundle_tree_sha256: str | None  # 產生端報告記錄的實際受測內容指紋（R3-F2／R4-F1）；未量測時為 None
    runtime_tree_sha256: str | None
    xfail_cases: tuple[str, ...]
    started_at: str
    files: tuple[Path, ...]         # 需要複製的檔案（報告目錄時為整棵樹）
    ledger_through: str | None      # 僅 canary-report；其他 kind None
    ledger_records: int | None      # 僅 canary-report；其他 kind None
    l1_set_sha256: str | None       # 僅 junit（R5-F3）；其他 kind None
    l1_verdict: Mapping[str, Any] | None   # 僅 junit：P2 l1_judge.judge 的完整回傳；其他 kind None
    note: str
    detail: tuple[str, ...] = ()    # FAIL／INSUFFICIENT 的違規或缺漏清單（P9-T15 新增）；無則空
