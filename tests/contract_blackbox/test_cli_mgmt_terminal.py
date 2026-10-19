"""P4-T14 終端類承接黑箱：`serialwrap-minicom` 以 Rust CLI 執行時的 attach／detach 行為。

- wrapper 來源為 `sw_core/assets/tools/minicom_router.sh`（`setup` 物化的同一來源檔）。mgmt_bundle 不含物化結果，
  因此本檔於 bundle 的 `bin/` 下複製為 `serialwrap-minicom`（比照 P4-T10 物化結果，不改動內容）。
- minicom 以 `stubs/bin/minicom` 取代，只記錄 `-D <vtty>` 並依 `SW_STUB_MINICOM_HOLD_S` 停留。
- daemon 一律在隔離 env（`SERIALWRAP_*` 目錄、HOME、XDG）內執行；socket 與 PTY 皆為測試專用。
- 所有案例只在 Rust 輪執行（`rust_only`），並需要 POSIX PTY。

紅燈階段：`test_minicom_wrapper_auto_start_daemon_on_demand` 依賴 Rust `daemon start`（P4-T7）與委派清空（P4-T14）；
目前 `daemon start` 與 `supervision-mode` 仍委派，故以 `xfail(strict=True)` 標示。`attach_detach` 與
`sigterm` 案例在 P3 原生 console-attach／console-detach 上已實測 PASS（wrapper 在 live daemon 已就緒時不呼叫
`supervision-mode`），因此不標 xfail；它們是承接驗證，須附突變檢查（計畫 §0.1）。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from bb_daemon import _send_rpc
from bb_faketarget import FakeTarget
from bb_harness import repo_root
from mgmt_fixtures import LIVE_PROFILE_YAML, STUBS_BIN, run_wrapper

pytestmark = [pytest.mark.rust_only, pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

WRAPPER_SRC = repo_root() / "sw_core" / "assets" / "tools" / "minicom_router.sh"


def _materialize_wrapper(bundle) -> Path:
    """把 wrapper 複製到 bundle 的 `bin/serialwrap-minicom`（可執行），來源與 `setup` 物化相同。"""
    dst = bundle.materialized_bin / "serialwrap-minicom"
    shutil.copyfile(WRAPPER_SRC, dst)
    dst.chmod(0o755)
    return dst


def _set_wrapper_env(env: dict[str, str], bundle, *, socket: str | None, hold_s: str, auto_start: str) -> None:
    env["SERIALWRAP_BIN"] = str(bundle.cli)
    env["MINICOM_BIN"] = str(STUBS_BIN / "minicom")
    env["MINICOM_AUTO_CAPTURE"] = "0"
    env["SW_STUB_MINICOM_HOLD_S"] = hold_s
    env["SERIALWRAP_AUTO_START_DAEMON"] = auto_start
    if socket is None:
        env.pop("SERIALWRAP_SOCKET", None)
    else:
        env["SERIALWRAP_SOCKET"] = socket


def _minicom_rows(stub_log) -> list[dict]:
    return [row for row in stub_log() if row["tool"] == "minicom"]


def _vtty_of(row: dict) -> str:
    argv = row["argv"]
    return argv[argv.index("-D") + 1]


def _minicom_clients(consoles: list[dict]) -> list[dict]:
    """wrapper 以 `--label minicom:$$` attach 的 console（daemon 另有自動建立的 `primary` console，不屬本案例）。"""
    return [c for c in consoles if str(c.get("label", "")).startswith("minicom:")]


def test_minicom_wrapper_attach_detach_via_rust_cli(mgmt_bundle, live_daemon, env, stub_path, stub_log):
    _materialize_wrapper(mgmt_bundle)
    wrapper = mgmt_bundle.materialized_bin / "serialwrap-minicom"
    _set_wrapper_env(env, mgmt_bundle, socket=live_daemon.endpoint, hold_s="1", auto_start="0")
    baseline = [c["client_id"] for c in live_daemon.console_list("COM0")]

    p = run_wrapper(wrapper, ["COM0"], env)

    assert p.returncode == 0, p.stderr
    minicom = _minicom_rows(stub_log)
    assert len(minicom) == 1 and "-D" in minicom[0]["argv"]
    assert _vtty_of(minicom[0]).startswith("/dev/pts/")
    after = live_daemon.console_list("COM0")
    assert _minicom_clients(after) == [], "wrapper 結束後 minicom client 應已 detach"
    assert [c["client_id"] for c in after] == baseline, "detach 不得移除 daemon 既有 console"


def test_minicom_wrapper_sigterm_still_detaches(mgmt_bundle, live_daemon, env, stub_path, stub_log):
    _materialize_wrapper(mgmt_bundle)
    wrapper = mgmt_bundle.materialized_bin / "serialwrap-minicom"
    _set_wrapper_env(env, mgmt_bundle, socket=live_daemon.endpoint, hold_s="5", auto_start="0")
    baseline = [c["client_id"] for c in live_daemon.console_list("COM0")]

    proc = subprocess.Popen(
        ["bash", str(wrapper), "COM0"],
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20.0
        while not _minicom_rows(stub_log):
            assert proc.poll() is None, f"wrapper 在 minicom 啟動前結束：{proc.stderr.read()}"
            assert time.monotonic() < deadline, "minicom 未在時限內啟動"
            time.sleep(0.1)
        assert _minicom_clients(live_daemon.console_list("COM0")), "minicom 啟動時 console 應已 attach"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    after = live_daemon.console_list("COM0")
    assert _minicom_clients(after) == [], "SIGTERM 後 trap 仍須送出 console-detach"
    assert [c["client_id"] for c in after] == baseline, "detach 不得移除 daemon 既有 console"


@pytest.mark.xfail(
    strict=True,
    reason="P4-T14 紅燈：daemon start／supervision-mode 仍委派 Python（P4-T7、P4-T14 未完成）",
)
def test_minicom_wrapper_auto_start_daemon_on_demand(mgmt_bundle, env, stub_path, stub_log, tmp_path):
    _materialize_wrapper(mgmt_bundle)
    wrapper = mgmt_bundle.materialized_bin / "serialwrap-minicom"
    # DF-05 第二順位：與 CLI 同目錄的 serialwrapd。以 shim 指向本 repo 的 Python daemon（與 bb_harness.daemon_prefix 同源）。
    launcher = mgmt_bundle.materialized_bin / "serialwrapd"
    launcher.write_text(
        f'#!/bin/sh\nexec python3 "{repo_root() / "serialwrapd.py"}" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)

    # 只準備 FakeTarget 與 profile，不預先啟動 daemon，讓 wrapper 走 on-demand auto-start。
    target = FakeTarget(tmp_path / "auto-target")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    if target.slave_fd is not None:
        os.close(target.slave_fd)
        target.slave_fd = None
    profile_dir = Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "live.yaml").write_text(LIVE_PROFILE_YAML, encoding="utf-8")
    sock = str(Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock")

    _set_wrapper_env(env, mgmt_bundle, socket=None, hold_s="1", auto_start="1")
    try:
        p = run_wrapper(wrapper, ["COM0"], env)

        assert p.returncode == 0, p.stderr
        minicom = _minicom_rows(stub_log)
        assert len(minicom) == 1 and "-D" in minicom[0]["argv"]
        assert _vtty_of(minicom[0]).startswith("/dev/pts/")
        resp = _send_rpc(sock, "session.console_list", {"selector": "COM0"}, timeout_s=5.0)
        assert resp is not None and resp.get("ok") is True, resp
        assert _minicom_clients(resp.get("consoles") or []) == [], "wrapper 結束後 minicom client 應已 detach"
    finally:
        _send_rpc(sock, "daemon.stop", {}, timeout_s=5.0)
        target.stop()
