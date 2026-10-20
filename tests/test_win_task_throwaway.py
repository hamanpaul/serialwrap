# tests/test_win_task_throwaway.py
"""Task P2-T18：Windows host 監管 >72h throwaway 排程工作的失敗測試（OD-15）。

驗證：
1. throwaway 工作名稱前綴 `serialwrap-throwaway-<run_id>`，嚴禁等於或衝突正式 `serialwrap` 工作；
   run_id 格式必須符合 `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`。
2. 五個 `SERIALWRAP_*_DIR` 環境變數皆位於 `<localappdata>\\sw-throwaway\\<run_id>\\` 之下；
   `SERIALWRAP_TEST_DEVICE_ALLOWLIST` 為 COM20；RPC endpoint 為 loopback TCP 且 port ≠ 48700。
3. `win-task-throwaway.ps1 -WhatIf` 輸出的 Task XML 包含 `<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>`、
   `<URI>\\serialwrap-throwaway-<rid></URI>`，所有路徑在隔離根下，且不含 `\\serialwrap</URI>`。
4. `-Cleanup` 僅移除 throwaway 排程工作，不影響正式 `serialwrap` 工作的存在與否。

同檔混合平台（P0 T14 規則 2）：
模組層不放平台與資源 marker。
- 純字串與環境變數函式：`core_portable` ＋ `memory_only`
- PowerShell 與 schtasks 原生排程器測試：`windows_native` ＋ `process_rpc`
"""
from __future__ import annotations

