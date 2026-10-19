# tests/test_spike1_remaining.py
"""Task P2-T18：Windows Spike 1 剩餘項收尾測試（OD-15；COM20／COM21）。

涵蓋：
1. `build_report` 產生的報告通過 `spike1_report.schema.json` 驗證，未執行的項目預設為 NOT_RUN。
2. `build_report` 要求 host 字典必含 `windows_build`、`hvci_running`、`secure_boot`、`com0com_version`，缺欄位拋 ValueError。
3. 原生 Windows COM 控制線與選配 Console-side pair 隔離實測。
4. 原生 Windows COM peer-close 語意實測（COM21 關閉後 COM20 read(1) 於 timeout 內回 b"" 或拋例外）。

同檔混合平台（P0 T14 規則 2）：
模組層不放平台與資源 marker。
- 報告與 Schema 純驗證：`core_portable` ＋ `memory_only`
- Windows 原生 COM 實測：class `TestSpike1NativeCom` 標註 `windows_native` ＋ `windows_com`
"""
from __future__ import annotations

import json
import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike1_report_schema_and_not_run_default():
    """驗證 build_report 產出符合 Schema，未提供結果的項目預設填入 NOT_RUN。"""
    import jsonschema
    from realhw.acceptance.spike1_remaining import build_report

    schema_path = REPO_ROOT / "realhw" / "acceptance" / "spike1_report.schema.json"
    assert schema_path.exists(), f"Schema 檔不存在: {schema_path}"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    host = {
        "windows_build": "10.0.26200",
        "hvci_running": True,
        "secure_boot": False,
        "com0com_version": "3.0.0.0",
    }
    partial_results = {
        "control_lines": {
            "verdict": "PASS",
            "details": "COM20 RTS->COM21 CTS verified",
        }
    }
    report = build_report(partial_results, host=host, pair="COM20:COM21", console_pair=None)

    jsonschema.validate(instance=report, schema=schema)

    items = report.get("items") or report.get("results") or report
    assert "console_pair" in items
    assert items["console_pair"]["verdict"] == "NOT_RUN"
    assert "peer_close" in items
    assert items["peer_close"]["verdict"] == "NOT_RUN"
    assert items["control_lines"]["verdict"] == "PASS"
    assert report["producer"] == "spike1_remaining.py"
    assert report["uart_kind"] == "native"
    assert [row["item"] for row in report["results"]] == ["control_lines", "console_pair", "peer_close"]
    assert report["tool_revision"].startswith("sha256:")
    assert report["pyserial_version"] is None or isinstance(report["pyserial_version"], str)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike1_report_requires_host_fields():
    """host 缺必要欄位（如 hvci_running）時拋出 ValueError。"""
    from realhw.acceptance.spike1_remaining import build_report

    required_keys = ("windows_build", "hvci_running", "secure_boot", "com0com_version")
    base_host = {
        "windows_build": "10.0.26200",
        "hvci_running": True,
        "secure_boot": False,
        "com0com_version": "3.0.0.0",
    }

    # 缺 hvci_running → ValueError
    host_missing_hvci = dict(base_host)
    del host_missing_hvci["hvci_running"]
    with pytest.raises(ValueError):
        build_report({"control_lines": {"verdict": "PASS"}}, host=host_missing_hvci, pair="COM20:COM21", console_pair=None)

    # 逐一驗證缺少其他必填欄位時亦拋出 ValueError
    for key in required_keys:
        incomplete_host = dict(base_host)
        del incomplete_host[key]
        with pytest.raises(ValueError):
            build_report({"control_lines": {"verdict": "PASS"}}, host=incomplete_host, pair="COM20:COM21", console_pair=None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike1_build_report_is_accepted_by_evidence_index_when_all_items_pass(tmp_path):
    import yaml
    import jsonschema
    from realhw.acceptance.spike1_remaining import build_report

    host = {
        "windows_build": "10.0.26200",
        "hvci_running": True,
        "secure_boot": False,
        "com0com_version": "3.0.0.0",
    }
    results = {name: {"verdict": "PASS"} for name in ("control_lines", "console_pair", "peer_close")}
    report = build_report(results, host=host, pair="COM20:COM21", console_pair="COM22:COM23")
    schema = json.loads((REPO_ROOT / "realhw/acceptance/spike1_report.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=report, schema=schema)

    artifact = tmp_path / "spike1-remaining.json"
    artifact.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    index = tmp_path / "index.yaml"
    index.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "stage": "0",
            "items": [{
                "id": "S0-SPIKE1-REMAINING",
                "required_layer": "native-windows",
                "artifact_path": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "status": "present",
            }],
        }),
        encoding="utf-8",
    )
    checker_path = REPO_ROOT / "tests/acceptance/check_evidence_index.py"
    spec = importlib.util.spec_from_file_location("test_check_evidence_index", checker_path)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    _, errors = checker.verify_item(index, "S0-SPIKE1-REMAINING", root=tmp_path)
    assert errors == []

    report["results"][1]["verdict"] = "NOT_RUN"
    artifact.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
    index_data["items"][0]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    index.write_text(yaml.safe_dump(index_data), encoding="utf-8")
    _, errors = checker.verify_item(index, "S0-SPIKE1-REMAINING", root=tmp_path)
    assert errors == ["evidence_not_pass S0-SPIKE1-REMAINING"]


