"""``f8-legacy-attached-upgrade``（SC-39 D5.2，PR 250）實機 case 的純邏輯替身測試；不接觸 live daemon／UART。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import harness  # noqa: E402
from serialwrap_regression.cases import f08_legacy_upgrade as case  # noqa: E402
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _success_body(**legacy_stop) -> dict:
    stop = {"method": "systemctl_stop", "waited_ms": 9000, "stability_window_ms": 5000, **legacy_stop}
    return {"ok": True, "install": {"steps": [{"step": "stop_supervisor", "detail": {"legacy_stop": stop}}]}}


def _obs(**overrides) -> dict:
    obs = {
        "setup_rc": 0,
        "body": _success_body(holders_status="partial", uninspectable_procs=[
            {"pid": 408, "start_ticks": 1408, "comm": "(sd-pam)", "reason": "fd_dir_denied"},
        ]),
        "elapsed_s": 14.2,
        "old_tree_alive": [],
        "daemons_after": [5252],
        "expected_version": "serialwrap 0.4.1",
        "version_after": "0.4.1",
        "attached_before": {"COM0": "/dev/ttyUSB0"},
        "sessions_after": [{"com": "COM0", "state": "READY", "attached_real_path": "/dev/ttyUSB0"}],
    }
    obs.update(overrides)
    return obs


def _verdict(obs: dict) -> tuple[str, str, str]:
    result = case.evaluate_legacy_attached_upgrade(obs)
    return result.verdict, result.category, result.reason_code


def test_case_is_registered_as_pending_issue_destructive_f8():
    reg = {c.id: c for c in harness.load_registry()}
    item = reg["f8-legacy-attached-upgrade"]
    assert item.family == "F8"
    assert item.pending_issue is True and item.issues == (), "擁有者尚未開 issue：不得填假號"
    assert item.destructive is True, "會替換部署中的安裝"
    assert "supervisor_runtime" in item.requires


def test_bounded_switch_with_reported_partial_holders_passes():
    assert _verdict(_obs()) == ("PASS", "", "")
    assert _verdict(_obs(body=_success_body())) == ("PASS", "", ""), "沒有無法檢查的行程時不需要 partial 標註"


def test_original_defect_timeout_legacy_stop_pending_fails():
    body = {"ok": False, "error_code": "TIMEOUT", "reason": "legacy_stop_pending", "holders_status": "permission",
            "alive_pids": [], "holder_pids": [], "waited_ms": 330000}
    assert _verdict(_obs(setup_rc=2, body=body, elapsed_s=331.0)) == ("FAIL", "test", "legacy_stop_pending")


def test_setup_not_returning_fails_as_hung():
    assert _verdict(_obs(setup_rc=None, body=None, elapsed_s=420.0)) == ("FAIL", "test", "setup_hung")


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"setup_rc": 2, "body": {"ok": False, "error_code": "DEVICE_STILL_HELD"}}, "setup_failed"),
        ({"elapsed_s": 120.0}, "setup_too_slow"),
        ({"body": {"ok": True}}, "legacy_stop_missing"),
        ({"body": _success_body(uninspectable_procs=[{"pid": 408, "comm": "(sd-pam)", "reason": "fd_dir_denied"}])},
         "uninspectable_not_marked_partial"),
        ({"old_tree_alive": [4242]}, "legacy_tree_alive"),
        ({"daemons_after": [4242, 5252]}, "two_reader"),
        ({"daemons_after": []}, "two_reader"),
        ({"version_after": "0.3.2"}, "new_daemon_version_mismatch"),
        ({"version_after": None}, "new_daemon_version_mismatch"),
        ({"sessions_after": [{"com": "COM0", "state": "DETACHED", "attached_real_path": None}]}, "uart_not_reattached"),
        ({"sessions_after": [{"com": "COM0", "state": "READY", "attached_real_path": "/dev/ttyUSB1"}]}, "uart_not_reattached"),
        ({"sessions_after": []}, "uart_not_reattached"),
    ],
)
def test_each_broken_invariant_fails_with_its_reason_code(overrides, reason_code):
    assert _verdict(_obs(**overrides)) == ("FAIL", "test", reason_code)


def test_legacy_version_detection_rejects_unknown_versions():
    assert case.is_legacy_version("0.3.2")
    assert case.is_legacy_version("serialwrap 0.3.0")
    assert not case.is_legacy_version("0.4.0")
    assert not case.is_legacy_version("1.0.0")
    assert not case.is_legacy_version(None), "版本不明不得視為前置條件成立"
    assert not case.is_legacy_version("unknown")


def _fake_proc(root: Path, pid: int, uids: tuple[int, int, int, int], comm: str, argv: list[str]) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "status").write_text("Name:\t{}\nUid:\t{}\t{}\t{}\t{}\n".format(comm, *uids), encoding="utf-8")
    (d / "comm").write_text(comm + "\n", encoding="utf-8")
    (d / "cmdline").write_bytes(b"".join(part.encode() + b"\0" for part in argv))
    # `)` 之後第 20 個欄位（索引 19）為 starttime。
    (d / "stat").write_text(f"{pid} ({comm}) S 1" + " 0" * 17 + f" {1000 + pid}\n", encoding="utf-8")


def test_process_snapshot_requires_all_four_uids_and_detects_user_session(tmp_path):
    _fake_proc(tmp_path, 407, (1000, 1000, 1000, 1000), "systemd", ["/usr/lib/systemd/systemd", "--user"])
    _fake_proc(tmp_path, 408, (1000, 1000, 1000, 1000), "(sd-pam)", ["(sd-pam)"])
    _fake_proc(tmp_path, 4242, (1000, 1000, 1000, 1000), "python3",
               ["/usr/bin/python3", "/opt/a/.local/bin/serialwrapd", "--socket", "/run/user/1000/s.sock"])
    _fake_proc(tmp_path, 900, (1000, 10, 10, 10), "cu", ["cu", "-l", "/dev/ttyUSB0"])
    _fake_proc(tmp_path, 901, (0, 0, 0, 0), "serialwrapd", ["/usr/bin/serialwrapd"])
    procs = case.same_uid_processes(tmp_path, uid=1000)
    assert [p["pid"] for p in procs] == [407, 408, 4242], "setuid 工具與他 uid 行程不算同使用者"
    assert procs[1]["start_ticks"] == 1408
    assert case.has_systemd_user_session(procs)
    assert not case.has_systemd_user_session([p for p in procs if p["comm"] != "(sd-pam)"])
    assert case.serialwrapd_pids(procs) == [4242]
