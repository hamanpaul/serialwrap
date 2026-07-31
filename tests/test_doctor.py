"""serialwrap doctor 環境診斷的單元測試。

- Linux 檢查清單（順序與內容）逐字 pin（#131：doctor 平台感知後不得改變）。
- dialout 群組缺漏時 ok=False 並給出 usermod 修復提示。
- Windows（#131 點 4）：檢查清單改為 pyserial／PATH／daemon endpoint／SERIALCOMM
  裝置列舉，不再有 dialout／systemd／wsl_systemd／single_daemon。
- #154：`serialwrapd_on_path` 後新增 `other_serialwrap_installs`（同機多份安裝
  版本一致性診斷），Linux／Windows 兩份清單皆刻意更新（非誤傷）。
- #148：新增 `wal_dir` 檢查，兩份清單同步更新。
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

from sw_core.doctor_cmd import run_doctor
from sw_core.sysenv import FakeEffects


LINUX_CHECKS = [
    "python",
    "pyyaml",
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "other_serialwrap_installs",
    "dialout",
    "systemd",
    "supervision_mode",
    "single_daemon",
    "wal_dir",
    "devices",
    "wsl_systemd",
]

WINDOWS_CHECKS = [
    "python",
    "pyyaml",
    "pyserial",
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "other_serialwrap_installs",
    "supervision_mode",
    "daemon_endpoint",
    "wal_dir",
    "devices",
]


def test_doctor_linux_check_list_pinned():
    """Linux 清單順序逐字不變（#131 平台感知後的回歸 pin）。"""
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
    assert [i["check"] for i in report] == LINUX_CHECKS


def test_doctor_reports_dialout_missing_with_fix():
    fx = FakeEffects(systemd=True, in_groups=set())  # 不在 dialout
    report = run_doctor(fx=fx, platform="linux")
    item = next(i for i in report if i["check"] == "dialout")
    assert item["ok"] is False
    assert "usermod -aG dialout" in item["fix"]


def test_doctor_dialout_ok_when_member():
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
    item = next(i for i in report if i["check"] == "dialout")
    assert item["ok"] is True and item["fix"] == ""


def test_doctor_python_check_passes_on_current_interpreter():
    report = run_doctor()
    assert next(i for i in report if i["check"] == "python")["ok"] is True


class TestWindowsDoctor:
    def _win_report(self, **patches):
        with (
            mock.patch("sw_core.lock_win._endpoint_alive", patches.get("alive", lambda ep: True)),
            mock.patch(
                "sw_core.device_source._read_serialcomm",
                lambda: patches.get("serialcomm", {r"\Device\VCP2": "COM5", r"\Device\VCP3": "COM7"}),
            ),
            mock.patch(
                "sw_core.device_source._read_bt_ports",
                lambda: patches.get("bt", set()),
            ),
        ):
            return run_doctor(fx=FakeEffects(systemd=False, in_groups=set()), platform="win32")

    def test_windows_check_list(self):
        report = self._win_report()
        assert [i["check"] for i in report] == WINDOWS_CHECKS

    def test_windows_has_no_linux_only_checks(self):
        names = {i["check"] for i in self._win_report()}
        assert names.isdisjoint({"dialout", "systemd", "wsl_systemd", "single_daemon"})

    def test_pyserial_ok_when_importable(self):
        item = next(i for i in self._win_report() if i["check"] == "pyserial")
        # 本測試環境已裝 pyserial（CI 亦顯式安裝）→ ok。
        assert item["ok"] is True and item["fix"] == ""

    def test_pyserial_missing_reports_fix(self):
        with mock.patch.dict(sys.modules, {"serial": None}):
            report = self._win_report()
        item = next(i for i in report if i["check"] == "pyserial")
        assert item["ok"] is False
        assert "pyserial" in item["fix"]

    def test_daemon_endpoint_alive(self):
        item = next(i for i in self._win_report(alive=lambda ep: True) if i["check"] == "daemon_endpoint")
        assert item["ok"] is True

    def test_daemon_endpoint_down_suggests_daemon_start(self):
        item = next(i for i in self._win_report(alive=lambda ep: False) if i["check"] == "daemon_endpoint")
        assert item["ok"] is False
        assert "daemon start" in item["fix"]

    def test_daemon_endpoint_ignores_stale_unix_config(self):
        """config 殘留非 tcp:// 的 unix socket_path → 視為缺席、探測 canonical，
        不與 CLI 的 #108 fallback 行為分歧（#131 review）。"""
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "/run/user/1000/serialwrap/serialwrapd.sock"
        probed: list[str] = []

        def _probe(ep):
            probed.append(ep)
            return True

        with mock.patch("sw_core.cli._safe_runtime_config", return_value=fake_rc):
            report = self._win_report(alive=_probe)
        item = next(i for i in report if i["check"] == "daemon_endpoint")
        assert item["ok"] is True
        assert "/run/user/1000" not in item["detail"]
        assert probed and not probed[0].startswith("/run/user")

    def test_devices_lists_coms_and_excludes_bluetooth(self):
        report = self._win_report(
            serialcomm={
                r"\Device\VCP2": "COM5",
                r"\Device\BthModem0": "COM3",
            },
            bt={"COM3"},
        )
        item = next(i for i in report if i["check"] == "devices")
        assert item["ok"] is True
        assert "COM5" in item["detail"]
        assert "COM3" not in item["detail"].split("排除")[0]  # 保留清單不含藍牙

    def test_devices_none_after_exclusion_not_ok(self):
        report = self._win_report(
            serialcomm={r"\Device\BthModem0": "COM3"},
            bt={"COM3"},
        )
        item = next(i for i in report if i["check"] == "devices")
        assert item["ok"] is False


