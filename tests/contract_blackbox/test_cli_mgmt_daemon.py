"""P4-T7 `daemon start／stop／status／restart` 黑箱契約（SC-10、SC-17、SC-18、SC-20、DF-05）。

A/B 案例同時以 Python 基線與受測 CLI 執行並比對輸出。預設執行（Python 基線）時三個 A/B 案例應通過（Python 對 Python）；
以 `SERIALWRAP_CLI_EXE=<mkbundle --dev --delegate-sentinel 產物>` 與 `SERIALWRAP_AB_LABEL=rust-1b` 執行時，`daemon` 群組
仍委派 sentinel（exit 97）且寫入 DELEGATED marker，因此案例應失敗（紅燈）。不使用 xfail：strict xfail 會在基線模式 XPASS 而失敗。

`test_autostart_l15_tx_once` 為 `rust_only` + `windows_native`：Linux 不執行；原生 Windows 需 owner 提供 COM 配對與 DUT 模擬器
（BLOCKED-OWNER，OD-08／OD-15）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

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


def test_daemon_restart_ab(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """SC-20：on-demand restart 後 pid 改變；兩實作輸出形狀 {ok,restarted,start,stopped} 正規化後相同。

    以 detached daemon 啟動（見 `_start_detached_daemon`）。Python `daemon restart` 固定 `force_spawn=False`（sw_core/cli.py），
    宿主上若另有 serialwrapd（例如使用者的生產 daemon），SC-10 多開預檢會拒絕 spawn；此環境條件只在 Python 基線輪次
    以 skip 標示（基線即 oracle，無法在此宿主執行），Rust 輪次仍執行以呈現失敗。
    """
    from bb_daemon import _send_rpc

    if not is_rust() and _foreign_serialwrapd_pids():
        pytest.skip("宿主已有非測試 serialwrapd（SC-10 多開預檢會阻擋 restart 的 spawn；只讀偵測，未觸碰）")
    results: dict[str, str] = {}
    # 先跑受測 CLI，再跑 Python 基線：宿主衝突只影響基線 oracle，受測輪次的失敗才能獨立呈現。
    for label, prefix in (("cli", cli_prefix()), ("python", python_baseline_prefix())):
        sock = str(tmp_path / f"restart-{label}.sock")
        profile_dir = tmp_path / f"restart-{label}-profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        before_pid = _start_detached_daemon(env, profile_dir, sock, tmp_path / f"restart-{label}.lock")
        try:
            assert _rpc_pid(sock) == before_pid, "重啟前 daemon 必須健康且 pid 一致"
            res = subprocess.run(
                prefix + ["--socket", sock, "daemon", "restart"], env=env, capture_output=True, text=True, timeout=120
            )
            assert res.returncode == 0, res.stderr
            after = _rpc_pid(sock)
            assert after is not None and after != before_pid, "restart 後 pid 必須改變"
            shape = json.loads(res.stdout)
            assert set(shape) == {"ok", "restarted", "start", "stopped"}
            results[label] = _normalize(res.stdout, sock)
        finally:
            # 收尾：停止重啟後的 daemon（由 CLI 啟動，非 pytest 子行程）；舊 pid 若仍存活只針對本測試建立的 pid。
            subprocess.run(prefix + ["--socket", sock, "daemon", "stop"], env=env, capture_output=True, timeout=60)
            try:
                os.kill(before_pid, 15)
            except ProcessLookupError:
                pass
    assert results["cli"] == results["python"]


@pytest.mark.rust_only
@pytest.mark.windows_native
def test_autostart_l15_tx_once(tmp_path: Path, env: dict[str, str], no_delegation: Path) -> None:
    """SC-18（原生 Windows）：無 daemon 時 `cmd submit` 經 on-demand 自動啟動，DUT 端 `echo X\\r` 恰出現一次。

    需 owner 在原生 Windows 提供 COM 配對（`SW_TEST_COM_PAIR`）與 DUT 模擬器寫出的 wire 記錄（`SW_TEST_DUT_WIRE`）。
    """
    if os.name != "nt":
        pytest.skip("windows_native：僅原生 Windows 執行")
    if not os.environ.get("SW_TEST_COM_PAIR"):
        pytest.skip("BLOCKED-OWNER：需 SW_TEST_COM_PAIR（OD-08／OD-15 擁有者 host 的 com0com 配對）")
    wire = os.environ.get("SW_TEST_DUT_WIRE")
    assert wire, "SW_TEST_DUT_WIRE 必須指向 DUT 模擬器的 wire.ndjson"

    res = run_cli(["cmd", "submit", "--selector", "COM0", "--cmd", "echo X"], env=env, timeout_s=120)
    assert res.rc == 0, res.stderr
    text = Path(wire).read_text(encoding="utf-8", errors="replace")
    assert text.count("echo X\r") == 1
