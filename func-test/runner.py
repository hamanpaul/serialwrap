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
import os
import pathlib
import re
import sys
import threading
import time
from typing import Any

# 確保能 import 本地 lib
FUNC_TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(FUNC_TEST_DIR))

from lib.cli_client import cli_run
from lib.console_client import (
    ConsoleHandle,
    attach_console,
    console_read,
    console_write,
    detach_console,
)
from lib.daemon_harness import DaemonHarness, HarnessConfig
from lib.expect_engine import ExpectError, check_expect
from lib.fake_target import TargetConfig
from lib.yaml_loader import TestCase, discover_test_cases


class StepError(Exception):
    """步驟執行失敗。"""
    pass


class TestRunner:
    """執行單一 TestCase 的引擎。"""

    def __init__(self, tc: TestCase, *, verbose: bool = False) -> None:
        self.tc = tc
        self.verbose = verbose
        self.harness: DaemonHarness | None = None
        self.variables: dict[str, Any] = {}
        self.console_handles: dict[str, ConsoleHandle] = {}

    def run(self) -> tuple[bool, str]:
        """執行測試，回傳 (通過, 訊息)。"""
        target_cfg = TargetConfig.from_yaml(self.tc.target)
        harness_cfg = HarnessConfig(
            target_config=target_cfg,
            profile_overrides=self.tc.profile,
            env_files=self.tc.env_files,
        )
        self.harness = DaemonHarness(harness_cfg)
        try:
            self.harness.start()
            # 等待 daemon socket 出現
            self._wait_for_socket(timeout_s=5.0)
            # 執行步驟
            for i, step in enumerate(self.tc.steps):
                self._exec_step(step, step_idx=i)
            return True, "OK"
        except StepError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"未預期例外: {type(exc).__name__}: {exc}"
        finally:
            # 清理 console handles
            for handle in self.console_handles.values():
                handle.close_fd()
            self.console_handles.clear()
            if self.harness is not None:
                self.harness.stop()

    def _wait_for_socket(self, timeout_s: float) -> None:
        assert self.harness is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if os.path.exists(self.harness.socket_path):
                return
            time.sleep(0.1)
        raise StepError(f"Daemon socket 未在 {timeout_s}s 內出現")

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
        }.get(action)

        if handler is None:
            raise StepError(f"步驟 {step_idx}: 未知 action '{action}'")
        handler(step, step_idx)

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

    def _step_wait_ready(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        timeout_s = step.get("timeout_s", 15.0)
        selector = step.get("selector", "COM0")
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = cli_run(
                ["session", "list"],
                socket_path=self.harness.socket_path,
                env=self.harness.env,
                timeout=5.0,
            )
            sessions = last.get("sessions") or []
            for s in sessions:
                if s.get("com") == selector and s.get("state") == "READY":
                    self._log(f"Session {selector} READY")
                    return
            time.sleep(0.3)
        raise StepError(f"步驟 {idx}: wait_ready 逾時 ({timeout_s}s)，最後回應: {last}")

    def _step_cli(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        argv = self._expand(step.get("argv", []))
        timeout = step.get("timeout", 10.0)
        resp = cli_run(
            argv,
            socket_path=self.harness.socket_path,
            env=self.harness.env,
            timeout=timeout,
        )
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

    def _step_wait_command_done(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        cmd_id = self._expand(step.get("cmd_id", ""))
        timeout_s = step.get("timeout_s", 10.0)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = cli_run(
                ["cmd", "status", "--cmd-id", cmd_id],
                socket_path=self.harness.socket_path,
                env=self.harness.env,
                timeout=5.0,
            )
            command = st.get("command") or {}
            status = command.get("status")
            if status in {"done", "error", "canceled"}:
                self._log(f"命令 {cmd_id} 完成: {status}")
                return
            time.sleep(0.2)
        raise StepError(f"步驟 {idx}: wait_command_done 逾時 ({timeout_s}s)，cmd_id={cmd_id}")

    def _step_parallel(self, step: dict[str, Any], idx: int) -> None:
        sub_steps = step.get("steps", [])
        errors: list[str] = []
        lock = threading.Lock()

        def _run_sub(sub: dict[str, Any], sub_idx: int) -> None:
            try:
                self._exec_step(sub, step_idx=idx * 100 + sub_idx)
            except StepError as exc:
                with lock:
                    errors.append(str(exc))

        threads = [
            threading.Thread(target=_run_sub, args=(s, i), daemon=True)
            for i, s in enumerate(sub_steps)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        if errors:
            raise StepError(f"步驟 {idx} (parallel): {'; '.join(errors)}")

    def _step_attach_console(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        selector = step.get("selector", "COM0")
        label = step.get("label", "func-test-console")
        resp = attach_console(
            selector, label,
            socket_path=self.harness.socket_path,
            env=self.harness.env,
        )
        if not resp.get("ok"):
            raise StepError(f"步驟 {idx}: attach_console 失敗: {resp}")

        handle: ConsoleHandle = resp["_handle"]
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
        data = self._expand(step.get("input", ""))
        delay_ms = step.get("delay_ms", 0)
        console_write(handle, data, delay_ms=delay_ms)
        self._log(f"Console write: {data!r}")

    def _step_console_read(self, step: dict[str, Any], idx: int) -> None:
        console_id = self._expand(step.get("console_id", ""))
        handle = self.console_handles.get(console_id)
        if handle is None:
            raise StepError(f"步驟 {idx}: console_id '{console_id}' 不存在")
        timeout_s = step.get("timeout_s", 5.0)
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
        resp = detach_console(
            selector, console_id,
            socket_path=self.harness.socket_path,
            env=self.harness.env,
            handle=handle,
        )
        if not resp.get("ok"):
            raise StepError(f"步驟 {idx}: detach_console 失敗: {resp}")
        self._log(f"Console detached: {console_id}")

    def _step_assert_state(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        selector = step.get("selector", "COM0")
        resp = cli_run(
            ["session", "list"],
            socket_path=self.harness.socket_path,
            env=self.harness.env,
        )
        sessions = resp.get("sessions") or []
        target_session = None
        for s in sessions:
            if s.get("com") == selector:
                target_session = s
                break
        if target_session is None:
            raise StepError(f"步驟 {idx}: session {selector} 不存在")

        expect = step.get("expect", {})
        errors = check_expect(target_session, self._expand(expect))
        if errors:
            raise StepError(f"步驟 {idx} (assert_state): {errors[0]}\nSession: {target_session}")

    def _step_assert_wal(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        selector = step.get("selector", "COM0")
        resp = cli_run(
            ["log", "tail-raw", "--selector", selector, "--from-seq", "0", "--limit", "10000"],
            socket_path=self.harness.socket_path,
            env=self.harness.env,
        )
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

        self._log(f"WAL 驗證通過 ({len(records)} records)")

    def _step_sleep(self, step: dict[str, Any], _idx: int) -> None:
        seconds = step.get("seconds", 1.0)
        self._log(f"等待 {seconds}s")
        time.sleep(seconds)

    def _step_inject_device_event(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        event = step.get("event", "remove")
        link_path = self.harness.root / "by-id" / "fake-uart0"
        if event == "remove":
            try:
                link_path.unlink()
            except FileNotFoundError:
                pass
            self._log("裝置移除（symlink 刪除）")
        elif event == "add":
            if self.harness.fake_target:
                os.symlink(self.harness.fake_target.slave_path, link_path)
            self._log("裝置新增（symlink 重建）")
        else:
            raise StepError(f"步驟 {idx}: 未知 device event '{event}'")

    def _step_target_stop(self, step: dict[str, Any], idx: int) -> None:
        assert self.harness is not None
        duration_s = step.get("duration_s", 5.0)
        if self.harness.fake_target:
            self.harness.fake_target.pause_responding(duration_s)
            self._log(f"Target 暫停回應 {duration_s}s")

    def _step_repeat(self, step: dict[str, Any], idx: int) -> None:
        count = step.get("count", 1)
        sub_steps = step.get("steps", [])
        for iteration in range(count):
            self._log(f"Repeat {iteration + 1}/{count}")
            for si, sub in enumerate(sub_steps):
                self._exec_step(sub, step_idx=idx * 1000 + iteration * 100 + si)


# -- 主程式 --

def main() -> None:
    parser = argparse.ArgumentParser(description="serialwrap 功能測試執行器")
    parser.add_argument("--category", "-c", help="只執行指定分類")
    parser.add_argument("--case", "-t", help="只執行指定 test case（stem 名）")
    parser.add_argument("--repeat", "-r", type=int, default=0, help="覆寫重複次數")
    parser.add_argument("--verbose", "-v", action="store_true", help="詳細輸出")
    parser.add_argument("--list", "-l", action="store_true", help="只列出測試案例")
    args = parser.parse_args()

    cases_dir = FUNC_TEST_DIR / "cases"
    test_cases = discover_test_cases(cases_dir, category=args.category, case_name=args.case)

    if not test_cases:
        print("找不到符合條件的測試案例。")
        sys.exit(1)

    if args.list:
        print(f"共 {len(test_cases)} 個測試案例：\n")
        for tc in test_cases:
            tags = ", ".join(tc.tags) if tc.tags else "-"
            print(f"  [{tc.severity:>8}] {tc.name:<40} ({tc.category}) tags=[{tags}]")
            if tc.description:
                print(f"           {tc.description}")
        return

    total = len(test_cases)
    passed = 0
    failed = 0
    results: list[tuple[str, bool, str]] = []

    print(f"\n═══ serialwrap 功能測試 ═══")
    print(f"測試案例: {total}\n")

    for tc in test_cases:
        repeat = args.repeat if args.repeat > 0 else tc.repeat
        for iteration in range(repeat):
            label = tc.name if repeat == 1 else f"{tc.name} (#{iteration + 1})"
            print(f"  ▶ {label} ...", end=" ", flush=True)

            runner = TestRunner(tc, verbose=args.verbose)
            if args.verbose:
                print()  # 換行讓 verbose 輸出不黏在一起
            ok, msg = runner.run()

            if ok:
                passed += 1
                print("✅ PASS" if not args.verbose else f"    ✅ PASS")
            else:
                failed += 1
                print(f"❌ FAIL: {msg}" if not args.verbose else f"    ❌ FAIL: {msg}")
            results.append((label, ok, msg))

    print(f"\n═══ 結果 ═══")
    print(f"通過: {passed}  失敗: {failed}  合計: {passed + failed}")

    if failed > 0:
        print(f"\n失敗案例：")
        for label, ok, msg in results:
            if not ok:
                print(f"  ✗ {label}: {msg}")
        sys.exit(1)
    else:
        print("\n🎉 全部通過！")


if __name__ == "__main__":
    main()