class TestDoctorNeverRaises:
    """run_doctor 契約「每項檢查永不拋例外」（#132 Copilot review）：
    config.yaml 損壞（RuntimeConfig 建構即拋）時 supervision_mode 檢查
    須回結構化結果（退化 on-demand），不得讓 doctor 崩潰。"""

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_broken_config_does_not_crash_doctor(self, platform: str) -> None:
        with (
            mock.patch("sw_core.cli._default_runtime_config", side_effect=ValueError("bad yaml")),
            mock.patch("sw_core.lock_win._endpoint_alive", lambda ep: False),
            mock.patch("sw_core.device_source._read_serialcomm", lambda: {}),
            mock.patch("sw_core.device_source._read_bt_ports", lambda: set()),
        ):
            report = run_doctor(fx=FakeEffects(systemd=False, in_groups=set()), platform=platform)
        item = next(i for i in report if i["check"] == "supervision_mode")
        assert item["ok"] is True
        assert item["detail"] == "on-demand"


class TestCliAdvisorySets:
    def test_win_advisory_set_keeps_overall_ok(self):
        """win advisory 全掛仍 ok=True；pyserial（非 advisory）掛 → False。"""
        from sw_core import cli

        report = [
            {"check": "python", "ok": True},
            {"check": "pyyaml", "ok": True},
            {"check": "pyserial", "ok": True},
            {"check": "serialwrap_on_path", "ok": False},
            {"check": "serialwrapd_on_path", "ok": False},
            {"check": "other_serialwrap_installs", "ok": False},
            {"check": "supervision_mode", "ok": True},
            {"check": "daemon_endpoint", "ok": False},
            {"check": "wal_dir", "ok": True},
            {"check": "devices", "ok": False},
        ]
        assert "wal_dir" in cli._DOCTOR_ADVISORY_CHECKS_WIN
        assert all(
            item["ok"] or item["check"] in cli._DOCTOR_ADVISORY_CHECKS_WIN for item in report
        )
        report[-2]["ok"] = False  # wal_dir 掛（shell/daemon 不一致）仍應被 advisory 吸收
        assert all(
            item["ok"] or item["check"] in cli._DOCTOR_ADVISORY_CHECKS_WIN for item in report
        )
        report[2]["ok"] = False  # pyserial 掛（非 advisory）
        assert not all(
            item["ok"] or item["check"] in cli._DOCTOR_ADVISORY_CHECKS_WIN for item in report
        )

    def test_linux_advisory_set_includes_other_serialwrap_installs(self):
        """#154：新增 other_serialwrap_installs 為 advisory——純診斷資訊，偵測到
        多份不同版本安裝也不應讓整體 doctor 判定失敗（呼應 (b) 的「勿擋」精神）。
        #148：wal_dir（shell/daemon WAL_DIR 不一致）亦為 advisory、僅 WARN 不擋。"""
        from sw_core import cli

        assert cli._DOCTOR_ADVISORY_CHECKS == {
            "systemd", "wsl_systemd", "devices", "other_serialwrap_installs", "wal_dir",
        }


