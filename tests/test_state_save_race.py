"""#133：Windows 上 `_save_state` 並發 `os.replace` 撞 WinError 5 的修復。

多條 attach 執行緒（#100 並行 attach）同時對 `state.json` 做 unique-temp +
`os.replace`；Windows 的 `MoveFileEx(REPLACE_EXISTING)` 對「目的檔正被另一個
replace／讀者持有 handle」會 `PermissionError: [WinError 5]`，POSIX `rename()`
無此限制——「唯一 temp 名 → 並發安全、last-writer-wins」只在 POSIX 成立。

修復：instance 級 `_state_io_lock` 序列化 I/O 段（消除行程內並發 replace）＋
`_replace_state_file()` 於 Windows 對 PermissionError 短退避重試（外部讀者／
防毒的瞬時 handle），重試耗盡才上拋；POSIX 路徑不重試、行為不變。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sw_core.session_manager as sm_mod
from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter


def _make_profile(com: str = "COM0", by_id: str = "/dev/serial/by-id/dev0") -> SessionProfile:
    return SessionProfile(
        profile_name="p", com=com, act_no=1, alias=None,
        device_by_id=by_id, platform="prpl", uart=UartProfile(),
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))
        self.mgr = SessionManager(
            [_make_profile()], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )


class TestReplaceStateFileRetry(_Base):
    def test_retries_on_permissionerror_when_forced(self) -> None:
        """瞬時 PermissionError（外部讀者/防毒短暫持有 handle）→ 退避重試後成功。"""
        calls: list[int] = []

        def flaky(src: str, dst: str) -> None:
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(5, "存取被拒")
            # 第三次視為成功（不真的動檔案系統）

        with (
            mock.patch.object(sm_mod.os, "replace", side_effect=flaky),
            mock.patch.object(sm_mod.time, "sleep"),  # 退避不真睡
        ):
            sm_mod._replace_state_file("ignored-src", "ignored-dst", retry=True)
        self.assertEqual(len(calls), 3)

    def test_exhausted_retries_reraise(self) -> None:
        with (
            mock.patch.object(
                sm_mod.os, "replace", side_effect=PermissionError(5, "存取被拒")
            ) as rep,
            mock.patch.object(sm_mod.time, "sleep"),
            self.assertRaises(PermissionError),
        ):
            sm_mod._replace_state_file("src", "dst", retry=True)
        self.assertGreater(rep.call_count, 1)  # 有重試而非首錯即拋

    def test_no_retry_on_posix_semantics(self) -> None:
        """retry=False（POSIX 預設）：行為與裸 os.replace 相同，首錯即拋。"""
        with (
            mock.patch.object(
                sm_mod.os, "replace", side_effect=PermissionError(5, "存取被拒")
            ) as rep,
            self.assertRaises(PermissionError),
        ):
            sm_mod._replace_state_file("src", "dst", retry=False)
        self.assertEqual(rep.call_count, 1)

    def test_default_follows_platform(self) -> None:
        """retry 預設依 os.name（nt → 重試；posix → 不重試）。"""
        with (
            mock.patch.object(
                sm_mod.os, "replace", side_effect=PermissionError(5, "存取被拒")
            ) as rep,
            mock.patch.object(sm_mod.time, "sleep"),
            self.assertRaises(PermissionError),
        ):
            sm_mod._replace_state_file("src", "dst")
        if os.name == "nt":
            self.assertGreater(rep.call_count, 1)
        else:
            self.assertEqual(rep.call_count, 1)


class TestSaveStateSerialized(_Base):
    def test_concurrent_save_state_serialized_and_no_exception(self) -> None:
        """行程內並發 _save_state：I/O 段序列化 → 不互踩、不拋例外、檔案恆為完整 JSON。"""
        errors: list[BaseException] = []
        active = 0
        max_active = 0
        gate = threading.Lock()
        real_replace = os.replace

        def counting_replace(src: str, dst: str) -> None:
            nonlocal active, max_active
            with gate:
                active += 1
                max_active = max(max_active, active)
            try:
                real_replace(src, dst)
            finally:
                with gate:
                    active -= 1

        def worker() -> None:
            try:
                for _ in range(20):
                    self.mgr._save_state()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with mock.patch.object(sm_mod.os, "replace", side_effect=counting_replace):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10.0)

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1, "os.replace 必須被 _state_io_lock 序列化")
        with open(sm_mod.STATE_PATH, encoding="utf-8") as fh:
            json.load(fh)  # 完整 JSON


if __name__ == "__main__":
    unittest.main()
