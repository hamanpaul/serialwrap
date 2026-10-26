"""`scripts/rust/check_p6_prereqs.py` 的旁路防護（parity-amendments-2026-10-26 #180，P6 審查 F2）。

`--max-task N` 只把 Task ≤ N 的缺漏當阻擋，輸出卻以 `OK` 結尾，容易被誤讀成全表通過；
要求：`--max-task` 模式末行固定印「仍有 N 項後續缺漏」，並提供 `--strict`（全表模式、缺任何一項非零 exit），
切片出口（Task 13 `s22_gate_check.py`）只能以全表模式呼叫。腳本以 importlib 在行程內載入。
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("check_p6_prereqs", ROOT / "scripts/rust/check_p6_prereqs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _root_with(tmp_path: pathlib.Path, mod, max_task: int) -> pathlib.Path:
    """只複製「最早消費 Task ≤ max_task」各列的檔案，其餘缺漏。"""
    for rel, _pat, _src, task in mod.CHECKS:
        if task <= max_task and (ROOT / rel).is_file():
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, dst)
    return tmp_path


def _later(mod, root: pathlib.Path, max_task: int) -> int:
    n = sum(1 for *_x, task in mod.check(root) if task > max_task)
    assert n > 0, "合成 root 應留有後續缺漏"
    return n


def test_max_task_last_line_reports_remaining(tmp_path, capsys):
    m = _mod()
    root = _root_with(tmp_path, m, 5)
    assert m.main(["--root", str(root), "--max-task", "5"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == f"[check_p6_prereqs] 仍有 {_later(m, root, 5)} 項後續缺漏（--max-task 5 非全表結果，不得作為切片出口依據）"
    assert not any(line.endswith("OK") for line in lines), "--max-task 模式不得以 OK 結尾"


def test_max_task_last_line_even_when_nothing_remains(capsys):
    m = _mod()
    rc = m.main(["--root", str(ROOT), "--max-task", "99"])
    lines = capsys.readouterr().out.strip().splitlines()
    remaining = len(m.check(ROOT))
    assert rc == (0 if remaining == 0 else 1)
    if rc == 0:
        assert lines[-1].startswith("[check_p6_prereqs] 仍有 0 項後續缺漏")


def test_strict_is_full_table_nonzero(tmp_path, capsys):
    m = _mod()
    root = _root_with(tmp_path, m, 5)
    assert m.main(["--root", str(root), "--strict"]) == 1
    out = capsys.readouterr().out
    assert "MISSING (阻擋)" in out and "MISSING (後續)" not in out


def test_strict_rejects_max_task(tmp_path):
    m = _mod()
    with pytest.raises(SystemExit) as exc:
        m.main(["--root", str(tmp_path), "--strict", "--max-task", "5"])
    assert exc.value.code == 2


def test_blocked_upstream_registry_names_t10_t11():
    m = _mod()
    assert {10, 11} <= set(m.BLOCKED_UPSTREAM), "T10／T11 須登記為 BLOCKED-UPSTREAM"
    for task, reason in m.BLOCKED_UPSTREAM.items():
        assert reason.strip(), task