class TestOtherSerialwrapInstalls:
    """#154：`_check_other_serialwrap_installs`——同機 PATH 上多份 serialwrap 安裝的
    版本一致性診斷。"""

    def _item(self, report):
        return next(i for i in report if i["check"] == "other_serialwrap_installs")

    def test_no_path_match_is_ok_and_skips_subprocess(self):
        fx = FakeEffects(systemd=True, in_groups={"dialout"})  # which_all 預設空
        report = run_doctor(fx=fx, platform="linux")
        item = self._item(report)
        assert item["ok"] is True
        assert fx.calls == []

    def test_single_install_is_ok_and_skips_subprocess(self):
        fx = FakeEffects(
            systemd=True, in_groups={"dialout"},
            which_all={"serialwrap": ["/usr/local/bin/serialwrap"]},
        )
        report = run_doctor(fx=fx, platform="linux")
        item = self._item(report)
        assert item["ok"] is True
        assert item["detail"] == "僅偵測到目前這份"
        assert fx.calls == []  # trivially single 時不跑 subprocess（效能設計）

    def test_two_installs_same_version_is_ok(self):
        path_a, path_b = "/opt/a/serialwrap", "/opt/b/serialwrap"
        fx = FakeEffects(
            systemd=True, in_groups={"dialout"},
            which_all={"serialwrap": [path_a, path_b]},
            commands={
                (path_a, "--version"): (0, "serialwrap 0.2.4", ""),
                (path_b, "--version"): (0, "serialwrap 0.2.4", ""),
            },
        )
        report = run_doctor(fx=fx, platform="linux")
        item = self._item(report)
        assert item["ok"] is True
        assert path_a in item["detail"] and path_b in item["detail"]

    def test_two_installs_different_version_is_not_ok(self):
        path_a, path_b = "/opt/a/serialwrap", "/opt/b/serialwrap"
        fx = FakeEffects(
            systemd=True, in_groups={"dialout"},
            which_all={"serialwrap": [path_a, path_b]},
            commands={
                (path_a, "--version"): (0, "serialwrap 0.2.4", ""),
                (path_b, "--version"): (0, "serialwrap 0.2.1", ""),
            },
        )
        report = run_doctor(fx=fx, platform="linux")
        item = self._item(report)
        assert item["ok"] is False
        assert "0.2.4" in item["detail"] and "0.2.1" in item["detail"]
        assert item["fix"]

    def test_timeout_or_nonzero_rc_marked_unavailable_not_raised(self):
        """某路徑 --version 逾時／非零 rc → 該筆列為「無法取得」，不拋例外；
        與其他已解析版本不同即 ok=False（可疑訊號，不靜默吞掉）。"""
        path_a, path_b = "/opt/a/serialwrap", "/opt/b/serialwrap"
        fx = FakeEffects(
            systemd=True, in_groups={"dialout"},
            which_all={"serialwrap": [path_a, path_b]},
            commands={
                (path_a, "--version"): (0, "serialwrap 0.2.4", ""),
                (path_b, "--version"): (-1, "", "TIMEOUT"),
            },
        )
        report = run_doctor(fx=fx, platform="linux")
        item = self._item(report)
        assert item["ok"] is False
        assert "無法取得" in item["detail"]

    def test_run_called_with_short_timeout(self):
        path_a, path_b = "/opt/a/serialwrap", "/opt/b/serialwrap"
        fx = FakeEffects(
            systemd=True, in_groups={"dialout"},
            which_all={"serialwrap": [path_a, path_b]},
        )
        run_doctor(fx=fx, platform="linux")
        assert fx.timeouts and all(t == 2.0 for t in fx.timeouts)


class TestWalDirCheck:
    """#148：doctor 印出 daemon 實際生效 WAL_DIR，shell 覆寫不一致時 WARN。"""

    def _item(self, monkeypatch, *, reachable, wal_path=None, env=None, platform="linux"):
        def _fake_rpc(endpoint, method, params, timeout_s=0.5, **kw):
            assert method == "health.status"
            if not reachable:
                return {"ok": False, "error_code": "SOCKET_ERROR"}
            return {"ok": True, "wal_path": wal_path}
        monkeypatch.setattr("sw_core.client.rpc_call", _fake_rpc)
        monkeypatch.setattr("sw_core.cli._safe_runtime_config", lambda: None)
        if env is None:
            monkeypatch.delenv("SERIALWRAP_WAL_DIR", raising=False)
        else:
            monkeypatch.setenv("SERIALWRAP_WAL_DIR", env)
        report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform=platform)
        return next(i for i in report if i["check"] == "wal_dir")

    def test_daemon_unreachable_is_informational_ok(self, monkeypatch):
        item = self._item(monkeypatch, reachable=False)
        assert item["ok"] is True
        assert item["fix"] == ""

    def test_daemon_reachable_no_shell_override_is_ok(self, monkeypatch):
        # 刻意避開 /home/<user>/ 前綴（R-21 結構偵測器會誤觸個人絕對路徑掃描）。
        item = self._item(monkeypatch, reachable=True,
                           wal_path="/srv/serialwrap-state/wal/raw.wal.ndjson")
        assert item["ok"] is True
        assert "/srv/serialwrap-state/wal" in item["detail"]

    def test_shell_override_matching_daemon_is_ok(self, monkeypatch):
        item = self._item(monkeypatch, reachable=True,
                           wal_path="/srv/custom-wal/raw.wal.ndjson",
                           env="/srv/custom-wal")
        assert item["ok"] is True

    def test_shell_override_mismatch_warns_with_systemd_hint(self, monkeypatch):
        item = self._item(monkeypatch, reachable=True,
                           wal_path="/srv/serialwrap-state/wal/raw.wal.ndjson",
                           env="/srv/b-log")
        assert item["ok"] is False
        assert "b-log" in item["detail"] and "serialwrap-state/wal" in item["detail"]
        assert "systemd" in item["fix"] and "Environment=" in item["fix"]

    def test_wal_dir_check_present_in_linux_and_windows_lists(self, monkeypatch):
        monkeypatch.delenv("SERIALWRAP_WAL_DIR", raising=False)
        with mock.patch("sw_core.client.rpc_call", lambda *a, **k: {"ok": False}):
            linux = {i["check"] for i in run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")}
            win = {i["check"] for i in run_doctor(fx=FakeEffects(systemd=False, in_groups=set()), platform="win32")}
        assert "wal_dir" in linux and "wal_dir" in win
