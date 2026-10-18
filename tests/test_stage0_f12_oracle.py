# tests/test_stage0_f12_oracle.py
"""Stage 0 P1 Task T17：F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改為 seq／marker／CRC oracle（TDD 紅燈）。

依據：`docs/rust-refactor/plans/02-stage0-python-bc.md` Task T17 的 A1 失敗測試清單
（規格 §8.3、§10.4、§15.10；OD-13）。本檔只含失敗測試，不含產品碼。

- `realhw.wal_oracle`（本 Task 新增）一律於測試函式內經 `_oracle()` 延遲 import；
  F12 與 P0 case 模組同樣經 `_f12()`／`_p0()` 延遲 import，使本檔在實作前仍可 collection
  （P0 規則：不得在模組頂層 import 本 Phase 才新增的模組或名稱）。
- 依 P0 T14 規則：全檔資源 marker 為 `memory_only`（只用暫存目錄與行程內替身）；
  平台 marker 逐函式標註。`test_native_reader_does_not_block_rotate` 為 `windows_native`，
  在非 Windows 以 `pytest.skip` 呈現，屬「證據不足」，不得計為 PASS。
- 所有測試以替身 `sw` 與假時鐘驅動，不啟動 daemon、不開 socket、不碰真實 WAL 或裝置。
"""
from __future__ import annotations

import ast
import base64
import importlib
import json
import os
import sys
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]

# 資源 marker 放模組層（全檔為 memory_only）；平台 marker 放函式層（同檔混合平台時逐函式標註）
pytestmark = [pytest.mark.memory_only]

_WINDOWS_SKIP_REASON = "windows_native：請以 scripts/stage0/win-pytest.sh 執行（證據不足，非 PASS）"

# 歸檔名沿用產品 rotate 的 UTC 時間戳後綴格式（sw_core/wal.py `%Y%m%d-%H%M%S`）
ARCH1 = "raw.wal.ndjson.20261015-000000"
ARCH2 = "raw.wal.ndjson.20261015-000001"


def _ensure_paths() -> None:
    """把 repo 根與 regression 套件目錄放進 sys.path（冪等）。"""
    for entry in (str(REPO), str(REPO / "regression")):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _oracle():
    """延遲 import 本 Task 新增的 `realhw.wal_oracle`（實作前於此拋 ModuleNotFoundError）。"""
    _ensure_paths()
    return importlib.import_module("realhw.wal_oracle")


def _f12():
    """延遲 import 既有的 F12 case 模組（B1 才會補上 `import time`）。"""
    _ensure_paths()
    return importlib.import_module("serialwrap_regression.cases.f12_diagnostics")


def _p0():
    """延遲 import 既有的 P0 case 模組（`p0_wal_live` 的新判定於 B1 實作）。"""
    _ensure_paths()
    return importlib.import_module("realhw.cases.p0")


def _rec(seq: int, direction: str, payload: bytes, cmd_id: str | None = None, *, crc: str | None = None) -> dict[str, Any]:
    """組出一筆 WAL 紀錄（欄位對齊 T02 的 NDJSON 形狀；crc 可注入錯誤值以製造損壞）。"""
    return {"seq": seq, "dir": direction, "cmd_id": cmd_id, "com": "COM0", "len": len(payload),
            "crc32": crc or f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
            "payload_b64": base64.b64encode(payload).decode(), "loss_flag": False, "meta": {},
            "mono_ts_ns": 1, "source": "agent", "wall_ts": "2026-10-15T00:00:00+00:00"}


def _append(p: Path, *recs: dict[str, Any]) -> None:
    """以文字模式附加完整紀錄（每筆以 LF 結尾）。"""
    with open(p, "a", encoding="utf-8") as fp:
        for r in recs:
            fp.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")


class _FakeClock:
    """注入 sleep／monotonic，使 wait_for_marker 的期限判斷不依賴真實時間。"""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _wait(wo: Any, cursor: Any, *, marker: bytes, cmd_id: str | None, timeout_s: float = 1.0,
          allow_reset: bool = False) -> Any:
    clock = _FakeClock()
    return wo.wait_for_marker(cursor, marker=marker, cmd_id=cmd_id, timeout_s=timeout_s,
                              allow_reset=allow_reset, sleep=clock.sleep, monotonic=clock.monotonic)


