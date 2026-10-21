"""Python daemon 退役切換（P9-T16）測試用假 repo 與核准紀錄替身。

`retirement_repo` 建出含證據索引、兩平台 Stage 3 target、`evidence/` 與 keep 清單目錄的假 repo。
兩平台 bundle 位於 `bundles/<platform>/`：先以 `make_bundle(stage="3")` 建立並放入資產檔
`assets/skill/SKILL.md`，**之後**才以 `set_target` 設 target，使 `bundle_tree_sha256` 涵蓋資產。

前置模組（`tests/stage3_fakes.py`、`tests/fakes_evidence.py`、`tools/stage3/*`）於函式內延遲匯入，
使本檔可在前置 Task 尚未合入時被收集；缺件時測試以 ImportError／ModuleNotFoundError 失敗。
本檔不 import `sw_core`，不得用產品內部邏輯計算預期結果（規格 §11.3、§15.4.2）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS: tuple[str, ...] = ("linux", "windows")
TARGETS: dict[str, str] = {"linux": "x86_64-unknown-linux-gnu", "windows": "x86_64-pc-windows-gnu"}
INDEX_REL = "docs/rust-refactor/evidence/index.json"
RETIREMENT_REL = "packaging/python_daemon_retirement.json"
# keep 清單（P9-T16 KEEP_PYTHON_TEST_TOOLS）：退役後仍必須存在的 Python 測試工具
KEEP_PATHS: tuple[str, ...] = (
    "realhw", "regression", "reliability", "func-test", "tools/uart_stdio_bridge",
    "tests/contract_blackbox", "tests/acceptance", "tools/stage3", "sw_core",
)
# stage3_result → 寫入假證據的 entry result：BLOCKED 以「證據不足」表達（stage_status 判為 BLOCKED）
STAGE3_RESULT_TO_ENTRY: dict[str, str] = {"PASS": "PASS", "BLOCKED": "INSUFFICIENT", "FAIL": "FAIL"}


def _unapproved() -> dict:
    """未核准狀態：`retired` 為 false，其餘欄位為 null（evidence_index 除外）。"""
    return {
        "retired": False,
        "evidence_index": INDEX_REL,
        "evidence_index_sha256": None,
        "approved_by": None,
        "approved_at": None,
        "decision_ref": None,
        "effective_bundle_version": None,
        "stage3_target_artifact_ids": {"linux": None, "windows": None},
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def retirement_repo(root: Path, *, stage3_result: str) -> Path:
    """建立假 repo（回傳 `root`）。stage3_result ∈ {"PASS", "BLOCKED", "FAIL"}。"""
    if stage3_result not in STAGE3_RESULT_TO_ENTRY:
        raise ValueError(f"stage3_result 必須是 {sorted(STAGE3_RESULT_TO_ENTRY)} 之一：{stage3_result!r}")
    import stage3_fakes as fakes
    from tools.stage3 import evidence_index

    root.mkdir(parents=True, exist_ok=True)
    idx = root / INDEX_REL
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"entries": [], "schema_version": 2, "stage_targets": {}, "target_history": []})
                   + "\n", encoding="utf-8")
    (root / "evidence").mkdir(exist_ok=True)

    for rel in KEEP_PATHS:
        keep = root / rel
        keep.mkdir(parents=True, exist_ok=True)
        (keep / ".keep").write_text("", encoding="utf-8")

    l1_nodeids = {
        "linux": ["tests/contract_blackbox/test_release_contract.py::test_frozen_contract"],
        "windows": ["tests/contract_blackbox/test_release_contract.py::test_frozen_contract"],
    }
    l1_lock = {
        "schema_version": 1,
        "sets": {
            "s3_release_contract": {
                platform: {"selector": {"argv": ["-q"], "markexpr": ""}, "nodeids": nodeids}
                for platform, nodeids in l1_nodeids.items()
            }
        },
    }
    (root / "tests" / "acceptance" / "l1_required.lock.json").write_text(
        json.dumps(l1_lock, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    _write_json(root / RETIREMENT_REL, _unapproved())

    # 先建 bundle 並放入資產，再設 target（bundle_tree_sha256 因此涵蓋資產檔）
    for platform in PLATFORMS:
        bundle = root / "bundles" / platform
        fakes.make_bundle(bundle, stage="3", target=TARGETS[platform])
        skill = bundle / "assets" / "skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# serialwrap skill（假資產）\n", encoding="utf-8")

    for platform in PLATFORMS:
        bundle = root / "bundles" / platform
        manifest = bundle / "release-manifest.json"
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        evidence_index.set_target(
            idx, stage="3", platform=platform, release_manifest=manifest,
            contract_revision=doc["contract_revision"], tool_revision="t" * 40, case_list_sha256="0" * 64,
            set_by="fakes_retirement", now="2026-10-31T00:00:00+00:00", bundle_dir=bundle,
        )

    catalog = evidence_index.load_exit_catalog()
    entry_result = STAGE3_RESULT_TO_ENTRY[stage3_result]
    for item_id, item in sorted(catalog.items()):
        if item.stage != "3":
            continue
        platforms = PLATFORMS if item.platform == "both" else (item.platform,)
        for platform in platforms:
            evidence_index.add_entry(idx, entry(root, item_id, entry_result, platform=platform, n=0), catalog=catalog)
    return root


def _stub_report(repo: Path, item_id: str, result: str, platform: str, n: int) -> tuple:
    """在 `repo/evidence` 寫一個 stub 報告，回傳相對 evidence_root（`repo/evidence`）的 FrozenFile 元組。"""
    from tools.stage3.ab_manifest import FrozenFile
    from tools.stage3.jsonio import sha256_file

    evidence_root = repo / "evidence"
    report = evidence_root / f"{item_id}-{platform}-{n}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"item_id": item_id, "platform": platform, "n": n, "result": result},
                                 sort_keys=True) + "\n", encoding="utf-8")
    return (FrozenFile(path=report.relative_to(evidence_root).as_posix(), sha256=sha256_file(report)),)


def entry(repo: Path, item_id: str, result: str, *, platform: str, n: int = 0):
    """建立一筆假證據 entry（底層為 `fakes_evidence.entry`）。

    `fakes_evidence.entry` 以 `<root>/index.json` 讀取 target，且報告路徑相對 root；本計畫的假 repo 把索引
    放在 `docs/rust-refactor/evidence/index.json`、報告放在 `repo/evidence`（evidence_root）。因此這裡把
    root 設為索引所在目錄，並以 `files` 明確指定相對 `repo/evidence` 的報告。P9-T16 測試中所有 entry 建立一律經此函式。
    """
    import fakes_evidence
    from tools.stage3 import evidence_index

    index_dir = (repo / INDEX_REL).parent
    files = _stub_report(repo, item_id, result, platform, n)
    catalog = evidence_index.load_exit_catalog()
    item = catalog[item_id]
    extra = {}
    if item.required_l1_set is not None:
        lock = json.loads((repo / "tests" / "acceptance" / "l1_required.lock.json").read_text(encoding="utf-8"))
        nodeids = lock["sets"][item.required_l1_set][platform]["nodeids"]
        digest = hashlib.sha256(json.dumps(sorted(nodeids), separators=(",", ":")).encode()).hexdigest()
        extra = {"producer": "ingest:junit", "l1_set_sha256": digest}
    return fakes_evidence.entry(
        index_dir, item_id, result, platform=platform, n=n, files=files, **extra
    )


def approved(repo: Path) -> dict:
    """依目前索引 target 填好核准欄位的 retirement dict（retired 為 true）。"""
    import stage3_fakes as fakes
    from tools.stage3.jsonio import sha256_file

    idx = repo / INDEX_REL
    targets = json.loads(idx.read_text(encoding="utf-8"))["stage_targets"]["3"]
    linux_manifest = repo / "bundles" / "linux" / "release-manifest.json"
    bundle_version = json.loads(linux_manifest.read_text(encoding="utf-8"))["bundle_version"]
    return {
        "retired": True,
        "evidence_index": INDEX_REL,
        "evidence_index_sha256": sha256_file(idx),
        "approved_by": "owner",
        "approved_at": fakes.ts(30, 1),
        "decision_ref": "OD-14",
        "effective_bundle_version": bundle_version,
        "stage3_target_artifact_ids": {p: targets[p]["artifact_id"] for p in PLATFORMS},
    }


def write_approved(repo: Path) -> dict:
    """把 `approved(repo)` 寫回 `packaging/python_daemon_retirement.json` 並回傳。"""
    doc = approved(repo)
    _write_json(repo / RETIREMENT_REL, doc)
    return doc


def bundle_args(repo: Path) -> dict[str, Path]:
    """`--bundle-manifest` 對應：{平台: 該平台 bundle 的 release-manifest.json}。"""
    return {p: repo / "bundles" / p / "release-manifest.json" for p in PLATFORMS}