import datetime as dt
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _decode_whatif_environment_value(value: str) -> str:
    return re.sub(
        r"\\u([0-9A-Fa-f]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_task_script_has_utf8_bom_for_windows_powershell_51():
    script = REPO_ROOT / "scripts" / "stage0" / "win-task-throwaway.ps1"
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_throwaway_report_schema_and_evidence_gate(tmp_path):
    import hashlib
    import importlib.util
    import jsonschema
    import yaml

    schema_path = REPO_ROOT / "realhw/acceptance/win_task_throwaway_report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    rid = "20261016T120000Z-a1b2c3"
    root = "C:\\Users\\u\\AppData\\Local\\sw-throwaway\\" + rid
    check_ids = (
        "task_registration", "duration_gt_72h", "health_polling", "task_history", "cleanup",
    )
    report = {
        "schema_version": 1,
        "evidence_layer": "host-throwaway",
        "producer": "win-task-throwaway.ps1",
        "uart_kind": "native",
        "run_id": rid,
        "task_name": f"serialwrap-throwaway-{rid}",
        "principal_sid": "S-1-5-21-test",
        "bundle_revision": "python-baseline",
        "bundle_sha256": "a" * 64,
        "root": root,
        "paths": {
            key: root + "\\" + value for key, value in {
                "state": "state", "run": "run", "config": "config", "profiles": "config\\profiles",
                "wal": "wal", "log": "log", "data": "data", "events": "events.d",
                "events_runtime": "events-runtime", "by_id": "by-id", "by_path": "by-path",
            }.items()
        },
        "endpoint": "tcp://127.0.0.1:49153",
        "allowlist": "COM20",
        "task_xml": {"path": "task.xml", "sha256": "b" * 64},
        "started_at": "2026-10-16T12:00:00Z",
        "finished_at": "2026-10-19T13:00:01Z",
        "duration_s": 262801,
        "poll_interval_s": 300,
        "poll_count": 877,
        "missing_poll_count": 0,
        "task_history_enabled": True,
        "restart_count": 0,
        "last_task_result": 0,
        "results": [{"check_id": check_id, "verdict": "PASS"} for check_id in check_ids],
    }
    jsonschema.validate(report, schema)

    artifact = tmp_path / "throwaway.json"
    artifact.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    index = tmp_path / "index.yaml"
    index.write_text(yaml.safe_dump({
        "schema_version": 1,
        "stage": "0",
        "items": [{
            "id": "S0-WIN-TASK-THROWAWAY-72H",
            "required_layer": "host-throwaway",
            "artifact_path": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "status": "present",
        }],
    }), encoding="utf-8")
    checker_path = REPO_ROOT / "tests/acceptance/check_evidence_index.py"
    spec = importlib.util.spec_from_file_location("test_win_task_evidence_checker", checker_path)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    assert {"S0-SPIKE1-REMAINING", "S0-WIN-TASK-THROWAWAY-72H"} <= checker.STAGE0_REQUIRED_ITEM_IDS
    _, errors = checker.verify_item(index, "S0-WIN-TASK-THROWAWAY-72H", root=tmp_path)
    assert errors == []

    report["results"][0]["verdict"] = "NOT_RUN"
    artifact.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    index_data = yaml.safe_load(index.read_text(encoding="utf-8"))
    index_data["items"][0]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    index.write_text(yaml.safe_dump(index_data), encoding="utf-8")
    _, errors = checker.verify_item(index, "S0-WIN-TASK-THROWAWAY-72H", root=tmp_path)
    assert errors == ["evidence_not_pass S0-WIN-TASK-THROWAWAY-72H"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_throwaway_name_prefix_and_not_production():
    """工作名稱必須帶 serialwrap-throwaway- 前綴，不得為正式工作名稱，且 run_id 格式不合拋 ValueError。"""
    from realhw.acceptance.spike1_remaining import throwaway_task_name

    valid_id = "20261016T120000Z-a1b2c3"
    name = throwaway_task_name(valid_id)
    assert name == f"serialwrap-throwaway-{valid_id}"
    assert name != "serialwrap"
    assert not name.endswith("/serialwrap")
    assert not name.endswith("\\serialwrap")

    # run_id 格式不符 → ValueError
    with pytest.raises(ValueError):
        throwaway_task_name("x")

    with pytest.raises(ValueError):
        throwaway_task_name("")

    with pytest.raises(ValueError):
        throwaway_task_name("20261016T120000Z")  # 缺 hash 後綴


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_throwaway_env_all_under_isolated_root():
    """驗證五個 SERIALWRAP_*_DIR 在隔離目錄下、COM20 允許名單與獨立 TCP port。"""
    from realhw.acceptance.spike1_remaining import throwaway_env

    rid = "20261016T120000Z-a1b2c3"
    localappdata = r"C:\Users\u\AppData\Local"
    env = throwaway_env(rid, localappdata)

    expected_root = PureWindowsPath(localappdata) / "sw-throwaway" / rid

    dir_keys = (
        "SERIALWRAP_STATE_DIR",
        "SERIALWRAP_RUN_DIR",
        "SERIALWRAP_CONFIG_DIR",
        "SERIALWRAP_WAL_DIR",
        "SERIALWRAP_LOG_DIR",
        "SERIALWRAP_DATA_DIR",
        "SERIALWRAP_PROFILE_DIR",
        "SERIALWRAP_EVENTS_DIR",
        "SERIALWRAP_EVENTS_RUNTIME_DIR",
        "SERIALWRAP_EVENTS_LOG_PATH",
        "SERIALWRAP_BY_ID_DIR",
        "SERIALWRAP_BY_PATH_DIR",
    )
    for k in dir_keys:
        assert k in env, f"缺少環境變數 {k}"
        val = env[k]
        actual_path = PureWindowsPath(val)
        assert actual_path != expected_root and actual_path.is_relative_to(expected_root), (
            f"{k}={val} 未在隔離目錄 {expected_root} 下"
        )
        assert "\\serialwrap\\" not in val and "/serialwrap/" not in val, f"{k}={val} 不得指向正式 serialwrap 目錄"

    assert env.get("SERIALWRAP_TEST_DEVICE_ALLOWLIST") == "COM20"
    assert env.get("SERIALWRAP_ENDPOINT") == env.get("SERIALWRAP_SOCKET")

    assert "SERIALWRAP_SOCKET" in env
    sock = env["SERIALWRAP_SOCKET"]
    assert sock.startswith("tcp://127.0.0.1:"), f"SERIALWRAP_SOCKET 必須以 tcp://127.0.0.1: 開頭，得到 {sock}"
    port_str = sock.split(":")[-1]
    assert port_str.isdigit(), f"無效 port: {port_str}"
    port = int(port_str)
    assert port != 48700, f"RPC endpoint port 不得為 48700（正式 daemon port），得到 {port}"

    with pytest.raises(ValueError):
        throwaway_env("x", localappdata)
    with pytest.raises(ValueError):
        throwaway_env("20261016T120000Z-A1B2C3", localappdata)
    with pytest.raises(ValueError):
        throwaway_env("20261016T120000Z-a1b2c3\n", localappdata)


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_task_xml_whatif(tmp_path):
    """驗證 win-task-throwaway.ps1 -WhatIf 輸出的 Task XML 格式與隔離設定。"""
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        pytest.skip("capability missing: pwsh or powershell not found")

    script = REPO_ROOT / "scripts" / "stage0" / "win-task-throwaway.ps1"
    assert script.exists(), f"腳本不存在: {script}"

    rid = "20261016T120000Z-a1b2c3"
    bundle_path = r"C:\b"
    cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RunId",
        rid,
        "-Hours",
        "72",
        "-Bundle",
        bundle_path,
        "-WhatIf",
    ]
    whatif_env = dict(os.environ)
    whatif_env["LOCALAPPDATA"] = r"C:\Users\測試\AppData\Local"
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=whatif_env)
    assert proc.returncode == 0, f"-WhatIf 失敗 (rc={proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"

    out = proc.stdout
    assert out.isascii(), "-WhatIf stdout 必須是 ASCII，避免 Windows code page 造成 UTF-8 decode 失敗"
    xml_start = out.find("<?xml")
    xml_end = out.find("</Task>")
    assert xml_start >= 0 and xml_end >= 0, f"-WhatIf 未輸出 Task XML：{out}"
    xml_text = out[xml_start : xml_end + len("</Task>")]
    task = ET.fromstring(xml_text)
    assert task.tag == f"{{{TASK_NS['task']}}}Task"
    assert task.findtext("task:Settings/task:ExecutionTimeLimit", namespaces=TASK_NS) == "PT0S"
    assert task.findtext("task:RegistrationInfo/task:URI", namespaces=TASK_NS) == f"\\serialwrap-throwaway-{rid}"
    assert task.findtext("task:Triggers/task:LogonTrigger/task:UserId", namespaces=TASK_NS)
    assert task.findtext("task:Principals/task:Principal/task:LogonType", namespaces=TASK_NS) == "InteractiveToken"
    assert task.findtext("task:Settings/task:RestartOnFailure/task:Interval", namespaces=TASK_NS) == "PT1M"
    assert task.findtext("task:Settings/task:RestartOnFailure/task:Count", namespaces=TASK_NS) == "999"
    assert task.findtext("task:RegistrationInfo/task:Description", namespaces=TASK_NS).isascii()

    env = {}
    for line in out[xml_end + len("</Task>") :].splitlines():
        key, separator, value = line.partition("=")
        if separator and key.startswith("SERIALWRAP_"):
            env[key] = _decode_whatif_environment_value(value.strip())
    from realhw.acceptance.spike1_remaining import throwaway_env

    expected_env = throwaway_env(rid, whatif_env["LOCALAPPDATA"])
    assert env == expected_env

    for invalid_rid in (rid.upper(), rid + "\n"):
        invalid_cmd = list(cmd)
        invalid_cmd[invalid_cmd.index(rid)] = invalid_rid
        invalid = subprocess.run(invalid_cmd, capture_output=True, text=True, timeout=30, env=whatif_env)
        assert invalid.returncode != 0

    if os.name == "nt":
        validator = tmp_path / "validate-task-xml.ps1"
        xml_path = tmp_path / "task.xml"
        xml_path.write_bytes(xml_text.encode("utf-16"))
        validator.write_text(
            "$service = New-Object -ComObject Schedule.Service\n"
            "$service.Connect()\n"
            "$folder = $service.GetFolder('\\')\n"
            "$xml = [System.IO.File]::ReadAllText($args[0])\n"
            "$folder.RegisterTask($args[1], $xml, 1, $null, $null, 3, $null) | Out-Null\n",
            encoding="ascii",
        )
        validate = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(validator),
             str(xml_path), f"serialwrap-throwaway-validate-{rid}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert validate.returncode == 0, f"TASK_VALIDATE_ONLY 拒絕 XML：{validate.stdout}\n{validate.stderr}"


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_cleanup_removes_only_throwaway(tmp_path, request):
    """驗證 -Cleanup 僅移除 throwaway 工作，正式 serialwrap 工作狀態不變。"""
    if not shutil.which("schtasks"):
        pytest.skip("capability missing: schtasks not found")
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        pytest.skip("capability missing: pwsh or powershell not found")

    script = REPO_ROOT / "scripts" / "stage0" / "win-task-throwaway.ps1"
    assert script.exists(), f"腳本不存在: {script}"

    rid = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    task_name = f"serialwrap-throwaway-{rid}"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "serialwrapd.exe").write_bytes(b"registration-only fixture")

    cleanup_cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RunId",
        rid,
        "-Cleanup",
    ]

    def cleanup_task():
        completed = subprocess.run(cleanup_cmd, capture_output=True, text=True, timeout=30)
        if completed.returncode == 0 and os.environ.get("LOCALAPPDATA"):
            test_root = Path(os.environ["LOCALAPPDATA"]) / "sw-throwaway" / rid
            shutil.rmtree(test_root, ignore_errors=True)
        return completed

    request.addfinalizer(cleanup_task)

    # 記錄執行前正式 serialwrap 排程工作的存在狀態
    prod_query_before = subprocess.run(
        ["schtasks", "/Query", "/TN", "serialwrap"],
        capture_output=True,
    )
    prod_existed_before = prod_query_before.returncode == 0

    # 註冊 throwaway 工作
    register_cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RunId",
        rid,
        "-Hours",
        "72",
        "-Bundle",
        str(bundle),
    ]
    reg_proc = subprocess.run(register_cmd, capture_output=True, text=True, timeout=30)
    assert reg_proc.returncode == 0, f"註冊 throwaway 工作失敗:\n{reg_proc.stderr}"

    # 執行 -Cleanup
    clean_proc = cleanup_task()
    assert clean_proc.returncode == 0, f"Cleanup 失敗:\n{clean_proc.stderr}"

    # 確認 throwaway 工作已被移除
    throwaway_query = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name],
        capture_output=True,
    )
    assert throwaway_query.returncode != 0, f"throwaway 工作 {task_name} 於 cleanup 後仍然存在"

    # 確認正式 serialwrap 工作的狀態完全未變
    prod_query_after = subprocess.run(
        ["schtasks", "/Query", "/TN", "serialwrap"],
        capture_output=True,
    )
    prod_existed_after = prod_query_after.returncode == 0
    assert prod_existed_after == prod_existed_before, (
        f"正式 serialwrap 工作狀態在 cleanup 後發生改變: before={prod_existed_before}, after={prod_existed_after}"
    )
    assert "No throwaway daemon process remains" in clean_proc.stdout


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_generated_launcher_redirects_stderr_and_obeys_deadline(tmp_path):
    """產生的 launcher 須隔離原生 stderr，並在監管期限到達時停止 daemon。"""
    if os.name != "nt":
        pytest.skip("capability missing: native Windows process semantics")
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        pytest.skip("capability missing: pwsh or powershell not found")

    script = REPO_ROOT / "scripts" / "stage0" / "win-task-throwaway.ps1"
    source = script.read_text(encoding="utf-8-sig")
    template_match = re.search(r"\$launcher\s*=\s*@'\r?\n(.*?)\r?\n'@", source, re.DOTALL)
    assert template_match is not None, "找不到 launcher template"

    from realhw.acceptance.spike1_remaining import throwaway_env

    root = tmp_path / "path with spaces"
    root.mkdir()
    rid = "20261016T120000Z-a1b2c3"
    env = throwaway_env(rid, str(root))
    Path(env["SERIALWRAP_LOG_DIR"]).mkdir(parents=True)
    settings = {
        "daemon_path": ps,
        "daemon_args": [
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::Error.WriteLine('stub stderr'); "
            "[Console]::Error.WriteLine($env:SERIALWRAP_DATA_DIR); "
            "[Console]::Error.WriteLine($env:SERIALWRAP_ENDPOINT); "
            "Start-Sleep -Seconds 20",
        ],
        "hours": 0.0002,
        "environment": env,
    }
    launch_data = base64.b64encode(json.dumps(settings, ensure_ascii=True).encode("utf-8")).decode("ascii")
    launcher = template_match.group(1).replace("__LAUNCH_DATA__", launch_data)
    launcher_path = root / "launch.ps1"
    # 與 win-task-throwaway.ps1 相同以 UTF-8 BOM 寫出（範本含繁中註解，PowerShell 5.1 依 BOM 判讀編碼）。
    launcher_path.write_text(launcher, encoding="utf-8-sig")
    inherited_env = dict(os.environ)
    inherited_env["SERIALWRAP_DATA_DIR"] = r"C:\production\data"
    inherited_env["SERIALWRAP_ENDPOINT"] = "tcp://127.0.0.1:48700"
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(launcher_path)],
        capture_output=True,
        text=True,
        timeout=15,
        env=inherited_env,
    )
    assert proc.returncode == 0, f"launcher failed: {proc.stdout}\n{proc.stderr}"
    assert not proc.stderr
    log_dir = Path(env["SERIALWRAP_LOG_DIR"])
    stderr_logs = list(log_dir.glob("serialwrapd.*.err.log"))
    stdout_logs = list(log_dir.glob("serialwrapd.*.out.log"))
    assert len(stderr_logs) == len(stdout_logs) == 1
    stderr_contents = stderr_logs[0].read_bytes()
    assert b"stub stderr" in stderr_contents
    assert env["SERIALWRAP_DATA_DIR"].encode("utf-8") in stderr_contents
    assert env["SERIALWRAP_ENDPOINT"].encode("utf-8") in stderr_contents
    assert b"C:\\production\\data" not in stderr_contents
    assert b"48700" not in stderr_contents
    assert stdout_logs[0].exists()