class _FakeSw:
    """F12 case 用的 `ctx.sw` 替身：只回應 daemon status、doctor 與 submit。"""

    def __init__(self, wal_path: str, on_submit: Callable[[str, str], dict[str, Any]]) -> None:
        self._wal_path = wal_path
        self._on_submit = on_submit

    def run(self, *args: str, **kwargs: Any) -> dict[str, Any]:
        if args == ("daemon", "status"):
            return {"wal_path": self._wal_path}
        if args == ("doctor",):
            return {"checks": [{"check": "wal_dir"}]}
        raise AssertionError(args)

    def submit_and_wait(self, com: str, cmd: str, **kwargs: Any) -> dict[str, Any]:
        return self._on_submit(com, cmd)


def _submit_done(cmd_id: str, *, append_to: Path | None = None) -> Callable[[str, str], dict[str, Any]]:
    """回傳 submit 替身：可選擇在 WAL 追加一筆含 marker 的 TX 紀錄，再回傳 done。"""

    def _submit(com: str, cmd: str) -> dict[str, Any]:
        if append_to is not None:
            _append(append_to, _rec(2, "TX", (cmd + "\n").encode(), cmd_id))
        return {"status": "done", "cmd_id": cmd_id}

    return _submit


def _f12_ctx(wal_path: str, *, on_submit: Callable[[str, str], dict[str, Any]],
             timeouts: dict[str, Any] | None = None) -> SimpleNamespace:
    notes: dict[str, str] = {}
    return SimpleNamespace(
        sw=_FakeSw(wal_path, on_submit),
        note=lambda name, content: notes.__setitem__(name, content) or name,
        cfg={"boards": [{"com": "COM0"}], "timeouts": dict(timeouts or {})},
        notes=notes,
    )


# ---------------------------------------------------------------------------
# realhw.wal_oracle：判定規則 1～5、8 與 seq 語意
# ---------------------------------------------------------------------------

@pytest.mark.core_portable
def test_marker_found_with_valid_crc(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"boot"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "TX", b"echo MARKER_123456\n", "c1"))
    r = wo.wait_for_marker(cur, marker=b"MARKER_123456", cmd_id="c1", timeout_s=0.1, sleep=lambda s: None)
    assert r.ok is True and r.reason_code == "ok" and r.matched["seq"] == 2


@pytest.mark.core_portable
def test_corrupt_crc_fails(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "TX", b"echo MARKER_1\n", "c1", crc="00000000"))
    r = wo.wait_for_marker(cur, marker=b"MARKER_1", cmd_id="c1", timeout_s=0.1, sleep=lambda s: None)
    assert r.ok is False and r.reason_code == "wal_record_corrupt"


@pytest.mark.core_portable
def test_background_noise_is_not_enough(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"noise"), _rec(3, "RX", b"more noise"))
    t = [0.0]
    r = wo.wait_for_marker(cur, marker=b"MARKER_9", cmd_id="c9", timeout_s=1.0,
                           sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0])
    assert r.ok is False and r.reason_code == "wal_marker_not_found"


@pytest.mark.core_portable
def test_rotation_between_cursor_and_marker(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"pre-rotate"))
    os.replace(p, tmp_path / ARCH1)
    _append(p, _rec(3, "TX", b"echo MARKER_42\n", "c42"))
    r = wo.wait_for_marker(cur, marker=b"MARKER_42", cmd_id="c42", timeout_s=0.1, sleep=lambda s: None)
    assert r.ok is True and r.diagnostics["archives_scanned"] == [ARCH1]


@pytest.mark.core_portable
def test_unreadable_is_environment_fail():
    wo = _oracle()
    try:
        wo.open_cursor("/nonexistent/raw.wal.ndjson")
    except OSError:
        pass
    r = wo.wait_for_marker(wo.WalCursor("/nonexistent/raw.wal.ndjson", (0, 0), 0, None),
                           marker=b"M", cmd_id=None, timeout_s=0.0, sleep=lambda s: None)
    assert r.reason_code == "wal_unreadable"


