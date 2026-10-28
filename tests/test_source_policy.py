"""`--source`／`--owner` 來源字串的外部入口驗證（source_policy）。

來源字串的 human／agent 分流與內部強制恢復旗標全靠前綴，過去打錯（``--source human``
少冒號）會默默當 agent、逾時多送 CTRL-C／CTRL-D 把 console 登出；冒用
``system:recover`` 可觸發 force clear + reattach。本檔驗：

- ``check_source`` 純函式：bare human／空名字正規化、``system``／``system:*`` 拒絕、
  其餘原樣放行（含 repo 內所有既有 source 值）。
- service RPC 邊界（``command.submit``／``file.push``／``file.pull``／``device.release``／
  ``session.interactive_open`` owner）：被擋時不碰 arbiter／manager；正規化時回應標註。
- 端到端行為：bare ``human`` 逾時走 interactive（不送按鍵）；未知前綴仍走 agent 恢復
  （CTRL-C）；``system:recover`` 無法經 ``command.submit`` 觸發 force recover。
- CLI 預檢：五個入口對保留前綴即時拒絕、不呼叫 RPC。
- 內部 system 來源（``recover_session`` 的 ``system:recover`` 與 force fallback）照常運作。
"""
from __future__ import annotations

import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from sw_core import cli
from sw_core.config import SessionProfile, UartProfile
from sw_core.service import SerialwrapService
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.source_policy import (
    ERROR_SOURCE_RESERVED,
    SOURCE_CLASS_AGENT,
    SOURCE_CLASS_HUMAN,
    check_source,
    classify_source,
)
from sw_core.wal import WalWriter

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


# repo 內 tests／tools／regression／realhw／func-test／docs／skill 實際出現過的 source 值。
EXISTING_SOURCE_VALUES = [
    "agent", "agent:diag", "agent:flash", "agent:me", "agent:bg", "agent:verify",
    "agent:realhw", "agent:swreg", "agent:serialwrap-regression", "agent:rhwremote",
    "agent:rhw1", "agent:wp252-test", "agent:preempt-test", "agent:cleanup-test",
    "agent:1", "agent:2", "agent:3", "agent:brcm-test", "agent:stress-test",
    "agent:demo", "agent:ci", "agent:test", "agent:x", "agent:A", "agent-A",
    "cli", "codex", "a1", "t", "a", "test", "file_transfer", "uart", "detected",
]
EXISTING_HUMAN_VALUES = ["human:c1", "human:x", "human:cid-7", "human:minicom", "human:other"]


class TestCheckSource(unittest.TestCase):
    def test_bare_human_normalized_to_human_cli(self) -> None:
        for raw in ("human", "human:", "human:   ", "HUMAN", "Human:", " human "):
            with self.subTest(raw=raw):
                check = check_source(raw, default="agent")
                self.assertIsNone(check.error)
                self.assertEqual(check.source, "human:cli")
                self.assertEqual(check.normalized_from, raw)
                self.assertEqual(classify_source(check.source), SOURCE_CLASS_HUMAN)

    def test_human_with_name_kept_and_case_of_prefix_folded(self) -> None:
        keep = check_source("human:bob", default="agent")
        self.assertEqual((keep.source, keep.normalized_from, keep.error), ("human:bob", None, None))
        fold = check_source("Human:Bob", default="agent")
        self.assertEqual(fold.source, "human:Bob")
        self.assertEqual(fold.normalized_from, "Human:Bob")
        colon = check_source("human:a:b", default="agent")
        self.assertEqual((colon.source, colon.normalized_from), ("human:a:b", None))

    def test_reserved_prefix_rejected(self) -> None:
        for raw in ("system", "system:", "system:recover", "system:self_test", "system:secret",
                    "SYSTEM:recover", "System:x", " system:recover", "system :x"):
            with self.subTest(raw=raw):
                check = check_source(raw, default="agent")
                self.assertIsNotNone(check.error)
                assert check.error is not None
                self.assertFalse(check.error["ok"])
                self.assertEqual(check.error["error_code"], ERROR_SOURCE_RESERVED)
                self.assertEqual(check.error["reserved_prefix"], "system")
                self.assertEqual(check.error["value"], raw)
                self.assertEqual(check.error["field"], "source")
                self.assertIn("agent:<名>", check.error["hint"])

    def test_lookalikes_not_reserved(self) -> None:
        for raw in ("systemd:foo", "system-check", "systems", "mysystem:x", "sys:recover", "agent:system"):
            with self.subTest(raw=raw):
                check = check_source(raw, default="agent")
                self.assertIsNone(check.error)
                self.assertEqual(check.source, raw)
                self.assertIsNone(check.normalized_from)

    def test_empty_uses_default(self) -> None:
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(check_source(raw, default="agent").source, "agent")
                self.assertEqual(check_source(raw, default="cli").source, "cli")

    def test_existing_values_unchanged_and_classified(self) -> None:
        for raw in EXISTING_SOURCE_VALUES:
            with self.subTest(raw=raw):
                check = check_source(raw, default="agent")
                self.assertIsNone(check.error)
                self.assertEqual(check.source, raw)
                self.assertIsNone(check.normalized_from)
                self.assertEqual(classify_source(raw), SOURCE_CLASS_AGENT)
        for raw in EXISTING_HUMAN_VALUES:
            with self.subTest(raw=raw):
                check = check_source(raw, default="agent")
                self.assertIsNone(check.error)
                self.assertEqual(check.source, raw)
                self.assertIsNone(check.normalized_from)
                self.assertEqual(classify_source(raw), SOURCE_CLASS_HUMAN)

    def test_owner_mode_rejects_reserved_but_keeps_bare_human(self) -> None:
        err = check_source("system:x", default="agent", field="owner", normalize_human=False)
        assert err.error is not None
        self.assertEqual(err.error["error_code"], ERROR_SOURCE_RESERVED)
        self.assertEqual(err.error["field"], "owner")
        keep = check_source("human", default="agent", field="owner", normalize_human=False)
        self.assertEqual((keep.source, keep.normalized_from, keep.error), ("human", None, None))


