# tests/test_stage0_wal_handle.py
"""Stage 0 P1 Task T02：BC-01a WAL 常駐 handle、每筆 flush、唯一 close、DF-17（TDD 紅燈）。

依據：`docs/rust-refactor/plans/02-stage0-python-bc.md` Task T02 的 A1 失敗測試清單
（規格 §8.1、§8.2；盤點 04 §1.4）。本檔只含失敗測試，不含產品碼。

- `sw_core.wal_fs`（本 Task 新增模組）一律於測試函式內經 `_load()` 延遲 import，
  使本檔在實作前仍可 collection；Stage A 下各測試應因 `ModuleNotFoundError` 紅燈。
- 依 P0 T14 規則：本檔混合 `core_portable` 與 `windows_native`，**模組層不放平台 marker**，
  平台 marker 逐函式標註；資源 marker 統一為 `memory_only`（只用暫存目錄與行程內物件）。
- 替身 `CountingFs` 以 `PosixWalFs` 子類包裝 `WalFile`，記錄 open／close／write 並依劇本注入失敗。
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import shutil

import pytest

from sw_core.wal import WalWriter

# 資源 marker 放模組層（全檔一致為 memory_only，不違反 P0 T14 規則 2）；平台 marker 放函式層
pytestmark = [pytest.mark.memory_only]

_WINDOWS_SKIP_REASON = "windows_native：請以 scripts/stage0/win-pytest.sh 執行（證據不足，非 PASS）"


def _load():
    """延遲 import 本 Task 新增的 `sw_core.wal_fs`（實作前於此拋 ModuleNotFoundError）。

    以 importlib 載入而非 `from sw_core import wal_fs`：後者於子模組缺失時會包成 ImportError，
    使紅燈原因不明確。
    """
    return importlib.import_module("sw_core.wal_fs")


def _counting_fs_cls():
    """建立 `CountingFs` 類別：包裝 `PosixWalFs`，記錄 open／close／write 並可注入失敗劇本。

    劇本 `script[path]` 為依序消費的動作，每次 `WalFile.write_all_flush` 消費一個（佇列空則為 `("ok",)`）：
      - `("ok",)`：委派完整寫出。
      - `("raise", errno_value)`：不寫入，擲一般 `OSError(errno_value)`（依規格視為 sent == 0）。
      - `("partial", n)`：先委派寫出前 n bytes，再擲 `PartialWriteError`（`sent = n`）。
    `fail_always[path]`：之後每次寫入皆擲 `OSError(errno)`。`open_fail[path]`：`open_append` 直接擲該例外。
    """
    wal_fs = _load()

    class CountingFs(wal_fs.PosixWalFs):
        def __init__(self) -> None:
            super().__init__()
            self.opens: list[str] = []
            self.closed: list[str] = []
            self.writes: list[tuple[str, bytes]] = []
            self.size_calls: list[str] = []
            self.script: dict[str, list[tuple]] = {}
            self.fail_always: dict[str, int] = {}
            self.open_fail: dict[str, BaseException] = {}

        def _next_action(self, path: str) -> tuple:
            queue = self.script.get(path)
            return queue.pop(0) if queue else ("ok",)

        def open_append(self, path: str):
            self.opens.append(path)
            if path in self.open_fail:
                raise self.open_fail[path]
            inner = super().open_append(path)
            fs = self

            class _Wrapped:
                def write_all_flush(self, data: bytes) -> None:
                    fs.writes.append((path, bytes(data)))
                    if path in fs.fail_always:
                        code = fs.fail_always[path]
                        raise OSError(code, os.strerror(code))
                    action = fs._next_action(path)
                    kind = action[0]
                    if kind == "raise":
                        code = action[1]
                        raise OSError(code, os.strerror(code))
                    if kind == "partial":
                        n = action[1]
                        inner.write_all_flush(bytes(data[:n]))
                        exc = wal_fs.PartialWriteError(errno.EIO, "partial write")
                        exc.sent = n  # 契約：PartialWriteError.sent＝失敗前 OS 已確認寫出的 bytes
                        raise exc
                    if kind == "full_then_raise":
                        inner.write_all_flush(data)
                        raise wal_fs.PartialWriteError(
                            errno.EIO, os.strerror(errno.EIO), sent=len(data)
                        )
                    if kind == "caused_partial":
                        code = action[1]
                        cause = OSError(code, os.strerror(code))
                        raise wal_fs.PartialWriteError(
                            code, str(cause), sent=0
                        ) from cause
                    if kind != "ok":
                        raise AssertionError(f"未知劇本動作：{action!r}")
                    inner.write_all_flush(data)

                def sync(self) -> None:
                    inner.sync()

                def identity(self):
                    return inner.identity()

                def unlinked(self) -> bool:
                    return inner.unlinked()

                def close(self) -> None:
                    fs.closed.append(path)
                    inner.close()

            return _Wrapped()

        def size(self, path: str) -> int | None:
            self.size_calls.append(path)
            return super().size(path)

    return CountingFs


def _new_fs():
    return _counting_fs_cls()()


def _lines(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fp:
        return [json.loads(line) for line in fp if line.strip()]


def _wal_writes(fs, w: WalWriter) -> list[bytes]:
    """回傳替身記錄到的、針對 WAL 檔的每一次 write_all_flush 輸入（依呼叫順序）。"""
    return [data for path, data in fs.writes if path == w.wal_path]


@pytest.mark.core_portable
def test_handles_opened_lazily_once_and_reused(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    assert fs.opens == []  # 建構時不開檔
    assert not os.path.exists(w.wal_path)  # 無流量不產生檔案
    for i in range(100):
        w.append(com="COM0", direction="RX", source="device", payload=b"x%d" % i)
    assert fs.opens == [w.wal_path, w.mirror_path]  # 各只開一次，WAL 先於 mirror


@pytest.mark.core_portable
def test_each_record_visible_to_reader_immediately(tmp_path):
    w = WalWriter(wal_dir=str(tmp_path), fs=_new_fs())
    rec = w.append(com="COM0", direction="TX", source="agent:x", payload=b"ls\n", cmd_id="c1")
    assert _lines(w.wal_path)[-1]["seq"] == rec["seq"]
    with open(w.mirror_path, "rb") as fp:
        assert fp.read() == b"ls\n"


@pytest.mark.core_portable
def test_line_bytes_identical_to_baseline_format(tmp_path):
    w = WalWriter(wal_dir=str(tmp_path), fs=_new_fs())
    rec = w.append(com="COM0", direction="META", source="system", payload=b"", meta={"z": 1, "a": "中"})
    with open(w.wal_path, "rb") as fp:
        raw = fp.read()
    expected = (json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert raw == expected


@pytest.mark.core_portable
def test_empty_meta_first_append_creates_empty_mirror(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    rec = w.append(com="COM0", direction="META", source="system", payload=b"")
    assert rec["loss_flag"] is False
    assert os.path.exists(w.mirror_path)
    assert os.path.getsize(w.mirror_path) == 0
    assert fs.opens == [w.wal_path, w.mirror_path]


@pytest.mark.core_portable
def test_empty_meta_mirror_open_failure_does_not_count_as_write_failure(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    fs.open_fail[w.mirror_path] = PermissionError(errno.EACCES, "denied")
    rec = w.append(com="COM0", direction="META", source="system", payload=b"")
    assert rec["loss_flag"] is False
    assert w.health()["write_failures"] == 0
    assert w.unhealthy is False


@pytest.mark.core_portable
def test_mirror_failure_does_not_duplicate_wal_seq(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    fs.script[w.mirror_path] = [("raise", errno.ENOSPC)]  # 只讓 mirror 第一次寫入失敗
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert [r["seq"] for r in _lines(w.wal_path)] == [1, 2]
    assert rec["loss_flag"] is False
    assert w.health()["write_failures"] == 0  # 重試成功不計失敗
    with open(w.mirror_path, "rb") as fp:
        assert fp.read() == b"ab"


@pytest.mark.core_portable
def test_wal_failure_retried_only_on_wal(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    fs.script[w.wal_path] = [("raise", errno.ENOSPC)]  # 只讓 WAL 第一次寫入失敗
    w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert [r["seq"] for r in _lines(w.wal_path)] == [1, 2]
    with open(w.mirror_path, "rb") as fp:
        assert fp.read() == b"ab"
    assert fs.opens.count(w.mirror_path) == 1  # mirror 未被重開


@pytest.mark.core_portable
def test_close_is_idempotent_and_syncs(tmp_path, monkeypatch):
    fs = _new_fs()
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    w.close()
    w.close()
    assert sorted(fs.closed) == sorted([w.wal_path, w.mirror_path])  # 每個 handle 唯一 close 一次
    assert len(synced) >= 2  # close 前 WAL 與 mirror 各 sync 一次
    assert w.closed is True


@pytest.mark.core_portable
def test_append_after_close_is_recorded_as_loss_without_reopen(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    w.close()
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert rec["loss_flag"] is True and rec["seq"] == 2
    assert w.health()["last_write_error"] == "WalClosed: writer closed"
    assert len(fs.opens) == 2  # close 後不重開檔


@pytest.mark.core_portable
def test_mirror_permanent_failure_keeps_wal_loss_flag_false(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    fs.fail_always[w.mirror_path] = errno.ENOSPC
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"a")
    assert len(_lines(w.wal_path)) == 1  # 權威 WAL 已寫入
    assert rec["loss_flag"] is False
    assert w.health()["write_failures"] == 1


@pytest.mark.core_portable
def test_wal_permanent_failure_sets_loss_flag(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")  # seq 1
    fs.fail_always[w.wal_path] = errno.ENOSPC
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")  # seq 2，兩次寫入皆失敗
    assert rec["loss_flag"] is True
    assert w.health()["write_failures"] == 1
    fs.fail_always.pop(w.wal_path)
    nxt = w.append(com="COM0", direction="RX", source="device", payload=b"c")
    assert nxt["seq"] == 3  # 失敗的那筆 seq 仍被消耗


@pytest.mark.core_portable
def test_dir_removed_still_self_heals(tmp_path):
    wal_dir = tmp_path / "wal"
    fs = _new_fs()
    w = WalWriter(wal_dir=str(wal_dir), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    shutil.rmtree(wal_dir)
    # 不推進任何 clock：名稱消失檢查不受 50 ms 節流影響
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert rec["seq"] == 2
    assert os.path.exists(w.wal_path)
    assert any(r["seq"] == 2 for r in _lines(w.wal_path))  # 紀錄落在新檔
    assert w.health()["recreated_count"] == 1


@pytest.mark.core_portable
def test_file_unlinked_name_check_unthrottled(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    os.unlink(w.wal_path)
    w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert os.path.exists(w.wal_path)
    assert any(r["seq"] == 2 for r in _lines(w.wal_path))  # META 補寫由 T05 負責，本測試不斷言


@pytest.mark.core_portable
def test_restart_resumes_max_seq(tmp_path):
    # 回歸防護（既綠）：不依賴本 Task 新增模組
    w1 = WalWriter(wal_dir=str(tmp_path))
    for _ in range(5):
        w1.append(com="COM0", direction="RX", source="device", payload=b"a")
    w2 = WalWriter(wal_dir=str(tmp_path))
    assert w2.append(com="COM0", direction="RX", source="device", payload=b"b")["seq"] == 6


@pytest.mark.core_portable
def test_restart_terminates_unterminated_wal_tail_before_new_record(tmp_path):
    fs = _new_fs()
    w1 = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w1.append(com="COM0", direction="RX", source="device", payload=b"a")
    w1.close()
    with open(w1.wal_path, "ab") as fp:
        fp.write(b'{"seq":2,"partial":')

    w2 = WalWriter(wal_dir=str(tmp_path), fs=fs)
    rec = w2.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert rec["seq"] == 2 and rec["loss_flag"] is False
    with open(w2.wal_path, "rb") as fp:
        lines = fp.read().split(b"\n")
    assert len(lines) == 4  # 舊紀錄、隔離的殘行、新紀錄與尾端空段
    assert json.loads(lines[0])["seq"] == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[1])
    assert json.loads(lines[2])["seq"] == 2
    assert lines[3] == b""


@pytest.mark.core_portable
def test_append_reuses_rotation_size_checks_for_name_detection(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    fs.size_calls.clear()
    w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert fs.size_calls == [w.wal_path, w.mirror_path]


@pytest.mark.core_portable
def test_partial_write_then_eio_completes_suffix_only(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")  # seq 1
    # seq 2 的第一次 WAL 寫入只寫出前 10 bytes 後失敗（PartialWriteError，sent=10）；補寫應成功
    fs.script[w.wal_path] = [("partial", 10)]
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    wal_writes = _wal_writes(fs, w)
    assert len(wal_writes) == 3  # seq 1 整行一次、seq 2 整行一次（部分寫出）、seq 2 後綴一次
    assert wal_writes[2] == wal_writes[1][10:]  # 只補寫剩餘後綴，不重寫整行
    assert rec["loss_flag"] is False
    with open(w.wal_path, "rb") as fp:
        raw = fp.read()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]  # 每行皆為完整 JSON
    assert [r["seq"] for r in rows] == [1, 2]  # 全檔 seq 唯一


@pytest.mark.core_portable
def test_partial_write_suffix_failure_terminates_tail(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")  # seq 1
    # seq 2：前綴 10 bytes 寫出後，補寫也失敗
    fs.script[w.wal_path] = [("partial", 10), ("raise", errno.EIO)]
    rec_b = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert rec_b["loss_flag"] is True
    # seq 3：寫入前須先以 b"\n" 終結殘行，殘行不得與新行串接
    rec_c = w.append(com="COM0", direction="RX", source="device", payload=b"c")
    assert rec_c["loss_flag"] is False
    with open(w.wal_path, "rb") as fp:
        parts = fp.read().split(b"\n")
    assert len(parts) == 4  # 三行（含殘行）各以 \n 結尾，最後為空段
    assert json.loads(parts[0])["seq"] == 1
    with pytest.raises(ValueError):  # json.JSONDecodeError 為 ValueError 子類：殘行獨立且無效
        json.loads(parts[1])
    assert json.loads(parts[2])["seq"] == 3
    assert parts[3] == b""


@pytest.mark.core_portable
def test_full_write_then_exception_not_rewritten(tmp_path):
    """write_all_flush 已寫完整行才回報例外時，不得重寫該 WAL 行。"""
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    fs.script[w.wal_path] = [("full_then_raise",)]
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert [r["seq"] for r in _lines(w.wal_path)] == [1, 2]  # 該 seq 只出現一次
    assert len(_wal_writes(fs, w)) == 2  # WAL 對 seq 2 只寫出一次
    assert rec["loss_flag"] is False


@pytest.mark.core_portable
def test_partial_write_error_preserves_underlying_os_message(tmp_path):
    wal_fs = _load()
    cause = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    class FailingStream:
        def write(self, data):
            raise cause

    handle = wal_fs.PosixWalFile(FailingStream())
    with pytest.raises(wal_fs.PartialWriteError) as caught:
        handle.write_all_flush(b"x")
    assert str(caught.value) == str(cause)
    assert caught.value.__cause__ is cause
    assert caught.value.sent == 0


@pytest.mark.core_portable
def test_health_last_write_error_uses_underlying_oserror(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    fs.script[w.wal_path] = [
        ("caused_partial", errno.ENOSPC),
        ("caused_partial", errno.ENOSPC),
    ]
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"a")
    cause = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    assert rec["loss_flag"] is True
    assert w.health()["last_write_error"] == f"OSError: {cause}"


@pytest.mark.core_portable
def test_wal_persistent_failure_marks_unhealthy_write_failed(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    fs.fail_always[w.wal_path] = errno.ENOSPC
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    h = w.health()
    assert h["healthy"] is False
    assert h["unhealthy_reason"] == "write_failed"
    assert w.unhealthy is True
    fs.fail_always.pop(w.wal_path)
    w.append(com="COM0", direction="RX", source="device", payload=b"b")  # 下一筆成功即恢復
    h = w.health()
    assert h["unhealthy_reason"] is None
    assert w.unhealthy is False
    assert h["healthy"] is True


@pytest.mark.core_portable
def test_first_open_failure_marks_unhealthy(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    fs.open_fail[w.wal_path] = PermissionError(errno.EACCES, "denied")
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"a")
    assert rec["loss_flag"] is True
    assert w.health()["unhealthy_reason"] == "write_failed"
    assert w.unhealthy is True


@pytest.mark.core_portable
def test_mirror_only_failure_keeps_healthy(tmp_path):
    fs = _new_fs()
    w = WalWriter(wal_dir=str(tmp_path), fs=fs)
    fs.fail_always[w.mirror_path] = errno.ENOSPC
    w.append(com="COM0", direction="RX", source="device", payload=b"a")
    assert w.unhealthy is False
    h = w.health()
    assert h["unhealthy_reason"] is None
    assert h["healthy"] is True


@pytest.mark.windows_native
def test_native_line_ending_is_lf_only(tmp_path):
    if os.name != "nt":
        pytest.skip(_WINDOWS_SKIP_REASON)
    w = WalWriter(wal_dir=str(tmp_path))
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"abc")
    with open(w.wal_path, "rb") as fp:
        raw = fp.read()
    assert raw.endswith(b"}\n")  # 新格式行尾只寫 LF（現況文字模式會寫成 CRLF）
    assert b"\r\n" not in raw
    # 舊版寫出的 CRLF 行尾檔，讀取端仍須正常讀出
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy_line = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (legacy_dir / "raw.wal.ndjson").write_bytes(legacy_line + b"\r\n")
    rows = WalWriter(wal_dir=str(legacy_dir)).tail_raw()
    assert [r["seq"] for r in rows] == [rec["seq"]]