@pytest.mark.core_portable
def test_partial_trailing_line_ignored_until_complete(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    line = json.dumps(_rec(2, "TX", b"echo MARKER_P\n", "cp"), sort_keys=True, separators=(",", ":"))
    half = len(line) // 2
    with open(p, "a", encoding="utf-8") as fp:
        fp.write(line[:half])  # 尾端無換行的半行：不得判為 corrupt，也不得被當成紀錄
    r1 = _wait(wo, cur, marker=b"MARKER_P", cmd_id="cp")
    assert r1.ok is False and r1.reason_code == "wal_marker_not_found"
    with open(p, "a", encoding="utf-8") as fp:
        fp.write(line[half:] + "\n")  # 補齊同一行後，同一 cursor 應可命中
    r2 = _wait(wo, cur, marker=b"MARKER_P", cmd_id="cp")
    assert r2.ok is True and r2.reason_code == "ok" and r2.matched["seq"] == 2


@pytest.mark.core_portable
def test_seq_regression_same_identity_fails(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"a"), _rec(2, "RX", b"b"))
    cur = wo.open_cursor(str(p))
    assert cur.last_seq == 2
    _append(p, _rec(1, "TX", b"echo MARKER_SR\n", "csr"))  # 同一檔案 identity 下 seq 小於 last_seq
    r = _wait(wo, cur, marker=b"MARKER_SR", cmd_id="csr")
    assert r.ok is False and r.reason_code == "wal_seq_regressed"


@pytest.mark.core_portable
def test_reset_new_file_seq_restart_allowed(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"boot"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"pre-reset"))
    os.replace(p, tmp_path / ARCH1)  # reset：歸檔後新檔 identity 改變
    _append(p, _rec(1, "RX", b"boot2"), _rec(2, "TX", b"echo MARKER_RS\n", "crs"))  # 新檔 seq 從 1 重算
    r = _wait(wo, cur, marker=b"MARKER_RS", cmd_id="crs", allow_reset=True)
    assert r.ok is True and r.reason_code == "ok"


@pytest.mark.core_portable
def test_rotate_with_seq_reset_fails(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"boot"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"pre-rotate"))
    os.replace(p, tmp_path / ARCH1)  # rotate：rotate 不重置 seq
    _append(p, _rec(1, "RX", b"boot2"), _rec(2, "TX", b"echo MARKER_RT\n", "crt"))  # 新檔 seq 從 1 重算
    r = _wait(wo, cur, marker=b"MARKER_RT", cmd_id="crt", allow_reset=False)
    assert r.ok is False and r.reason_code == "wal_seq_discontinuous"


@pytest.mark.core_portable
def test_double_rotate_reads_intermediate_archive(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"pre-rot-1"))
    os.replace(p, tmp_path / ARCH1)
    _append(p, _rec(3, "TX", b"echo MARKER_DR\n", "cdr"))  # marker 落在中間歸檔
    os.replace(p, tmp_path / ARCH2)
    _append(p, _rec(4, "RX", b"after-rot-2"))
    r = _wait(wo, cur, marker=b"MARKER_DR", cmd_id="cdr")
    assert r.ok is True and r.reason_code == "ok"
    assert r.diagnostics["archives_scanned"] == [ARCH1, ARCH2]
    assert [json.loads(line)["seq"] for line in r.raw_lines] == [2, 3, 4]  # seq 跨三檔連續


@pytest.mark.core_portable
def test_intermediate_archive_missing_is_incomplete(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"pre-rot-1"))
    os.replace(p, tmp_path / ARCH1)
    _append(p, _rec(3, "RX", b"mid"))
    os.replace(p, tmp_path / ARCH2)
    (tmp_path / ARCH2).unlink()  # 刪除中間歸檔
    _append(p, _rec(4, "TX", b"echo MARKER_IM\n", "cim"))  # marker 在 current 檔
    r = _wait(wo, cur, marker=b"MARKER_IM", cmd_id="cim")
    assert r.ok is False and r.reason_code == "wal_evidence_incomplete"


