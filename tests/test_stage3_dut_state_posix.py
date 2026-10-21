"""P9-T5 POSIX 專屬行為：/tmp 暫存 RUN_DIR（AF_UNIX 路徑上限）與 run-dir.txt 記錄的清理。

自 test_stage3_dut_state.py 分出：core_portable 檔內不得含平台 skip（test_resource_markers 稽核）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import stage3_fakes as fakes
from tools.stage3 import ab_manifest, dut_state

pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]


def _load(tmp_path: Path, **overrides):
    return ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path, **overrides))


@pytest.fixture
def instances():
    """收集測試中建立的實例，結束時清理 POSIX 的 /tmp RUN_DIR（cleanup 須可重入）。"""
    items: list[dut_state.Instance] = []
    yield items
    for inst in items:
        dut_state.cleanup_instance(inst)


def test_cleanup_removes_tmp_run_dir(tmp_path, instances):
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    run_dir = Path((inst.root / "run-dir.txt").read_text(encoding="utf-8").strip())
    assert run_dir.is_dir()
    assert dut_state.cleanup_instance(inst) == []
    assert not run_dir.exists()


def test_cleanup_refuses_tampered_record(tmp_path, instances):
    """補充：run-dir.txt 被改指向非 /tmp/sw-ab-* 的路徑時拒絕刪除，且該路徑內容保留。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    record = inst.root / "run-dir.txt"
    genuine = record.read_text(encoding="utf-8")
    victim = tmp_path / "victim"
    victim.mkdir()
    record.write_text(f"{victim}\n", encoding="utf-8")
    assert dut_state.cleanup_instance(inst) == ["run dir cleanup failed"]
    assert victim.is_dir()
    record.write_text(genuine, encoding="utf-8")   # 還原，讓 fixture 清除真正的暫存目錄


def test_cleanup_reports_undecodable_record(tmp_path, instances):
    """補充：run-dir.txt 內容非合法 UTF-8 時回報 `run dir cleanup failed`，不得以例外穿出 cleanup。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    record = inst.root / "run-dir.txt"
    genuine = record.read_text(encoding="utf-8")
    record.write_bytes(b"\xff\xfe\x00corrupt")
    assert dut_state.cleanup_instance(inst) == ["run dir cleanup failed"]
    record.write_text(genuine, encoding="utf-8")   # 還原，讓 fixture 清除真正的暫存目錄


def test_leftover_dangling_state_symlink_detected(tmp_path, instances):
    """補充：state.json 為懸空 symlink 時仍視為殘留（以 lexists 判斷，而非 exists）。"""
    m = _load(tmp_path)
    inst = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out", base_env={})
    instances.append(inst)
    os.symlink(tmp_path / "nowhere.json", inst.root / "state" / "state.json")
    assert dut_state.verify_clean(m, inst, m.sides[0]) == ["state.json present"]
