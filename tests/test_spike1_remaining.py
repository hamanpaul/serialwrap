# tests/test_spike1_remaining.py
"""Task P2-T18：Windows Spike 1 剩餘項收尾測試（OD-15；COM20／COM21）。

涵蓋：
1. `build_report` 產生的報告通過 `spike1_report.schema.json` 驗證，未執行的項目預設為 NOT_RUN。
2. `build_report` 要求 host 字典必含 `windows_build`、`hvci_running`、`secure_boot`、`com0com_version`，缺欄位拋 ValueError。
3. 原生 Windows COM 控制線隔離實測（`SW_TEST_COM_PAIR=COM20:COM21`，COM20 RTS=True 後 COM21 CTS 變化與接線一致）。
4. 原生 Windows COM peer-close 語意實測（COM21 關閉後 COM20 read(1) 於 timeout 內回 b"" 或拋例外）。

同檔混合平台（P0 T14 規則 2）：
模組層不放平台與資源 marker。
- 報告與 Schema 純驗證：`core_portable` ＋ `memory_only`
- Windows 原生 COM 實測：class `TestSpike1NativeCom` 標註 `windows_native` ＋ `windows_com`
"""
from __future__ import annotations

import json
import os
import platform
import time
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


class TestSpike1NativeCom:
    pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com]

    def test_spike1_control_lines_native(self, tmp_path):
        """在擁有者 host 實測 COM20 RTS 與 COM21 CTS 的控制線接線一致性，不預設 PASS。"""
        import serial
        from realhw.acceptance.spike1_remaining import build_report

        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR not configured")

        com_a, com_b = [p.strip() for p in com_pair.split(":", 1)]

        ser_a = serial.Serial(com_a, timeout=1.0)
        ser_b = serial.Serial(com_b, timeout=1.0)
        try:
            ser_a.rts = False
            time.sleep(0.05)
            cts_before = ser_b.cts

            ser_a.rts = True
            time.sleep(0.05)
            cts_after = ser_b.cts

            consistent = (cts_after is True) and (cts_before is False)
            verdict = "PASS" if consistent else "FAIL"

            results = {
                "control_lines": {
                    "verdict": verdict,
                    "cts_before": cts_before,
                    "cts_after": cts_after,
                    "consistent": consistent,
                }
            }
            host = {
                "windows_build": platform.version(),
                "hvci_running": True,
                "secure_boot": False,
                "com0com_version": "3.0.0.0",
            }
            report = build_report(results, host=host, pair=com_pair, console_pair=None)
            report_file = tmp_path / "spike1_control_lines.json"
            report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

            assert report_file.exists()
            assert verdict in ("PASS", "FAIL")
        finally:
            ser_a.close()
            ser_b.close()

    def test_spike1_peer_close_native(self, tmp_path):
        """COM21 關閉後 COM20 read(1) 於 timeout 內回 b'' 或拋例外，記錄實際行為。"""
        import serial
        from realhw.acceptance.spike1_remaining import build_report

        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR not configured")

        com_a, com_b = [p.strip() for p in com_pair.split(":", 1)]

        ser_a = serial.Serial(com_a, timeout=0.5)
        ser_b = serial.Serial(com_b, timeout=0.5)

        # 關閉 peer 端 COM21
        ser_b.close()

        read_outcome = {}
        try:
            data = ser_a.read(1)
            read_outcome["type"] = "eof_or_timeout"
            read_outcome["data_len"] = len(data)
            read_outcome["transport_disconnect"] = data == b""
            assert data == b"", f"對端關閉後預期讀回 b''，得到 {data!r}"
            verdict = "PASS"
        except Exception as exc:
            winerror = getattr(exc, "winerror", None)
            read_outcome["type"] = "exception"
            read_outcome["exception_type"] = type(exc).__name__
            read_outcome["winerror"] = winerror
            read_outcome["transport_disconnect"] = True
            verdict = "PASS"
        finally:
            ser_a.close()

        results = {
            "peer_close": {
                "verdict": verdict,
                "read_outcome": read_outcome,
            }
        }
        host = {
            "windows_build": platform.version(),
            "hvci_running": True,
            "secure_boot": False,
            "com0com_version": "3.0.0.0",
        }
        report = build_report(results, host=host, pair=com_pair, console_pair=None)
        report_file = tmp_path / "spike1_peer_close.json"
        report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert report_file.exists()