class _FakeSerialPort:
    def __init__(self, peer=None, *, fail_read=None, read_result=b"", write_error=None):
        self.peer = peer
        self.rts = False
        self.dtr = False
        self.closed = False
        self.fail_read = fail_read
        self.read_result = read_result
        self.write_error = write_error

    @property
    def cts(self):
        if self.fail_read == "cts":
            raise self.serial_module.SerialException("line unreadable")
        if getattr(getattr(self, "serial_module", None), "swapped", False):
            return bool(self.peer.dtr) if self.peer is not None else False
        return bool(self.peer.rts) if self.peer is not None else False

    @property
    def dsr(self):
        if self.fail_read == "dsr":
            raise self.serial_module.SerialException("line unreadable")
        if getattr(getattr(self, "serial_module", None), "swapped", False):
            return bool(self.peer.rts) if self.peer is not None else False
        return bool(self.peer.dtr) if self.peer is not None else False

    @property
    def cd(self):
        if self.fail_read == "cd":
            raise self.serial_module.SerialException("line unreadable")
        if getattr(getattr(self, "serial_module", None), "swapped", False):
            return bool(self.peer.rts) if self.peer is not None else False
        return bool(self.peer.dtr) if self.peer is not None else False

    def close(self):
        self.closed = True

    def read(self, _size):
        if isinstance(self.read_result, BaseException):
            raise self.read_result
        return self.read_result

    def write(self, _payload):
        if self.write_error is not None:
            raise self.write_error
        return 1


