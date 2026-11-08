"""Spike 1 剩餘 COM 實測與 Windows throwaway 工作設定。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any

ITEMS: tuple[str, ...] = ("control_lines", "console_pair", "peer_close")
VERDICTS: tuple[str, ...] = ("PASS", "FAIL", "NOT_RUN")

_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_PAIR_RE = re.compile(r"^(COM[0-9]+):(COM[0-9]+)$", re.IGNORECASE)
_TEST_PAIR_ALLOWLIST = {
    "DUT-side": "COM20:COM21",
    "Console-side": "COM22:COM23",
}
_THROWAWAY_COM_ALLOWLIST = frozenset({"COM24"})
_DIR_ENV_KEYS = (
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_RUN_DIR",
    "SERIALWRAP_CONFIG_DIR",
    "SERIALWRAP_WAL_DIR",
    "SERIALWRAP_LOG_DIR",
)
_DEVICE_GONE_WINERRORS = frozenset({2, 3, 6, 1167})


def throwaway_task_name(run_id: str) -> str:
    """建立不會等於正式工作名稱的 throwaway 工作名稱。"""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id 必須符合 YYYYMMDDTHHMMSSZ-xxxxxx 格式")
    return f"serialwrap-throwaway-{run_id}"


def throwaway_env(
    run_id: str,
    localappdata: str,
    *,
    allow_com: str = "COM24",
) -> dict[str, str]:
    """產生完全位於本次 run 根目錄內的 Windows daemon 環境。"""
    throwaway_task_name(run_id)
    if not isinstance(allow_com, str) or allow_com not in _THROWAWAY_COM_ALLOWLIST:
        raise ValueError("throwaway 長跑只允許 COM24")
    if not isinstance(localappdata, str) or not localappdata.strip():
        raise ValueError("localappdata 不得為空")
    local_root = PureWindowsPath(localappdata)
    if not local_root.is_absolute():
        raise ValueError("localappdata 必須是絕對 Windows 路徑")

    root = local_root / "sw-throwaway" / run_id
    suffix = int(run_id[-6:], 16)
    port = 49152 + suffix % 16384
    env = {
        "SERIALWRAP_STATE_DIR": str(root / "state"),
        "SERIALWRAP_RUN_DIR": str(root / "run"),
        "SERIALWRAP_CONFIG_DIR": str(root / "config"),
        "SERIALWRAP_WAL_DIR": str(root / "wal"),
        "SERIALWRAP_LOG_DIR": str(root / "log"),
        "SERIALWRAP_DATA_DIR": str(root / "data"),
        "SERIALWRAP_PROFILE_DIR": str(root / "config" / "profiles"),
        "SERIALWRAP_EVENTS_DIR": str(root / "events.d"),
        "SERIALWRAP_EVENTS_RUNTIME_DIR": str(root / "events-runtime"),
        "SERIALWRAP_EVENTS_LOG_PATH": str(root / "events-runtime" / "events.ndjson"),
        "SERIALWRAP_BY_ID_DIR": str(root / "by-id"),
        "SERIALWRAP_BY_PATH_DIR": str(root / "by-path"),
        "SERIALWRAP_TEST_DEVICE_ALLOWLIST": allow_com,
        "SERIALWRAP_ENDPOINT": f"tcp://127.0.0.1:{port}",
        "SERIALWRAP_SOCKET": f"tcp://127.0.0.1:{port}",
    }
    return env


def _require_configured_pair(pair: str, configured: str | None, label: str) -> None:
    """僅允許本批已配置的兩組 com0com 埠進行 I/O。"""
    env_name = "SW_TEST_COM_PAIR" if label == "DUT-side" else "SW_TEST_CONSOLE_COM_PAIR"
    if configured is None or not configured.strip():
        raise ValueError(f"原生 COM 實測必須明確設定 {env_name}")
    configured_pair = _canonical_pair(configured)
    allowed_pair = _TEST_PAIR_ALLOWLIST.get(label)
    if allowed_pair is None:
        raise ValueError(f"未知 pair 類別：{label}")
    if configured_pair != allowed_pair:
        raise ValueError(f"{env_name} 必須是固定允許的 com0com pair {allowed_pair}")
    if _canonical_pair(pair) != configured_pair:
        raise ValueError(f"--pair 必須符合 {env_name}={configured}")


def _canonical_pair(pair: str) -> str:
    if not isinstance(pair, str):
        raise ValueError("COM pair 必須是 COMx:COMy")
    match = _PAIR_RE.fullmatch(pair.strip())
    if match is None:
        raise ValueError("COM pair 必須是 COMx:COMy")
    first, second = (part.upper() for part in match.groups())
    if first == second:
        raise ValueError("COM pair 的兩端必須不同")
    return f"{first}:{second}"


def _validate_pair_separation(pair: str, console_pair: str | None) -> None:
    if console_pair is not None and set(pair.split(":")) & set(console_pair.split(":")):
        raise ValueError("DUT-side 與 Console-side pair 不得共用 COM port")


def _validate_host(host: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(host, dict):
        raise ValueError("host 必須是物件")
    required = ("windows_build", "hvci_running", "secure_boot", "com0com_version")
    missing = [key for key in required if key not in host]
    if missing:
        raise ValueError(f"host 缺少必要欄位：{', '.join(missing)}")
    if not isinstance(host["windows_build"], str) or not host["windows_build"].strip():
        raise ValueError("host.windows_build 必須是非空字串")
    if not isinstance(host["com0com_version"], str) or not host["com0com_version"].strip():
        raise ValueError("host.com0com_version 必須是非空字串；未知時填 unknown")
    for key in ("hvci_running", "secure_boot"):
        if host[key] is not None and not isinstance(host[key], bool):
            raise ValueError(f"host.{key} 必須是布林值或 null")
    return dict(host)


def _tool_revision() -> str:
    """回報目前執行之工具原始碼摘要，包含未提交的工作樹內容。"""
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _pyserial_version() -> str | None:
    serial = sys.modules.get("serial")
    if serial is None:
        return None
    value = getattr(serial, "VERSION", None) or getattr(serial, "__version__", None)
    return str(value) if value else None


def build_report(
    results: dict[str, dict[str, Any]],
    *,
    host: dict[str, Any],
    pair: str,
    console_pair: str | None,
) -> dict[str, Any]:
    """建立 Spike 1 報告；未量測項目一律保留為 NOT_RUN。"""
    if not isinstance(results, dict):
        raise ValueError("results 必須是物件")
    unknown = sorted(set(results) - set(ITEMS))
    if unknown:
        raise ValueError(f"results 含未知項目：{', '.join(unknown)}")

    normalized_pair = _canonical_pair(pair)
    normalized_console_pair = _canonical_pair(console_pair) if console_pair is not None else None
    _validate_pair_separation(normalized_pair, normalized_console_pair)

    items: dict[str, dict[str, Any]] = {}
    for name in ITEMS:
        value = results.get(name, {"verdict": "NOT_RUN"})
        if not isinstance(value, dict):
            raise ValueError(f"results.{name} 必須是物件")
        verdict = value.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"results.{name}.verdict 必須是 PASS、FAIL 或 NOT_RUN")
        items[name] = dict(value)

    if normalized_console_pair is None and items["console_pair"]["verdict"] != "NOT_RUN":
        raise ValueError("未提供 Console-side pair 時，console_pair 必須是 NOT_RUN")

    results_summary = [
        {"item": name, "verdict": items[name]["verdict"]}
        for name in ITEMS
    ]

    return {
        "schema_version": 1,
        "evidence_layer": "native-windows",
        "producer": "spike1_remaining.py",
        "uart_kind": "native",
        "tool_revision": _tool_revision(),
        "pyserial_version": _pyserial_version(),
        "results": results_summary,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host": _validate_host(host),
        "pair": normalized_pair,
        "console_pair": normalized_console_pair,
        "items": items,
    }


def _windows_host() -> dict[str, Any]:
    """從 Windows 唯讀擷取 build、HVCI、Secure Boot 與 com0com driver 資訊。"""
    if os.name != "nt":
        raise RuntimeError("此實測只可在 Windows 原生 Python 執行")
    command = r"""
