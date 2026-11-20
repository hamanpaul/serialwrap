"""`scripts/rust/check_p8_prereqs.py`（P8 Task 0 上游介面核對）的行為測試。

(1) 真實 tree：上游（P0～P7）零缺漏、`--max-task 1` 時 Task 0 的產物（介面核對＋黑箱 fixture）必須已存在；
    每列 PLANNED 的輸出狀態與該符號實際是否存在一致（PLANNED:Tn＝缺、LANDED:Tn＝在）——隨後續 Task 合入仍成立。
(2) 合成 root（替換 CHECKS／PLANNED／DIFFERS 表）：PLANNED 只在 `--max-task M`（n < M）或 `--strict` 時阻擋、
    Task M 自己的產物不擋 Task M；上游缺漏才影響 exit code；`--max-task` 末行固定印「仍有 N 項後續缺漏」不以 OK 結尾
    （check_p6_prereqs #180 旁路防護）；骨架與形狀差異只列資訊；glob 檔名可跨巢狀檔案比對。
腳本以 importlib 在行程內載入。stdlib＋pytest。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("check_p8_prereqs", ROOT / "scripts/rust/check_p8_prereqs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.strip().splitlines()


# ---------------------------------------------------------------- (1) 真實 tree
def test_real_tree_full_table_has_no_upstream_missing(capsys):
    m = _mod()
    assert m.main(["--root", str(ROOT)]) == 0
    lines = _lines(capsys)
    assert not [ln for ln in lines if ln.startswith("MISSING")], "上游（P0～P7）不得有缺漏"
    assert lines[-1].startswith("[check_p8_prereqs] OK")


def test_real_tree_task0_deliverables_landed(capsys):
    """`--max-task 1`：PLANNED:T0（介面核對＋黑箱 fixture）已合入才可開工 Task 1。"""
    m = _mod()
    assert m.main(["--root", str(ROOT), "--max-task", "1"]) == 0, capsys.readouterr().out
    lines = _lines(capsys)
    assert lines[-1].startswith("[check_p8_prereqs] 仍有 ")
    assert lines[-1].endswith("項後續缺漏（--max-task 1 非全表結果，不得作為切片出口依據）")
    assert any(ln.startswith("LANDED:T0 tests/contract_blackbox/p8_fixtures.py") for ln in lines)


def test_real_tree_planned_status_matches_presence(capsys):
    m = _mod()
    m.main(["--root", str(ROOT)])
    out = capsys.readouterr().out
    _missing, _skel, _diff, planned = m.check(ROOT)
    assert {row[0] for row in planned} >= {2, 6, 7, 8, 10, 12, 14}, "PLANNED 表至少涵蓋 brief 列出的 T2/T6/T7/T8/T10/T12/T14"
    for task, rel, pat, _note, present in planned:
        tag = "LANDED" if present else "PLANNED"
        assert f"{tag}:T{task} {rel} :: {pat}" in out, (task, rel, pat)


def test_tables_are_well_formed():
    m = _mod()
    for rel, pat, src, task, fn in m.CHECKS:
        re.compile(pat)
        assert src and isinstance(task, int) and task >= 0, (rel, pat)
    for task, rel, pat, fn, note in m.PLANNED:
        re.compile(pat)
        assert 0 <= task <= 15 and note, (rel, pat)
    for rel, pat, note in m.DIFFERS:
        re.compile(pat)
        assert note.startswith("#"), (rel, note)
        assert "*" in rel or (ROOT / rel).is_file(), f"DIFFERS 列的檔案不存在：{rel}"


# ---------------------------------------------------------------- (2) 合成 root
@pytest.fixture
def synth(tmp_path, monkeypatch):
    m = _mod()
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "up.rs").write_text("pub fn upstream_ok() {}\npub fn skel() {\n    todo!(\"P5\")\n}\n", encoding="utf-8")
    (tmp_path / "src" / "deep" / "landed.rs").write_text("pub struct LandedThing;\n", encoding="utf-8")
    monkeypatch.setattr(m, "CHECKS", [
        ("src/up.rs", r"pub fn upstream_ok\b", "P5", 1, None),
        ("src/up.rs", r"pub fn skel\b", "P5", 3, "skel"),
    ])
    monkeypatch.setattr(m, "PLANNED", [
        (2, "src/**/*.rs", r"pub struct LandedThing\b", None, "T2 產物"),
        (6, "src/later.rs", r"pub fn later\b", None, "T6 產物"),
    ])
    monkeypatch.setattr(m, "DIFFERS", [("src/up.rs", r"todo!\(\"P5\"\)", "#1 形狀差異")])
    return m, tmp_path


def test_full_mode_planned_missing_is_not_blocking(synth, capsys):
    m, root = synth
    assert m.main(["--root", str(root)]) == 0
    lines = _lines(capsys)
    assert "LANDED:T2 src/**/*.rs :: pub struct LandedThing\\b（T2 產物）" in lines
    assert any(ln.startswith("PLANNED:T6 src/later.rs") for ln in lines)
    assert "SKELETON src/up.rs :: fn skel（P5 尚未實作；P8 Task 3 起需要其行為）" in lines
    assert "DIFFERS src/up.rs :: #1 形狀差異" in lines
    assert lines[-1].startswith("[check_p8_prereqs] OK")


def test_max_task_own_planned_not_blocking(synth, capsys):
    m, root = synth
    assert m.main(["--root", str(root), "--max-task", "6"]) == 0
    lines = _lines(capsys)
    assert lines[-1] == "[check_p8_prereqs] 仍有 1 項後續缺漏（--max-task 6 非全表結果，不得作為切片出口依據）"
    assert not any(ln.endswith("OK") or "OK（" in ln for ln in lines), "--max-task 模式不得以 OK 結尾"


def test_max_task_after_planned_task_blocks(synth, capsys):
    m, root = synth
    assert m.main(["--root", str(root), "--max-task", "7"]) == 1
    out = capsys.readouterr().out
    assert "MISSING (阻擋) src/later.rs :: pub fn later\\b（P8 T6 產物「T6 產物」尚未合入；P8 Task 7 的前置）" in out
    assert "仍有 0 項後續缺漏" in out


def test_strict_requires_all_planned(synth, capsys):
    m, root = synth
    assert m.main(["--root", str(root), "--strict"]) == 1
    out = capsys.readouterr().out
    assert "MISSING (阻擋) src/later.rs" in out and "切片出口" in out
    (root / "src" / "later.rs").write_text("pub fn later() {}\n", encoding="utf-8")
    assert m.main(["--root", str(root), "--strict"]) == 0
    assert _lines(capsys)[-1] == "[check_p8_prereqs] OK（全表＋全部 PLANNED）"


def test_upstream_missing_blocks_only_within_max_task(synth, capsys):
    m, root = synth
    (root / "src" / "up.rs").write_text("pub fn skel() {}\n", encoding="utf-8")   # upstream_ok（Task 1）消失
    assert m.main(["--root", str(root)]) == 1
    assert "MISSING (阻擋) src/up.rs :: pub fn upstream_ok\\b（上游 P5；P8 Task 1 需要）" in capsys.readouterr().out
    assert m.main(["--root", str(root), "--max-task", "0"]) == 0
    out = capsys.readouterr().out
    assert "MISSING (後續) src/up.rs" in out and "不擋 --max-task 0" in out


def test_strict_and_max_task_are_mutually_exclusive(synth):
    m, root = synth
    with pytest.raises(SystemExit) as exc:
        m.main(["--root", str(root), "--strict", "--max-task", "5"])
    assert exc.value.code == 2
