"""P9-T17：Stage 3 擁有者程序文件的失敗測試（TDD red）。"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "docs" / "rust-refactor" / "procedures"
DOCS = [
    "stage3-runbook.md",
    "sequential-ab.md",
    "canary.md",
    "rollback-drill.md",
    "python-daemon-retirement.md",
    "evidence-index.md",
]  # od09-deferred-freeze.md 屬 P2 Task 16（命令為 realhw.acceptance.od09_freeze），不在本表


def _read(name: str) -> str:
    return (PROC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("doc", DOCS)
def test_commands_parse(doc):
    from tools.stage3.__main__ import build_parser

    text = _read(doc)
    cmds = [
        ln.strip()
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        for ln in block.splitlines()
        if ln.strip().startswith("python3 -m tools.stage3 ")
    ]
    assert cmds, doc
    for c in cmds:
        argv = shlex.split(c)[3:]
        build_parser().parse_args(argv)


def test_runbook_marks_every_blocked_owner_item():
    text = _read("stage3-runbook.md")
    for n in range(1, 19):
        assert f"【BLOCKED-OWNER BO-P9-{n:02d}】" in text


def test_runbook_order():
    text = _read("stage3-runbook.md")
    order = [
        "BO-P9-12", "BO-P9-06", "BO-P9-17", "BO-P9-01", "BO-P9-02", "BO-P9-18",
        "BO-P9-04", "BO-P9-05", "BO-P9-07", "BO-P9-10", "BO-P9-09", "BO-P9-13",
    ]
    pos = [text.index(f"【BLOCKED-OWNER {k}】") for k in order]
    assert pos == sorted(pos)


def test_od09_freeze_timed_at_stage0_exit():
    for doc in ("stage3-runbook.md", "od09-deferred-freeze.md"):
        assert "S1B-L3-MIXED-48H-WINDOWS" in _read(doc), doc
    od09 = _read("od09-deferred-freeze.md")  # P2 Task 16 交付（R4-F2）
    assert "python3 -m realhw.acceptance.od09_freeze freeze" in od09
    assert "python3 -m tools.stage3 od09" not in od09  # Stage 0 程序不得依賴本 Phase 工具
    runbook = _read("stage3-runbook.md")
    assert "realhw.acceptance.od09_freeze" in runbook
    assert "tools.stage3 od09 freeze" not in runbook


def test_od12_single_user_and_no_isolation_claim():
    assert "單人使用" in _read("canary.md")
    for doc in DOCS:
        text = _read(doc)
        assert "同使用者隔離" not in text or "不得宣稱「同使用者隔離」" in text, doc
        for ln in text.splitlines():
            if "Defender" in ln:
                assert "關閉" in ln, (doc, ln)  # OD-05：代表機台為第三方 EDR、Defender 關閉


def test_no_interactive_status_in_procedures():
    files = [PROC / d for d in DOCS + ["od09-deferred-freeze.md"]]
    files += sorted((ROOT / "tools" / "stage3").glob("*.py"))
    for path in files:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if "interactive-status" in ln or "interactive_status" in ln:
                assert "不得呼叫" in ln, (path, ln)


def test_retirement_procedure_verifies_shipped_bytes():
    text = _read("python-daemon-retirement.md")
    blocks = "\n".join(re.findall(r"```bash\n(.*?)```", text, re.S))
    marks = [
        "python3 -m tools.stage3 retirement verify --mode release",
        "python3 -m tools.stage3 retirement pack",
        "python3 -m tools.stage3 retirement verify-archive",
    ]
    pos = [blocks.index(m) for m in marks]
    assert pos == sorted(pos)
    assert "Compress-Archive" not in text
    for ln in blocks.splitlines():
        assert not ln.strip().startswith("tar "), ln

    runbook = _read("stage3-runbook.md")
    for ln in runbook.splitlines():
        if "python3 -m tools.stage3 evidence target" in ln and "--stage 3" in ln:
            assert "--bundle-dir" in ln, ln