class _FakeSerialModule:
    class SerialException(Exception):
        pass

    class SerialTimeoutException(SerialException):
        pass

    def __init__(self, *, fail_open=None, fail_read=None, read_result=b"", write_error=None, swapped=False):
        self.fail_open = fail_open
        self.fail_read = fail_read
        self.read_result = read_result
        self.write_error = write_error
        self.swapped = swapped
        self.ports = {}
        self.open_count = {}

    def Serial(self, *, port, **_kwargs):
        self.open_count[port] = self.open_count.get(port, 0) + 1
        if port == self.fail_open:
            raise self.SerialException("port is busy")
        if port in ("COM20", "COM21", "COM22", "COM23"):
            pair = {"COM20": ("COM20", "COM21"), "COM21": ("COM20", "COM21"),
                    "COM22": ("COM22", "COM23"), "COM23": ("COM22", "COM23")}[port]
            if pair not in self.ports:
                left = _FakeSerialPort()
                right = _FakeSerialPort(peer=left)
                left.peer = right
                self.ports[pair] = {pair[0]: left, pair[1]: right}
            endpoint = self.ports[pair][port]
            endpoint.serial_module = self
            endpoint.read_result = self.read_result
            endpoint.write_error = self.write_error
            if port == self.fail_read:
                endpoint.fail_read = "cts"
            return endpoint
        if self.open_count[port] == 1:
            endpoint = _FakeSerialPort(read_result=self.read_result, write_error=self.write_error)
            endpoint.serial_module = self
            return endpoint
        if self.open_count[port] == 2:
            raise self.SerialException(
                f"could not open port '{port}': PermissionError(13, 'Access is denied.', None, 5)"
            )
        endpoint = _FakeSerialPort()
        endpoint.serial_module = self
        return endpoint


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_pyserial_wrapped_winerror_is_parsed_and_recorded():
    from realhw.acceptance.spike1_remaining import _probe_exclusive, _serial_error

    wrapped = _FakeSerialModule.SerialException(
        "could not open port 'COM22': PermissionError(13, 'Access is denied.', None, 5)"
    )
    assert _serial_error(wrapped)["winerror"] == 5
    cause = _FakeSerialModule.SerialException("pyserial wrapper")
    cause.__cause__ = type("WinErrorCause", (Exception,), {"winerror": 5})("Access is denied")
    assert _serial_error(cause)["winerror"] == 5

    fake_serial = _FakeSerialModule()
    outcome = _probe_exclusive(fake_serial, "COM42")
    assert outcome["second_open_error"]["winerror"] == 5
    assert outcome["exclusive_error_access_denied"] is True
    assert outcome["verdict"] == "PASS"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_control_line_read_failure_cannot_pass_isolation(monkeypatch):
    from realhw.acceptance import spike1_remaining

    fake_serial = _FakeSerialModule(fail_read="COM23")
    monkeypatch.setattr(spike1_remaining, "_serial_module", lambda: fake_serial)
    result = spike1_remaining._measure_control_lines("COM20:COM21", "COM22:COM23")
    assert result["verdict"] == "NOT_RUN"
    assert result["blocked_reason"]
    assert result["read_errors"]
    assert result["console_side_isolation"]["unaffected"] is None


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_control_lines_measure_rts_and_dtr_independently_with_positive_control(monkeypatch):
    from realhw.acceptance import spike1_remaining

    fake_serial = _FakeSerialModule()
    monkeypatch.setattr(spike1_remaining, "_serial_module", lambda: fake_serial)
    result = spike1_remaining._measure_control_lines("COM20:COM21", "COM22:COM23")
    assert result["verdict"] == "PASS"
    assert result["console_pair_positive_control"]["rts_only"]["observed"] == {
        "cts": True, "dsr": False, "cd": False,
    }
    assert result["console_pair_positive_control"]["dtr_only"]["observed"] == {
        "cts": False, "dsr": True, "cd": True,
    }
    assert result["dut_side"]["phases"]["rts_only"]["dut_b"]["values"] == {
        "cts": True, "dsr": False, "cd": False,
    }
    assert result["dut_side"]["phases"]["dtr_only"]["dut_b"]["values"] == {
        "cts": False, "dsr": True, "cd": True,
    }


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_control_lines_reject_crossed_rts_dtr_mapping(monkeypatch):
    from realhw.acceptance import spike1_remaining

    fake_serial = _FakeSerialModule(swapped=True)
    monkeypatch.setattr(spike1_remaining, "_serial_module", lambda: fake_serial)
    result = spike1_remaining._measure_control_lines("COM20:COM21", "COM22:COM23")
    assert result["verdict"] == "FAIL"
    assert result["dut_side"]["mapping_consistent"] is False
    assert result["console_pair_positive_control"]["rts_only"]["mapping_consistent"] is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_control_line_setup_error_is_not_run(monkeypatch):
    from realhw.acceptance import spike1_remaining

    fake_serial = _FakeSerialModule(fail_open="COM20")
    monkeypatch.setattr(spike1_remaining, "_serial_module", lambda: fake_serial)
    result = spike1_remaining._measure_control_lines("COM20:COM21", "COM22:COM23")
    assert result["verdict"] == "NOT_RUN"
    assert result["blocked_reason"] == "serial_port_open_failed"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_peer_close_timeout_is_not_transport_disconnect(monkeypatch):
    from realhw.acceptance import spike1_remaining

    fake_serial = _FakeSerialModule(
        read_result=b"",
        write_error=_FakeSerialModule.SerialTimeoutException("write timed out"),
    )
    monkeypatch.setattr(spike1_remaining, "_serial_module", lambda: fake_serial)
    result = spike1_remaining._measure_peer_close("COM20:COM21")
    assert result["fault_kind"] == "peer_close"
    assert result["read_outcome"]["timed_out"] is True
    assert result["write_outcome"]["fault_kind"] == "timeout"
    assert result["transport_disconnect"] is False
    assert result["verdict"] == "PASS"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_peer_close_unclassified_serial_error_is_not_run(monkeypatch):
    from realhw.acceptance import spike1_remaining

    fake_serial = _FakeSerialModule(read_result=_FakeSerialModule.SerialException("unclassified failure"))
    monkeypatch.setattr(spike1_remaining, "_serial_module", lambda: fake_serial)
    result = spike1_remaining._measure_peer_close("COM20:COM21")
    assert result["read_outcome"]["winerror"] is None
    assert result["transport_disconnect"] is False
    assert result["verdict"] == "NOT_RUN"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_previous_not_run_result_is_retained_for_followup_measurement(tmp_path):
    from realhw.acceptance.spike1_remaining import _read_previous_report, build_report

    host = {
        "windows_build": "10.0.26200",
        "hvci_running": True,
        "secure_boot": False,
        "com0com_version": "3.0.0.0",
    }
    previous_report = build_report(
        {"control_lines": {"verdict": "NOT_RUN", "dut_side": {"mapping_consistent": True}}},
        host=host,
        pair="COM20:COM21",
        console_pair=None,
    )
    report_path = tmp_path / "spike1-remaining.json"
    report_path.write_text(json.dumps(previous_report), encoding="utf-8")
    results, console_pair = _read_previous_report(report_path, host, "COM20:COM21")
    assert results["control_lines"]["dut_side"]["mapping_consistent"] is True
    assert console_pair is None


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_native_pair_arguments_must_match_explicit_test_allowlist():
    from realhw.acceptance.spike1_remaining import _require_configured_pair

    _require_configured_pair("COM20:COM21", "COM20:COM21", "DUT-side")
    _require_configured_pair("COM22:COM23", "COM22:COM23", "Console-side")
    with pytest.raises(ValueError, match="SW_TEST_COM_PAIR"):
        _require_configured_pair("COM20:COM99", "COM20:COM21", "DUT-side")
    with pytest.raises(ValueError, match="允許的 com0com pair"):
        _require_configured_pair("COM20:COM21", "COM1:COM2", "DUT-side")
    with pytest.raises(ValueError, match="SW_TEST_CONSOLE_COM_PAIR"):
        _require_configured_pair("COM22:COM23", None, "Console-side")