$osInfo = Get-CimInstance -ClassName Win32_OperatingSystem
$deviceGuard = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace 'root\Microsoft\Windows\DeviceGuard' -ErrorAction SilentlyContinue
$secureBoot = $null
try { $secureBoot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) } catch { $secureBoot = $null }
$hvci = $null
$vbsStatus = $null
$securityServices = @()
if ($null -ne $deviceGuard) {
    $vbsStatus = [int]$deviceGuard.VirtualizationBasedSecurityStatus
    $securityServices = @($deviceGuard.SecurityServicesRunning)
    $hvci = [bool]($securityServices -contains 2)
}
$driver = Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
    Where-Object { $_.DeviceName -match 'com0com' } |
    Select-Object -First 1
$com0comVersion = 'unknown'
if ($null -ne $driver -and $driver.DriverVersion) { $com0comVersion = [string]$driver.DriverVersion }
[pscustomobject]@{
    windows_build = [string]$osInfo.BuildNumber
    hvci_running = $hvci
    secure_boot = $secureBoot
    com0com_version = $com0comVersion
    virtualization_based_security_status = $vbsStatus
    security_services_running = $securityServices
} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Windows host 資訊擷取失敗：{completed.stderr.strip()}")
    try:
        host = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Windows host 資訊不是有效 JSON") from exc
    if not isinstance(host, dict):
        raise RuntimeError("Windows host 資訊不是 JSON 物件")
    return _validate_host(host)


