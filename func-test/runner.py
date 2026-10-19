#!/usr/bin/env python3
"""serialwrap 功能測試執行器。

讀取 YAML test case，啟動 daemon + fake target，
依序執行步驟並驗證期望結果。

用法::

    python3 func-test/runner.py                              # 全部
    python3 func-test/runner.py --category state-machine     # 分類
    python3 func-test/runner.py --case sm-01-attach-to-ready # 單一
    python3 func-test/runner.py --repeat 10 --case rc-01-*   # 重複
    python3 func-test/runner.py --list                       # 列表
    python3 func-test/runner.py --verbose                    # 詳細
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# 確保能 import 本地 lib
FUNC_TEST_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = FUNC_TEST_DIR.parent
sys.path.insert(0, str(FUNC_TEST_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.cli_client import cli_run
from lib.console_client import (
    ConsoleHandle,
    TelnetConsole,
    console_read,
    console_write,
)
from lib.correlation import CorrelationLog
from lib.daemon_harness import DaemonHarness, HarnessConfig, StepError, resolve_cli_exe, resolve_daemon_argv
from lib.expect_engine import check_expect
from lib.target_config import TargetConfig
from lib.yaml_loader import TestCase, discover_test_cases


def parse_input_raw(value: str) -> bytes:
    """將 YAML 字串依 Latin-1 byte 範圍編碼。"""
    try:
        return bytes(ord(char) for char in value)
    except (TypeError, ValueError) as exc:
        raise StepError("input_raw 只接受 U+0000 至 U+00FF 字元") from exc


def check_wal_tx_contains_raw(
    records: list[dict[str, Any]],
    expected: str | list[str],
    *,
    tx_source_prefix: str | None = None,
) -> bool:
    """檢查符合來源前綴的 TX payload 串流是否包含全部指定 bytes。"""
    needles = [expected] if isinstance(expected, str) else expected
    chunks: list[bytes] = []
    for record in records:
        if record.get("dir") != "TX":
            continue
        if tx_source_prefix is not None and not str(record.get("source", "")).startswith(tx_source_prefix):
            continue
        value = record.get("payload_b64")
        if isinstance(value, str):
            try:
                chunks.append(base64.b64decode(value, validate=True))
            except (ValueError, TypeError):
                continue
        elif isinstance(record.get("payload"), str):
            chunks.append(record["payload"].encode("utf-8"))
    stream = b"".join(chunks)
    try:
        return all(parse_input_raw(item) in stream for item in needles)
    except StepError:
        return False


def compute_has_interactive(consoles: list[dict[str, Any]]) -> bool:
    """依 console-list 的 interactive_owner 欄位計算互動 ownership。"""
    return any(row.get("interactive_owner") is True for row in consoles)


def resolve_secrets_from_env_file(
    env_files: dict[str, str], spec: dict[str, Any]
) -> list[bytes]:
    """只從案例內的 env file 解析比較器遮罩值，不讀 runner process 環境。"""
    filename = spec.get("file")
    keys = spec.get("keys") or []
    content = env_files.get(str(filename))
    if content is None:
        raise StepError(f"找不到案例 env file：{filename}")
    parsed: dict[str, str] = {}
    for line in content.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not match or match.group(1).startswith("#"):
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[match.group(1)] = value
    secrets: list[bytes] = []
    for key in keys:
        value = parsed.get(str(key))
        if not value:
            raise StepError(f"案例 env file 缺少非空機密欄位：{key}")
        secrets.append(value.encode("utf-8"))
    return secrets


def resolve_secrets_from_env(names: list[str] | tuple[str, ...]) -> list[bytes]:
    """解析 bridge.secrets_from_env 列出的環境變數遮罩值。"""
    return [value.encode("utf-8") for name in names if (value := os.environ.get(str(name)))]


def evaluate_bridge_eligibility(report_path: pathlib.Path | str | None) -> tuple[str, bool]:
    """以 Task 8 的正式資格判定核對自測、核准 revision、平台與工具樹。"""
    if report_path is None:
        return "INSUFFICIENT_EVIDENCE", False
    try:
        bridge_src = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))
        from uart_stdio_bridge.identity import tool_tree_sha256
        from uart_stdio_bridge.selftest.runner import is_qualified, load_approved_revision

        report = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
        approved = load_approved_revision()
        qualified = is_qualified(
            report,
            endpoint="win-serial" if os.name == "nt" else "posix-pty",
            tool_tree_sha256=tool_tree_sha256(),
            platform_os=platform.system(),
            approved=approved,
        )
        return ("PASS", True) if qualified else ("INSUFFICIENT_EVIDENCE", False)
    except Exception:
        return "INSUFFICIENT_EVIDENCE", False


def _scrub_sensitive(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """遞迴移除原始 payload 欄位與遮罩報告內的機密字串。"""
    sensitive_names = {"payload_b64", "token", "secret", "password", "raw_payload"}
    if isinstance(value, dict):
        return {
            key: _scrub_sensitive(item, secrets)
            for key, item in value.items()
            if str(key).lower() not in sensitive_names
        }
    if isinstance(value, list):
        return [_scrub_sensitive(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_scrub_sensitive(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


def create_test_report(
    *,
    results: list[dict[str, Any]],
    comparison: dict[str, Any] | None = None,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """建立不包含 payload 或案例機密的單次 func-test 報告。"""
    return _scrub_sensitive({
        "schema_version": "func-test-report.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "comparison": comparison,
    }, secrets)


def build_summary_report(
    results: list[dict[str, Any]],
    *,
    independent_cases: list[str] | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """將獨立案例數與各模式的執行統計分開計算。"""
    modes: dict[str, dict[str, int]] = {}
    for mode in ("pty", "bridge"):
        selected = [item for item in results if item.get("mode") == mode]
        modes[mode] = {
            "runs": len(selected),
            "pass": sum(item.get("verdict") == "PASS" for item in selected),
            "fail": sum(item.get("verdict") == "FAIL" for item in selected),
            "insufficient": sum(item.get("verdict") == "INSUFFICIENT_EVIDENCE" for item in selected),
            "skip": sum(item.get("verdict") == "SKIP" for item in selected),
        }
    case_ids = independent_cases if independent_cases is not None else [str(r.get("case_id")) for r in results]
    return {
        "schema_version": "func-test-report.v1",
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "modes": modes,
        "independent_cases": len(set(case_ids)),
        "results": results,
    }


class TestRunner:
    """執行單一 TestCase 的引擎。"""

    def __init__(
        self,
        tc: TestCase,
        *,
        verbose: bool = False,
        mode: str = "pty",
        report_dir: pathlib.Path | None = None,
        run_index: int = 0,
        selftest_report: pathlib.Path | None = None,
        dut_mode: str = "local-process",
        dut_image: str | None = None,
        daemon_com: str | None = None,
        bridge_com: str | None = None,
        transport_mode: str = "virtual_pty",
    ) -> None:
        self.tc = tc
        self.verbose = verbose
        self.mode = mode
        self.report_root = report_dir or (pathlib.Path.home() / "b-log" / "func-test-reports" / time.strftime("%y%m%d-%H%M%S"))
        self.run_index = run_index
        self.selftest_report = selftest_report
        self.dut_mode = dut_mode
        self.dut_image = dut_image
        self.daemon_com = daemon_com
        self.bridge_com = bridge_com
        self.transport_mode = transport_mode
        self.harness: DaemonHarness | None = None
        self.variables: dict[str, Any] = {}
        self.console_handles: dict[str, ConsoleHandle | TelnetConsole] = {}
        self.run_dir = self.report_root / tc.name / mode / str(run_index)
        self.run_id = uuid.uuid4().hex
        self.correlation: CorrelationLog | None = None
        self.last_result: dict[str, Any] = {}
        self.comparison: dict[str, Any] | None = None
        self._bridge_eligible = mode != "bridge"
        self._bridge_tool: dict[str, Any] = {}
        self._secrets: list[bytes] = []
        self._deployed_start: dict[str, Any] = {}
        self._deployed_end: dict[str, Any] = {}
        self._live_daemon: list[dict[str, Any]] = []
        self._daemon_device_path: str | None = None
        self._settle_failure: str | None = None
        self._bridge_shutdown_failure: str | None = None
        self._cases_yaml_digest: str | None = None
        self._cases_frozen = False

    def run(self) -> tuple[bool, str]:
        """執行測試，回傳 (通過, 訊息)。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._cases_yaml_digest, self._cases_frozen = self._acceptance_cases_info()
        self.variables = {"python": sys.executable, "run": {"root": ""}}
        if self.mode == "bridge":
            eligibility, self._bridge_eligible = evaluate_bridge_eligibility(self.selftest_report)
            if not self._bridge_eligible:
                self.last_result = self._result(eligibility, "bridge 自測報告未通過核准 revision 與工具樹資格檢查")
                self._write_manifest(None, None)
                return False, self.last_result["message"]

        target_cfg = TargetConfig.from_yaml(self.tc.target)
        cli_argv = resolve_cli_exe()
        daemon_argv = resolve_daemon_argv()
        from lib.artifact_identity import collect_artifacts
        from realhw import deployed_content
        self._deployed_start = deployed_content.measure(cli_argv[0], daemon_argv=daemon_argv)
        self._bridge_tool = self._tool_identity()
        if self.tc.bridge.get("secrets_from_env_file"):
            self._secrets = resolve_secrets_from_env_file(self.tc.env_files, self.tc.bridge["secrets_from_env_file"])
        if self.tc.bridge.get("secrets_from_env"):
            self._secrets.extend(resolve_secrets_from_env(self.tc.bridge["secrets_from_env"]))

        harness_cfg = HarnessConfig(
            target_config=target_cfg,
            profile_overrides=self.tc.profile,
            env_files=self.tc.env_files,
            mode=self.mode,
            dut_mode=self.dut_mode,
            dut_plan=self.tc.scenario,
            dut_image=self.dut_image,
            evidence_dir=self.run_dir,
            run_id=self.run_id,
            case_id=self.tc.name,
            daemon_com=self.daemon_com,
            bridge_com=self.bridge_com,
            transport_mode=self.transport_mode,
            bridge_run_args=tuple(self.tc.bridge.get("run_args") or ()),
        )
        self.harness = DaemonHarness(harness_cfg)
        if self.mode == "bridge":
            self.correlation = self._new_correlation()
        try:
            self.harness.start()
            self.variables["run"]["root"] = str(self.harness.root)
            self._daemon_device_path = self.daemon_com or str(self.harness.root / "by-id" / "fake-uart0")
            self._capture_live_daemon()
            # 執行步驟
            for i, step in enumerate(self.tc.steps):
                self._exec_step(step, step_idx=i)
            self._settle_marker("__settle_end__", len(self.tc.steps))
            ok = True
            message = "OK"
        except StepError as exc:
            ok = False
            message = str(exc)
        except Exception as exc:
            ok = False
            message = f"未預期例外: {type(exc).__name__}: {exc}"
        finally:
            # 清理 console handles
            for handle in self.console_handles.values():
                handle.close_fd()
            self.console_handles.clear()
            if self.correlation is not None:
                self.correlation.close()
                self.correlation = None
            if self.harness is not None:
                self.harness.stop()
                stop_result = getattr(self.harness, "bridge_stop_result", None)
                if self.mode == "bridge" and stop_result is not None:
                    if (
                        stop_result.get("exit_code") != 0
                        or stop_result.get("closed_cleanly") is not True
                        or stop_result.get("failure") is not None
                    ):
                        self._bridge_shutdown_failure = (
                            "bridge 關閉證據無效："
                            f"exit_code={stop_result.get('exit_code')}, "
                            f"closed_cleanly={stop_result.get('closed_cleanly')}, "
                            f"failure={stop_result.get('failure')}"
                        )
            self._deployed_end = deployed_content.measure(cli_argv[0], daemon_argv=daemon_argv)

        if self.mode == "bridge" and ok:
            compare_mode = self.tc.bridge.get("compare", "none")
            if compare_mode != "none":
                self.comparison = self._compare_evidence(str(compare_mode))
                verdict = self.comparison.get("verdict", "INSUFFICIENT_EVIDENCE")
                if verdict != "PASS":
                    ok = False
                    message = f"bridge comparison {verdict}: {self.comparison.get('reasons', [])}"

        if self._bridge_shutdown_failure:
            ok = False
            message = self._bridge_shutdown_failure
        elif self._settle_failure:
            ok = False
            message = self._settle_failure
        verdict = self._final_verdict(ok=ok)
        gate_eligible = (
            self.mode == "bridge"
            and self._bridge_eligible
            and self._cases_frozen
            and self._settle_failure is None
            and self._bridge_shutdown_failure is None
            and (self.comparison or {}).get("verdict") == "PASS"
        )
        self.last_result = self._result(verdict, message, gate_eligible=gate_eligible)
        self._write_manifest(self._artifact_record(collect_artifacts(cli_argv, daemon_argv, ROOT_DIR)), self.comparison)
        return verdict == "PASS", message

    def _final_verdict(self, *, ok: bool) -> str:
        """保留測試結果與 bridge 證據資格的三態區分。"""
        if self.mode != "bridge":
            return "PASS" if ok else "FAIL"
        if not self._bridge_eligible or self._settle_failure or self._bridge_shutdown_failure:
            return "INSUFFICIENT_EVIDENCE"
        if self.comparison is not None:
            comparison_verdict = self.comparison.get("verdict")
            if comparison_verdict == "INSUFFICIENT_EVIDENCE":
                return "INSUFFICIENT_EVIDENCE"
            if comparison_verdict == "FAIL":
                return "FAIL"
            if comparison_verdict != "PASS":
                return "INSUFFICIENT_EVIDENCE"
        return "PASS" if ok else "FAIL"

    def _acceptance_cases_info(self) -> tuple[str | None, bool]:
        """讀取承接清單摘要，只有清單與凍結鎖一致時才允許 gate 資格。"""
        acceptance_dir = ROOT_DIR / "tests" / "acceptance"
        cases_path = acceptance_dir / "cases.yaml"
        lock_path = acceptance_dir / "cases.lock.json"
        if not cases_path.is_file():
            return None, False
        digest = self._sha256(cases_path)
        import yaml

        try:
            cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            return digest, False
        if not isinstance(cases, dict) or not isinstance(lock, dict):
            return digest, False
        revision = cases.get("revision")
        sets = lock.get("sets")
        required_sets = {"stage0", "cli_1A", "cli_1B", "slice_2.1", "slice_2.2", "slice_2.3", "slice_2.4"}
        if (
            type(lock.get("schema_version")) is not int
            or lock.get("schema_version") != 1
            or not isinstance(revision, str)
            or not revision
            or lock.get("cases_revision") != revision
            or not isinstance(sets, dict)
            or not required_sets.issubset(sets)
            or any(
                not isinstance(ids, list)
                or any(not isinstance(case_id, str) for case_id in ids)
                or ids != sorted(set(ids))
                for ids in (sets[key] for key in required_sets)
            )
        ):
            return digest, False
        checker = acceptance_dir / "regen_lock.py"
        if checker.is_file():
            try:
                result = subprocess.run(
                    [sys.executable, str(checker), "--check"],
                    cwd=ROOT_DIR,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return digest, False
            if result.returncode != 0:
                return digest, False
        return digest, True

    def _result(self, verdict: str, message: str, *, gate_eligible: bool = False) -> dict[str, Any]:
        comparison_verdict = self.comparison.get("verdict") if self.comparison else None
        return {
            "case_id": self.tc.name,
            "mode": self.mode,
            "run_index": self.run_index,
            "verdict": verdict,
            "message": message,
            "evidence_dir": str(self.run_dir),
            "comparison_verdict": comparison_verdict,
            "gate_eligible": gate_eligible,
        }

    def _new_correlation(self) -> CorrelationLog:
        bridge_src = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))
        from uart_stdio_bridge.evidence.clock import clock_id

        return CorrelationLog(
            self.run_dir / "correlation.ndjson",
            run_id=self.run_id,
            case_id=self.tc.name,
            clock_id=clock_id(),
        )

    def _tool_identity(self) -> dict[str, Any]:
        bridge_src = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))
        from uart_stdio_bridge import TOOL_NAME, __version__
        from uart_stdio_bridge.identity import tool_tree_sha256

        return {
            "name": TOOL_NAME,
            "version": __version__,
            "tree_sha256": tool_tree_sha256(),
            "approved": self.mode == "bridge" and self._bridge_eligible,
        }

    def _capture_live_daemon(self) -> None:
        if self.harness is None:
            return
        from realhw.deployed_content import live_daemon

        response = self._cli(["daemon", "status"], timeout=5.0)
        pid = response.get("pid")
        if type(pid) is int:
            install = self._deployed_start.get("install_dir")
            self._live_daemon.append(live_daemon(pid, pathlib.Path(install) if install else None))

    def _cli(self, argv: list[str], *, timeout: float = 10.0) -> dict[str, Any]:
        assert self.harness is not None
        return cli_run(
            argv,
            socket_path=self.harness.endpoint,
            env=self.harness.env,
            timeout=timeout,
            log_path=self.run_dir / "cli.ndjson",
        )

    def _bridge_snapshot(self, *, settle: bool) -> dict[str, Any] | None:
        if self.mode != "bridge" or self.harness is None or self.harness.bridge is None:
            return None
        return self.harness.bridge.snapshot(settle=settle)

    def _settle_marker(self, op_id: str, step_idx: int) -> None:
        if self.correlation is None:
            return
        snapshot = self._bridge_snapshot(settle=True)
        if not isinstance(snapshot, dict) or snapshot.get("settled") is not True:
            self._settle_failure = f"bridge settle 未完成：{op_id}"
        self.correlation.op_start(op_id, step_index=step_idx, action="settle", argv=None, snapshot=snapshot)
        self.correlation.op_end(
            op_id,
            step_index=step_idx,
            action="settle",
            rc=0 if self._settle_failure is None else 1,
            response=None if self._settle_failure is None else {"error": "HARNESS_WINDOW_UNSETTLED"},
            snapshot=snapshot,
        )

    def _compare_evidence(self, compare_mode: str) -> dict[str, Any]:
        bridge_src = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))
        from uart_stdio_bridge.compare import CompareInputs, compare

        until_op = (
            compare_mode.partition(":")[2]
            if compare_mode.startswith("until_op:")
            else "__settle_end__" if compare_mode == "full"
            else None
        )
        port_map = {"uart0": (self.tc.target.get("com") or "COM0",)}
        rules = list(self.tc.bridge.get("rules") or [])
        if self._secrets and "secret_tx_unlogged" not in rules:
            rules.append("secret_tx_unlogged")
        result = compare(CompareInputs(
            evidence_dir=self.run_dir,
            wal_dir=self.run_dir / "wal",
            port_map=port_map,
            rules=tuple(rules),
            secrets=tuple(self._secrets),
            until_op=until_op,
        ))
        (self.run_dir / "comparison.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    def _artifact_record(self, artifacts: dict[str, Any]) -> dict[str, Any]:
        from realhw.deployed_content import bracket

        return {
            **artifacts,
            "deployed_content": bracket(
                self._deployed_start,
                self._deployed_end,
                live=self._live_daemon,
            ),
        }

    @staticmethod
    def _git_value(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            value = proc.stdout.strip()
            return value if proc.returncode == 0 and value else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _sha256(path: pathlib.Path) -> str:
        if not path.is_file():
            return hashlib.sha256(b"").hexdigest()
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_manifest(
        self,
        artifacts: dict[str, Any] | None,
        comparison: dict[str, Any] | None,
    ) -> None:
        bridge_src = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))
        from lib.artifact_identity import collect_artifacts
        from realhw import deployed_content
        from uart_stdio_bridge.evidence.manifest import platform_info

        cli_argv = resolve_cli_exe()
        daemon_argv = resolve_daemon_argv()
        if not self._deployed_start:
            self._deployed_start = deployed_content.measure(cli_argv[0], daemon_argv=daemon_argv)
        if not self._deployed_end:
            self._deployed_end = deployed_content.measure(cli_argv[0], daemon_argv=daemon_argv)
        artifact_record = self._artifact_record(artifacts or collect_artifacts(cli_argv, daemon_argv, ROOT_DIR))

        bridge_src = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))
        from uart_stdio_bridge import TOOL_NAME, __version__
        from uart_stdio_bridge.identity import tool_tree_sha256

        tool_record = self._bridge_tool or {
            "name": TOOL_NAME,
            "version": __version__,
            "tree_sha256": tool_tree_sha256(),
            "approved": False,
        }
        case_path = pathlib.Path(self.tc.source_path) if self.tc.source_path else None
        case_path = case_path.resolve() if case_path and case_path.is_file() else None
        case_rel = case_path.relative_to(ROOT_DIR).as_posix() if case_path and case_path.is_relative_to(ROOT_DIR) else None
        case_revision = self._git_value("rev-parse", f"HEAD:{case_rel}") if case_rel else None
        case_sha = self._sha256(case_path) if case_path else hashlib.sha256(
            json.dumps({"name": self.tc.name, "steps": self.tc.steps}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if self._cases_yaml_digest is None:
            self._cases_yaml_digest, self._cases_frozen = self._acceptance_cases_info()
        cases_yaml_sha = self._cases_yaml_digest
        git_status = self._git_value("status", "--porcelain", "--untracked-files=all") or ""
        contract_status = self._git_value("status", "--porcelain", "--", "contracts") or ""
        evidence_map: dict[str, str] = {
            "wire": "wire.ndjson",
            "control": "control.ndjson",
            "correlation": "correlation.ndjson",
            "dut_stderr": "dut.stderr.log",
            "cli_log": "cli.ndjson",
        }
        evidence: dict[str, Any] = {}
        for key, filename in evidence_map.items():
            path = self.run_dir / filename
            evidence[key] = (
                {"path": path.relative_to(self.run_dir).as_posix(), "sha256": self._sha256(path)}
                if path.is_file()
                else None
            )
        wal_dir = self.run_dir / "wal"
        bridge_manifest_path = self.run_dir / "bridge-manifest.json"
        bridge_manifest_sha = self._sha256(bridge_manifest_path) if bridge_manifest_path.is_file() else None
        bridge_dut: dict[str, Any] = {}
        if bridge_manifest_path.is_file():
            try:
                bridge_manifest = json.loads(bridge_manifest_path.read_text(encoding="utf-8"))
                value = bridge_manifest.get("dut") if isinstance(bridge_manifest, dict) else None
                if isinstance(value, dict):
                    bridge_dut = value
            except (OSError, ValueError, TypeError):
                bridge_dut = {}

        target_json = TargetConfig.from_yaml(self.tc.target).to_json()
        target_sha = hashlib.sha256(
            json.dumps(target_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        scenario_sha = None
        if self.tc.scenario is not None:
            scenario_sha = hashlib.sha256(
                json.dumps(self.tc.scenario, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        topology = (
            "faketarget_pty" if self.mode == "pty" else
            "dut_in_container_daemon_native" if self.dut_mode == "docker" else
            "dut_local_process_daemon_native"
        )
        transport = "faketarget_pty" if self.mode == "pty" else self.transport_mode
        daemon_side = self._daemon_device_path if self.mode == "bridge" else None
        bridge_side = self.bridge_com if self.mode == "bridge" and os.name == "nt" else daemon_side
        platform_record = platform_info(endpoint="win-serial" if self.mode == "bridge" and os.name == "nt" else "posix-pty" if self.mode == "bridge" else None)
        if self.mode == "pty":
            platform_record.setdefault("evidence_insufficient", []).append("bridge evidence not applicable in pty mode")
        manifest = {
            "schema_version": "l15-manifest.v1",
            "run_id": self.run_id,
            "case_id": self.tc.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "topology": topology,
            "transport": {
                "mode": transport,
                "port_id": "uart0",
                "daemon_side": daemon_side,
                "bridge_side": bridge_side,
            },
            "artifacts": artifact_record,
            "tool": tool_record,
            "dut": {
                "mode": "faketarget" if self.mode == "pty" else self.dut_mode,
                "image": self.dut_image or (self.tc.scenario or {}).get("image"),
                "image_id": bridge_dut.get("image_id"),
                "target_sha256": target_sha,
                "scenario_sha256": scenario_sha,
                "seed": (self.tc.scenario or {}).get("seed", 0),
            },
            "repo_git_revision": self._git_value("rev-parse", "HEAD"),
            "repo_dirty": bool(git_status),
            "contract_revision": self._git_value("rev-parse", "HEAD:contracts"),
            "contract_dirty": bool(contract_status),
            "cases_yaml_sha256": cases_yaml_sha,
            "case": {
                "source_path": str(case_path) if case_path else "inline-case",
                "sha256": case_sha,
                "revision": case_revision,
            },
            "platform": platform_record,
            "bridge_manifest_sha256": bridge_manifest_sha,
            "evidence": {
                **evidence,
                "wal_dir": "wal" if wal_dir.is_dir() else None,
            },
        }
        (self.run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if comparison is not None and not (self.run_dir / "comparison.json").is_file():
            (self.run_dir / "comparison.json").write_text(
                json.dumps(comparison, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    {msg}")

    # -- 步驟分派 --

    def _exec_step(self, step: dict[str, Any], step_idx: int) -> None:
        action = step.get("action", "")
        self._log(f"步驟 {step_idx}: {action}")

        handler = {
            "wait_ready": self._step_wait_ready,
            "cli": self._step_cli,
            "wait_command_done": self._step_wait_command_done,
            "parallel": self._step_parallel,
            "attach_console": self._step_attach_console,
            "console_write": self._step_console_write,
            "console_read": self._step_console_read,
            "detach_console": self._step_detach_console,
            "assert_state": self._step_assert_state,
            "assert_wal": self._step_assert_wal,
            "sleep": self._step_sleep,
            "inject_device_event": self._step_inject_device_event,
            "target_stop_responding": self._step_target_stop,
            "repeat": self._step_repeat,
            "shell": self._step_shell,
            "target_emit": self._step_target_emit,
            "wait_for_file": self._step_wait_for_file,
            "write_file": self._step_write_file,
            "wait_state": self._step_wait_state,
        }.get(action)

        if handler is None:
            raise StepError(f"步驟 {step_idx}: 未知 action '{action}'")
        op_id = f"s{step_idx:03d}-{action}"
        snapshot = None
        settle_start = self.mode == "bridge" and self.tc.bridge.get("compare", "none") == f"until_op:{op_id}"
        if self.correlation is not None:
            snapshot = self._bridge_snapshot(settle=settle_start)
            argv = self._expand(step.get("argv", [])) if action == "cli" else None
            self.correlation.op_start(
                op_id,
                step_index=step_idx,
                action=str(action),
                argv=list(argv) if isinstance(argv, list) else None,
                snapshot=snapshot,
            )
        try:
            response = handler(step, step_idx)
        except Exception as exc:
            if self.correlation is not None:
                self.correlation.op_end(
                    op_id,
                    step_index=step_idx,
                    action=str(action),
                    rc=1,
                    response={"error": f"{type(exc).__name__}: {exc}"},
                    snapshot=self._bridge_snapshot(settle=False),
                )
            raise
        if self.correlation is not None:
            end_snapshot = self._bridge_snapshot(settle=False)
            response_obj = response if isinstance(response, dict) else None
            self.correlation.op_end(
                op_id,
                step_index=step_idx,
                action=str(action),
                rc=response_obj.get("_rc", 0) if response_obj else 0,
                response=response_obj,
                snapshot=end_snapshot,
            )
        if action == "wait_ready":
            self._settle_marker("__settle_start__", step_idx)

    # -- 變數展開 --

    def _expand(self, value: Any) -> Any:
        """展開 {var.field} 參照。"""
        if isinstance(value, str):
            def _replace(m: re.Match[str]) -> str:
                path = m.group(1)
                parts = path.split(".")
                current: Any = self.variables
                for p in parts:
                    if isinstance(current, dict):
                        current = current.get(p)
                    else:
                        return m.group(0)
                return str(current) if current is not None else m.group(0)
            return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _replace, value)
        if isinstance(value, list):
            return [self._expand(v) for v in value]
        if isinstance(value, dict):
            return {k: self._expand(v) for k, v in value.items()}
        return value

    # -- 各 action 實作 --

    def _step_wait_ready(self, step: dict[str, Any], idx: int) -> dict[str, Any] | None:
        assert self.harness is not None
        timeout_s = step.get("timeout_s", 15.0)
        selector = step.get("selector", "COM0")
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._cli(["session", "list"], timeout=5.0)
            sessions = last.get("sessions") or []
            for s in sessions:
                if s.get("com") == selector and s.get("state") == "READY":
                    self._log(f"Session {selector} READY")
                    return last
            time.sleep(0.3)
        raise StepError(f"步驟 {idx}: wait_ready 逾時 ({timeout_s}s)，最後回應: {last}")

    def _step_cli(self, step: dict[str, Any], idx: int) -> dict[str, Any]:
        assert self.harness is not None
        argv = self._expand(step.get("argv", []))
        timeout = step.get("timeout", 10.0)
        resp = self._cli(argv, timeout=timeout)
        self._log(f"CLI 回應: ok={resp.get('ok')}")

        # 儲存變數
        save_as = step.get("save_as")
        if save_as:
            self.variables[save_as] = resp

        # 檢查 expect
        expect = step.get("expect")
        if expect:
            errors = check_expect(resp, self._expand(expect))
            if errors:
                raise StepError(f"步驟 {idx} (cli): {errors[0]}\n回應: {resp}")
        return resp

    def _step_wait_command_done(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        cmd_id = self._expand(step.get("cmd_id", ""))
        timeout_s = step.get("timeout_s", 10.0)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self._cli(["cmd", "status", "--cmd-id", cmd_id], timeout=5.0)
            command = st.get("command") or {}
            status = command.get("status")
            if status in {"done", "error", "canceled"}:
                self._log(f"命令 {cmd_id} 完成: {status}")
                return st
            time.sleep(0.2)
        raise StepError(f"步驟 {idx}: wait_command_done 逾時 ({timeout_s}s)，cmd_id={cmd_id}")

    def _step_parallel(self, step: dict[str, Any], idx: int) -> None:
        sub_steps = step.get("steps", [])
        errors: list[str] = []
        lock = threading.Lock()

        def _run_sub(sub: dict[str, Any], sub_idx: int) -> None:
            try:
                self._exec_step(sub, step_idx=idx * 100 + sub_idx)
            except Exception as exc:
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=_run_sub, args=(s, i), daemon=True)
            for i, s in enumerate(sub_steps)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
            if t.is_alive():
                with lock:
                    errors.append(f"子步驟 {t.name} 超過 30 秒仍在執行")

        if errors:
            raise StepError(f"步驟 {idx} (parallel): {'; '.join(errors)}")

    def _step_attach_console(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        selector = step.get("selector", "COM0")
        label = step.get("label", "func-test-console")
        existing_ids: set[str] = set()
        if os.name == "nt":
            existing = self._cli(["session", "console-list", "--selector", selector])
            existing_ids = {str(row.get("client_id")) for row in existing.get("consoles", []) if row.get("client_id")}
        resp = self._cli(["session", "console-attach", "--selector", selector, "--label", label])
        if not resp.get("ok"):
            raise StepError(f"步驟 {idx}: attach_console 失敗: {resp}")

        if resp.get("transport") == "tcp":
            endpoint = str(resp.get("endpoint") or resp.get("vtty") or "")
            if not endpoint:
                raise StepError(f"步驟 {idx}: Windows console attach 未回傳 TCP endpoint")
            telnet = TelnetConsole(endpoint)
            deadline = time.monotonic() + 3.0
            client_id = ""
            while time.monotonic() < deadline:
                listed = self._cli(["session", "console-list", "--selector", selector])
                for row in listed.get("consoles", []):
                    value = row.get("client_id")
                    if value and str(value) not in existing_ids:
                        client_id = str(value)
                        break
                if client_id:
                    break
                time.sleep(0.05)
            if not client_id:
                telnet.close()
                raise StepError(f"步驟 {idx}: Telnet 連線後無法從 console-list 找到 client_id")
            telnet.client_id = client_id
            telnet.label = label
            handle: ConsoleHandle | TelnetConsole = telnet
        else:
            client_id = str(resp.get("client_id") or "")
            if not client_id:
                raise StepError(f"步驟 {idx}: console-attach 未回傳 client_id: {resp}")
            handle = ConsoleHandle(
                client_id=client_id,
                vtty=str(resp.get("vtty") or ""),
                label=label,
            )
            try:
                handle.open_fd()
            except OSError as exc:
                raise StepError(f"步驟 {idx}: 無法開啟 console PTY：{exc}") from exc
        self.console_handles[handle.client_id] = handle
        self._log(f"Console attached: {handle.client_id} → {handle.vtty}")

        save_as = step.get("save_as")
        if save_as:
            self.variables[save_as] = {
                "client_id": handle.client_id,
                "vtty": handle.vtty,
            }

    def _step_console_write(self, step: dict[str, Any], idx: int) -> None:
        console_id = self._expand(step.get("console_id", ""))
        handle = self.console_handles.get(console_id)
        if handle is None:
            raise StepError(f"步驟 {idx}: console_id '{console_id}' 不存在")
        raw_value = step.get("input_raw")
        data: str | bytes = parse_input_raw(self._expand(raw_value)) if raw_value is not None else str(self._expand(step.get("input", "")))
        delay_ms = step.get("delay_ms", 0)
        if isinstance(handle, TelnetConsole):
            handle.write(
                data if isinstance(data, bytes) else data.encode("utf-8"),
                delay_ms=delay_ms,
            )
        else:
            console_write(handle, data, delay_ms=delay_ms)
        self._log(f"Console write: {data!r}")

    def _step_console_read(self, step: dict[str, Any], idx: int) -> None:
        console_id = self._expand(step.get("console_id", ""))
        handle = self.console_handles.get(console_id)
        if handle is None:
            raise StepError(f"步驟 {idx}: console_id '{console_id}' 不存在")
        timeout_s = step.get("timeout_s", 5.0)
        if isinstance(handle, TelnetConsole):
            output = handle.read(timeout_s=timeout_s).decode("utf-8", errors="replace")
        else:
            output = console_read(handle, timeout_s=timeout_s)
        self._log(f"Console read ({len(output)} bytes)")

        save_as = step.get("save_as")
        if save_as:
            self.variables[save_as] = {"output": output}

        expect = step.get("expect")
        if expect:
            errors = check_expect({"output": output}, {f"output": expect} if not isinstance(expect, dict) else {"output": expect})
            if errors:
                raise StepError(f"步驟 {idx} (console_read): {errors[0]}\n輸出: {output[:500]}")

    def _step_detach_console(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        console_id = self._expand(step.get("console_id", ""))
        selector = step.get("selector", "COM0")
        handle = self.console_handles.pop(console_id, None)
        resp = self._cli(["session", "console-detach", "--selector", selector, "--client-id", console_id])
        if handle is not None:
            handle.close_fd()
        if not resp.get("ok"):
            raise StepError(f"步驟 {idx}: detach_console 失敗: {resp}")
        self._log(f"Console detached: {console_id}")

    def _step_assert_state(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        selector = step.get("selector", "COM0")
        resp = self._cli(["session", "list"])
        sessions = resp.get("sessions") or []
        target_session = None
        for s in sessions:
            if s.get("com") == selector:
                target_session = s
                break
        if target_session is None:
            raise StepError(f"步驟 {idx}: session {selector} 不存在")

        expect = step.get("expect", {})
        if "has_interactive" in expect:
            consoles_resp = self._cli(["session", "console-list", "--selector", selector])
            target_session["has_interactive"] = compute_has_interactive(consoles_resp.get("consoles") or [])
        errors = check_expect(target_session, self._expand(expect))
        if errors:
            raise StepError(f"步驟 {idx} (assert_state): {errors[0]}\nSession: {target_session}")

    def _step_assert_wal(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        selector = step.get("selector", "COM0")
        resp = self._cli(["log", "tail-raw", "--selector", selector, "--from-seq", "0", "--limit", "10000"])
        if not resp.get("ok"):
            raise StepError(f"步驟 {idx}: assert_wal tail-raw 失敗: {resp}")

        records = resp.get("records") or []
        expect = step.get("expect", {})

        if "min_tx_count" in expect:
            tx_count = sum(1 for r in records if r.get("dir") == "TX")
            threshold = expect["min_tx_count"]
            if isinstance(threshold, dict):
                threshold = threshold.get("gte", 0)
            if tx_count < threshold:
                raise StepError(f"步驟 {idx}: TX 記錄數 {tx_count} < {threshold}")

        if "has_source" in expect:
            sources = {r.get("source") for r in records if r.get("dir") == "TX"}
            for expected_src in expect["has_source"]:
                if expected_src not in sources:
                    raise StepError(f"步驟 {idx}: WAL 缺少 source '{expected_src}'，有: {sources}")

        if "any_tx_contains_raw" in expect:
            expected = expect["any_tx_contains_raw"]
            source_prefix = expect.get("tx_source_prefix")
            if not check_wal_tx_contains_raw(records, expected, tx_source_prefix=source_prefix):
                raise StepError(f"步驟 {idx}: WAL TX 未包含所有指定 raw payload，source prefix={source_prefix!r}")

        self._log(f"WAL 驗證通過 ({len(records)} records)")

    def _step_sleep(self, step: dict[str, Any], _idx: int) -> None:
        seconds = step.get("seconds", 1.0)
        self._log(f"等待 {seconds}s")
        time.sleep(seconds)

    def _step_inject_device_event(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        event = step.get("event", "remove")
        self.harness.device_event(event)
        self._log(f"裝置事件：{event}")

    def _step_target_stop(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        duration_s = step.get("duration_s", 5.0)
        self.harness.pause_target(duration_s)
        self._log(f"Target 暫停回應 {duration_s}s")

    def _step_repeat(self, step: dict[str, Any], idx: int) -> None:
        count = step.get("count", 1)
        sub_steps = step.get("steps", [])
        for iteration in range(count):
            self._log(f"Repeat {iteration + 1}/{count}")
            for si, sub in enumerate(sub_steps):
                self._exec_step(sub, step_idx=idx * 1000 + iteration * 100 + si)

    def _step_shell(self, step: dict[str, Any], idx: int) -> None:
        cmd = self._expand(step.get("cmd", ""))
        timeout = step.get("timeout", 10.0)
        result = subprocess.run(
            cmd,
            shell=True,
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=self.harness.root if self.harness else ROOT_DIR,
            env=self.harness.env if self.harness else None,
        )
        if result.returncode != 0:
            raise StepError(
                f"步驟 {idx} (shell): 命令失敗 rc={result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        self._log(f"Shell OK: {cmd[:60]}")

    def _step_target_emit(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        payload = str(self._expand(step.get("payload", "")))
        self.harness.target_emit(payload.encode("utf-8"))
        self._log(f"target_emit: {repr(payload[:40])}")

    def _step_wait_for_file(self, step: dict[str, Any], idx: int) -> None:
        path = self._expand(step.get("path", ""))
        timeout_s = step.get("timeout_s", 5.0)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if os.path.exists(path):
                self._log(f"檔案出現: {path}")
                return
            time.sleep(0.1)
        raise StepError(f"步驟 {idx}: 等待檔案逾時 {timeout_s}s: {path}")

    def _step_write_file(self, step: dict[str, Any], idx: int) -> None:
        path = pathlib.Path(str(self._expand(step.get("path", ""))))
        raw_content = str(step.get("content", ""))
        if path.suffix.lower() == ".json":
            try:
                json_content = json.loads(raw_content)
            except json.JSONDecodeError:
                content = str(self._expand(raw_content))
            else:
                expanded = self._expand(json_content)
                content = json.dumps(expanded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            content = str(self._expand(raw_content))
        if not str(path):
            raise StepError(f"步驟 {idx}: write_file 缺少 path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _step_wait_state(self, step: dict[str, Any], idx: int) -> dict[str, Any]:
        assert self.harness is not None
        selector = str(self._expand(step.get("selector", "COM0")))
        expected = str(self._expand(step.get("state", "")))
        accepted = self._expand(step.get("accept", [expected]))
        timeout_s = float(step.get("timeout_s", 15.0))
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._cli(["session", "list"], timeout=5.0)
            for session in last.get("sessions") or []:
                if session.get("com") == selector and session.get("state") in accepted:
                    return last
            time.sleep(0.3)
        raise StepError(f"步驟 {idx}: wait_state 逾時 ({timeout_s}s)，期望 {accepted}，最後回應：{last}")


# -- 主程式 --

def main(argv: list[str] | None = None) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    parser = argparse.ArgumentParser(description="serialwrap 功能測試執行器")
    parser.add_argument("--category", "-c", help="只執行指定分類")
    parser.add_argument("--case", "-t", help="只執行指定 test case（stem 名）")
    parser.add_argument("--repeat", "-r", type=int, default=0, help="覆寫重複次數")
    parser.add_argument("--verbose", "-v", action="store_true", help="詳細輸出")
    parser.add_argument("--list", "-l", action="store_true", help="只列出測試案例")
    parser.add_argument("--mode", choices=("pty", "bridge", "both"), default="pty")
    parser.add_argument("--dut-mode", choices=("local-process", "docker"), default="local-process")
    parser.add_argument("--dut-image")
    parser.add_argument("--report-dir", type=pathlib.Path)
    parser.add_argument("--selftest-report", type=pathlib.Path)
    parser.add_argument("--scenarios", action="store_true", help="載入情境案例（Task 11 格式）")
    parser.add_argument("--daemon-com")
    parser.add_argument("--bridge-com")
    parser.add_argument("--transport-mode", choices=("virtual_pty", "virtual_com0com", "physical_usb_uart"))
    args = parser.parse_args(argv)

    if args.scenarios:
        parser.error("--scenarios 將於 Task 11 支援")
    modes = ["pty", "bridge"] if args.mode == "both" else [args.mode]
    if "bridge" in modes and args.selftest_report is None:
        parser.error("bridge 模式必須提供 --selftest-report")
    if "bridge" in modes and os.name == "nt":
        if not args.daemon_com or not args.bridge_com:
            parser.error("Windows bridge 模式必須提供 --daemon-com 與 --bridge-com")
        if args.transport_mode is None:
            parser.error("Windows bridge 模式必須明確提供 --transport-mode")
        if args.transport_mode not in {"virtual_com0com", "physical_usb_uart"}:
            parser.error("Windows bridge 模式只支援 virtual_com0com 或 physical_usb_uart")
    if args.dut_mode == "docker" and "bridge" in modes and not args.dut_image:
        parser.error("docker DUT 模式必須提供 --dut-image")
    transport_mode = args.transport_mode or ("virtual_com0com" if os.name == "nt" else "virtual_pty")
    report_dir = (args.report_dir or (pathlib.Path.home() / "b-log" / "func-test-reports" / time.strftime("%y%m%d-%H%M%S"))).expanduser()

    cases_dir = FUNC_TEST_DIR / "cases"
    test_cases = discover_test_cases(cases_dir, category=args.category, case_name=args.case)

    if not test_cases:
        print("找不到符合條件的測試案例。")
        return 1

    if args.list:
        print(f"共 {len(test_cases)} 個測試案例：\n")
        for tc in test_cases:
            tags = ", ".join(tc.tags) if tc.tags else "-"
            print(f"  [{tc.severity:>8}] {tc.name:<40} ({tc.category}) tags=[{tags}]")
            if tc.description:
                print(f"           {tc.description}")
        return 0

    results: list[dict[str, Any]] = []
    report_secrets: list[str] = []

    print(f"\n═══ serialwrap 功能測試 ═══")
    print(f"測試案例: {len(test_cases)}；模式: {', '.join(modes)}\n")

    for tc in test_cases:
        repeat = args.repeat if args.repeat > 0 else tc.repeat
        for mode in modes:
            for iteration in range(repeat):
                label = tc.name if repeat == 1 else f"{tc.name} (#{iteration + 1})"
                print(f"  ▶ {label} [{mode}] ...", end=" ", flush=True)
                runner = TestRunner(
                    tc,
                    verbose=args.verbose,
                    mode=mode,
                    report_dir=report_dir,
                    run_index=iteration,
                    selftest_report=args.selftest_report,
                    dut_mode=args.dut_mode,
                    dut_image=args.dut_image,
                    daemon_com=args.daemon_com,
                    bridge_com=args.bridge_com,
                    transport_mode=transport_mode,
                )
                if mode == "pty" and os.name == "nt":
                    runner.last_result = runner._result("SKIP", "cap_missing:posix_pty")
                    runner._write_manifest(None, None)
                    ok, msg = False, "SKIP: cap_missing:posix_pty"
                elif mode == "bridge" and not tc.bridge.get("applicable", False):
                    runner.last_result = runner._result("SKIP", "bridge.applicable=false")
                    runner._write_manifest(None, None)
                    ok, msg = False, "SKIP: bridge.applicable=false"
                elif mode == "bridge" and os.name == "nt" and "by_id_link" in tc.bridge.get("windows_caps", []):
                    runner.last_result = runner._result("SKIP", "cap_missing:by_id_link")
                    runner._write_manifest(None, None)
                    ok, msg = False, "SKIP: cap_missing:by_id_link"
                else:
                    ok, msg = runner.run()
                if args.verbose:
                    print()
                print(("✅ PASS" if ok else f"❌ {runner.last_result.get('verdict', 'FAIL')}: {msg}"))
                results.append(runner.last_result)
                if runner._secrets:
                    report_secrets.extend(secret.decode("utf-8", errors="replace") for secret in runner._secrets)

    summary = build_summary_report(
        results,
        independent_cases=[tc.name for tc in test_cases],
        started_at=started_at,
    )
    summary = _scrub_sensitive(summary, tuple(report_secrets))
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "func-test-report.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    passed = sum(item.get("verdict") == "PASS" for item in results)
    failed = len(results) - passed

    print(f"\n═══ 結果 ═══")
    print(f"通過: {passed}  非通過: {failed}  合計: {len(results)}")
    print(f"報告: {report_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
