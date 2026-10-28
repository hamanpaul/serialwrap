"""OD-36／OD-37 修正後案例的「錯誤實作仍會紅」證明（P5 第四輪；CHG-S21-OD36-*）。

以記憶體內假 daemon 驅動修正後的黑箱案例本體：假 daemon 表現出各案要攔下的錯誤行為（拒絕路徑仍送 bytes、
submit 被接受、錯誤碼不符、selector 不見等），案例須以 AssertionError 失敗；另以正確行為確認不會誤報。
不啟動 daemon、不碰 PTY。
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

import test_f2_backpressure as f2  # noqa: E402
import test_f3_observability as f3  # noqa: E402
import test_safety_core as sc  # noqa: E402


class _Tgt:
    def __init__(self, daemon: "_Fake") -> None:
        self.d = daemon
        self.wire = bytearray()
        self.link_path = None

    def written(self, timeout_s: float = 1.0) -> bytes:
        return bytes(self.wire)

    def emit(self, data: bytes) -> None:
        self.d.on_emit(data)

    def stop(self) -> None:
        self.d.session["state"] = "RECOVERING"

    def pause_responding(self, seconds: float) -> None:
        pass


class _Fake:
    """可程式化的假 daemon：`submit(cmd)` 決定 submit 回應與副作用。"""

    def __init__(self, submit, **session) -> None:
        self.session = {"state": "READY", "last_error": None, "boot_quiet_remaining_s": None,
                        "ready_reconfirm_failed": False, **session}
        self.targets = {"COM0": _Tgt(self)}
        self.tx: list[dict] = []
        self.records: dict[str, dict] = {}
        self._submit = submit

    def on_emit(self, data: bytes) -> None:
        if b"U-Boot" in data:
            self.session["boot_quiet_remaining_s"] = 179.0

    def send(self, payload: bytes, source: str = "agent") -> None:
        self.targets["COM0"].wire += payload
        self.tx.append({"dir": "TX", "source": source})

    def rpc(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        assert method == "session.get_state", method
        return {"ok": True, "session": dict(self.session)}

    def sw(self, *args: str, timeout: float = 30.0) -> dict:
        if args[:2] == ("cmd", "submit"):
            return self._submit(self, args[args.index("--cmd") + 1])
        if args[:2] == ("session", "interactive-open"):
            return {"ok": True, "interactive_id": "iid"}
        if args[:2] == ("session", "attach"):
            return {"_rc": 2, "_stderr": "serialwrap: session.attach failed: SESSION_NOT_FOUND",
                    "ok": False, "error_code": "SESSION_NOT_FOUND"}
        return {"ok": True}

    def wait_command_done(self, cmd_id: str, timeout: float = 20.0) -> dict:
        return self.records[cmd_id]

    def wal_records(self) -> list[dict]:
        return list(self.tx)

    def wait_state(self, com: str, state: str, timeout: float = 20.0) -> dict:
        self.session["state"] = state
        return dict(self.session)


class _Req:
    def __init__(self, d: _Fake) -> None:
        self.d = d

    def getfixturevalue(self, name: str):
        assert name == "shell_daemon", name
        return self.d


def _accepting(d: _Fake, cmd: str) -> dict:
    """錯誤實作：照單全收並送出命令。"""
    d.send(cmd.encode() + b"\n")
    cid = f"c{len(d.records)}"
    d.records[cid] = {"cmd_id": cid, "status": "done", "error_code": None, "done_at": "x"}
    return {"ok": True, "cmd_id": cid}


def _rejecting(code: str):
    def _submit(d: _Fake, cmd: str) -> dict:
        return {"ok": False, "error_code": code}
    return _submit


def test_redproof_selector_oracle():
    """F3：stderr 與 stdout JSON 皆不含 selector → 紅；任一含 → 綠。"""
    assert not f3.names_selector({"_stderr": "boom", "error_code": "SESSION_NOT_FOUND"}, "NOSUCH")
    assert f3.names_selector({"_stderr": "boom", "selector": "NOSUCH"}, "NOSUCH")
    assert f3.names_selector({"_stderr": "x NOSUCH y"}, "NOSUCH")
    with pytest.raises(AssertionError):
        f3.test_device_error_names_selector(_Fake(_accepting))   # 假 daemon 的 attach 回應不含 selector


def test_redproof_reject_tx_scope():
    assert not sc.reject_tx_clean(None, b"x", [])
    assert not sc.reject_tx_clean(None, b"", [{"dir": "TX", "source": "system"}])
    attributable = ("attributable", b"echo x")
    assert not sc.reject_tx_clean(attributable, b"\necho x\n", [])
    assert not sc.reject_tx_clean(attributable, b"", [{"dir": "TX", "source": "agent"}])
    assert sc.reject_tx_clean(attributable, b"\necho __READY__ab\n", [{"dir": "TX", "source": "system"}])


def test_redproof_recovering_submit_accepted_after_io_error():
    with pytest.raises(AssertionError):
        sc.test_recovering_submit_session_recovering(_Fake(_accepting))
    sc.test_recovering_submit_session_recovering(_Fake(_rejecting("SESSION_RECOVERING")))


@pytest.mark.parametrize("row,wrong", [
    ("SESSION_RECOVERING", _accepting),
    ("AUTOBOOT_QUIET", _accepting),
    ("AUTOBOOT_QUIET", _rejecting("SESSION_NOT_READY")),
    ("SESSION_QUEUE_FULL", _accepting),
    ("SESSION_INTERACTIVE_BUSY", _accepting),
    ("SESSION_NOT_READY-ATTACHING", _accepting),
    ("READY_UNCONFIRMED", _rejecting("AUTOBOOT_QUIET")),
])
def test_redproof_reject_rows_fail_on_wrong_impl(row, wrong, monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    kwargs = {"ready_reconfirm_failed": True} if row == "READY_UNCONFIRMED" else {}
    d = _Fake(wrong, **kwargs)
    with pytest.raises(AssertionError):
        sc.test_reject_paths_zero_tx(row, _Req(d))


def test_redproof_attaching_allows_system_probe_only(monkeypatch):
    """ATTACHING：拒絕後只有 system 來源的 attach 登入序列 → 綠（不把合法 probe 算進拒絕路徑）。"""
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)

    def _reject_then_probe(d: _Fake, cmd: str) -> dict:
        d.send(b"\necho __READY__ab\n", source="system")
        return {"ok": False, "error_code": "SESSION_NOT_READY"}

    sc.test_reject_paths_zero_tx("SESSION_NOT_READY-ATTACHING", _Req(_Fake(_reject_then_probe)))


def test_redproof_recovery_flush_sends_queued(monkeypatch):
    """DF-21：佇列命令被 flush 卻仍送出（Python q0 競態型錯誤）→ 紅。"""
    import ab_rows

    monkeypatch.setattr(ab_rows, "is_rust", lambda: True)
    monkeypatch.setattr(ab_rows, "ab_label", lambda: "rust-s21")

    def _submit(d: _Fake, cmd: str) -> dict:
        cid = f"c{len(d.records)}"
        if cmd == "echo q0":
            d.send(b"echo q0\n")
        d.records[cid] = {"cmd_id": cid, "status": "error", "error_code": "FLUSHED_BY_RECOVERY", "done_at": "x"}
        return {"ok": True, "cmd_id": cid}

    with pytest.raises(AssertionError):
        f2.test_recovery_flushes_queue(_Fake(_submit))