def _serial_module() -> Any:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("Windows 原生實測需要 pyserial") from exc
    return serial


def _extract_winerror(exc: BaseException) -> int | None:
    """讀取 pyserial 包裝於例外文字或 __cause__ 的 Windows 錯誤碼。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        winerror = getattr(current, "winerror", None)
        if isinstance(winerror, int):
            return winerror
        match = re.search(r",\s*(\d+)\s*\)\s*$", str(current))
        if match is not None:
            return int(match.group(1))
        current = current.__cause__
    return None


def _serial_error(exc: BaseException) -> dict[str, Any]:
    return {
        "exception_type": type(exc).__name__,
        "winerror": _extract_winerror(exc),
        "errno": getattr(exc, "errno", None),
        "message": str(exc),
    }


def _open_serial(serial: Any, port: str) -> Any:
    return serial.Serial(port=port, baudrate=115200, timeout=0.5, write_timeout=0.5)


def _probe_exclusive(serial: Any, port: str) -> dict[str, Any]:
    first = None
    second = None
    observation: dict[str, Any] = {"port": port}
    try:
        first = _open_serial(serial, port)
        observation["first_open"] = "success"
    except Exception as exc:  # 開埠結果須留在報告中，不得冒充通過。
        observation["first_open"] = "error"
        observation["first_open_error"] = _serial_error(exc)
        observation["verdict"] = "NOT_RUN"
        observation["blocked_reason"] = "serial_port_open_failed"
        return observation

    try:
        second = _open_serial(serial, port)
    except Exception as exc:
        error = _serial_error(exc)
        observation["second_open"] = "error"
        observation["second_open_error"] = error
        observation["exclusive_error_access_denied"] = error["winerror"] == 5
    else:
        observation["second_open"] = "success"
        observation["exclusive_error_access_denied"] = False
    finally:
        if second is not None:
            try:
                second.close()
            except Exception as exc:
                observation["second_close_error"] = _serial_error(exc)
        try:
            first.close()
        except Exception as exc:
            observation["first_close_error"] = _serial_error(exc)

    try:
        reopened = _open_serial(serial, port)
    except Exception as exc:
        observation["reopen_after_close"] = "error"
        observation["reopen_error"] = _serial_error(exc)
        observation["verdict"] = "FAIL"
    else:
        observation["reopen_after_close"] = "success"
        try:
            reopened.close()
        except Exception as exc:
            observation["reopen_close_error"] = _serial_error(exc)
        close_ok = not any(
            key in observation
            for key in ("first_close_error", "second_close_error", "reopen_close_error")
        )
        observation["verdict"] = "PASS" if observation["exclusive_error_access_denied"] and close_ok else "FAIL"
    return observation


def _measure_console_pair(pair: str) -> dict[str, Any]:
    serial = _serial_module()
    ports = _canonical_pair(pair).split(":")
    endpoints = [_probe_exclusive(serial, port) for port in ports]
    if all(item["verdict"] == "PASS" for item in endpoints):
        verdict = "PASS"
    elif any(item["verdict"] == "NOT_RUN" for item in endpoints):
        verdict = "NOT_RUN"
    else:
        verdict = "FAIL"
    return {"verdict": verdict, "endpoints": endpoints}


def _read_lines(port: Any) -> dict[str, Any]:
    values: dict[str, bool | None] = {}
    errors: dict[str, dict[str, Any]] = {}
    for name in ("cts", "dsr", "cd"):
        try:
            values[name] = bool(getattr(port, name))
        except Exception as exc:
            values[name] = None
            errors[name] = _serial_error(exc)
    return {"values": values, "errors": errors}


def _all_lines_known(samples: list[dict[str, Any]]) -> bool:
    return all(
        isinstance(sample.get("values"), dict)
        and all(isinstance(value, bool) for value in sample["values"].values())
        and not sample.get("errors")
        for sample in samples
    )


def _line_mapping(values: dict[str, bool | None], *, rts: bool, dtr: bool) -> bool:
    return values == {"cts": rts, "dsr": dtr, "cd": dtr}


def _all_lines_low(values: dict[str, bool | None] | None) -> bool:
    return values == {"cts": False, "dsr": False, "cd": False}


def _consoles_idle(phase: dict[str, Any]) -> bool:
    """該階段兩個 console 埠的輸入線（CTS／DSR／CD）皆為低電位。"""
    return _all_lines_low(phase["console_a"]["values"]) and _all_lines_low(phase["console_b"]["values"])


def _sample_control_lines(dut_b: Any, console_a: Any | None, console_b: Any | None) -> dict[str, Any]:
    sample = {"dut_b": _read_lines(dut_b)}
    if console_a is not None:
        sample["console_a"] = _read_lines(console_a)
        sample["console_b"] = _read_lines(console_b)
    return sample


def _sample_has_errors(sample: dict[str, Any]) -> bool:
    return any(endpoint.get("errors") for endpoint in sample.values())


def _measure_control_lines(pair: str, console_pair: str | None) -> dict[str, Any]:
    serial = _serial_module()
    dut_a_name, dut_b_name = _canonical_pair(pair).split(":")
    console_names = _canonical_pair(console_pair).split(":") if console_pair else []
    opened: list[Any] = []
    try:
        dut_a = _open_serial(serial, dut_a_name)
        opened.append(dut_a)
        dut_b = _open_serial(serial, dut_b_name)
        opened.append(dut_b)
        console_a = console_b = None
        if console_names:
            console_a = _open_serial(serial, console_names[0])
            opened.append(console_a)
            console_b = _open_serial(serial, console_names[1])
            opened.append(console_b)
    except Exception as exc:
        for endpoint in reversed(opened):
            try:
                endpoint.close()
            except Exception:
                pass
        return {
            "verdict": "NOT_RUN",
            "blocked_reason": "serial_port_open_failed",
            "setup_error": _serial_error(exc),
        }

    try:
        # 基線前先把所有已開埠的輸出線拉低。pyserial 開埠預設 RTS／DTR 為 True，
        # 未拉低的 COM23 會把 COM22 的 CTS／DSR／CD 釘在 true，使隔離比較失去鑑別力（P2-T18 修正）。
        for endpoint in opened:
            endpoint.rts = False
            endpoint.dtr = False
        time.sleep(0.1)
        baseline = _sample_control_lines(dut_b, console_a, console_b)
        dut_phases: dict[str, dict[str, Any]] = {}
        console_phases: dict[str, dict[str, Any]] = {}

        if console_a is not None:
            # 正向對照（A→B）：確認 COM23 的輸入線確實由 COM22 驅動。
            console_a.rts = True
            time.sleep(0.1)
            console_rts = _read_lines(console_b)
            console_phases["rts_only"] = {
                "observed": console_rts["values"],
                "errors": console_rts["errors"],
                "mapping_consistent": _line_mapping(console_rts["values"], rts=True, dtr=False),
            }
            console_a.rts = False
            console_a.dtr = True
            time.sleep(0.1)
            console_dtr = _read_lines(console_b)
            console_phases["dtr_only"] = {
                "observed": console_dtr["values"],
                "errors": console_dtr["errors"],
                "mapping_consistent": _line_mapping(console_dtr["values"], rts=False, dtr=True),
            }
            console_a.dtr = False
            time.sleep(0.1)
            # 反向對照（B→A）：確認 COM22 的輸入線確實由 COM23 驅動。
            # 缺此方向時，COM22 的恆低讀值無法證明隔離（只是線路斷路或讀值凍結）。
            console_b.rts = True
            time.sleep(0.1)
            reverse_rts = _read_lines(console_a)
            console_phases["reverse_rts_only"] = {
                "observed": reverse_rts["values"],
                "errors": reverse_rts["errors"],
                "mapping_consistent": _line_mapping(reverse_rts["values"], rts=True, dtr=False),
            }
            console_b.rts = False
            console_b.dtr = True
            time.sleep(0.1)
            reverse_dtr = _read_lines(console_a)
            console_phases["reverse_dtr_only"] = {
                "observed": reverse_dtr["values"],
                "errors": reverse_dtr["errors"],
                "mapping_consistent": _line_mapping(reverse_dtr["values"], rts=False, dtr=True),
            }
            console_b.dtr = False
            time.sleep(0.1)

        dut_a.rts = True
        time.sleep(0.1)
        dut_rts = _sample_control_lines(dut_b, console_a, console_b)
        dut_phases["rts_only"] = dut_rts
        dut_a.rts = False
        dut_a.dtr = True
        time.sleep(0.1)
        dut_dtr = _sample_control_lines(dut_b, console_a, console_b)
        dut_phases["dtr_only"] = dut_dtr

        known_samples: list[dict[str, Any]] = [baseline["dut_b"], dut_rts["dut_b"], dut_dtr["dut_b"]]
        if console_a is not None:
            for phase in (baseline, dut_rts, dut_dtr):
                known_samples.extend((phase["console_a"], phase["console_b"]))
            known_samples.extend(
                {"values": phase["observed"], "errors": phase["errors"]} for phase in console_phases.values()
            )
        readings_known = _all_lines_known(known_samples)

        baseline_values = baseline["dut_b"]["values"]
        dut_mapping_ok = (
            _line_mapping(baseline_values, rts=False, dtr=False)
            and _line_mapping(dut_rts["dut_b"]["values"], rts=True, dtr=False)
            and _line_mapping(dut_dtr["dut_b"]["values"], rts=False, dtr=True)
        )
        positive_control_ok = console_a is not None and all(
            phase["mapping_consistent"] for phase in console_phases.values()
        )
        # 隔離判定：基線與各 DUT 階段，兩個 console 埠的輸入線皆須全低。
        # 不再以「與基線相同」比對——基線若已飽和，console 端的洩漏將無從偵測（P2-T18 修正）。
        isolation_ok = console_a is not None and all(
            _consoles_idle(phase) for phase in (baseline, dut_rts, dut_dtr)
        )

        errors_present = any(
            _sample_has_errors(phase)
            for phase in (baseline, dut_rts, dut_dtr)
        ) or any(phase.get("errors") for phase in console_phases.values())
        if not readings_known:
            verdict = "NOT_RUN"
            blocked_reason = "control_line_reading_unavailable"
        elif not dut_mapping_ok or (console_a is not None and (not positive_control_ok or not isolation_ok)):
            verdict = "FAIL"
            blocked_reason = None
        elif console_a is None:
            verdict = "NOT_RUN"
            blocked_reason = "console_pair_not_configured"
        else:
            verdict = "PASS"
            blocked_reason = None

        read_errors = {
            "baseline": baseline,
            "dut_rts_only": dut_rts,
            "dut_dtr_only": dut_dtr,
            "console_positive_control": console_phases,
        } if errors_present else {}
        return {
            "verdict": verdict,
            **({"blocked_reason": blocked_reason} if blocked_reason else {}),
            "outputs_cleared_before_baseline": [dut_a_name, dut_b_name, *console_names],
            "dut_side": {
                "port_a": dut_a_name,
                "port_b": dut_b_name,
                "baseline": baseline["dut_b"]["values"],
                "phases": dut_phases,
                "mapping_consistent": dut_mapping_ok,
            },
            "console_pair_positive_control": console_phases,
            "console_side_isolation": {
                "tested": console_pair is not None,
                "port_a": console_names[0] if console_names else None,
                "port_b": console_names[1] if console_names else None,
                "baseline": {
                    "console_a": baseline.get("console_a", {}).get("values"),
                    "console_b": baseline.get("console_b", {}).get("values"),
                },
                "dut_rts_only": {
                    "console_a": dut_rts.get("console_a", {}).get("values"),
                    "console_b": dut_rts.get("console_b", {}).get("values"),
                },
                "dut_dtr_only": {
                    "console_a": dut_dtr.get("console_a", {}).get("values"),
                    "console_b": dut_dtr.get("console_b", {}).get("values"),
                },
                "unaffected": isolation_ok if console_pair is not None and readings_known else None,
            },
            **({"read_errors": read_errors} if read_errors else {}),
        }
    except Exception as exc:
        return {
            "verdict": "NOT_RUN",
            "blocked_reason": "control_line_measurement_unavailable",
            "measurement_error": _serial_error(exc),
        }
    finally:
        for endpoint in reversed(opened):
            try:
                endpoint.rts = False
                endpoint.dtr = False
            except Exception:
                pass
            try:
                endpoint.close()
            except Exception:
                pass


def _measure_peer_close(pair: str) -> dict[str, Any]:
    serial = _serial_module()
    port_a, port_b = _canonical_pair(pair).split(":")
    peer = None
    endpoint = None
    try:
        endpoint = _open_serial(serial, port_a)
        peer = _open_serial(serial, port_b)
    except Exception as exc:
        if peer is not None:
            try:
                peer.close()
            except Exception:
                pass
        if endpoint is not None:
            try:
                endpoint.close()
            except Exception:
                pass
        return {
            "verdict": "NOT_RUN",
            "fault_kind": "peer_close",
            "blocked_reason": "serial_port_open_failed",
            "setup_error": _serial_error(exc),
        }

    try:
        peer.close()
    except Exception as exc:
        try:
            endpoint.close()
        except Exception:
            pass
        return {
            "verdict": "NOT_RUN",
            "fault_kind": "peer_close",
            "blocked_reason": "peer_close_action_failed",
            "peer_close_error": _serial_error(exc),
        }
    read_outcome: dict[str, Any]
    write_outcome: dict[str, Any]
    disconnect_errors: list[dict[str, Any]] = []
    try:
        data = endpoint.read(1)
        read_outcome = {
            "result": "returned",
            "data_hex": data.hex(),
            "data_len": len(data),
            "timed_out": len(data) == 0,
        }
    except Exception as exc:
        error = _serial_error(exc)
        error_kind = _serial_exception_kind(serial, exc)
        read_outcome = {"result": "error", "fault_kind": error_kind, **error}
        if _is_device_gone_error(serial, exc, error):
            disconnect_errors.append({"operation": "read", **error})

    try:
        bytes_written = endpoint.write(b"\xA5")
        write_outcome = {"result": "success", "bytes_written": int(bytes_written)}
    except Exception as exc:
        error = _serial_error(exc)
        error_kind = _serial_exception_kind(serial, exc)
        write_outcome = {"result": "error", "fault_kind": error_kind, **error}
        if _is_device_gone_error(serial, exc, error):
            disconnect_errors.append({"operation": "write", **error})
    finally:
        try:
            endpoint.close()
        except Exception:
            pass

    read_interpretable = (
        read_outcome.get("result") == "returned" and read_outcome.get("data_len") == 0
    ) or (
        read_outcome.get("result") == "error"
        and read_outcome.get("fault_kind") == "timeout"
    ) or (
        read_outcome.get("result") == "error"
        and read_outcome.get("fault_kind") == "serial_error"
        and isinstance(read_outcome.get("winerror"), int)
    )
    write_interpretable = (
        write_outcome.get("result") == "success" and write_outcome.get("bytes_written") == 1
    ) or (
        write_outcome.get("result") == "error"
        and write_outcome.get("fault_kind") == "timeout"
    ) or (
        write_outcome.get("result") == "error"
        and write_outcome.get("fault_kind") == "serial_error"
        and isinstance(write_outcome.get("winerror"), int)
    )
    result = {
        "verdict": "PASS" if read_interpretable and write_interpretable else "NOT_RUN",
        "fault_kind": "peer_close",
        "read_outcome": read_outcome,
        "write_outcome": write_outcome,
        "transport_disconnect": bool(disconnect_errors),
    }
    if not read_interpretable or not write_interpretable:
        result["blocked_reason"] = "peer_close_outcome_unclassified"
    if disconnect_errors:
        result["transport_disconnect_evidence"] = disconnect_errors
    return result


def _serial_exception_kind(serial: Any, exc: BaseException) -> str:
    timeout_type = getattr(serial, "SerialTimeoutException", None)
    if isinstance(timeout_type, type) and isinstance(exc, timeout_type):
        return "timeout"
    serial_type = getattr(serial, "SerialException", None)
    if isinstance(serial_type, type) and isinstance(exc, serial_type):
        return "serial_error"
    return "unclassified_error"


def _is_device_gone_error(serial: Any, exc: BaseException, error: dict[str, Any]) -> bool:
    return (
        _serial_exception_kind(serial, exc) == "serial_error"
        and error.get("winerror") in _DEVICE_GONE_WINERRORS
    )


def _run_item(action: str, pair: str, console_pair: str | None) -> dict[str, Any]:
    if action == "control-lines":
        return _measure_control_lines(pair, console_pair)
    if action == "console-pair":
        if console_pair is None:
            return {"verdict": "NOT_RUN"}
        return _measure_console_pair(console_pair)
    if action == "peer-close":
        return _measure_peer_close(pair)
    raise ValueError(f"未知 Spike 1 項目：{action}")


def _read_previous_report(path: Path, host: dict[str, Any], pair: str) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"既有 Spike 1 報告無法讀取：{exc}") from exc
    if not isinstance(previous, dict) or previous.get("schema_version") != 1:
        raise ValueError("既有 Spike 1 報告版本不符")
    if previous.get("pair") != pair or previous.get("host") != host:
        raise ValueError("既有報告的 pair 或 host 不同；請以新 evidence 目錄開始")
    previous_items = previous.get("items")
    if not isinstance(previous_items, dict):
        raise ValueError("既有 Spike 1 報告缺少 items")
    results = {
        name: dict(value)
        for name, value in previous_items.items()
        if name in ITEMS and isinstance(value, dict)
    }
    return results, previous.get("console_pair")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 原生 Spike 1 剩餘 COM 實測")
    parser.add_argument("action", choices=("control-lines", "console-pair", "peer-close"))
    parser.add_argument("--pair", required=True, help="DUT-side pair，例如 COM20:COM21")
    parser.add_argument("--console-pair", default=None, help="選配 Console-side pair，例如 COM22:COM23")
    parser.add_argument("--out", required=True, type=Path, help="持久化 evidence 目錄")
    args = parser.parse_args(argv)

    if os.name != "nt":
        parser.error("原生 COM 實測只可在 Windows 執行；Linux 僅支援匯入純函式")

    try:
        pair = _canonical_pair(args.pair)
        console_pair = _canonical_pair(args.console_pair) if args.console_pair else None
        _require_configured_pair(pair, os.environ.get("SW_TEST_COM_PAIR"), "DUT-side")
        if console_pair is not None:
            _require_configured_pair(
                console_pair,
                os.environ.get("SW_TEST_CONSOLE_COM_PAIR"),
                "Console-side",
            )
        _validate_pair_separation(pair, console_pair)
        host = _windows_host()
        args.out.mkdir(parents=True, exist_ok=True)
        report_path = args.out / "spike1-remaining.json"
        results, previous_console_pair = _read_previous_report(report_path, host, pair)
        if console_pair is not None and previous_console_pair not in (None, console_pair):
            raise ValueError("既有報告使用不同 Console-side pair；請以新 evidence 目錄開始")
        effective_console_pair = console_pair or previous_console_pair
        item_name = _action_item(args.action)
        if item_name in results and results[item_name].get("verdict") != "NOT_RUN":
            raise ValueError(f"{item_name} 已有實測結果；重測請使用新的 evidence 目錄")
        item_result = _run_item(args.action, pair, console_pair)
        item_result["measured_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        results[item_name] = item_result
        report = build_report(
            results,
            host=host,
            pair=pair,
            console_pair=effective_console_pair,
        )
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(report_path)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Spike 1 實測失敗：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(report_path)
    return 1 if report["items"][_action_item(args.action)]["verdict"] == "FAIL" else 0


def _action_item(action: str) -> str:
    return {
        "control-lines": "control_lines",
        "console-pair": "console_pair",
        "peer-close": "peer_close",
    }[action]


if __name__ == "__main__":
    sys.exit(main())