class TestSpike1NativeCom:
    pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com]

    def _run_native(self, action, tmp_path, com_pair, console_pair=None):
        import jsonschema
        from realhw.acceptance.spike1_remaining import main

        args = [action, "--pair", com_pair, "--out", str(tmp_path)]
        if console_pair:
            args.extend(["--console-pair", console_pair])
        exit_code = main(args)
        report_path = tmp_path / "spike1-remaining.json"
        assert report_path.exists(), "原生工具未產生合併報告"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        schema_path = REPO_ROOT / "realhw" / "acceptance" / "spike1_report.schema.json"
        jsonschema.validate(instance=report, schema=json.loads(schema_path.read_text(encoding="utf-8")))
        assert report["evidence_layer"] == "native-windows"
        host = report["host"]
        assert isinstance(host["windows_build"], str) and host["windows_build"]
        assert host["hvci_running"] is None or isinstance(host["hvci_running"], bool)
        assert host["secure_boot"] is None or isinstance(host["secure_boot"], bool)
        assert isinstance(host["com0com_version"], str) and host["com0com_version"]
        return exit_code, report

    def test_spike1_control_lines_native(self, tmp_path):
        """實測控制線接線與跨 pair 隔離，host 欄位取自 Windows 查詢。"""
        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR not configured")
        console_pair = os.environ.get("SW_TEST_CONSOLE_COM_PAIR")
        exit_code, report = self._run_native("control-lines", tmp_path, com_pair, console_pair)

        result = report["items"]["control_lines"]
        assert result["dut_side"]["mapping_consistent"] is True
        baseline = result["dut_side"]["baseline"]
        rts_only = result["dut_side"]["phases"]["rts_only"]["dut_b"]["values"]
        dtr_only = result["dut_side"]["phases"]["dtr_only"]["dut_b"]["values"]
        assert baseline == {"cts": False, "dsr": False, "cd": False}
        assert rts_only == {"cts": True, "dsr": False, "cd": False}
        assert dtr_only == {"cts": False, "dsr": True, "cd": True}
        if console_pair:
            assert result["verdict"] == "PASS"
            assert result["console_side_isolation"]["unaffected"] is True
            assert result["console_pair_positive_control"]["rts_only"]["mapping_consistent"] is True
            assert result["console_pair_positive_control"]["dtr_only"]["mapping_consistent"] is True
        else:
            assert result["verdict"] == "NOT_RUN"
            assert result["console_side_isolation"]["tested"] is False
        assert exit_code == 0

    def test_spike1_console_pair_native(self, tmp_path):
        """量測選配 Console-side pair 的獨占開啟及關閉後重開。"""
        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        console_pair = os.environ.get("SW_TEST_CONSOLE_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR not configured")
        if not console_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_CONSOLE_COM_PAIR not configured")
        exit_code, report = self._run_native("console-pair", tmp_path, com_pair, console_pair)

        item = report["items"]["console_pair"]
        assert item["verdict"] == "PASS"
        assert len(item["endpoints"]) == 2
        assert all(endpoint["first_open"] == "success" for endpoint in item["endpoints"])
        assert all(endpoint["exclusive_error_access_denied"] is True for endpoint in item["endpoints"])
        assert all(endpoint["reopen_after_close"] == "success" for endpoint in item["endpoints"])
        assert exit_code == 0

    def test_spike1_peer_close_native(self, tmp_path):
        """關閉 COM21 後記錄 COM20 讀與寫的實際結果及錯誤碼。"""
        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR not configured")
        exit_code, report = self._run_native("peer-close", tmp_path, com_pair)

        item = report["items"]["peer_close"]
        assert item["verdict"] == "PASS"
        assert item["fault_kind"] == "peer_close"
        assert item["read_outcome"]["result"] in ("returned", "error")
        assert item["write_outcome"]["result"] in ("success", "error")
        assert isinstance(item["transport_disconnect"], bool)
        if item["read_outcome"]["result"] == "returned":
            assert item["read_outcome"]["data_hex"] == ""
            assert item["read_outcome"]["data_len"] == 0
        for outcome in (item["read_outcome"], item["write_outcome"]):
            if outcome["result"] == "error":
                assert "exception_type" in outcome
                if outcome["exception_type"] == "SerialException":
                    assert isinstance(outcome["winerror"], int)
        if item["transport_disconnect"]:
            assert any(
                row["winerror"] in {2, 3, 6, 1167}
                for row in item["transport_disconnect_evidence"]
            )
        assert exit_code == 0
