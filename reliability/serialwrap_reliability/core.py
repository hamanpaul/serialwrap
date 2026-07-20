"""serialwrap_reliability 核心邏輯——只提供 repo root 與 realhw bootstrap。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def resolve_repo_root(core_file: Path) -> Path:
    """由模組檔位置推回 repo root；僅支援 editable 安裝。"""
    root = Path(core_file).resolve().parents[2]
    if not (root / "realhw" / "harness.py").is_file():
        raise RuntimeError(
            f"serialwrap_reliability.core 僅支援 editable 安裝；"
            f"REPO_ROOT={root} 下找不到 realhw/harness.py。"
            "請從 repo root 執行 pip install -e reliability/"
        )
    return root


REPO_ROOT: Path = resolve_repo_root(Path(__file__))


def ensure_realhw_importable() -> Path:
    """把 repo root 冪等插入 ``sys.path``，讓 ``import realhw`` 可用。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def load_registry() -> list[Any]:
    """載入 realhw registry，回傳淺拷貝。

    首次呼叫會 import ``realhw.cases`` 觸發所有 case 的 ``register()``，
    將結果寫入 ``harness.REGISTRY``；此副作用是 process-wide 且不可逆。
    後續呼叫只讀取既有 registry，不重複註冊。
    """
    ensure_realhw_importable()
    import realhw.cases  # noqa: F401
    from realhw import harness

    return list(harness.REGISTRY)


def synth_longrun_steps(duration_s: int, interval_s: int) -> list[dict[str, Any]]:
    """duration/interval → N 個 checkpoint step（最少 1）。"""
    n = max(1, int(duration_s) // max(1, int(interval_s)))
    return [
        {"id": f"checkpoint-{i:03d}", "action": "longrun_checkpoint", "target": "bench"}
        for i in range(1, n + 1)
    ]


def case_to_dict(case: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """realhw Case → testpilot case dict 最小形狀。"""
    devices: dict[str, dict[str, str]] = {}
    for index, board in enumerate(cfg.get("boards", [])):
        com = board.get("com")
        if not com:
            raise ValueError(f"boards[{index}] 缺 'com' 欄位（board={board!r}）")
        if str(com) in devices:
            raise ValueError(f"boards[{index}] 的 com={com!r} 與前面的 board 重複，topology key 碰撞")
        devices[str(com)] = {
            "role": str(board.get("alias", "")),
            "serial": str(board.get("serial", "")),
            "busid": str(board.get("busid", "")),
            "platform": str(board.get("platform", "")),
        }
    if case.tier == "longrun":
        interval_s = int((cfg.get("longrun") or {}).get("snapshot_interval_s") or 300)
        steps = synth_longrun_steps(int(cfg.get("duration_s") or 0), interval_s)
    else:
        steps = [{"id": "exec", "action": "run_case", "target": "bench"}]
    return {
        "id": case.id,
        "name": case.title,
        "topology": {"devices": devices},
        "steps": steps,
        "pass_criteria": ["realhw_case_verdict"],
        "metadata": {
            "tier": case.tier,
            "destructive": bool(case.destructive),
            "requires": list(case.requires),
            "hints": list(case.hints),
        },
    }


def build_case_dicts(registry: list[Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """整個 registry → case dicts（維持註冊順序）。"""
    return [case_to_dict(case, cfg) for case in registry]


def filter_for_run(cases: list[dict[str, Any]], requested_ids: set[str]) -> list[dict[str, Any]]:
    """預設排除 destructive；顯式點名時允許。"""
    if requested_ids:
        return [case for case in cases if case["id"] in requested_ids]
    return [case for case in cases if not (case.get("metadata") or {}).get("destructive")]