@pytest.mark.core_portable
def test_corrupt_line_in_archive_fails(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "RX", b"bad", crc="00000000"))  # 歸檔中的新增區段有 CRC 錯誤
    os.replace(p, tmp_path / ARCH1)
    _append(p, _rec(3, "TX", b"echo MARKER_CA\n", "cca"))
    r = _wait(wo, cur, marker=b"MARKER_CA", cmd_id="cca")
    assert r.ok is False and r.reason_code == "wal_record_corrupt"


@pytest.mark.core_portable
def test_crlf_lines_accepted(tmp_path):
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    line = json.dumps(_rec(2, "TX", b"echo MARKER_CR\n", "ccr"), sort_keys=True, separators=(",", ":"))
    with open(p, "ab") as fp:
        fp.write(line.encode("utf-8") + b"\r\n")  # Windows 舊檔行尾
    r = _wait(wo, cur, marker=b"MARKER_CR", cmd_id="ccr")
    assert r.ok is True and r.reason_code == "ok" and r.matched["seq"] == 2


# ---------------------------------------------------------------------------
# F12 f12-wal-path-live：判定結果映射
# ---------------------------------------------------------------------------

@pytest.mark.core_portable
def test_f12_case_passes_without_mtime_change(tmp_path, monkeypatch):
    f12 = _f12()
    p = tmp_path / "raw.wal.ndjson"; _append(p, _rec(1, "RX", b"x"))
    st = os.stat(p)

    class Sw:
        def run(self, *a):
            if a == ("daemon", "status"): return {"wal_path": str(p)}
            if a == ("doctor",): return {"checks": [{"check": "wal_dir"}]}
            raise AssertionError(a)

        def submit_and_wait(self, com, cmd):
            _append(p, _rec(2, "TX", (cmd + "\n").encode(), "cid-1"))
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))     # mtime 不前進
            return {"status": "done", "cmd_id": "cid-1"}

    notes = {}
    ctx = SimpleNamespace(sw=Sw(), note=lambda n, c: notes.__setitem__(n, c) or n,
                          cfg={"boards": [{"com": "COM0"}], "timeouts": {}})
    monkeypatch.setattr(f12.time, "sleep", lambda s: None)
    result = f12.f12_wal_path_live(ctx)
    assert result.verdict == "PASS"
    assert "wal-oracle.json" in notes


@pytest.mark.core_portable
def test_f12_case_wal_unreadable_is_environment_fail(tmp_path, monkeypatch):
    f12 = _f12()
    # 以目錄代表「存在、可 stat、卻無法以檔案讀取」的 wal_path：跨平台且不受 root 權限影響
    unreadable = tmp_path / "wal-is-a-directory"
    unreadable.mkdir()
    ctx = _f12_ctx(str(unreadable), on_submit=_submit_done("cid-u"))
    # 以全域 time.sleep 替身免等待：紅燈應落在判定結果，而非替身設定（不依賴 f12 是否已 import time）
    monkeypatch.setattr(time, "sleep", lambda s: None)
    result = f12.f12_wal_path_live(ctx)
    assert result.verdict == "FAIL"
    assert result.category == "environment"
    assert result.reason_code == "wal_unreadable"


@pytest.mark.core_portable
def test_f12_case_marker_missing_fails(tmp_path, monkeypatch):
    f12 = _f12()
    wal = tmp_path / "raw.wal.ndjson"; _append(wal, _rec(1, "RX", b"x"))
    # submit 完成但 WAL 中沒有 marker；timeout 設 0 讓期限內只掃一次，避免真實等待
    ctx = _f12_ctx(str(wal), on_submit=_submit_done("cid-m"), timeouts={"wal_oracle_s": 0.0})
    monkeypatch.setattr(time, "sleep", lambda s: None)  # 同上：全域替身，紅燈落在判定結果
    result = f12.f12_wal_path_live(ctx)
    assert result.verdict == "FAIL"
    assert result.category == "test"
    assert result.reason_code == "wal_marker_not_found"


# ---------------------------------------------------------------------------
# L3 p0-wal-live：改讀 daemon status.wal_path
# ---------------------------------------------------------------------------

