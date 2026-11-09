"""P8 Task 14（09-slice24-transfer-flash.md）：更新／回滾守衛對 Rust daemon 的黑箱（A/B：Python 基線與 Rust artifact
同檔各跑一次；CLI 一律為 dev bundle 內的 Rust CLI——更新／回滾屬 P4 Rust 管理面，Python CLI 無此流程）。

案例（tests/acceptance/cases.yaml，Slice 2.4 凍結時登記的節點，`s24-upd-guards`，L1.5）：
- `test_rollback_refuses_while_push`：file push 進行中 `setup --rollback` → `SESSION_BUSY`；參數化 `timing`：
  `before_probe`（預檢即見忙碌）與 `after_probe`（dev bundle 以 `SW_TEST_UPDATE_PAUSE_AFTER=safety_check` 在預檢後暫停，
  期間才開始 push，放行後由 daemon 的原子交接 `daemon.handoff_stop` 拒絕，`handoff:"refused"`；計畫
  `rollback_refuses_push_started_after_probe`）；
- `test_rollback_refuses_while_flashing`：FLASHING 中 `setup --rollback` → `FLASHING_BUSY`，flash 照常完成；
- `test_rollback_force_refuses_while_push`／`test_rollback_force_refuses_while_flashing`：同上帶 `--force`，並以
  `setup --force`（bundle 更新）各再跑一次：`--force` 只記 `force_ignored:true`，拒絕結果不變；
- `test_rollback_refuses_while_interactive_send`：interactive lease 開啟、`interactive_send` 進行中 → `SESSION_BUSY`，
  DUT 收到完整 payload；
- `test_supervision_set_force_refuses_while_push`（計畫 `supervision_set_force_refuses_rust_daemon_pushing`，SC-22／R1-F3）：
  file push 進行中 `supervision-mode set systemd-user --force`（PATH 前置 stub `systemctl`／`loginctl`／`sudo`）→ `SESSION_BUSY`、
  `phase:"supervision_set_safety_check"`、`force_ignored:true`；stub `systemctl` 未收到停止／停用、`config.yaml` 位元組不變。

每個拒絕都斷言：exit 2、`phase:"update_safety_check"`（`supervision-mode set` 為 `supervision_set_safety_check`）、daemon pid 不變且 `health.ping` 仍 ok、WAL 無停止紀錄、
`current-release.json` 位元組不變、被拒的操作照常完成（DUT 端內容／md5 為外部 oracle），並以 `HolderSampler(slave, 0.01)`
連續取樣證明全程只有受測 daemon 持有 UART（`max_concurrent()<=1`，no_reopen_before_handoff）。

install root 由兩個 dev bundle（`bb_l15.update_bundles()`；`SW_G5_BUNDLE_OLD`／`SW_G5_BUNDLE_NEW` 可指向預建 bundle）的
`versions/<v>` 以硬連結建立，並寫入 `current-release.json` 與 Previous 備份（manifest／registration）：拒絕路徑只走到
`safety_check`／步驟 3 交接，不切換版本、不啟動 bundle 內的 daemon。本機若另有同 uid 的 `serialwrapd`（例如擁有者的
production daemon），只影響「探測失敗」路徑，本檔的拒絕路徑在受測 daemon 可 RPC 時不經 ProcScan。

屬 L1.5（PTY shelltarget／fakemcu＋隔離 daemon），不得計為真板 L2／L3。stdlib＋pytest，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from bb_l15 import bundle_manifest_path, resume_pause, update_bundles, wait_pause
from p8_fixtures import FakeFlasher, HolderSampler

pytestmark = [pytest.mark.posix_only]

OLD = "0.4.0"
NEW = "0.4.1"
SYNC = b"\x55\x55"
ACK = b"\x00\xcc"
PACKET = b"\x03\x10\x20"
FLASH_MSG = "flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）"
PUSH_BYTES = 64 * 1024
CHUNK_TIMEOUT_S = 90
# setup 抵達 safety_check 暫停點的上限（debug dev bundle 的 verify 逐檔 sha256 實測約 20～30 s）。
SETUP_REACH_S = 120.0


# ---------------------------------------------------------------------------
# install root（Current／Previous／備份）與 bundle CLI
# ---------------------------------------------------------------------------

def _link_tree(src: Path, dst: Path) -> None:
    """以硬連結複製版本目錄（唯讀使用；跨檔案系統時退回一般複製）。"""
    try:
        shutil.copytree(src, dst, symlinks=True, copy_function=os.link)
    except OSError:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True)


class Installed:
    """隔離 install root＋與受測 daemon 共用的 env（CONFIG／STATE／RUN… 皆為 `sw_env` 的隔離目錄）。

    `previous=True`：Current＝0.4.1、Previous＝0.4.0（含完整備份），供 `setup --rollback`；
    `previous=False`：Current＝0.4.0，供 0.4.1 bundle 的 `setup`（更新路徑）。
    """

    def __init__(self, tmp_path: Path, sw_env, *, previous: bool) -> None:
        old, new = update_bundles()
        self.root = tmp_path / "install-root"
        versions = self.root / "versions"
        versions.mkdir(parents=True)
        _link_tree(old / "versions" / OLD, versions / OLD)
        current = OLD
        prev = None
        backup_dir = None
        if previous:
            _link_tree(new / "versions" / NEW, versions / NEW)
            current = NEW
            prev = {"bundle_version": OLD, "dir": f"versions/{OLD}"}
            backup = self.root / "backups" / f"{OLD}-to-{NEW}-seed"
            backup.mkdir(parents=True)
            (backup / "manifest.json").write_text(json.dumps({"files": [], "schema_version": 1}), encoding="utf-8")
            (backup / "registration.json").write_text(
                json.dumps({"enabled": None, "endpoint": sw_env.endpoint, "mode": "on-demand",
                            "registration": None, "running": False}),
                encoding="utf-8",
            )
            backup_dir = str(backup)
        doc = {"backup_dir": backup_dir, "bundle_version": current, "dir": f"versions/{current}",
               "previous": prev, "switched_at": "2026-11-08T00:00:00Z"}
        self.current_file = self.root / "current-release.json"
        self.current_file.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.current_bytes = self.current_file.read_bytes()
        self.cli_path = new / "bin" / "serialwrap"
        self.env = sw_env.isolated_env()
        self.env["SERIALWRAP_INSTALL_ROOT"] = str(self.root)
        self.env["SERIALWRAP_RELEASE_MANIFEST"] = str(bundle_manifest_path(new, NEW))
        config_dir = Path(self.env["SERIALWRAP_CONFIG_DIR"])
        config_dir.mkdir(parents=True, exist_ok=True)
        config = config_dir / "config.yaml"
        if not config.exists():
            # daemon 啟動時再寫入 socket_path（SC-09）；CLI 依 on-demand 的 instance endpoint 探測受測 daemon。
            config.write_text("supervision_mode: on-demand\n", encoding="utf-8")

    def setup(self, *args: str, extra_env: dict[str, str] | None = None, timeout_s: float = 120.0):
        env = dict(self.env)
        env.update(extra_env or {})
        r = subprocess.run([str(self.cli_path), "setup", *args], env=env, capture_output=True, text=True,
                           timeout=timeout_s, check=False)
        data = None
        for line in reversed(r.stdout.splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                data = value
                break
        return r, data

    def assert_unchanged(self) -> None:
        assert self.current_file.read_bytes() == self.current_bytes, "current-release.json 不得改變"


def _assert_refused(r, data, code: str, *, force: bool, key: str, what: str) -> None:
    assert r.returncode == 2, f"{what}：{r.stdout}\n{r.stderr}"
    assert data is not None, f"{what}：{r.stdout}"
    assert (data.get("error_code"), data.get("phase")) == (code, "update_safety_check"), f"{what}：{data}"
    steps = (data.get(key) or {}).get("steps") or []
    names = [s.get("step") for s in steps]
    assert "backup_and_switch" not in names and "start_and_verify" not in names, f"{what}：不得切換：{names}"
    safety = next((s for s in steps if s.get("step") == "safety_check"), None)
    assert safety is not None, f"{what}：{names}"
    if force:
        assert safety.get("detail", {}).get("force_ignored") is True, f"{what}：{safety}"
        assert "skipped" not in safety.get("detail", {}), f"{what}：busy 判定不得被略過"
    else:
        assert "force_ignored" not in safety.get("detail", {}), f"{what}：{safety}"


def _assert_daemon_untouched(sw_env, pid: int, what: str) -> None:
    assert sw_env.daemon_pid == pid, f"{what}：daemon 不得重啟"
    status = sw_env.rpc("health.status", {})
    assert status.get("ok") is True and status.get("pid") == pid, f"{what}：{status}"
    assert sw_env.rpc("health.ping", {}).get("ok") is True, f"{what}：daemon 仍在服務"
    assert not any("shutdown" in json.dumps(row, ensure_ascii=False).lower() for row in sw_env.wal_records()), \
        f"{what}：WAL 不得出現停止紀錄"


def _assert_single_holder(sampler: HolderSampler, pid: int, what: str) -> None:
    assert sampler.max_concurrent() <= 1, f"{what}：UART 不得同時被兩個行程持有：{sampler.samples()[-5:]}"
    assert sampler.holders_seen() <= {pid}, f"{what}：只有受測 daemon 持有 UART：{sampler.holders_seen()}"


# ---------------------------------------------------------------------------
# file push 進行中
# ---------------------------------------------------------------------------

def _ready(sw_env, shell_target) -> int:
    sw_env.start(targets={"COM0": shell_target})
    sw_env.wait_state("COM0", "READY", timeout=20)
    pid = sw_env.daemon_pid
    assert pid
    return pid


class HeldPush:
    """DUT 讀到第一個 chunk 命令即停讀（`ShellTarget.hold_read_on`），push 停在等 chunk ACK；`release()` 後照常完成。"""

    def __init__(self, sw_env, shell_target, tmp_path: Path, name: str) -> None:
        block = bytes(range(256))
        self.data = (block * (PUSH_BYTES // len(block)))[:PUSH_BYTES]
        self.local = tmp_path / f"{name}.bin"
        self.local.write_bytes(self.data)
        self.remote = shell_target.root / f"{name}.bin"
        self.shell_target = shell_target
        # chunk 1024 bytes：base64 後的命令行低於 DUT 端 tty 正規模式的行長上限（4096 會被截斷而永不回 ACK）。
        shell_target.hold_read_on(b"SW_XFER_CHUNK_")
        self.future = sw_env.rpc_async("file.push", {
            "selector": "COM0", "local_path": str(self.local), "remote_path": str(self.remote),
            "ack_mode": "none", "chunk_size": 1024, "chunk_timeout_s": CHUNK_TIMEOUT_S,
        }, timeout_s=300.0)
        sw_env.wait_until(shell_target.read_held, timeout=30)
        sw_env.wait_until(lambda: sw_env.session("COM0").get("foreground_busy") is True, timeout=10)

    def release_and_check(self, what: str) -> None:
        self.shell_target.release_read()
        result = self.future.result(timeout=300)
        assert result.get("ok") is True, f"{what}：push 必須照常完成：{result}"
        assert result.get("md5") == hashlib.md5(self.data).hexdigest(), f"{what}：{result}"
        assert self.remote.read_bytes() == self.data, f"{what}：DUT 端檔案 md5 正確（外部 oracle）"


def _rollback_while_push(sw_env, shell_target, tmp_path, *, force: bool, timing: str) -> None:
    pid = _ready(sw_env, shell_target)
    inst = Installed(tmp_path, sw_env, previous=True)
    args = ["--rollback"] + (["--force"] if force else [])
    what = f"rollback force={force} timing={timing}"
    with HolderSampler(shell_target.slave_path, 0.01, exclude=(os.getpid(),)) as sampler:
        if timing == "before_probe":
            push = HeldPush(sw_env, shell_target, tmp_path, "before")
            r, data = inst.setup(*args)
            _assert_refused(r, data, "SESSION_BUSY", force=force, key="rollback", what=what)
            busy = data.get("busy") or []
            assert any(row.get("com") == "COM0" and row.get("foreground_busy") is True for row in busy), f"{what}：{data}"
        else:
            pause_dir = tmp_path / "pause"
            extra = {"SW_TEST_UPDATE_PAUSE_AFTER": "safety_check", "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir)}
            out: dict = {}
            runner = threading.Thread(target=lambda: out.update(zip(("r", "data"), inst.setup(*args, extra_env=extra))))
            runner.start()
            try:
                # dev bundle 為 debug build：verify 步驟逐檔 sha256 約需 20～30 s，預檢暫停點的等待需涵蓋。
                wait_pause(pause_dir, "safety_check", timeout_s=SETUP_REACH_S)
                push = HeldPush(sw_env, shell_target, tmp_path, "after")
            finally:
                resume_pause(pause_dir, "safety_check")
            runner.join(120)
            assert not runner.is_alive(), f"{what}：setup 未在 120 s 內結束"
            r, data = out["r"], out["data"]
            _assert_refused(r, data, "SESSION_BUSY", force=force, key="rollback", what=what)
            assert data.get("handoff") == "refused", f"{what}：交接由 daemon 原子判定拒絕：{data}"
            busy = data.get("busy") or []
            assert any(row.get("com") == "COM0" and "file.push" in (row.get("ops") or []) for row in busy), \
                f"{what}：daemon 回的 busy 列含 file.push：{data}"
        inst.assert_unchanged()
        _assert_daemon_untouched(sw_env, pid, what)
        push.release_and_check(what)
        _assert_single_holder(sampler, pid, what)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.parametrize("timing", ["before_probe", "after_probe"])
def test_rollback_refuses_while_push(sw_env, shell_target, tmp_path, timing):
    _rollback_while_push(sw_env, shell_target, tmp_path, force=False, timing=timing)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.parametrize("entry", ["rollback", "update"])
def test_rollback_force_refuses_while_push(sw_env, shell_target, tmp_path, entry):
    if entry == "rollback":
        _rollback_while_push(sw_env, shell_target, tmp_path, force=True, timing="before_probe")
        return
    # `setup --force`（0.4.1 bundle 更新 Current 0.4.0）：同一 safety_check，--force 只記 force_ignored。
    pid = _ready(sw_env, shell_target)
    inst = Installed(tmp_path, sw_env, previous=False)
    with HolderSampler(shell_target.slave_path, 0.01, exclude=(os.getpid(),)) as sampler:
        push = HeldPush(sw_env, shell_target, tmp_path, "update-force")
        r, data = inst.setup("--force")
        _assert_refused(r, data, "SESSION_BUSY", force=True, key="update", what="update --force")
        inst.assert_unchanged()
        _assert_daemon_untouched(sw_env, pid, "update --force")
        push.release_and_check("update --force")
        _assert_single_holder(sampler, pid, "update --force")


# ---------------------------------------------------------------------------
# FLASHING 中
# ---------------------------------------------------------------------------

def _flashing(sw_env, fake_mcu) -> tuple[int, FakeFlasher]:
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough")
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    link = sw_env.run_dir / "dev" / "ttyMCU"
    sw_env.wait_until(link.is_symlink, timeout=10)
    pid = sw_env.daemon_pid
    assert pid
    fl = FakeFlasher(link, baud=115200)
    fl.write(SYNC)
    assert fl.read_exact(2, timeout=5.0) == ACK
    assert sw_env.session("COM0")["state"] == "FLASHING"
    return pid, fl


def _finish_flash(sw_env, fake_mcu, fl: FakeFlasher, what: str) -> None:
    fl.write(PACKET)
    sw_env.wait_until(lambda: fake_mcu.rx_log().endswith(PACKET), timeout=5)
    assert fl.read_exact(2, timeout=5.0) == ACK, f"{what}：flash 照常進行（封包 ACK 轉回 flasher）"
    time.sleep(1.2)  # 讓 holder-probe 至少 armed 一次（poll 0.5 s）
    fl.close()
    sw_env.wait_until(lambda: sw_env.session("COM0")["state"] == "ATTACHED", timeout=10)
    assert fake_mcu.rx_log() == SYNC + PACKET, f"{what}：MCU 收到的燒錄位元組與單獨燒錄時相同"


def _rollback_while_flashing(sw_env, fake_mcu, tmp_path, *, force: bool, entry: str) -> None:
    pid, fl = _flashing(sw_env, fake_mcu)
    try:
        inst = Installed(tmp_path, sw_env, previous=(entry == "rollback"))
        args = (["--rollback"] if entry == "rollback" else []) + (["--force"] if force else [])
        key = "rollback" if entry == "rollback" else "update"
        what = f"{entry} force={force} while flashing"
        with HolderSampler(fake_mcu.slave_path, 0.01, exclude=(os.getpid(),)) as sampler:
            r, data = inst.setup(*args)
            _assert_refused(r, data, "FLASHING_BUSY", force=force, key=key, what=what)
            assert data.get("message") == FLASH_MSG, f"{what}：{data}"
            assert sw_env.session("COM0")["state"] == "FLASHING", f"{what}：拒絕後仍在 FLASHING"
            inst.assert_unchanged()
            _assert_daemon_untouched(sw_env, pid, what)
            _finish_flash(sw_env, fake_mcu, fl, what)
            _assert_single_holder(sampler, pid, what)
    finally:
        fl.close()


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_rollback_refuses_while_flashing(sw_env, fake_mcu, tmp_path):
    _rollback_while_flashing(sw_env, fake_mcu, tmp_path, force=False, entry="rollback")


@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.parametrize("entry", ["rollback", "update"])
def test_rollback_force_refuses_while_flashing(sw_env, fake_mcu, tmp_path, entry):
    _rollback_while_flashing(sw_env, fake_mcu, tmp_path, force=True, entry=entry)


# ---------------------------------------------------------------------------
# interactive lease＋interactive_send 進行中
# ---------------------------------------------------------------------------

@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_rollback_refuses_while_interactive_send(sw_env, shell_target, tmp_path):
    pid = _ready(sw_env, shell_target)
    inst = Installed(tmp_path, sw_env, previous=True)
    opened = sw_env.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent:t14", "timeout_s": 120})
    assert opened.get("ok") is True, opened
    iid = opened["interactive_id"]
    payload = "echo T14_" + "x" * 2000 + "\n"
    with HolderSampler(shell_target.slave_path, 0.01, exclude=(os.getpid(),)) as sampler:
        shell_target.hold_read_on(b"T14_")
        sent = sw_env.rpc_async("session.interactive_send", {"interactive_id": iid, "data": payload})
        sw_env.wait_until(shell_target.read_held, timeout=30)
        r, data = inst.setup("--rollback")
        _assert_refused(r, data, "SESSION_BUSY", force=False, key="rollback", what="interactive send")
        busy = data.get("busy") or []
        assert any(row.get("com") == "COM0" and row.get("interactive_session_id") == iid for row in busy), data
        inst.assert_unchanged()
        _assert_daemon_untouched(sw_env, pid, "interactive send")
        shell_target.release_read()
        result = sent.result(timeout=60)
        assert result.get("ok") is True, result
        sw_env.wait_until(lambda: payload.rstrip("\n").encode() in shell_target.rx_log(), timeout=20)
        _assert_single_holder(sampler, pid, "interactive send")
    closed = sw_env.rpc("session.interactive_close", {"interactive_id": iid})
    assert closed.get("ok") is True, closed



# ---------------------------------------------------------------------------
# supervision-mode set（SC-22，R1-F3）
# ---------------------------------------------------------------------------

def _host_tool_stubs(tmp_path: Path, env: dict[str, str]) -> Path:
    """PATH 前置記錄 argv 的 stub `systemctl`／`loginctl`／`sudo`（exit 0，不執行任何東西）；回傳記錄檔路徑。

    拒絕路徑不得呼叫任何停止／停用；若 CLI 迴歸而誤入切換（reconcile），stub 也擋住宿主的 user systemd、linger 與 sudo。"""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "host-tools.log"
    for tool in ("systemctl", "loginctl", "sudo"):
        stub = stub_bin / tool
        stub.write_text(f"#!/bin/sh\nprintf '{tool} %s\\n' \"$*\" >> \"$SW_T14_HOST_TOOL_LOG\"\nexit 0\n",
                        encoding="utf-8")
        stub.chmod(0o755)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env["SW_T14_HOST_TOOL_LOG"] = str(log)
    return log


def _last_json(stdout: str):
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_supervision_set_force_refuses_while_push(sw_env, shell_target, tmp_path):
    """計畫 `supervision_set_force_refuses_rust_daemon_pushing`：舊模式 on-demand、file push 進行中，
    `supervision-mode set systemd-user --force` 由 safety_check 拒絕（`--force` 只記 `force_ignored:true`）：
    不 hold／停用監管、不送停止、不寫 config；push 照常完成、daemon pid 不變、全程只有受測 daemon 持有 UART。"""
    pid = _ready(sw_env, shell_target)
    inst = Installed(tmp_path, sw_env, previous=False)
    env = dict(inst.env)
    tool_log = _host_tool_stubs(tmp_path, env)
    config = Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"
    what = "supervision-mode set systemd-user --force while push"
    with HolderSampler(shell_target.slave_path, 0.01, exclude=(os.getpid(),)) as sampler:
        push = HeldPush(sw_env, shell_target, tmp_path, "supervision")
        config_before = config.read_bytes()
        r = subprocess.run([str(inst.cli_path), "supervision-mode", "set", "systemd-user", "--force"], env=env,
                           capture_output=True, text=True, timeout=120, check=False)
        data = _last_json(r.stdout)
        assert r.returncode == 2, f"{what}：{r.stdout}\n{r.stderr}"
        assert data is not None, f"{what}：{r.stdout}"
        assert (data.get("ok"), data.get("error_code"), data.get("phase")) == \
            (False, "SESSION_BUSY", "supervision_set_safety_check"), f"{what}：{data}"
        assert data.get("force_ignored") is True, f"{what}：--force 只記 force_ignored：{data}"
        assert data.get("previous_mode") == "on-demand", f"{what}：{data}"
        busy = data.get("busy") or []
        assert any(row.get("com") == "COM0" and row.get("foreground_busy") is True for row in busy), f"{what}：{data}"
        calls = tool_log.read_text(encoding="utf-8").splitlines() if tool_log.exists() else []
        stops = [c for c in calls if c.startswith("systemctl ")
                 and any(w in c.split() for w in ("stop", "disable", "kill", "mask", "restart"))]
        assert not stops, f"{what}：stub systemctl 不得收到停止／停用：{calls}"
        assert not [c for c in calls if c.startswith(("loginctl ", "sudo "))], f"{what}：拒絕不得進入切換：{calls}"
        assert config.read_bytes() == config_before, f"{what}：config.yaml 位元組不得改變"
        inst.assert_unchanged()
        _assert_daemon_untouched(sw_env, pid, what)
        push.release_and_check(what)
        _assert_single_holder(sampler, pid, what)
