"""P4-T7 `daemon start／stop／status／restart` 黑箱契約（SC-10、SC-17、SC-18、SC-20、DF-05）。

A/B 案例同時以 Python 基線與受測 CLI 執行並比對輸出。預設執行（Python 基線）時三個 A/B 案例應通過（Python 對 Python）；
以 `SERIALWRAP_CLI_EXE=<mkbundle --dev --delegate-sentinel 產物>` 與 `SERIALWRAP_AB_LABEL=rust-1b` 執行時，`daemon` 群組
仍委派 sentinel（exit 97）且寫入 DELEGATED marker，因此案例應失敗（紅燈）。不使用 xfail：strict xfail 會在基線模式 XPASS 而失敗。

`test_autostart_l15_tx_once` 為 `rust_only` + `windows_native`：Linux 不執行；原生 Windows 需 owner 提供 COM 配對與 DUT 模擬器
（BLOCKED-OWNER，OD-08／OD-15）。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from bb_l15 import bundle_manifest_path
from bb_harness import cli_prefix, is_rust, python_baseline_prefix, repo_root, run_cli

FAKE_SERIALWRAPD = repo_root() / "tests" / "contract_blackbox" / "bin" / "fake-serialwrapd"
# 正規化時會被置換的 P1 欄位（行程識別與時間戳）。
_VOLATILE_KEYS = {"pid", "started_at", "stopped_pid", "existing_pid", "returncode"}


def _normalize(text: str, sock: str | None = None) -> str:
    """JSON 輸出的 pid／started_at 等易變欄位改為 null，其餘欄位保留（鍵排序）；非 JSON 原文保留。

    `sock` 為本輪 daemon 的 socket 路徑（每輪不同，`multi_open_detail` 會列出查詢中的 daemon 自身），以占位字串取代。
    """
    if sock:
        text = text.replace(sock, "<SOCK>")

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (None if k in _VOLATILE_KEYS else scrub(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    try:
        return json.dumps(scrub(json.loads(text)), ensure_ascii=False, sort_keys=True)
    except ValueError:
        return text


def _spawn_fake_serialwrapd(socket: str) -> subprocess.Popen:
    """假 serialwrapd（argv0＝serialwrapd，帶 `--socket`）；返回後保證 /proc 可見。"""
    proc = subprocess.Popen(
        [str(FAKE_SERIALWRAPD), "--socket", socket],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            argv0 = Path(f"/proc/{proc.pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        except OSError:
            argv0 = b""
        if argv0.endswith(b"serialwrapd"):
            return proc
        time.sleep(0.05)
    proc.kill()
    proc.wait()
    raise AssertionError("假 serialwrapd 未在 5 秒內出現於 /proc")


def _foreign_serialwrapd_pids() -> list[int]:
    """唯讀掃描 /proc，列出目前所有 serialwrapd 行程（不送任何訊號，不觸碰既有 daemon）。"""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        names = {os.path.basename(p) for p in parts if p}
        if names & {b"serialwrapd", b"serialwrapd.py"}:
            found.append(int(entry.name))
    return found


def _start_detached_daemon(env: dict[str, str], profile_dir: Path, sock: str, lock: Path) -> int:
    """以 sh 子殼脫離父行程啟動 Python daemon（不成為 pytest 的子行程，停止後不留殭屍，與 systemd 託管等價）。"""
    from bb_daemon import _send_rpc, daemon_prefix

    lock.parent.mkdir(parents=True, exist_ok=True)
    cmd = daemon_prefix() + ["--profile-dir", str(profile_dir), "--socket", sock, "--lock", str(lock)]
    res = subprocess.run(
        ["sh", "-c", '"$@" </dev/null >/dev/null 2>&1 & echo $!', "sh", *cmd],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    pid = int(res.stdout.strip().splitlines()[-1])
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        ping = _send_rpc(sock, "health.ping", {}, timeout_s=0.5)
        if ping and ping.get("ok"):
            return pid
        time.sleep(0.1)
    raise AssertionError(f"detached daemon 未在 15 秒內就緒（pid={pid}）")


def _rpc_pid(endpoint: str) -> int | None:
    from bb_daemon import _send_rpc

    resp = _send_rpc(endpoint, "health.status", {}, timeout_s=2.0)
    if resp is None or resp.get("ok") is not True:
        return None
    pid = resp.get("pid")
    return pid if isinstance(pid, int) else None


def _dut_tx_bytes(wire: str) -> bytes:
    """DUT 模擬器 wire 記錄中實際寫入 DUT stdin 的 bytes。"""
    path = Path(wire)
    if not path.is_file():
        return b""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return b"".join(
        base64.b64decode(row["payload_b64"], validate=True)
        for row in rows
        if row.get("dir") == "TX" and row.get("observation") == "dut_stdin_write"
    )


# bridge 證據 writer 以佇列非同步寫 wire.ndjson：CLI 回傳時對應 TX 列可能尚未落盤。
_WIRE_FLUSH_WAIT_S = 10.0
_WIRE_SETTLE_S = 1.0


def _wait_dut_tx(wire: str, baseline: int, expected: bytes) -> bytes:
    """等基線後的 DUT TX 至少達到 expected 長度（有界），再於 settle 後重讀，回傳最終片段。"""
    deadline = time.monotonic() + _WIRE_FLUSH_WAIT_S
    while time.monotonic() < deadline and len(_dut_tx_bytes(wire)[baseline:]) < len(expected):
        time.sleep(0.1)
    time.sleep(_WIRE_SETTLE_S)
    return _dut_tx_bytes(wire)[baseline:]


# passthrough 須連續 30 秒無 RX 才升 READY（PASSTHROUGH_READY_QUIET_S）；等待上限另留 attach 與
# 高負載排程裕度，且不超過首次 CLI 呼叫的 120 秒上限。
_PASSTHROUGH_READY_WAIT_S = 120.0


def _submit_after_on_demand_autostart(env: dict[str, str], wire: str, tx_baseline: int):
    """首次 `cmd submit` 必須經 on-demand 自動啟動 daemon；若 session 尚未 READY，等 READY 後再送一次。

    SC-18 只規定「連線階段被拒才自動啟動並重送一次」，不規定 CLI 等待 READY。passthrough
    session 在 attach 後須靜默 30 秒才升 READY，因此首次請求允許兩種結果：(a) 直接成功（原驗收形狀）；
    (b) 被拒絕為 SESSION_NOT_READY——此時被拒請求必須零 TX，之後的單次送出仍須恰好一次 TX。
    """
    initial = run_cli(
        ["cmd", "submit", "--selector", "COM0", "--cmd", "echo X", "--cmd-timeout", "1"],
        env=env,
        timeout_s=120.0,
    )
    initial_returned_at = time.monotonic()
    assert "已自動啟動" in initial.stderr.decode("utf-8", errors="replace"), (
        f"首次 cmd submit 未留下 on-demand autostart 證據：{initial.stderr!r}"
    )
    if initial.rc == 0:
        return initial
    try:
        initial_response = json.loads(initial.stdout.decode("utf-8"))
    except ValueError:
        initial_response = None
    assert isinstance(initial_response, dict) and initial_response.get("error_code") == "SESSION_NOT_READY", (
        "首次需求未成功時只接受 session 尚未 READY 的拒絕；"
        f"rc={initial.rc} stdout={initial.stdout!r} stderr={initial.stderr!r}"
    )
    deadline = time.monotonic() + _PASSTHROUGH_READY_WAIT_S
    last_session: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status = run_cli(["session", "list"], env=env, timeout_s=10.0)
        assert status.rc == 0, status.stderr.decode("utf-8", errors="replace")
        rows = json.loads(status.stdout.decode("utf-8")).get("sessions", [])
        last_session = next((row for row in rows if row.get("com") == "COM0"), None)
        if last_session and last_session.get("state") == "READY":
            break
        if last_session and last_session.get("state") == "DETACHED" and last_session.get("last_error"):
            pytest.fail(f"COM0 attach failed before READY: {last_session!r}")
        time.sleep(0.25)
    else:
        pytest.fail(f"COM0 未於 {_PASSTHROUGH_READY_WAIT_S:.0f} 秒內進入 READY：{last_session!r}")

    # 零 TX 檢查放在 READY 等待之後、唯一送出之前。READY 等待長度取決於被拒請求落在 30 秒靜默窗口的
    # 哪個時點、不保證夠長，故另確保距被拒回傳已過 wire 落盤等待上限並 settle，延遲落盤的 TX 必已寫入。
    flush_remaining_s = _WIRE_FLUSH_WAIT_S - (time.monotonic() - initial_returned_at)
    if flush_remaining_s > 0:
        time.sleep(flush_remaining_s)
    time.sleep(_WIRE_SETTLE_S)
    assert b"echo X" not in _dut_tx_bytes(wire)[tx_baseline:], "被拒絕的首次請求不得在 DUT 端留下任何 TX"

    return run_cli(
        [
            "cmd", "submit", "--selector", "COM0", "--cmd", "echo X",
            "--completion-regex", "echo X",
        ],
        env=env,
        timeout_s=30.0,
    )


def test_daemon_start_ab_conflict(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """SC-17／SC-10：同機已有 serialwrapd 綁在其他 socket → 兩實作以相同 stdout 與 rc 2 拒絕再 spawn。"""
    other = _spawn_fake_serialwrapd("/tmp/other")
    try:
        target = str(tmp_path / "target.sock")
        outputs: dict[str, tuple[int, bytes]] = {}
        for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
            res = subprocess.run(
                prefix + ["--socket", target, "daemon", "start"],
                env=env,
                capture_output=True,
                timeout=60,
            )
            outputs[label] = (res.returncode, res.stdout)
        assert outputs["cli"][0] == outputs["python"][0] == 2
        assert outputs["cli"][1] == outputs["python"][1]
    finally:
        other.kill()
        other.wait()


def test_daemon_start_stop_ab_real_daemon(tmp_path: Path, env: dict[str, str], py_daemon, no_delegation: Path) -> None:
    """隔離目錄內以 Python daemon 啟動真實 daemon → status ok → stop → ping 失敗；正規化後兩實作輸出相同。"""
    from bb_daemon import _send_rpc

    results: dict[str, tuple[str, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        sock = str(tmp_path / f"{label}.sock")
        daemon = py_daemon(socket=sock, lock=tmp_path / f"{label}.lock")
        daemon.start()
        status = subprocess.run(
            prefix + ["--socket", sock, "daemon", "status"], env=env, capture_output=True, text=True, timeout=60
        )
        assert status.returncode == 0, status.stderr
        stop = subprocess.run(
            prefix + ["--socket", sock, "daemon", "stop"], env=env, capture_output=True, text=True, timeout=60
        )
        assert stop.returncode == 0, stop.stderr
        assert _send_rpc(sock, "health.ping", {}, timeout_s=1.0) is None, "daemon stop 後 ping 必須失敗"
        # 等待行程實際結束（PyDaemon 為本測試的子行程，wait 同時回收），避免後續 status 把殘留行程列入多開偵測。
        daemon.proc.wait(timeout=10)
        results[label] = (_normalize(status.stdout, sock), _normalize(stop.stdout, sock))
    assert results["cli"] == results["python"]


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_daemon_start_releases_caller_stdio_pipes(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """Windows：`daemon start` 結束後，呼叫端擷取的 stdout／stderr pipe 必須立即 EOF。

    背景 daemon 不得繼承 CLI 的標準輸出 pipe；否則以 `subprocess.run(capture_output)` 呼叫 CLI 的
    agent 會一直等到 daemon 結束（SC-18 on-demand autostart 在原生 Windows 卡死 3600 秒的根因）。
    Python 基線以 `close_fds` 只傳標準 handle，兩實作須同樣立即放行。
    """
    _assert_daemon_start_releases_pipes(env, subprocess.PIPE)


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_daemon_start_releases_caller_merged_stdio_pipe(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """Windows：呼叫端以同一條 pipe 合併 stdout／stderr（`stderr=STDOUT`）時，`daemon start` 結束後該 pipe 也必須立即 EOF。

    兩個標準 handle 指向同一 pipe 是 StdOutputInheritGuard 去重的路徑；與分開兩條 pipe 的案例互補。
    """
    _assert_daemon_start_releases_pipes(env, subprocess.STDOUT)


def _assert_daemon_start_releases_pipes(env: dict[str, str], stderr: int) -> None:
    if os.name != "nt":
        pytest.skip("windows_native：僅原生 Windows 執行")
    if is_rust():
        bundle_root = os.environ.get("SW_WIN_DEV_BUNDLE")
        assert bundle_root, "Windows Rust daemon 案例需要隔離 staging bundle"
        manifest = bundle_manifest_path(Path(bundle_root))
        assert manifest.is_file(), f"Windows Rust bundle manifest 不存在：{manifest}"
        env["SERIALWRAP_RELEASE_MANIFEST"] = str(manifest)
    # 只放行不存在的 COM99：本案例不得開啟任何序列埠。
    env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = "COM99"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        env["SERIALWRAP_ENDPOINT"] = f"tcp://127.0.0.1:{listener.getsockname()[1]}"

    proc = subprocess.Popen(
        cli_prefix() + ["daemon", "start"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
    )
    drained = False
    try:
        proc.wait(timeout=90)
        try:
            out, err = proc.communicate(timeout=15)
            drained = True
        except subprocess.TimeoutExpired:
            pytest.fail("daemon start 已結束，但呼叫端 stdout／stderr pipe 15 秒內未 EOF：背景 daemon 繼承了 CLI 的 pipe")
        assert proc.returncode == 0, (err or out).decode("utf-8", errors="replace")
        if stderr == subprocess.STDOUT:
            # 合併 pipe：stdout 同時含 stderr；成功時 CLI 只輸出一行 JSON。
            out = out.strip().splitlines()[-1]
        assert json.loads(out.decode("utf-8")).get("ok") is True, out
    finally:
        # stop 的輸出導向 DEVNULL，避免本身也被同一缺陷卡住。
        subprocess.run(
            cli_prefix() + ["daemon", "stop"], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
        if proc.poll() is None:
            proc.kill()
        if not drained:
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                pass


@pytest.mark.case("sc20-daemon-restart")
@pytest.mark.pidns_rig
# P0 T14：補唯一平台 marker 與資源 marker。依賴 /proc 與 sh，故為 posix_only；Windows 由 cargo restart_* 承接 sc20，
# 不在 s1b_20groups windows 收集範圍（-m "core_portable or windows_native"）。
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_daemon_restart_ab(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """SC-20：on-demand restart 後 pid 改變；兩實作輸出形狀 {ok,restarted,start,stopped} 正規化後相同。

    以 detached daemon 啟動（見 `_start_detached_daemon`）。Python `daemon restart` 固定 `force_spawn=False`（sw_core/cli.py），
    宿主上若另有 serialwrapd（例如使用者的生產 daemon），SC-10 多開預檢會拒絕 spawn；此環境條件只在 Python 基線輪次
    以 skip 標示（基線即 oracle，無法在此宿主執行），Rust 輪次仍執行以呈現失敗。
    """
    from bb_daemon import _send_rpc

    cli_env = env
    if is_rust():
        # dev sentinel bundle 不含 daemon component；提供同層 Python daemon launcher 以實測 DF-05。
        launcher_dir = tmp_path / "rust-daemon-bin"
        launcher_dir.mkdir()
        launcher = launcher_dir / ("serialwrapd.exe" if os.name == "nt" else "serialwrapd")
        launcher.write_text(
            "#!/bin/sh\nexec \"" + str(repo_root() / "tests/contract_blackbox/bin/py-serialwrapd") + "\" \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        manifest = tmp_path / "rust-test-release-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bundle_version": "rust-p4-test",
                    "stage": "rust-1b-test",
                    "target": "test",
                    "git_revision": "test",
                    "components": {
                        "daemon": {"launcher": {"path": f"rust-daemon-bin/{launcher.name}"}},
                        "python_cli_delegate": {"entry": ["serialwrap.py"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        cli_env = {**env, "SERIALWRAP_RELEASE_MANIFEST": str(manifest)}

    if not is_rust() and _foreign_serialwrapd_pids():
        pytest.skip("宿主已有非測試 serialwrapd（SC-10 多開預檢會阻擋 restart 的 spawn；只讀偵測，未觸碰）")
    results: dict[str, str] = {}
    # 先跑受測 CLI，再跑 Python 基線：宿主衝突只影響基線 oracle，受測輪次的失敗才能獨立呈現。
    for label, prefix in (("cli", cli_prefix()), ("python", python_baseline_prefix())):
        run_env = cli_env if label == "cli" else env
        sock = str(tmp_path / f"restart-{label}.sock")
        profile_dir = tmp_path / f"restart-{label}-profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        before_pid = _start_detached_daemon(env, profile_dir, sock, tmp_path / f"restart-{label}.lock")
        try:
            assert _rpc_pid(sock) == before_pid, "重啟前 daemon 必須健康且 pid 一致"
            res = subprocess.run(
                prefix + ["--socket", sock, "daemon", "restart"], env=run_env, capture_output=True, text=True, timeout=120
            )
            assert res.returncode == 0, res.stderr
            after = _rpc_pid(sock)
            assert after is not None and after != before_pid, "restart 後 pid 必須改變"
            shape = json.loads(res.stdout)
            assert set(shape) == {"ok", "restarted", "start", "stopped"}
            results[label] = _normalize(res.stdout, sock)
        finally:
            # 收尾：停止重啟後的 daemon（由 CLI 啟動，非 pytest 子行程）；舊 pid 若仍存活只針對本測試建立的 pid。
            subprocess.run(prefix + ["--socket", sock, "daemon", "stop"], env=run_env, capture_output=True, timeout=60)
            try:
                os.kill(before_pid, 15)
            except ProcessLookupError:
                pass
    assert results["cli"] == results["python"]


@pytest.mark.rust_only
@pytest.mark.windows_native
@pytest.mark.windows_com
@pytest.mark.case("sc18-windows-ondemand-autostart")
def test_autostart_l15_tx_once(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """SC-18（原生 Windows）：無 daemon 時 `cmd submit` 經 on-demand 自動啟動，本案例基線後 DUT 端 TX 恰為一行 `echo X\\n`
    （daemon 以 body＋`\\n` 送出；原計畫寫 `\\r`，依 CHG-S1X-SC18-TX-BASELINE-01 更正）。

    需 owner 在原生 Windows 提供 COM 配對（`SW_TEST_COM_PAIR`）與 DUT 模擬器寫出的 wire 記錄（`SW_TEST_DUT_WIRE`）。
    """
    if os.name != "nt":
        pytest.skip("windows_native：僅原生 Windows 執行")
    if is_rust():
        bundle_root = os.environ.get("SW_WIN_DEV_BUNDLE")
        assert bundle_root, "Windows Rust COM 案例需要隔離 staging bundle"
        manifest = bundle_manifest_path(Path(bundle_root))
        assert manifest.is_file(), f"Windows Rust COM bundle manifest 不存在：{manifest}"
        # env fixture 清除外層 SERIALWRAP_*；明確傳入測試 bundle，避免誤查宿主安裝目錄。
        env["SERIALWRAP_RELEASE_MANIFEST"] = str(manifest)
    com_pair = os.environ.get("SW_TEST_COM_PAIR")
    if not com_pair:
        pytest.skip("BLOCKED-OWNER：需 SW_TEST_COM_PAIR（OD-08／OD-15 擁有者 host 的 com0com 配對）")
    assert com_pair == "COM20:COM21", "本案例只允許 DUT-side pair COM20:COM21"
    wire = os.environ.get("SW_TEST_DUT_WIRE")
    assert wire, "SW_TEST_DUT_WIRE 必須指向 DUT 模擬器的 wire.ndjson"

    # fixture 會清除外層 SERIALWRAP_*；重新只放行受測端 COM20，避免掃描其他主機序列埠。
    env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = "COM20"
    # 本案例驗證 on-demand 自動啟動；固定 COM0 → COM20，避免空 profile 目錄落入動態偵測 fallback。
    profile_dir = Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "windows-com.yaml").write_text(
        "profiles:\n"
        "  live-passthrough:\n"
        "    platform: passthrough\n"
        "targets:\n"
        "  - com: COM0\n"
        "    profile: live-passthrough\n"
        "    device_by_id: COM20\n",
        encoding="utf-8",
    )
    # 使用本案例獨有的 loopback endpoint，避免連到主機既有 daemon 或其他 throwaway run。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        env["SERIALWRAP_ENDPOINT"] = f"tcp://127.0.0.1:{listener.getsockname()[1]}"

    # wire 為整個 B 輪共用（含 COM 序列與巢狀 A/B 內的同一案例），只比對本案例開始後的 TX。
    tx_baseline = len(_dut_tx_bytes(wire))
    res = None
    try:
        res = _submit_after_on_demand_autostart(env, wire, tx_baseline)
        assert res.rc == 0, res.stderr.decode("utf-8", errors="replace")
        # 本案例自身的 TX（自基線起）必須恰為一行命令：daemon 以 body＋"\n" 送出（uart_io），
        # explicit target attach 零 TX（SC-35）；同一 B 輪的其他 COM 案例寫入不計。
        assert _wait_dut_tx(wire, tx_baseline, b"echo X\n") == b"echo X\n"
    finally:
        stop = run_cli(["daemon", "stop"], env=env, timeout_s=60.0)
        if res is not None and res.rc == 0:
            assert stop.rc == 0, stop.stderr.decode("utf-8", errors="replace")