@pytest.mark.core_portable
def test_p0_wal_live_uses_status_wal_path(tmp_path, monkeypatch):
    p0 = _p0()
    wal = tmp_path / "raw.wal.ndjson"
    _append(wal, _rec(1, "RX", b"boot"))
    calls: list[tuple[Any, ...]] = []

    class Sw:
        def run(self, *args, **kwargs):
            calls.append(tuple(args))
            if args == ("daemon", "status"):
                return {"wal_path": str(wal)}
            return {}

        def submit_and_wait(self, com, cmd, **kwargs):
            calls.append(("submit", com, cmd))
            _append(wal, _rec(2, "TX", (cmd + "\n").encode(), "cid-p0"))
            return {"status": "done", "cmd_id": "cid-p0"}

    def _no_home():
        raise AssertionError("p0_wal_live 不得存取 Path.home()，必須改讀 daemon status.wal_path")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    monkeypatch.setattr(p0.time, "sleep", lambda s: None)
    ctx = SimpleNamespace(sw=Sw(), note=lambda name, content: name,
                          cfg={"boards": [{"com": "COM0"}], "timeouts": {}})
    result = p0.p0_wal_live(ctx)
    assert ("daemon", "status") in calls
    assert result.verdict == "PASS"


# ---------------------------------------------------------------------------
# 測試工具獨立性與 Windows 原生分享旗標
# ---------------------------------------------------------------------------

@pytest.mark.core_portable
def test_wal_oracle_does_not_import_sw_core():
    src_path = REPO / "realhw" / "wal_oracle.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offending += [alias.name for alias in node.names if alias.name.split(".")[0] == "sw_core"]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "sw_core":
            offending.append(node.module)
    assert offending == []


@pytest.mark.windows_native
def test_native_reader_does_not_block_rotate(tmp_path):
    if os.name != "nt":
        pytest.skip(_WINDOWS_SKIP_REASON)
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    p.write_bytes(b"x" * (16 * 1024 * 1024))
    started = threading.Event()
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                wo.read_shared(str(p), 0)  # CreateFileW(FILE_SHARE_READ|WRITE|DELETE)
            except FileNotFoundError:
                return  # 檔案已被輪替走，屬預期結束
            except BaseException as exc:  # noqa: BLE001 - 收集於主執行緒斷言
                errors.append(exc)
                return
            started.set()

    t = threading.Thread(target=reader)
    t.start()
    try:
        assert started.wait(timeout=10)
        os.replace(p, tmp_path / ARCH1)  # 讀取器開檔期間，產品 rotate 不得被阻擋
    finally:
        stop.set()
        t.join(timeout=60)
    assert not errors


# ---------------------------------------------------------------------------
# 審閱追加回歸測試（Reviewer A 缺陷 1～5、7 與 Reviewer B 缺陷 1～3）
# ---------------------------------------------------------------------------

@pytest.mark.core_portable
def test_reset_allowed_when_cursor_archive_has_no_new_records(tmp_path):
    """Reviewer A 2a / B 1：cursor 所在歸檔在 offset 後無新增紀錄時，合法 reset 仍須 PASS。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot"), _rec(2, "RX", b"ready"))
    cur = wo.open_cursor(str(p))
    os.replace(p, tmp_path / ARCH1)  # reset：歸檔在游標之後無新紀錄
    _append(p, _rec(1, "RX", b"boot2"), _rec(2, "TX", b"echo MARKER_RS0\n", "crs0"))
    r = _wait(wo, cur, marker=b"MARKER_RS0", cmd_id="crs0", allow_reset=True)
    assert r.ok is True and r.reason_code == "ok"


@pytest.mark.core_portable
def test_rotate_with_seq_reset_fails_when_cursor_archive_has_no_new_records(tmp_path):
    """Reviewer A 2b / B 1：cursor 所在歸檔無新紀錄時，錯誤的 rotate seq 重置須回傳 discontinuous。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot"), _rec(2, "RX", b"ready"))
    cur = wo.open_cursor(str(p))
    os.replace(p, tmp_path / ARCH1)  # rotate：無新紀錄
    _append(p, _rec(1, "RX", b"boot2"), _rec(2, "TX", b"echo MARKER_RT0\n", "crt0"))
    r = _wait(wo, cur, marker=b"MARKER_RT0", cmd_id="crt0", allow_reset=False)
    assert r.ok is False and r.reason_code == "wal_seq_discontinuous"