def _profile() -> SessionProfile:
    return SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/fake",
        platform="prpl",
        uart=UartProfile(),
    )


class _ServiceBase(unittest.TestCase):
    """建 ``SerialwrapService`` 並注入 READY session（MagicMock bridge）。"""

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)  # #120 per-file 隔離（unittest 不載 conftest）
        self.svc = SerialwrapService([])
        self.session = sm_mod.SessionRuntime(session_id="p:COM0", profile=_profile())
        self.bridge = mock.MagicMock()
        self.session.bridge = self.bridge
        self.session.state = "READY"
        with self.svc._sessions._lock:
            self.svc._sessions._sessions[self.session.session_id] = self.session
        self.svc._arbiter.register_session(self.session.session_id)
        self.addCleanup(self.svc._arbiter.unregister_session, self.session.session_id)
        # submit-time READY 判定只看 state 快照；以固定快照避開 MagicMock bridge 的序列化。
        patcher = mock.patch.object(
            self.svc, "_resolve_session_id", return_value=("p:COM0", None)
        )
        self.resolve = patcher.start()
        self.addCleanup(patcher.stop)

    def _wait_terminal(self, cmd_id: str, timeout_s: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rec = self.svc.rpc("command.get", {"cmd_id": cmd_id}).get("command") or {}
            if rec.get("status") in ("done", "error", "interactive"):
                return rec
            time.sleep(0.02)
        self.fail(f"命令 {cmd_id} 在 {timeout_s}s 內未達終態")


class TestServiceBoundary(_ServiceBase):
    """RPC 邊界：被擋時零副作用；通過時 manager／arbiter 收到的是正規化後的值。"""

    def test_submit_reserved_source_rejected_before_queue(self) -> None:
        with mock.patch.object(self.svc._arbiter, "submit") as submit:
            for raw in ("system:recover", "system", "SYSTEM:self_test"):
                with self.subTest(raw=raw):
                    resp = self.svc.rpc(
                        "command.submit", {"selector": "COM0", "cmd": "echo hi", "source": raw}
                    )
                    self.assertFalse(resp["ok"])
                    self.assertEqual(resp["error_code"], ERROR_SOURCE_RESERVED)
                    self.assertNotIn("cmd_id", resp)
            submit.assert_not_called()
        self.resolve.assert_not_called()
        self.bridge.send_command.assert_not_called()
        self.bridge.send_bytes.assert_not_called()

    def test_submit_bare_human_normalized_and_annotated(self) -> None:
        self.bridge.rx_snapshot_len.return_value = 0
        self.bridge.wait_for_regex_from.return_value = True
        self.bridge.rx_text_from.return_value = "echo hi\nhi\n# "
        resp = self.svc.rpc("command.submit", {"selector": "COM0", "cmd": "echo hi", "source": "human"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["source"], "human:cli")
        self.assertEqual(resp["source_normalized_from"], "human")
        self.assertEqual(resp["source_class"], SOURCE_CLASS_HUMAN)
        rec = self._wait_terminal(resp["cmd_id"])
        self.assertEqual(rec["source"], "human:cli")
        self.assertEqual(rec["source_class"], SOURCE_CLASS_HUMAN)
        self.bridge.send_command.assert_called_once()
        self.assertEqual(self.bridge.send_command.call_args.kwargs["source"], "human:cli")

    def test_submit_unknown_and_agent_prefix_keep_agent_behavior(self) -> None:
        self.bridge.rx_snapshot_len.return_value = 0
        self.bridge.wait_for_regex_from.return_value = True
        self.bridge.rx_text_from.return_value = "x\n# "
        for raw in ("codex", "cli", "agent", "agent:diag", "human:minicom"):
            with self.subTest(raw=raw):
                resp = self.svc.rpc("command.submit", {"selector": "COM0", "cmd": "x", "source": raw})
                self.assertTrue(resp["ok"])
                self.assertNotIn("source_normalized_from", resp)
                expected = SOURCE_CLASS_HUMAN if raw.startswith("human:") else SOURCE_CLASS_AGENT
                self.assertEqual(resp["source_class"], expected)
                rec = self._wait_terminal(resp["cmd_id"])
                self.assertEqual(rec["source"], raw)
                self.assertEqual(rec["source_class"], expected)

    def test_submit_default_source_is_agent(self) -> None:
        self.bridge.rx_snapshot_len.return_value = 0
        self.bridge.wait_for_regex_from.return_value = True
        self.bridge.rx_text_from.return_value = "x\n# "
        resp = self.svc.rpc("command.submit", {"selector": "COM0", "cmd": "x"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["source_class"], SOURCE_CLASS_AGENT)
        self.assertEqual(self._wait_terminal(resp["cmd_id"])["source"], "agent")

    def test_human_console_line_unaffected(self) -> None:
        """minicom／console 路徑直接進 arbiter（``human:<client_id>``），不經外部驗證。"""
        self.bridge.rx_snapshot_len.return_value = 0
        self.bridge.wait_for_regex_from.return_value = True
        self.bridge.rx_text_from.return_value = "ls\n# "
        self.svc._on_console_line("p:COM0", "minicom", "ls")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.bridge.send_command.called:
            time.sleep(0.02)
        self.bridge.send_command.assert_called_once()
        self.assertEqual(self.bridge.send_command.call_args.kwargs["source"], "human:minicom")

    def test_file_push_pull_reserved_rejected_and_human_normalized(self) -> None:
        with mock.patch.object(self.svc._sessions, "file_push", side_effect=lambda *a, **k: {"ok": True}) as push, \
                mock.patch.object(self.svc._sessions, "file_pull", side_effect=lambda *a, **k: {"ok": True}) as pull:
            for method, params in (
                ("file.push", {"selector": "COM0", "local_path": "/tmp/a", "remote_path": "/tmp/b"}),
                ("file.pull", {"selector": "COM0", "remote_path": "/tmp/b"}),
            ):
                with self.subTest(method=method, case="reserved"):
                    resp = self.svc.rpc(method, {**params, "source": "system:recover"})
                    self.assertFalse(resp["ok"])
                    self.assertEqual(resp["error_code"], ERROR_SOURCE_RESERVED)
            push.assert_not_called()
            pull.assert_not_called()

            r1 = self.svc.rpc("file.push", {"selector": "COM0", "local_path": "/tmp/a",
                                            "remote_path": "/tmp/b", "source": "human"})
            r2 = self.svc.rpc("file.pull", {"selector": "COM0", "remote_path": "/tmp/b", "source": "human:"})
            r3 = self.svc.rpc("file.pull", {"selector": "COM0", "remote_path": "/tmp/b", "source": "codex"})
            self.assertEqual(push.call_args.kwargs["source"], "human:cli")
            self.assertEqual(r1["source_normalized_from"], "human")
            self.assertEqual(pull.call_args_list[0].kwargs["source"], "human:cli")
            self.assertEqual(r2["source_normalized_from"], "human:")
            self.assertEqual(pull.call_args_list[1].kwargs["source"], "codex")
            self.assertNotIn("source_normalized_from", r3)

    def test_device_release_reserved_rejected_and_human_normalized(self) -> None:
        with mock.patch.object(self.svc._sessions, "release_device", side_effect=lambda *a, **k: {"ok": True}) as rel:
            resp = self.svc.rpc("device.release", {"selector": "COM0", "source": "system:recover"})
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error_code"], ERROR_SOURCE_RESERVED)
            rel.assert_not_called()

            r1 = self.svc.rpc("device.release", {"selector": "COM0", "source": "human", "reason": "r"})
            rel.assert_called_once_with("COM0", source="human:cli", reason="r")
            self.assertEqual(r1["source_normalized_from"], "human")

            rel.reset_mock()
            self.svc.rpc("device.release", {"selector": "COM0"})
            rel.assert_called_once_with("COM0", source="cli", reason=None)  # 預設仍是 cli

    def test_interactive_open_owner_reserved_rejected_bare_human_untouched(self) -> None:
        with mock.patch.object(self.svc._sessions, "interactive_open", return_value={"ok": True}) as op:
            resp = self.svc.rpc("session.interactive_open", {"selector": "COM0", "owner": "system:recover"})
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error_code"], ERROR_SOURCE_RESERVED)
            self.assertEqual(resp["field"], "owner")
            op.assert_not_called()

            self.svc.rpc("session.interactive_open", {"selector": "COM0", "owner": "human"})
            self.assertEqual(op.call_args.kwargs["owner"], "human")  # 不正規化：避免開出無 peer 的 human lease
            self.svc.rpc("session.interactive_open", {"selector": "COM0"})
            self.assertEqual(op.call_args.kwargs["owner"], "agent")


class TestTimeoutBehaviorEndToEnd(_ServiceBase):
    """修的就是這個：source 字串決定命令逾時時送不送 CTRL-C／CTRL-D。"""

    def test_bare_human_timeout_promotes_interactive_without_keys(self) -> None:
        self.bridge.rx_snapshot_len.return_value = 10
        self.bridge.wait_for_regex_from.return_value = False
        resp = self.svc.rpc(
            "command.submit",
            {"selector": "COM0", "cmd": "vim notes.txt", "source": "human", "timeout_s": 0.1},
        )
        self.assertTrue(resp["ok"])
        rec = self._wait_terminal(resp["cmd_id"])
        self.assertEqual(rec["status"], "interactive")
        self.assertEqual(rec["recovery_action"], "PROMOTE_HUMAN_INTERACTIVE")
        self.bridge.send_bytes.assert_not_called()  # 沒有 CTRL-C／CTRL-D
        self.bridge.set_interactive_owner.assert_called_once_with("human:cli")

    def test_unknown_prefix_timeout_still_uses_agent_recovery(self) -> None:
        self.bridge.rx_snapshot_len.side_effect = [10, 10, 10, 20]
        self.bridge.wait_for_regex_from.side_effect = [False, True]
        self.bridge.rx_text_from.return_value = "# "
        resp = self.svc.rpc(
            "command.submit",
            {"selector": "COM0", "cmd": "printf 'broken", "source": "codex", "timeout_s": 0.1},
        )
        self.assertTrue(resp["ok"])
        rec = self._wait_terminal(resp["cmd_id"])
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["recovery_action"], "CTRL_C")
        self.assertEqual(rec["source_class"], SOURCE_CLASS_AGENT)
        self.bridge.send_bytes.assert_called_once_with(b"\x03", source="system:recover", cmd_id=None)

    def test_system_recover_cannot_trigger_force_recover_via_submit(self) -> None:
        """未擋時：source=system:recover 的命令逾時且 CTRL-C／D 都沒回 prompt →
        ``_force_recover``（clear + reattach）。擋下後命令根本不入列。"""
        self.bridge.rx_snapshot_len.return_value = 10
        self.bridge.wait_for_regex_from.return_value = False
        with mock.patch.object(self.svc._sessions, "_force_recover") as force:
            resp = self.svc.rpc(
                "command.submit",
                {"selector": "COM0", "cmd": "echo hi", "source": "system:recover", "timeout_s": 0.1},
            )
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error_code"], ERROR_SOURCE_RESERVED)
            time.sleep(0.3)
            force.assert_not_called()
        self.bridge.send_command.assert_not_called()
        self.bridge.send_bytes.assert_not_called()


class TestInternalSystemSourcesStillWork(unittest.TestCase):
    """內部 system 來源直接進 manager／bridge，不經外部驗證——照常運作。"""

    def setUp(self) -> None:
        td = state_iso.isolate_testcase(self)
        self.mgr = SessionManager(
            [_profile()],
            WalWriter(wal_dir=td),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        self.session = self.mgr.get_session("COM0")
        assert self.session is not None
        self.bridge = mock.MagicMock()
        self.bridge.rx_snapshot_len.return_value = 0
        self.bridge.rx_text_from.return_value = "# "
        self.session.bridge = self.bridge
        self.session.state = "READY"

    def test_recover_session_sends_system_recover_bytes(self) -> None:
        self.bridge.wait_for_regex_from.return_value = True
        resp = self.mgr.recover_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["recovery_action"], "CTRL_C")
        self.bridge.send_bytes.assert_called_once_with(b"\x03", source="system:recover", cmd_id=None)

    def test_recover_session_force_fallback_still_fires(self) -> None:
        self.bridge.wait_for_regex_from.return_value = False
        forced = {"ok": True, "recovered": True, "action": "FORCE_CLEAR_REATTACH", "session": {}}
        with mock.patch.object(self.mgr, "_force_recover", return_value=forced) as force:
            resp = self.mgr.recover_session("COM0", timeout_s=0.05)
        force.assert_called_once()
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT_FORCE_RECOVERED")
        self.assertEqual(resp["recovery_action"], "FORCE_CLEAR_REATTACH")

    def test_agent_command_timeout_does_not_force_recover(self) -> None:
        self.bridge.wait_for_regex_from.return_value = False
        with mock.patch.object(self.mgr, "_force_recover") as force:
            resp = self.mgr.execute_command("p:COM0", "echo hi", "agent:test", "cid-1", timeout_s=0.05)
        force.assert_not_called()
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT")


class TestCliPreflight(unittest.TestCase):
    """CLI 預檢：保留前綴即時拒絕（exit 1、不呼叫 RPC）；其餘原樣送出，由 daemon 正規化。"""

    CASES = [
        (["cmd", "submit", "--selector", "COM0", "--cmd", "x", "--source", "{v}"], "command.submit"),
        (["file", "push", "--selector", "COM0", "--local", "/tmp/a", "--remote", "/tmp/b", "--source", "{v}"], "file.push"),
        (["file", "pull", "--selector", "COM0", "--remote", "/tmp/b", "--source", "{v}"], "file.pull"),
        (["device", "release", "--selector", "COM0", "--source", "{v}"], "device.release"),
        (["session", "interactive-open", "--selector", "COM0", "--owner", "{v}"], "session.interactive_open"),
    ]

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)

    def _invoke(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_reserved_rejected_without_rpc(self) -> None:
        for template, method in self.CASES:
            for value in ("system:recover", "system"):
                with self.subTest(method=method, value=value):
                    argv = [a.replace("{v}", value) for a in template]
                    with mock.patch("sw_core.cli.rpc_call") as rpc:
                        rc, out, err = self._invoke(argv)
                    rpc.assert_not_called()
                    self.assertEqual(rc, 1)
                    body = json.loads(out)
                    self.assertFalse(body["ok"])
                    self.assertEqual(body["error_code"], ERROR_SOURCE_RESERVED)
                    self.assertIn(ERROR_SOURCE_RESERVED, err)

    def test_other_values_sent_through(self) -> None:
        for template, method in self.CASES:
            for value in ("agent:diag", "codex", "human:minicom", "human"):
                with self.subTest(method=method, value=value):
                    argv = [a.replace("{v}", value) for a in template]
                    with mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}) as rpc:
                        rc, _out, _err = self._invoke(argv)
                    self.assertEqual(rc, 0)
                    sent_method, sent_params = rpc.call_args.args[1], rpc.call_args.args[2]
                    self.assertEqual(sent_method, method)
                    sent = sent_params["owner" if method == "session.interactive_open" else "source"]
                    self.assertEqual(sent, value)  # 正規化由 daemon 權威處理，CLI 不改寫


if __name__ == "__main__":
    unittest.main()
