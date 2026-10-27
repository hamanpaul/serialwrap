"""state.json 雙 artifact 交叉讀寫（P5-T15）：受測 artifact 寫出的 state.json 由 Python 基線讀回（rollback 驗證）。

- `test_harness_smoke`（Step 0）：harness 本身可用（start_daemon → COM0 READY → stop()==0）。
- `test_rust_written_state_is_loaded_by_python`：受測 artifact 釋放 COM0 後停機，state.json 位元組格式
  （尾端換行、5 個 key 依序）與 Python 相同；換成 Python 基線沿用同一組目錄後 COM0 還原為 RELEASED。

Rust 輪（SERIALWRAP_DAEMON_EXE 指向 Rust serialwrapd）於 Task 19 daemon_main 落地前預期紅燈（daemon 啟動即結束）。
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = [
    pytest.mark.posix_pty,
    pytest.mark.process_rpc,
    pytest.mark.skipif(os.name != "posix", reason="需要 POSIX PTY（FakeTarget）"),
]


def test_harness_smoke(slice21_daemon_factory, artifact_identity, tmp_path):
    d = slice21_daemon_factory(tmp_path / "smoke")
    s = d.wait_state("COM0", "READY")
    assert s["com"] == "COM0"
    assert d.stop() == 0
    assert artifact_identity["daemon"] == d.daemon_cmd


def test_rust_written_state_is_loaded_by_python(slice21_daemon_factory, python_daemon_exe, tmp_path):
    d = slice21_daemon_factory(tmp_path / "a")                       # 受測 artifact（Rust 或 Python）
    d.wait_state("COM0", "READY")
    d.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "x")
    d.stop()
    raw = (d.state_dir / "state.json").read_bytes()
    assert raw.endswith(b"\n")
    assert list(json.loads(raw)) == ["aliases", "bindings", "profile_detected", "profile_pins", "released"]
    p = slice21_daemon_factory(tmp_path / "a", daemon=python_daemon_exe, reuse_dirs=True)   # 換成 Python 基線讀同一份
    s = p.wait_state("COM0", "RELEASED", timeout=10)
    assert s["released_by"] == "agent:t" and s["released_reason"] == "x"


CANONICAL_EMPTY = b'{"aliases":{},"bindings":{},"profile_detected":{},"profile_pins":{},"released":{}}\n'


def test_aliases_not_dict_fails_startup_like_python(slice21_daemon_factory, tmp_path):
    """#12（KEEP-fail）：aliases 列無法 dict(v) → 啟動即失敗；golden 取自 Python 基線實測（rc 1、traceback）。"""
    from harness import DaemonStartError, prepare_root

    body = b'{"aliases":{"x":1}}'
    root = prepare_root(tmp_path / "bad-alias", state_json=body)
    with pytest.raises(DaemonStartError) as ei:
        slice21_daemon_factory(root, reuse_dirs=True)
    lines = ei.value.stderr.splitlines()
    assert ei.value.rc == 1
    assert lines[0] == "Traceback (most recent call last):"
    assert lines[-1] == "TypeError: 'int' object is not iterable"
    assert (root / "state" / "state.json").read_bytes() == body          # 崩潰於 _save_state 之前：原檔不動
    assert not (root / "state" / "state.json.corrupt").exists()


def test_non_dict_json_not_backed_up(slice21_daemon_factory, tmp_path):
    """#95：合法 JSON 但非 object → 以空狀態啟動、不產生備份；啟動後重寫為 5 個 key（#13）。"""
    from harness import prepare_root

    root = prepare_root(tmp_path / "list", state_json=b"[1,2]")
    d = slice21_daemon_factory(root, reuse_dirs=True)
    d.wait_state("COM0", "READY")
    names = sorted(p.name for p in d.state_dir.iterdir())
    assert names == ["state.json"]
    obj = json.loads((d.state_dir / "state.json").read_bytes())
    assert list(obj) == ["aliases", "bindings", "profile_detected", "profile_pins", "released"]
    assert obj["released"] == {} and obj["bindings"] == {}


def test_startup_always_rewrites_state_json(slice21_daemon_factory, tmp_path):
    """#13：啟動完成 init 後一律寫回 state.json（即使內容相同，mtime 前進）。"""
    from harness import prepare_root

    root = prepare_root(tmp_path / "touch", state_json=CANONICAL_EMPTY)
    sp = root / "state" / "state.json"
    os.utime(sp, ns=(1_000_000_000, 1_000_000_000))
    d = slice21_daemon_factory(root, reuse_dirs=True)
    assert sp.stat().st_mtime_ns > 1_000_000_000
    d.wait_state("COM0", "READY")
    assert sp.read_bytes().endswith(b"\n")