@pytest.mark.core_portable
def test_missing_intermediate_archive_when_cursor_archive_has_no_new_records(tmp_path):
    """Reviewer A 2c / B 1：首個歸檔無新紀錄但中間歸檔遺失時，current 檔有 marker 亦不得 PASS。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot"))
    cur = wo.open_cursor(str(p))
    os.replace(p, tmp_path / ARCH1)  # rotate 1（ARCH1 無新紀錄）
    _append(p, _rec(2, "RX", b"mid"))
    os.replace(p, tmp_path / ARCH2)  # rotate 2
    (tmp_path / ARCH2).unlink()  # 刪除中間歸檔
    _append(p, _rec(3, "TX", b"echo MARKER_S2\n", "cs2"))  # current 檔含 marker
    r = _wait(wo, cur, marker=b"MARKER_S2", cmd_id="cs2")
    assert r.ok is False and r.reason_code == "wal_evidence_incomplete"


@pytest.mark.core_portable
def test_archive_missing_with_reset_is_incomplete_evidence(tmp_path):
    """Reviewer A 2d：歸檔遺失又伴隨 seq 重置時，判定須為 wal_evidence_incomplete（環境問題）。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot"))
    cur = wo.open_cursor(str(p))
    os.replace(p, tmp_path / ARCH1)
    (tmp_path / ARCH1).unlink()  # 歸檔消失
    _append(p, _rec(1, "RX", b"boot2"), _rec(2, "TX", b"echo MARKER_AMR\n", "camr"))
    r = _wait(wo, cur, marker=b"MARKER_AMR", cmd_id="camr", allow_reset=True)
    assert r.ok is False and r.reason_code == "wal_evidence_incomplete"


@pytest.mark.core_portable
def test_archive_sorting_handles_numeric_suffix_order(tmp_path):
    """Reviewer A 3i / B 3：同秒歸檔後綴須依 (時間, 數字後綴) 升冪排序（.2 < .9 < .10）。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"init"))
    cur = wo.open_cursor(str(p))

    # 模擬同秒內連續輪替，產生 .2、.9、.10 三個歸檔
    arch_base = "raw.wal.ndjson.20261015-000000"
    os.replace(p, tmp_path / arch_base)
    _append(p, _rec(2, "RX", b"in_2"))
    os.replace(p, tmp_path / f"{arch_base}.2")
    _append(p, _rec(3, "RX", b"in_9"))
    os.replace(p, tmp_path / f"{arch_base}.9")
    _append(p, _rec(4, "RX", b"in_10"))
    os.replace(p, tmp_path / f"{arch_base}.10")
    _append(p, _rec(5, "TX", b"echo MARKER_NUM\n", "cnum"))

    r = _wait(wo, cur, marker=b"MARKER_NUM", cmd_id="cnum")
    assert r.ok is True and r.reason_code == "ok"
    assert r.diagnostics["archives_scanned"] == [
        arch_base,
        f"{arch_base}.2",
        f"{arch_base}.9",
        f"{arch_base}.10",
    ]


@pytest.mark.core_portable
def test_archive_selection_ignores_non_archive_siblings(tmp_path):
    """Reviewer A 3ii：忽略同目錄非標準歸檔後綴之旁系檔案（.lock、.bak、.tmp）。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"init"))
    cur = wo.open_cursor(str(p))

    os.replace(p, tmp_path / ARCH1)
    # 建立旁系檔案
    (tmp_path / "raw.wal.ndjson.lock").write_text("dummy lock content", encoding="utf-8")
    (tmp_path / "raw.wal.ndjson.bak").write_text("dummy bak content", encoding="utf-8")
    _append(p, _rec(2, "TX", b"echo MARKER_SIB\n", "csib"))

    r = _wait(wo, cur, marker=b"MARKER_SIB", cmd_id="csib")
    assert r.ok is True and r.reason_code == "ok"
    assert r.diagnostics["archives_scanned"] == [ARCH1]


@pytest.mark.core_portable
def test_cursor_snapshot_consistency_with_racing_appender(tmp_path):
    """Reviewer A 1 / B 2：open_cursor 時寫入者在 stat 與 read 間追加紀錄，offset 與 last_seq 須一致。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot"))

    orig_read = wo.read_shared
    appended = {"done": False}

    def racing_read(path: str, offset: int) -> bytes:
        if not appended["done"]:
            appended["done"] = True
            _append(Path(path), _rec(2, "RX", b"appended mid-flight"))
        return orig_read(path, offset)

    wo.read_shared = racing_read
    try:
        cur = wo.open_cursor(str(p))
    finally:
        wo.read_shared = orig_read

    # offset 必須與快照資料長度對齊，last_seq 為快照最後一筆 seq (2)
    assert cur.last_seq == 2
    _append(p, _rec(3, "TX", b"echo MARKER_RACE\n", "crace"))
    r = _wait(wo, cur, marker=b"MARKER_RACE", cmd_id="crace")
    assert r.ok is True and r.reason_code == "ok"


@pytest.mark.core_portable
def test_cursor_offset_mid_line_drops_partial_fragment(tmp_path):
    """Reviewer A 1：游標落在行中時，丢棄前一行殘段，不誤判 wal_record_corrupt。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot"))
    line = json.dumps(_rec(2, "RX", b"inflight"), sort_keys=True, separators=(",", ":"))
    with open(p, "a", encoding="utf-8") as fp:
        fp.write(line[:30])  # 半行寫入
    cur = wo.open_cursor(str(p))
    with open(p, "a", encoding="utf-8") as fp:
        fp.write(line[30:] + "\n")  # 補齊
    _append(p, _rec(3, "TX", b"echo MARKER_MID\n", "cmid"))
    r = _wait(wo, cur, marker=b"MARKER_MID", cmd_id="cmid")
    assert r.ok is True and r.reason_code == "ok"


@pytest.mark.core_portable
def test_unlink_and_recreate_same_inode_is_incomplete_evidence(tmp_path):
    """Reviewer A 4：external unlink 後重建若重用同 inode，偵測內容改變並回報 evidence_incomplete。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"boot1"), _rec(2, "RX", b"boot2"))
    cur = wo.open_cursor(str(p))

    # 模擬外部刪除後重建物件：內容不同但模擬相同 identity
    p.unlink()
    _append(p, _rec(1, "RX", b"recreated_unrelated_content"), _rec(2, "TX", b"echo MARKER_UNL\n", "cunl"))

    r = _wait(wo, cur, marker=b"MARKER_UNL", cmd_id="cunl")
    assert r.ok is False and r.reason_code == "wal_evidence_incomplete"


@pytest.mark.core_portable
def test_mtime_before_captured_at_open_cursor(tmp_path):
    """Reviewer A 5：diagnostics 中的 mtime_before 須在 open_cursor 時取樣。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"x"))
    cur = wo.open_cursor(str(p))
    assert cur.mtime_ns is not None

    time.sleep(0.01)
    _append(p, _rec(2, "TX", b"echo MARKER_MT\n", "cmt"))
    r = _wait(wo, cur, marker=b"MARKER_MT", cmd_id="cmt")
    assert r.ok is True
    assert r.diagnostics["mtime_before"] <= r.diagnostics["mtime_after"]


@pytest.mark.core_portable
def test_transient_read_error_retries_until_deadline(tmp_path):
    """Reviewer A 7：暫態讀取失敗（如輪替瞬間之 FileNotFoundError）在期限內重試不致提早放棄。"""
    wo = _oracle()
    p = tmp_path / "raw.wal.ndjson"
    _append(p, _rec(1, "RX", b"init"))
    cur = wo.open_cursor(str(p))
    _append(p, _rec(2, "TX", b"echo MARKER_TRANS\n", "ctrans"))

    orig_read = wo.read_shared
    attempts = {"count": 0}

    def flaky_read(path: str, offset: int) -> bytes:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise FileNotFoundError(2, "transient file rotation race", path)
        return orig_read(path, offset)

    wo.read_shared = flaky_read
    try:
        r = _wait(wo, cur, marker=b"MARKER_TRANS", cmd_id="ctrans", timeout_s=5.0)
    finally:
        wo.read_shared = orig_read

    assert r.ok is True and r.reason_code == "ok"
    assert attempts["count"] >= 2

